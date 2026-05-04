"""Protocol-neutral baseline comparison helpers.

This module intentionally contains no Anthropic/OpenAI/Gemini detector names
or protocol wording. Protocol packages register their own diff rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    OK = "ok"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


_SEVERITY_ORDER = {
    Severity.OK: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
}


def max_severity(*items: Severity) -> Severity:
    return max(items, key=lambda s: _SEVERITY_ORDER[s])


@dataclass
class DetectorComparison:
    name: str
    display_name: str
    baseline_score: float
    relay_score: float
    score_diff: float
    severity: Severity
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "baseline_score": self.baseline_score,
            "relay_score": self.relay_score,
            "score_diff": self.score_diff,
            "severity": self.severity.value,
            "findings": self.findings,
        }


DiffRule = Callable[[dict[str, Any], dict[str, Any]], tuple[Severity, list[str]]]


def details(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {}
    value = result.get("details")
    return value if isinstance(value, dict) else {}
