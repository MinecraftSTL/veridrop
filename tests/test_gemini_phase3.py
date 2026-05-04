"""Tests for Gemini OpenAI-compat protocol detection (post-rewrite).

The Gemini detector talks ONLY to the OpenAI Chat Completions wire shape now;
there is no native /v1beta/models/X:generateContent path. Tests use a fake
client that satisfies the same interface as ThrottledGeminiClient
(chat_completions_create + chat_completions_stream + observe broadcast).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from relay_detector.core.models import ExecutionConfig, Mode
from relay_detector.protocols.gemini import (
    build_detectors,
    build_runner,
    default_base_url,
    model_choices,
    tier_banner,
)
from relay_detector.protocols.gemini.client import (
    DEFAULT_GEMINI_OPENAI_BASE_URL,
    GeminiClient,
    normalize_gemini_base_url,
)
from relay_detector.protocols.gemini.config import models_match


# ---------------------------------------------------------------------------
# Fake Chat Completions backend the FakeBase / runner exercise.
# ---------------------------------------------------------------------------


def _chat_response(
    text: str = "pong",
    *,
    model: str = "gemini-2.5-flash",
    prompt_tokens: int = 8,
    completion_tokens: int = 4,
    finish_reason: str = "stop",
    tool_calls: list[dict] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test1",
        "object": "chat.completion",
        "model": model,
        "created": 1_700_000_000,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _stream_chunks(
    text: str = "pong",
    *,
    model: str = "gemini-2.5-flash",
    prompt_tokens: int = 8,
    completion_tokens: int = 4,
    finish_reason: str = "stop",
    include_usage: bool = True,
) -> list[dict[str, Any]]:
    head = {
        "id": "chatcmpl-test1",
        "object": "chat.completion.chunk",
        "model": model,
        "created": 1_700_000_000,
        "choices": [{"index": 0, "delta": {"role": "assistant"}}],
    }
    body = {
        "id": "chatcmpl-test1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}}],
    }
    tail = {
        "id": "chatcmpl-test1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    out = [head, body, tail]
    if include_usage:
        out.append(
            {
                "id": "chatcmpl-test1",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )
    return out


def _prompt_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            parts.append(msg["content"])
    return "\n".join(parts)


class FakeChatBase:
    """Stand-in for the real GeminiClient used by ThrottledGeminiClient.

    Returns deterministic responses keyed off prompt content and tools so a
    full standard-mode run produces realistic per-detector outcomes.
    """

    async def chat_completions_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        text = _prompt_text(body)
        if body.get("tools"):
            tool_call = {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_current_weather",
                    "arguments": json.dumps({"city": "Boston, MA", "unit": "celsius"}),
                },
            }
            resp = _chat_response(
                text="",
                tool_calls=[tool_call],
                finish_reason="tool_calls",
                prompt_tokens=14,
                completion_tokens=8,
            )
        elif body.get("response_format", {}).get("type") == "json_schema":
            resp = _chat_response(
                text='{"ok": true, "nonce": "gemini-detector"}',
                prompt_tokens=18,
                completion_tokens=12,
            )
        elif "Reference text:" in text:
            resp = _chat_response(text="ok", prompt_tokens=88, completion_tokens=2)
        elif "veridrop stream check" in text:
            resp = _chat_response(
                text="veridrop stream check",
                prompt_tokens=10,
                completion_tokens=5,
            )
        elif "418" in text:
            resp = _chat_response(
                text="Status 418 means I'm a teapot.",
                prompt_tokens=14,
                completion_tokens=8,
            )
        else:
            resp = _chat_response(text="pong", prompt_tokens=8, completion_tokens=4)
        return body, resp, httpx.Headers(), 12

    async def chat_completions_stream(self, **body: Any):
        text = _prompt_text(body)
        if "veridrop stream check" in text:
            chunks = _stream_chunks(
                text="veridrop stream check",
                prompt_tokens=10,
                completion_tokens=5,
                include_usage=bool(body.get("stream_options", {}).get("include_usage")),
            )
        else:
            chunks = _stream_chunks(
                text="ok",
                prompt_tokens=8,
                completion_tokens=4,
                include_usage=bool(body.get("stream_options", {}).get("include_usage")),
            )
        for i, chunk in enumerate(chunks):
            yield chunk, 10 + i


# ---------------------------------------------------------------------------
# End-to-end runner test: every detector should get a real verdict.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_standard_run_passes_with_openai_compat_responses():
    cfg = ExecutionConfig.for_mode(Mode.STANDARD, max_concurrent=3)
    runner = build_runner(FakeChatBase(), build_detectors(cfg.mode), cfg)

    outcome = await runner.run("gemini-2.5-flash")
    by_name = {r.name: r for r in outcome.results}

    assert set(by_name) >= {
        "basic_request",
        "model_info",
        "function_calling",
        "structured_output",
        "protocol",
        "integrity",
        "token_usage",
    }
    assert by_name["basic_request"].status == "pass"
    assert by_name["model_info"].status == "pass"
    assert by_name["function_calling"].status == "pass"
    assert by_name["structured_output"].status == "pass"
    assert by_name["protocol"].status == "pass"
    # Token usage delta from the long-prompt probe should be in the wide range.
    assert by_name["token_usage"].status == "pass"
    # Stream + non-stream produce identical text in the fake.
    assert by_name["integrity"].status == "pass"

    # Every detector that actually ran should also have observed by the
    # passive ProtocolDetector at least once.
    proto = by_name["protocol"]
    assert proto.details["observation_count"] >= 6


# ---------------------------------------------------------------------------
# Per-piece checks the runner test alone wouldn't cover.
# ---------------------------------------------------------------------------


def test_normalize_gemini_base_url_preserves_explicit_prefixes():
    assert normalize_gemini_base_url("https://api.example.com/v1") == (
        "https://api.example.com/v1"
    )
    assert normalize_gemini_base_url("https://api.example.com/v1/") == (
        "https://api.example.com/v1"
    )
    assert normalize_gemini_base_url("https://generativelanguage.googleapis.com/v1beta/openai/") == (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )


def test_normalize_gemini_base_url_adds_v1_for_host_roots():
    # B.AI documents https://api.b.ai as the base URL but serves Gemini through
    # the OpenAI-compatible /v1/chat/completions path.
    assert normalize_gemini_base_url("https://api.example.com") == (
        "https://api.example.com/v1"
    )
    assert normalize_gemini_base_url("https://api.example.com/") == (
        "https://api.example.com/v1"
    )


def test_default_base_url_points_to_google_official_openai_compat():
    assert default_base_url() == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert DEFAULT_GEMINI_OPENAI_BASE_URL == default_base_url()


def test_tier_banner_signals_protocol_level_and_mentions_openai_compat():
    title, message = tier_banner()
    assert title == "协议级验证"
    assert "OpenAI" in message and "chat/completions" in message


def test_model_choices_contains_supported_aliases_and_no_deprecated():
    choices = model_choices()
    assert "gemini-2.5-flash" in choices
    assert "gemini-2.5-pro" in choices
    assert "gemini-2.5-flash-lite" in choices
    # 1.5 / 2.0 are deprecated per Google docs; they must not be advertised.
    assert not any(c.startswith("gemini-1.5") for c in choices)
    assert not any(c == "gemini-2.0-flash" for c in choices)


def test_models_match_handles_snapshot_and_models_prefix():
    assert models_match("gemini-2.5-flash", "gemini-2.5-flash") is True
    # Snapshot suffix
    assert models_match("gemini-2.5-flash", "gemini-2.5-flash-001") is True
    # Bare alias request, models/-prefixed response
    assert models_match("gemini-2.5-flash", "models/gemini-2.5-flash") is True
    assert models_match("gemini-2.5-pro", "gemini-2.5-flash") is False
    assert models_match("", "gemini-2.5-flash") is False


@pytest.mark.asyncio
async def test_gemini_client_uses_bearer_auth_and_posts_to_chat_completions():
    """Connectivity-level wiring: the wrapped httpx client targets the right
    URL and ships the right auth header. Mocked at the transport level so we
    don't need network, but it's a real GeminiClient end-to-end."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=_chat_response(text="pong"),
        )

    transport = httpx.MockTransport(handler)
    client = GeminiClient("https://relay.example.com/v1", "sk-test-key")
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=client.base_url,
        headers={
            "authorization": f"Bearer {client.api_key}",
            "content-type": "application/json",
        },
        transport=transport,
    )
    try:
        _req, resp, _h, _lat = await client.chat_completions_create(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_completion_tokens=8,
        )
    finally:
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"] == "https://relay.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test-key"
    assert resp["id"] == "chatcmpl-test1"


@pytest.mark.asyncio
async def test_gemini_client_propagates_400_unchanged():
    """Old code tried to fall back through bogus model aliases. New code
    must NOT swallow errors — the detector layer turns them into
    DetectorResult(status="error") so the user sees the real upstream
    response, not a misleading 'no model candidates' message."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "model_not_found", "message": "unknown model"}},
        )

    transport = httpx.MockTransport(handler)
    client = GeminiClient("https://relay.example.com/v1", "sk-test")
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=client.base_url,
        headers={"authorization": "Bearer sk-test"},
        transport=transport,
    )
    try:
        with pytest.raises(Exception) as excinfo:
            await client.chat_completions_create(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": "hi"}],
                max_completion_tokens=8,
            )
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    assert "400" in msg
    # The raw upstream message must be visible — the detector reports it
    # straight to the user instead of trying to be clever.
    assert "model_not_found" in msg
