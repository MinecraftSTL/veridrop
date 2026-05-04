"""Tests for core/long_context.py + OpenAI long_context detector."""

from __future__ import annotations

import pytest

from relay_detector.core.long_context import (
    ANSWER_RE,
    Needle,
    assemble_haystack,
    build_question,
    estimate_cost_usd,
    evaluate_recalls,
    make_needles,
)
from relay_detector.core.models import ExecutionConfig, Mode
from relay_detector.protocols.openai.detectors.long_context import (
    LongContextDetector,
)


# ---------- Core helpers ----------


def test_make_needles_returns_three_distinct_answers():
    needles = make_needles("test-seed-1")
    assert len(needles) == 3
    answers = {n.answer for n in needles}
    assert len(answers) == 3, "Each needle must have a unique answer"
    for n in needles:
        assert ANSWER_RE.match(n.answer), f"answer {n.answer} doesn't match expected ID format"


def test_make_needles_deterministic_per_seed():
    a = make_needles("seed-A")
    b = make_needles("seed-A")
    assert [n.answer for n in a] == [n.answer for n in b]


def test_make_needles_differ_per_seed():
    a = make_needles("seed-A")
    b = make_needles("seed-B")
    assert [n.answer for n in a] != [n.answer for n in b]


def test_needles_at_distinct_positions():
    needles = make_needles("seed")
    positions = [n.position_pct for n in needles]
    # Three positions: head, middle, tail
    assert positions == sorted(positions)
    assert positions[0] < 0.2
    assert 0.4 < positions[1] < 0.6
    assert positions[2] > 0.8


def test_assemble_haystack_approximates_target_size():
    needles = make_needles("test")
    # Aim for 1000 tokens (~4000 chars). Tolerance ±20% — exact char counts
    # depend on which template lengths the RNG picks.
    text = assemble_haystack(1000, needles, "test")
    assert 3200 <= len(text) <= 4800, f"got {len(text)} chars, expected ~4000"


def test_assemble_haystack_contains_all_needles():
    needles = make_needles("test")
    text = assemble_haystack(2000, needles, "test")
    for n in needles:
        assert n.answer in text, f"needle {n.answer} not embedded in haystack"
        assert n.sentence in text


def test_assemble_haystack_deterministic():
    needles = make_needles("test")
    a = assemble_haystack(500, needles, "test-seed")
    b = assemble_haystack(500, needles, "test-seed")
    assert a == b


def test_build_question_lists_all_needle_labels():
    needles = make_needles("test")
    q = build_question(needles)
    for n in needles:
        assert n.label in q
    assert "NOT FOUND" in q  # explicit miss instruction prevents fabrication


def test_evaluate_recalls_full_match():
    needles = make_needles("test")
    response = " ".join(n.answer for n in needles)
    assert evaluate_recalls(response, needles) == [True, True, True]


def test_evaluate_recalls_partial_match():
    needles = make_needles("test")
    # Model returns only first and third, says NOT FOUND for second.
    response = f"1) {needles[0].answer}\n2) NOT FOUND\n3) {needles[2].answer}"
    assert evaluate_recalls(response, needles) == [True, False, True]


def test_evaluate_recalls_case_insensitive():
    needles = make_needles("test")
    response = needles[0].answer.lower()  # model lower-cased
    recalls = evaluate_recalls(response, needles)
    assert recalls[0] is True
    assert recalls[1] is False
    assert recalls[2] is False


def test_evaluate_recalls_empty_response():
    needles = make_needles("test")
    assert evaluate_recalls("", needles) == [False, False, False]


def test_evaluate_recalls_no_collision_with_filler():
    """Sanity check that needle answers don't accidentally appear in filler."""
    needles = make_needles("test-1")
    # Build a haystack with DIFFERENT seed for needles vs filler
    other_needles = make_needles("test-2")
    haystack_only = assemble_haystack(500, [], "test-1:haystack-only")
    # The "test-1" needles should NOT appear in a haystack built from a
    # different seed and no needles inserted.
    assert evaluate_recalls(haystack_only, needles) == [False, False, False]
    _ = other_needles  # silence unused


# ---------- Cost estimation ----------


def test_estimate_cost_known_model():
    cost = estimate_cost_usd(100_000, "gpt-4o-mini")
    # 100k tokens × $0.15 / 1M = $0.015
    assert 0.014 <= cost <= 0.016


