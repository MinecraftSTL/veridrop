"""Bayesian-weighted ranking tests + pagination behavior.

The headline claim of the leaderboard redesign 2026-05-05 is "测试越多
分数越能反映真实能力, fluky single-pass relays don't beat consistent
performers". These tests pin that claim down so a future refactor can't
silently revert to flat median sort.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from web import leaderboard


def _stage_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: list[tuple[str, list[float]]],
) -> None:
    """Helper: spec is list of (domain, [score1, score2, ...])."""
    proto_dir = tmp_path / "anthropic"
    proto_dir.mkdir()
    job_id = 0
    for domain, scores in spec:
        for s in scores:
            job_id += 1
            (proto_dir / f"job{job_id}.json").write_text(json.dumps({
                "base_url": f"https://{domain}/v1",
                "protocol": "anthropic",
                "target_model": "claude-haiku-4-5",
                "total_score": s,
                "verdict": "passed",
                "timestamp": f"2026-05-{(job_id % 28) + 1:02d}T10:00:00Z",
                "results": [],
            }))
    monkeypatch.setattr(leaderboard, "REPORT_DIRS", [proto_dir])


def test_bayesian_score_pulls_few_samples_toward_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The fundamental sort claim: a 100-test consistent 99% beats a
    1-test 100%. This catches the regression where someone "fixes" the
    sort to use overall_median again — that would make the new relay win."""
    _stage_reports(tmp_path, monkeypatch, [
        ("flaky-newcomer.example.com", [100.0]),       # 1 sample, perfect
        ("seasoned-good.example.com", [99.0] * 100),   # 100 samples, 99
    ])
    relays, _ = leaderboard.aggregate()
    domains = [r.domain for r in relays]
    # Seasoned-good should rank ABOVE flaky-newcomer
    assert domains.index("seasoned-good.example.com") < domains.index(
        "flaky-newcomer.example.com"
    )


def test_ranking_score_formula():
    """Sanity check the formula: prior_value=50, prior_weight=5."""
    relay = leaderboard.RelayStats(domain="x.example.com")
    relay.by_protocol["anthropic"] = leaderboard.ProtocolStats(protocol="anthropic")
    relay.by_protocol["anthropic"].scores = [100.0]  # 1 sample
    # (100 + 50*5) / (1 + 5) = 350/6 = 58.333
    assert 58.0 < relay.ranking_score < 58.7

    relay.by_protocol["anthropic"].scores = [100.0] * 5  # 5 samples
    # (500 + 250) / (5 + 5) = 75
    assert 74.5 < relay.ranking_score < 75.5

    relay.by_protocol["anthropic"].scores = [100.0] * 100  # 100 samples
    # (10000 + 250) / (100 + 5) ≈ 97.6
    assert 97.0 < relay.ranking_score < 98.0


def test_ranking_score_no_samples_returns_prior():
    """Empty relay returns 50 (neutral prior) — sort doesn't crash on it."""
    empty = leaderboard.RelayStats(domain="x.example.com")
    assert empty.ranking_score == 50.0


def test_aggregate_ordering_is_ranked_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Even if a single-sample relay's ranking_score is high, it should
    NOT outrank a multi-sample relay (≥2). is_ranked tier comes first."""
    _stage_reports(tmp_path, monkeypatch, [
        ("single-pass.example.com", [100.0]),         # 1 sample → unranked
        ("modest-multi.example.com", [60.0, 65.0]),   # 2 samples → ranked
    ])
    relays, summary = leaderboard.aggregate()
    domains = [r.domain for r in relays]
    # Even with a much lower median, the multi-sample relay sorts higher
    # because is_ranked precedes ranking_score in the sort key.
    assert domains.index("modest-multi.example.com") < domains.index(
        "single-pass.example.com"
    )
    assert summary["ranked_relays"] == 1
    assert summary["total_relays"] == 2
