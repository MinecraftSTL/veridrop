"""Protocol-agnostic core types and orchestration helpers."""

from .models import (
    DetectionReport,
    DetectionTier,
    DetectorResult,
    ExecutionConfig,
    Mode,
    PerformanceMetrics,
    Protocol,
)
from .scorer import compute_total, summary_text, verdict_for

__all__ = [
    "DetectionReport",
    "DetectionTier",
    "DetectorResult",
    "ExecutionConfig",
    "Mode",
    "PerformanceMetrics",
    "Protocol",
    "compute_total",
    "summary_text",
    "verdict_for",
]
