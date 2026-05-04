"""Tests for the pre-submission /v1/models probe.

Covers the protocol classification heuristic, response shape parsing across
the OpenAI / Anthropic / Gemini / bare-list dialects, the graceful-degrade
paths (timeout / 404 / auth fail / non-JSON), and the in-memory cache.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from web.probe import (
    PROTOCOLS,
    _classify,
    _extract_model_ids,
    clear_cache,
    probe_relay,
)


# ---- _classify -----------------------------------------------------------


def test_classify_anthropic_aliases():
    assert _classify("claude-haiku-4-5-20251001") == "anthropic"
    assert _classify("claude-opus-4-7") == "anthropic"
    assert _classify("anthropic/claude-3-5-sonnet") == "anthropic"


def test_classify_openai_aliases():
    assert _classify("gpt-4o-mini") == "openai"
    assert _classify("gpt-5") == "openai"
    assert _classify("o1-preview") == "openai"
    assert _classify("o3-mini") == "openai"
    assert _classify("chatgpt-4o-latest") == "openai"
    assert _classify("text-embedding-3-small") == "openai"


def test_classify_gemini_aliases():
    assert _classify("gemini-2.5-flash") == "gemini"
    assert _classify("gemini-3-pro-preview") == "gemini"
    assert _classify("models/gemini-2.5-pro") == "gemini"
    assert _classify("google/gemini-2.5-flash") == "gemini"


def test_classify_unknown_returns_none():
    assert _classify("llama-3-70b") is None
    assert _classify("deepseek-v3") is None
    assert _classify("") is None
    assert _classify(None) is None  # type: ignore[arg-type]


# ---- _extract_model_ids --------------------------------------------------


def test_extract_openai_shape():
    payload = {"object": "list", "data": [
        {"id": "gpt-4o", "object": "model"},
        {"id": "gpt-4o-mini", "object": "model"},
    ]}
    assert _extract_model_ids(payload) == ["gpt-4o", "gpt-4o-mini"]


def test_extract_gemini_shape():
    payload = {"models": [
        {"name": "models/gemini-2.5-flash"},
        {"name": "models/gemini-2.5-pro"},
    ]}
    assert _extract_model_ids(payload) == ["models/gemini-2.5-flash", "models/gemini-2.5-pro"]


def test_extract_bare_list_shape():
    """Some private relays return just an array of strings."""
    assert _extract_model_ids(["gpt-4o", "claude-opus-4-6"]) == ["gpt-4o", "claude-opus-4-6"]


def test_extract_dedupes_repeated_ids():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o"}, {"id": "gpt-5"}]}
    assert _extract_model_ids(payload) == ["gpt-4o", "gpt-5"]


def test_extract_skips_non_string_ids_and_unknown_keys():
    payload = {"data": [
        {"id": "gpt-4o"},
        {"id": 12345},
        {"weird": "no-id-key"},
        "bare-string-also-ok",
    ]}
    assert _extract_model_ids(payload) == ["gpt-4o", "bare-string-also-ok"]


# ---- probe_relay end-to-end with MockTransport ---------------------------


def _mock(handler):
    """Patch httpx.AsyncClient to use a mock transport for one call."""
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    clear_cache()
    yield
    clear_cache()


def _patch_async_client(monkeypatch, transport):
    """Force probe_relay's httpx.AsyncClient to use this transport."""
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
async def test_probe_happy_path_classifies_all_buckets(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["xkey"] = request.headers.get("x-api-key")
        captured["anthropic_version"] = request.headers.get("anthropic-version")
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "claude-haiku-4-5-20251001"},
            {"id": "claude-opus-4-6"},
            {"id": "gpt-4o-mini"},
            {"id": "gpt-5"},
            {"id": "gemini-3-flash-preview"},
            {"id": "llama-3-70b"},  # unrecognised — must not be bucketed
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test-key")

    assert out["ok"] is True
    assert out["models_endpoint_supported"] is True
    assert out["raw_count"] == 6
    assert sorted(out["by_protocol"]["anthropic"]) == [
        "claude-haiku-4-5-20251001", "claude-opus-4-6",
    ]
    assert sorted(out["by_protocol"]["openai"]) == ["gpt-4o-mini", "gpt-5"]
    assert out["by_protocol"]["gemini"] == ["gemini-3-flash-preview"]
    # unrecognised model not in any bucket
    assert "llama-3-70b" not in (
        out["by_protocol"]["anthropic"]
        + out["by_protocol"]["openai"]
        + out["by_protocol"]["gemini"]
    )

    # request shape: hit /v1/models with both Bearer + x-api-key headers.
    assert captured["url"] == "https://relay.example.com/v1/models"
    assert captured["auth"] == "Bearer sk-test-key"
    assert captured["xkey"] == "sk-test-key"
    assert captured["anthropic_version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_probe_official_anthropic_models_includes_required_version(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["anthropic_version"] = request.headers.get("anthropic-version")
        if not captured["anthropic_version"]:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "anthropic-version: header is required",
                    },
                },
            )
        return httpx.Response(200, json={"data": [
            {"id": "claude-haiku-4-5-20251001", "type": "model"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://api.anthropic.com/v1", "sk-ant-test")

    assert captured["anthropic_version"] == "2023-06-01"
    assert out["ok"] is True
    assert out["auth_ok"] is True
    assert out["by_protocol"]["anthropic"] == ["claude-haiku-4-5-20251001"]


@pytest.mark.asyncio
async def test_probe_root_base_url_falls_back_to_v1_models(monkeypatch):
    """Providers like B.AI document a host root but only allow /v1/models."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://api.b.ai/models":
            return httpx.Response(
                403,
                json={
                    "message": (
                        "HTTP node only allows access to inference API paths "
                        "(/v1/chat/completions, /v1/messages, /v1/models)"
                    ),
                    "success": False,
                },
            )
        return httpx.Response(200, json={"data": [
            {
                "id": "gemini-3.1-pro",
                "owned_by": "google",
                "supported_endpoint_types": ["openai", "anthropic"],
            },
            {"id": "gpt-5.5"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://api.b.ai", "sk-test-key")

    assert seen == ["https://api.b.ai/models", "https://api.b.ai/v1/models"]
    assert out["ok"] is True
    assert out["auth_ok"] is True
    assert out["models_endpoint_supported"] is True
    assert "gemini-3.1-pro" in out["by_protocol"]["gemini"]
    assert "gpt-5.5" in out["by_protocol"]["openai"]


@pytest.mark.asyncio
async def test_probe_root_base_url_falls_back_when_models_returns_non_json(monkeypatch):
    """Some relays serve an HTML landing page at /models but JSON at /v1/models."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://api.sunyears.com/models":
            return httpx.Response(
                200,
                text="<html>not the models API</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, json={"data": [
            {"id": "claude-opus-4.6"},
            {"id": "claude-sonnet-4.6"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://api.sunyears.com", "sk-test-key")

    assert seen == [
        "https://api.sunyears.com/models",
        "https://api.sunyears.com/v1/models",
    ]
    assert out["ok"] is True
    assert out["auth_ok"] is True
    assert out["models_endpoint_supported"] is True
    assert "claude-opus-4.6" in out["by_protocol"]["anthropic"]


@pytest.mark.asyncio
async def test_probe_404_means_endpoint_unsupported_not_failure(monkeypatch):
    """Common on private relays. Frontend renders this as neutral, NOT red,
    and submission stays enabled."""
    _patch_async_client(monkeypatch, _mock(lambda req: httpx.Response(404, text="not found")))
    out = await probe_relay("https://relay.example.com", "sk-test")

    assert out["ok"] is True
    assert out["auth_ok"] is True
    assert out["models_endpoint_supported"] is False
    assert out["raw_count"] == 0
    assert "/v1/models" in (out["note"] or "") or "models" in (out["note"] or "")


@pytest.mark.asyncio
async def test_probe_401_signals_auth_failure(monkeypatch):
    """Auth fail is the ONE upstream error the UI should treat as blocking
    (red pill) — every other failure path is graceful."""
    _patch_async_client(monkeypatch, _mock(lambda req: httpx.Response(401, text="bad key")))
    out = await probe_relay("https://relay.example.com", "sk-bad-key")

    assert out["ok"] is False
    assert out["auth_ok"] is False
    assert "鉴权" in out["error"]


@pytest.mark.asyncio
async def test_probe_500_returns_structured_error(monkeypatch):
    _patch_async_client(monkeypatch, _mock(lambda req: httpx.Response(500, text="oops")))
    out = await probe_relay("https://relay.example.com", "sk-test")

    assert out["ok"] is False
    assert out["auth_ok"] is True  # no auth signal in 500
    assert "500" in out["error"]


@pytest.mark.asyncio
async def test_probe_non_json_response_handled(monkeypatch):
    _patch_async_client(monkeypatch, _mock(lambda req: httpx.Response(200, text="<html>hi</html>")))
    out = await probe_relay("https://relay.example.com", "sk-test")

    assert out["ok"] is False
    assert "JSON" in out["error"]


@pytest.mark.asyncio
async def test_probe_caches_subsequent_calls_for_same_key(monkeypatch):
    """Don't re-hit the relay if the user just blurs/re-blurs api_key."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    _patch_async_client(monkeypatch, _mock(handler))
    a = await probe_relay("https://relay.example.com/v1", "sk-test")
    b = await probe_relay("https://relay.example.com/v1", "sk-test")

    assert call_count["n"] == 1
    assert a is b  # cache returns the exact same dict


@pytest.mark.asyncio
async def test_probe_cache_isolated_by_api_key(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    _patch_async_client(monkeypatch, _mock(handler))
    await probe_relay("https://relay.example.com/v1", "sk-aaa")
    await probe_relay("https://relay.example.com/v1", "sk-bbb")

    assert call_count["n"] == 2


def test_protocols_constant_matches_buckets_in_response():
    assert set(PROTOCOLS) == {"anthropic", "openai", "gemini"}


# ---- best_by_protocol --------------------------------------------------


@pytest.mark.asyncio
async def test_probe_picks_protocol_preferred_default(monkeypatch):
    """best_by_protocol should follow each protocol's pick_default_model
    ordering, not just the relay's natural sort. For OpenAI that means
    gpt-4o-mini wins over gpt-3.5-turbo even when 3.5 sorts first."""
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "gpt-3.5-turbo"},      # sorts first alphabetically
            {"id": "gpt-4"},
            {"id": "gpt-4o"},
            {"id": "gpt-4o-mini"},        # but THIS is the preferred pick
            {"id": "gemini-2.5-flash"},
            {"id": "claude-haiku-4-5-20251001"},
            {"id": "claude-opus-4-7"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test")

    assert out["best_by_protocol"]["openai"] == "gpt-4o-mini"
    assert out["best_by_protocol"]["gemini"] == "gemini-2.5-flash"
    assert out["best_by_protocol"]["anthropic"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_probe_best_falls_back_to_first_when_no_preference_matches(monkeypatch):
    """A relay that only carries non-preferred SKUs should still get a
    suggestion (first available) — strictly better UX than null."""
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "gpt-7-future-only-on-this-relay"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test")
    assert out["best_by_protocol"]["openai"] == "gpt-7-future-only-on-this-relay"


@pytest.mark.asyncio
async def test_probe_best_is_null_for_empty_protocol_buckets(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test")
    assert out["best_by_protocol"]["openai"] == "gpt-4o"
    assert out["best_by_protocol"]["gemini"] is None
    assert out["best_by_protocol"]["anthropic"] is None


@pytest.mark.asyncio
async def test_probe_best_handles_snapshot_suffixes(monkeypatch):
    """When the relay only lists snapshots (e.g. gpt-4o-mini-2024-07-18),
    pick_default_model should still match via prefix and pick that snapshot."""
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "gpt-4o-mini-2024-07-18"},
            {"id": "gpt-3.5-turbo-0125"},
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test")
    assert out["best_by_protocol"]["openai"] == "gpt-4o-mini-2024-07-18"


@pytest.mark.asyncio
async def test_probe_404_response_includes_best_by_protocol_nulls(monkeypatch):
    """Even when /models is 404 the response shape must include the new
    field so the frontend doesn't NPE on data.best_by_protocol[protocol]."""
    _patch_async_client(monkeypatch, _mock(lambda req: httpx.Response(404, text="nope")))
    out = await probe_relay("https://relay.example.com", "sk-test")
    assert "best_by_protocol" in out
    assert out["best_by_protocol"] == {"anthropic": None, "openai": None, "gemini": None}


# ---- per-protocol pick_default_model -----------------------------------


def test_openai_pick_default_prefers_gpt_4o_mini():
    from relay_detector.protocols.openai import pick_default_model
    available = ["gpt-3.5-turbo", "gpt-5", "gpt-4o", "gpt-4o-mini"]
    assert pick_default_model(available) == "gpt-4o-mini"


def test_openai_pick_default_falls_back_to_first():
    from relay_detector.protocols.openai import pick_default_model
    assert pick_default_model(["unknown-model"]) == "unknown-model"
    assert pick_default_model([]) is None


def test_gemini_pick_default_prefers_25_flash_over_3_preview():
    from relay_detector.protocols.gemini import pick_default_model
    available = [
        "gemini-3-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-3-flash-preview",
    ]
    assert pick_default_model(available) == "gemini-2.5-flash"


def test_gemini_pick_default_strips_models_prefix_when_matching():
    from relay_detector.protocols.gemini import pick_default_model
    available = ["models/gemini-2.5-flash"]
    assert pick_default_model(available) == "models/gemini-2.5-flash"


def test_gemini_pick_default_only_previews_available():
    """Multi-protocol relays often carry only the 3.x preview line."""
    from relay_detector.protocols.gemini import pick_default_model
    available = [
        "gemini-3-flash-preview",
        "gemini-3-pro-preview",
        "gemini-3.1-flash-lite-preview",
    ]
    assert pick_default_model(available) == "gemini-3-flash-preview"


def test_anthropic_pick_default_prefers_haiku_over_sonnet_over_opus():
    from relay_detector.protocols.anthropic import pick_default_model
    available = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]
    assert pick_default_model(available) == "claude-haiku-4-5"


def test_anthropic_pick_default_matches_snapshot_suffixes():
    from relay_detector.protocols.anthropic import pick_default_model
    available = ["claude-haiku-4-5-20251001", "claude-opus-4-7"]
    assert pick_default_model(available) == "claude-haiku-4-5-20251001"


# ---- by_protocol sort ordering -----------------------------------------


@pytest.mark.asyncio
async def test_probe_sorts_each_protocol_bucket_by_preference(monkeypatch):
    """The dropdown shows the relay's actual whitelist; preferred (cheap +
    stable) models must come first so users default-select toward the
    healthy ones instead of the deprecated zombies the relay still lists."""
    def handler(request):
        return httpx.Response(200, json={"data": [
            # Intentionally out of preference order; alphabetical too.
            {"id": "gpt-3.5-turbo"},          # deprecated zombie — should sink
            {"id": "gpt-4"},                  # acceptable but not top
            {"id": "gpt-4o"},                 # 2nd preference
            {"id": "gpt-4o-mini"},            # 1st preference (best)
            {"id": "gpt-4o-mini-2024-07-18"}, # snapshot of best, also preferred
            {"id": "unknown-future-model"},   # not in preference list
        ]})

    _patch_async_client(monkeypatch, _mock(handler))
    out = await probe_relay("https://relay.example.com/v1", "sk-test")

    openai_sorted = out["by_protocol"]["openai"]
    # gpt-4o-mini variants float to top; gpt-3.5-turbo sinks to bottom.
    assert openai_sorted[0].startswith("gpt-4o-mini")
    assert openai_sorted[1].startswith("gpt-4o-mini") or openai_sorted[1] == "gpt-4o"
    assert openai_sorted.index("gpt-3.5-turbo") > openai_sorted.index("gpt-4o-mini")


# ---- probe_model_alive --------------------------------------------------


@pytest.mark.asyncio
async def test_probe_model_alive_openai_returns_true_on_2xx(monkeypatch):
    """Smoke: a model that responds 200 to a 4-token chat call is alive."""
    from web.probe import probe_model_alive

    def handler(request):
        return httpx.Response(200, json={
            "id": "chatcmpl-x", "object": "chat.completion", "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    _patch_async_client(monkeypatch, _mock(handler))
    alive, err = await probe_model_alive("https://relay.example.com/v1", "sk", "gpt-4o", "openai")
    assert alive is True
    assert err is None


@pytest.mark.asyncio
async def test_probe_model_alive_returns_false_with_upstream_body_on_4xx(monkeypatch):
    """A 4xx (model_not_found) must surface the upstream body so the user
    sees the real reason, not just a generic exception."""
    from web.probe import probe_model_alive

    def handler(request):
        return httpx.Response(404, text='{"error":{"code":"model_not_found","message":"gpt-3.5-turbo deprecated"}}')

    _patch_async_client(monkeypatch, _mock(handler))
    alive, err = await probe_model_alive(
        "https://relay.example.com/v1", "sk", "gpt-3.5-turbo", "openai",
    )
    assert alive is False
    assert err is not None
    assert "model_not_found" in err
    assert "gpt-3.5-turbo" in err  # upstream body, not a generic message


@pytest.mark.asyncio
async def test_probe_model_alive_unknown_protocol_fails_gracefully():
    from web.probe import probe_model_alive
    alive, err = await probe_model_alive("https://x", "sk", "m", "klingon")
    assert alive is False
    assert "klingon" in err
