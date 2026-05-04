"""Weighted score aggregation — see DESIGN.md §5."""

from __future__ import annotations

from collections.abc import Iterable

from .models import DetectorResult, Verdict


# DESIGN.md §5.3
def verdict_for(score: float) -> Verdict:
    if score >= 70.0:
        return "passed"
    if score >= 50.0:
        return "marginal"
    return "failed"


def has_critical_issues(results: Iterable[DetectorResult]) -> bool:
    """True if any detector reported one or more critical-severity issues.

    Detectors surface critical issues via either:
    - `details.critical_issue_count > 0` (passive detectors that aggregate),
    - `details.issues[*].severity == "critical"` (anything that lists raw issues).

    A single critical means the relay does something fundamentally wrong (e.g.
    impersonates OpenAI by leaking Anthropic backend fingerprints in usage).
    The weighted score may still be high — one critical-bearing detector at
    25% can be drowned out by six others at 100% — so we use this as a hard
    veto, separately from the score.
    """
    for r in results:
        if r.status == "skip":
            continue
        details = r.details if isinstance(r.details, dict) else {}
        count = details.get("critical_issue_count")
        if isinstance(count, int) and count > 0:
            return True
        for issue in details.get("issues") or []:
            if isinstance(issue, dict) and issue.get("severity") == "critical":
                return True
    return False


def effective_verdict(score: float, results: Iterable[DetectorResult]) -> Verdict:
    """Verdict capped by critical findings.

    A relay scoring 75 with NO critical issues is genuinely "passed". A relay
    scoring 75 with critical issues (e.g. usage_source_non_openai) is at best
    "marginal" — its high score comes from passing many cheap structural
    checks while failing the one check that would have exposed it. The UI
    reads verdict to pick the score-ring color, so this cap is what stops a
    detected impersonation from showing up as a green 75% circle.
    """
    base = verdict_for(score)
    if has_critical_issues(list(results)):
        if base == "passed":
            return "marginal"
    return base


def summary_text(score: float, verdict: Verdict) -> str:
    if verdict == "passed" and score >= 85.0:
        return "优秀"
    if verdict == "passed":
        return "通过"
    if verdict == "marginal":
        return "基本合格"
    return "未达标"


def compute_total(results: Iterable[DetectorResult]) -> float:
    """Weighted average over results that are not skipped.

    Per DESIGN.md §5.2:
        effective_weight = Σ d.weight for d.status != "skip"
        total = Σ (d.score × d.weight) / effective_weight
    """
    valid = [r for r in results if r.status != "skip"]
    if not valid:
        return 0.0
    weight_sum = sum(r.weight for r in valid)
    if weight_sum <= 0:
        return 0.0
    weighted = sum(r.score * r.weight for r in valid)
    return weighted / weight_sum


_FATAL_UPSTREAM_PATTERNS = (
    "credit balance is too low",
    "specified api usage limits",
    "usage limits",
    "insufficient_quota",
    "billing",
)

_FATAL_MODEL_UNAVAILABLE_PATTERNS = (
    "model_not_found",
    "model not found",
    "model_not_available",
    "model not available",
    "no available channel",
    "无可用渠道",
)


def fatal_run_error(results: Iterable[DetectorResult]) -> str | None:
    """Return a run-level error when upstream quota/billing blocks detection.

    These are not model-quality failures. Scoring them as ordinary detector
    failures produces misleading reports such as "35%" when the real outcome is
    "the account cannot run this test".
    """
    haystacks: list[str] = []
    for result in results:
        if result.error:
            haystacks.append(result.error)
        _collect_nested_errors(result.details, haystacks)
    text = "\n".join(haystacks).lower()
    if not text:
        return None
    if any(pattern in text for pattern in _FATAL_UPSTREAM_PATTERNS):
        return (
            "检测无效: 上游返回余额不足或用量限制错误。请更换有额度的 API key,"
            " 或降低检测模式/模型后重新检测。"
        )
    if any(pattern in text for pattern in _FATAL_MODEL_UNAVAILABLE_PATTERNS):
        return (
            "检测无效: 中转站没有当前模型的可用渠道,或该模型名称不被此中转站支持。"
            " 请更换该站已开通的模型后重新检测。"
        )
    return None


def _collect_nested_errors(value, out: list[str]) -> None:
    if isinstance(value, dict):
        err = value.get("error")
        if isinstance(err, str):
            out.append(err)
        for child in value.values():
            _collect_nested_errors(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_nested_errors(child, out)
