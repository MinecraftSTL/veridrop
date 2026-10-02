"""Read-only history listing for ModelTrace reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .jobs import JOBS_DIR


@dataclass(frozen=True)
class ModelTraceEntry:
    job_id: str
    domain: str
    expected_model: str
    prediction_name: str
    probability: float
    family_prediction_name: str
    family_probability: float
    used_outputs: int
    timestamp: datetime | None

    @property
    def probability_percent(self) -> str:
        return f"{self.probability * 100:.2f}%"

    @property
    def family_probability_percent(self) -> str:
        return f"{self.family_probability * 100:.2f}%"

    @property
    def date_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M") if self.timestamp else ""


def _domain(base_url: str) -> str:
    value = (base_url or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0]


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def list_entries() -> list[ModelTraceEntry]:
    directory = JOBS_DIR / "modeltrace"
    if not directory.is_dir():
        return []
    entries: list[ModelTraceEntry] = []
    for path in directory.glob("*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("test_method") != "modeltrace":
            continue
        entries.append(
            ModelTraceEntry(
                job_id=path.stem,
                domain=_domain(str(report.get("base_url") or "")),
                expected_model=str(report.get("expected_model") or report.get("target_model") or ""),
                prediction_name=str(report.get("prediction_name") or "无法判断"),
                probability=_safe_float(report.get("probability")),
                family_prediction_name=str(report.get("family_prediction_name") or "无法判断"),
                family_probability=_safe_float(report.get("family_probability")),
                used_outputs=_safe_int(report.get("used_outputs")),
                timestamp=_timestamp(report.get("timestamp")),
            )
        )
    entries.sort(
        key=lambda item: (item.timestamp.timestamp() if item.timestamp else 0, item.job_id),
        reverse=True,
    )
    return entries