def test_estimate_cost_unknown_model_falls_back():
    cost = estimate_cost_usd(100_000, "totally-fake-model")
    assert cost > 0  # uses default rate, doesn't crash


def test_estimate_cost_handles_snapshot_suffix():
    # gpt-4o-mini-2024-07-18 should match gpt-4o-mini's rate
    cost = estimate_cost_usd(100_000, "gpt-4o-mini-2024-07-18")
    assert 0.014 <= cost <= 0.016


# ---------- Detector behaviour ----------


class _MockClient:
    """Stub that records each chat_completions_create call and returns
    a configurable response. Used to drive the detector through pass/
    partial/fail paths without burning API credits."""

    def __init__(self, base_url: str = "https://mock.example.com"):
        self.base_url = base_url
        self.calls: list[dict] = []
        self.responses: list[dict] = []

    async def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("no canned response for call #" + str(len(self.calls)))
        resp = self.responses.pop(0)
        return ({}, resp, {}, 0)


def _build_resp(text: str, prompt_tokens: int = 1000) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 50,
            "total_tokens": prompt_tokens + 50,
        },
    }


@pytest.mark.asyncio
async def test_long_context_skips_when_not_opted_in():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=False)
    client = _MockClient()
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "skip"
    assert "可选" in result.details["skip_reason"]
    assert client.calls == []  # didn't burn any tokens


@pytest.mark.asyncio
async def test_long_context_passes_when_all_needles_recalled():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # We need to set up the right answers for each tier's needles.
    # The detector seeds per-tier with f"{seed}:{target_tokens}", and the
    # base seed includes time.time() so we don't know it ahead of time.
    # Workaround: monkey-patch make_needles to return predictable answers
    # we can echo back. But that's messy. Easier: use a callable response
    # builder that inspects the request and returns matching answers.

    # Simpler approach: drive the detector through real make_needles output
    # by overriding the mock's behaviour to extract needles from the prompt.
    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        # Extract all unique IDs from the prompt — those are the answers.
        ids = ANSWER_RE.findall(prompt.upper())
        # Return them as the response
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "pass"
    assert result.score == 100.0
    assert len(result.details["tiers_tested"]) == 3
    for tier in result.details["tiers_tested"]:
        assert tier["status"] == "pass"
        assert tier["needles_found"] == 3


@pytest.mark.asyncio
async def test_long_context_fails_at_first_tier_when_truncated():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # Simulate severe truncation — model says "NOT FOUND" for all.
    async def truncated_response(**kwargs):
        return ({}, _build_resp("NOT FOUND\nNOT FOUND\nNOT FOUND"), {}, 0)

    client.chat_completions_create = truncated_response
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "fail"
    # Only the first tier should have been probed (stop-on-first-failure)
    assert len(result.details["tiers_tested"]) == 1
    assert result.details["highest_tier_reached"] == 32_000
    assert result.details["truncation_inferred_at_tokens"] is not None
    # Score: first tier 0pct, rest untested → 0/3 = 0
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_long_context_request_error_treated_as_truncation():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def boom(**kwargs):
        raise RuntimeError("413 Payload Too Large")

    client.chat_completions_create = boom
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "fail"
    assert "413" in result.details["tiers_tested"][0]["error"]
    # Failed tier still counts — just costs $0 since request errored
    assert result.details["tiers_tested"][0]["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_long_context_partial_recall_in_one_tier():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # First tier: full pass. Second tier: 2/3 (partial). Should mark fail
    # because partial at advertised limits is itself a problem.
    call_count = {"n": 0}

    async def degrading_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First tier (32k): all three
            return ({}, _build_resp("\n".join(ids)), {}, 0)
        else:
            # Second tier (100k): only first two
            return ({}, _build_resp("\n".join(ids[:2]) + "\nNOT FOUND"), {}, 0)

    client.chat_completions_create = degrading_response
    result = await det.run(client, "gpt-4o-mini")
    # Partial counts as fail per our policy
    assert result.status == "fail"
    tiers = result.details["tiers_tested"]
    assert tiers[0]["status"] == "pass"
    assert tiers[1]["status"] == "partial"
    # Stops after partial — third tier not tested
    assert len(tiers) == 2


@pytest.mark.asyncio
async def test_long_context_estimated_cost_reported():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4o-mini")
    # gpt-4o-mini @ $0.15/M, total tokens 32k+100k+200k = 332k → ~$0.05
    cost = result.details["estimated_cost_usd"]
    assert 0.04 <= cost <= 0.06
