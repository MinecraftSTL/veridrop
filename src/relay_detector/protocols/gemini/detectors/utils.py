"""Shared helpers for Gemini OpenAI-compat detectors.

These pull values out of OpenAI Chat Completions–shaped dicts. Kept tiny on
purpose — duplicating one-line accessors is fine; the cost of an abstract
helper module that hides obvious lookups is higher than the saving.
"""

from __future__ import annotations

from typing import Any


def message_text(resp: dict[str, Any]) -> str:
    """Extract assistant message text from a non-stream Chat Completions response."""
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def finish_reason(resp: dict[str, Any]) -> Any:
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


def tool_calls(resp: dict[str, Any]) -> list[dict[str, Any]]:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    calls = msg.get("tool_calls") if isinstance(msg, dict) else None
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def usage(resp: dict[str, Any]) -> dict[str, Any]:
    value = resp.get("usage")
    return value if isinstance(value, dict) else {}


def int_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
