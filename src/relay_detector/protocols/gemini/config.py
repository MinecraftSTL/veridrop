"""Gemini OpenAI-compat detector config — model list, weights, mode mapping."""

from __future__ import annotations

from ...core.models import Mode


# Detector internal names match what the web UI expects in `_DETECTOR_DISPLAY`.
# `model_info` and `token_usage` are Gemini-flavored renames of OpenAI's
# `model_consistency` and `token_billing` — the validation logic is the same
# Chat Completions shape, the labels are what users see in the report.
DETECTOR_WEIGHTS: dict[str, float] = {
    "basic_request": 15.0,
    "model_info": 15.0,
    "function_calling": 15.0,
    "structured_output": 15.0,
    "protocol": 15.0,
    "integrity": 15.0,
    "token_usage": 10.0,
}


MODE_DETECTORS: dict[Mode, set[str]] = {
    Mode.QUICK: {
        "basic_request",
        "model_info",
        "protocol",
    },
    Mode.STANDARD: set(DETECTOR_WEIGHTS.keys()),
    Mode.FULL: set(DETECTOR_WEIGHTS.keys()),
}


# Curated suggestions surfaced in the web form. Free-form input is still
# accepted; this is just the dropdown. Updated against the official model page
# (May 2026): 2.0/1.5 are deprecated and removed; 3.x previews are listed but
# kept after the stable 2.5 family because preview support drifts.
GEMINI_MODEL_CHOICES = [
    # Preview models first — most multi-protocol relays carry the 3.x line
    # before the 2.5 line as of May 2026.
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    # Stable Google-direct lineup
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]


def models_match(request_model: str, response_model: str) -> bool:
    """Lenient match for Gemini OpenAI-compat responses.

    Relays return the model id in `response.model` in several shapes:
    - bare alias: `gemini-2.5-flash`
    - dated snapshot: `gemini-2.5-flash-001`, `gemini-2.5-flash-09-2025`
    - prefixed: `models/gemini-2.5-flash` (some relays echo Google's native form)

    We accept any of these as long as one is a prefix of the other after
    stripping the `models/` prefix.
    """
    if not request_model or not response_model:
        return False
    a = request_model.removeprefix("models/")
    b = response_model.removeprefix("models/")
    return a == b or a.startswith(b) or b.startswith(a)
