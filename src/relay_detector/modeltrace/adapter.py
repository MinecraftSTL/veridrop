"""Run ModelTrace's upstream fingerprinting algorithm without its Flask app."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Any

from third_party.ModelTrace.enrollment import test_automatic
from third_party.ModelTrace.fingerprint import load_bank


MODELTRACE_ROOT = Path(__file__).resolve().parents[3] / "third_party" / "ModelTrace"
MODELTRACE_BANK = MODELTRACE_ROOT / "data" / "unified_bank.json"
MODELTRACE_SOURCE = "xqy2006/ModelTrace"
MODELTRACE_SOURCE_REVISION = (
    "provided ModelTrace-main.zip; Git commit metadata was not included in the archive"
)
MODELTRACE_SOURCE_COMMIT: str | None = None
MODELTRACE_LICENSE = "MIT"


@lru_cache(maxsize=1)
def _load_unified_bank() -> dict[str, Any]:
    return load_bank(MODELTRACE_BANK)


def _redact(value: object, secret: str) -> str:
    text = str(value or "").strip()
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def _failure_result(model: str, api_key: str, error: object) -> dict[str, Any]:
    """Represent an upstream failure without inventing a probability."""
    message = _redact(error, api_key) or "ModelTrace 自动测试失败"
    return {
        "prediction": None,
        "prediction_name": None,
        "probability": None,
        "family_prediction": None,
        "family_prediction_name": None,
        "family_probability": None,
        "family_probabilities": [],
        "used_outputs": 0,
        "results": [],
        "diagnostics": [],
        "calibration": {},
        "method": "统一全局稳健数字指纹",
        "api_test": {
            "requested": 3,
            "attempted": 0,
            "max_attempts": 6,
            "received": 0,
            "errors": [message],
        },
        "run_error": message,
        "expected_model": model,
    }


def _insufficient_result(model: str, result: dict[str, Any]) -> dict[str, Any]:
    """Refuse to publish attribution probabilities from too few valid queries."""
    api_test = dict(result.get("api_test") or {})
    received = int(api_test.get("received") or result.get("used_outputs") or 0)
    requested = int(api_test.get("requested") or 3)
    errors = list(api_test.get("errors") or [])
    errors.append(f"有效回答不足：{received}/{requested}，不生成归因概率")
    result.update({
        "prediction": None,
        "prediction_name": None,
        "probability": None,
        "family_prediction": None,
        "family_prediction_name": None,
        "family_probability": None,
        "family_probabilities": [],
        "results": [],
        "run_error": f"有效回答不足：{received}/{requested}，不生成归因概率",
    })
    api_test.update({"received": received, "requested": requested, "errors": errors})
    result["api_test"] = api_test
    result["expected_model"] = model
    return result


def _run_sync(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    try:
        result = test_automatic(
            base_url=base_url,
            api_key=api_key,
            api_model=model,
            temperature=None,
            bank=_load_unified_bank(),
            api_format="auto",
        )
    except Exception as error:
        result = _failure_result(model, api_key, error)
    if not result.get("run_error") and int((result.get("used_outputs") or 0)) < 3:
        result = _insufficient_result(model, result)
    api_test = result.get("api_test") or {}
    api_test["errors"] = [_redact(error, api_key) for error in api_test.get("errors") or []]
    result["api_test"] = api_test
    result["source"] = MODELTRACE_SOURCE
    result["source_revision"] = MODELTRACE_SOURCE_REVISION
    result["source_commit"] = MODELTRACE_SOURCE_COMMIT
    result["license"] = MODELTRACE_LICENSE
    result["expected_model"] = model
    return result


async def run_modeltrace(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """Run the synchronous upstream probe away from the event loop."""
    return await asyncio.to_thread(_run_sync, base_url, api_key, model)
