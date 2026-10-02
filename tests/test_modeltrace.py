from __future__ import annotations

import json

import pytest

from relay_detector.modeltrace import adapter
from third_party.ModelTrace import enrollment


VALID_NUMBERS = " ".join(str((index % 355) + 1) for index in range(80))


def test_unified_bank_loads():
    bank = adapter._load_unified_bank()
    assert bank["models"]
    assert bank["calibration"]


def test_request_completion_auto_detects_openai(monkeypatch):
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args[5])
        return "openai-output"

    monkeypatch.setattr(enrollment, "_request_completion", fake_request)
    assert enrollment.request_completion("https://relay.example", "secret", "m", "p", None, "auto") == "openai-output"
    assert calls == ["openai"]


def test_request_completion_auto_falls_back_to_anthropic(monkeypatch):
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(args[5])
        if args[5] == "openai":
            raise RuntimeError("bad OpenAI shape")
        return "anthropic-output"

    monkeypatch.setattr(enrollment, "_request_completion", fake_request)
    assert enrollment.request_completion("https://relay.example", "secret", "m", "p", None, "auto") == "anthropic-output"
    assert calls == ["openai", "anthropic"]


def test_request_completion_reports_format_failure(monkeypatch):
    def fake_request(*args, **kwargs):
        raise RuntimeError(f"bad {args[5]}")

    monkeypatch.setattr(enrollment, "_request_completion", fake_request)
    with pytest.raises(RuntimeError, match="接口格式自动探测失败"):
        enrollment.request_completion("https://relay.example", "secret", "m", "p", None, "auto")


def test_automatic_collects_three_valid_outputs_after_invalid_attempts(monkeypatch):
    challenges = [{"prompt": f"p-{i}", "expected_count": 80} for i in range(6)]
    sequence = ["1 2", VALID_NUMBERS, VALID_NUMBERS, VALID_NUMBERS]
    monkeypatch.setattr(enrollment, "generate_challenges", lambda count: challenges[:count])
    monkeypatch.setattr(enrollment, "request_completion", lambda *args, **kwargs: sequence.pop(0))
    monkeypatch.setattr(
        enrollment,
        "analyze_global_outputs",
        lambda outputs, bank: {
            "prediction_name": "Mock model",
            "probability": 0.75,
            "family_prediction_name": "Mock family",
            "family_probability": 0.8,
            "used_outputs": len(outputs),
            "results": [],
        },
    )
    result = enrollment.test_automatic("https://relay.example", "secret", "m", None, {}, "auto")
    assert result["used_outputs"] == 3
    assert result["api_test"]["attempted"] == 4
    assert result["api_test"]["received"] == 3


def test_adapter_failure_does_not_invent_probability(monkeypatch):
    monkeypatch.setattr(adapter, "test_automatic", lambda **kwargs: (_ for _ in ()).throw(ValueError("没有可用回答")))
    result = adapter._run_sync("https://relay.example", "secret-key", "expected-model")
    assert result["expected_model"] == "expected-model"
    assert result["probability"] is None
    assert result["prediction_name"] is None
    assert result["used_outputs"] == 0
    assert "secret-key" not in json.dumps(result, ensure_ascii=False)


def test_expected_model_is_not_an_algorithm_input(monkeypatch):
    def fake_automatic(**kwargs):
        assert kwargs["api_model"] in {"model-a", "model-b"}
        return {
            "prediction_name": "same",
            "probability": 0.5,
            "family_prediction_name": "same-family",
            "family_probability": 0.5,
            "used_outputs": 3,
            "results": [{"model": "same", "probability": 0.5}],
        }

    monkeypatch.setattr(adapter, "test_automatic", fake_automatic)
    first = adapter._run_sync("https://relay.example", "secret", "model-a")
    second = adapter._run_sync("https://relay.example", "secret", "model-b")
    assert first["probability"] == second["probability"]
    assert first["results"] == second["results"]
    assert first["expected_model"] != second["expected_model"]


def test_adapter_rejects_partial_attribution(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "test_automatic",
        lambda **kwargs: {
            "prediction_name": "partial",
            "probability": 0.99,
            "family_prediction_name": "partial-family",
            "family_probability": 0.99,
            "used_outputs": 2,
            "results": [{"model": "partial", "probability": 0.99}],
            "api_test": {"requested": 3, "received": 2, "attempted": 6, "max_attempts": 6, "errors": []},
        },
    )
    result = adapter._run_sync("https://relay.example", "secret", "expected-model")
    assert result["probability"] is None
    assert result["prediction_name"] is None
    assert result["results"] == []
    assert "有效回答不足" in result["run_error"]


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_request_completion_parses_openai_chat_response(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        return _FakeHTTPResponse({"choices": [{"message": {"content": "1 2 3"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", fake_urlopen)
    assert enrollment._request_completion("https://relay.example/v1", "secret-key", "m", "p", None, "openai") == "1 2 3"
    assert captured == {"url": "https://relay.example/v1/chat/completions", "auth": "Bearer secret-key"}


def test_request_completion_parses_anthropic_messages_response(monkeypatch):
    monkeypatch.setattr(
        enrollment.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeHTTPResponse({"content": [{"type": "text", "text": "4 5 6"}], "stop_reason": "end_turn"}),
    )
    assert enrollment._request_completion("https://relay.example/v1", "secret-key", "m", "p", None, "anthropic") == "4 5 6"


def test_request_completion_retries_retryable_http_error(monkeypatch):
    calls = {"count": 0}

    error = enrollment.urllib.error.HTTPError(
        "https://relay.example/v1/chat/completions", 503, "busy", {}, None,
    )

    def fake_urlopen(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            error.fp = type("_Body", (), {"read": lambda self: b"temporary"})()
            raise error
        return _FakeHTTPResponse({"choices": [{"message": {"content": "7 8 9"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(enrollment.time, "sleep", lambda seconds: None)
    assert enrollment._request_completion("https://relay.example/v1", "secret-key", "m", "p", None, "openai") == "7 8 9"
    assert calls["count"] == 2
