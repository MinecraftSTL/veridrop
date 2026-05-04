"""Unit tests for scorer."""

from __future__ import annotations

from relay_detector.models import DetectorResult
from relay_detector.scorer import (
    compute_total,
    effective_verdict,
    fatal_run_error,
    has_critical_issues,
    summary_text,
    verdict_for,
)


def _r(
    name: str,
    score: float,
    weight: float,
    status: str = "pass",
    details: dict | None = None,
) -> DetectorResult:
    return DetectorResult(
        name=name,
        display_name=name,
        status=status,  # type: ignore[arg-type]
        score=score,
        weight=weight,
        details=details or {},
    )


def test_compute_total_basic_weighted_average():
    results = [
        _r("a", 100, 5),
        _r("b", 50, 5),
    ]
    # (100*5 + 50*5) / 10 = 75
    assert compute_total(results) == 75.0


def test_compute_total_skipped_excluded_from_denominator():
    results = [
        _r("a", 100, 5),
        _r("b", 0, 95, status="skip"),
    ]
    # skip excluded -> just a -> 100
    assert compute_total(results) == 100.0


def test_compute_total_empty_returns_zero():
    assert compute_total([]) == 0.0
    assert compute_total([_r("a", 100, 5, status="skip")]) == 0.0


def test_compute_total_zero_weight_safe():
    assert compute_total([_r("a", 100, 0)]) == 0.0


def test_verdict_thresholds():
    assert verdict_for(100) == "passed"
    assert verdict_for(85) == "passed"
    assert verdict_for(70) == "passed"
    assert verdict_for(69.99) == "marginal"
    assert verdict_for(50) == "marginal"
    assert verdict_for(49.99) == "failed"
    assert verdict_for(0) == "failed"


def test_summary_text_buckets():
    assert summary_text(95, "passed") == "优秀"
    assert summary_text(80, "passed") == "通过"
    assert summary_text(60, "marginal") == "基本合格"
    assert summary_text(20, "failed") == "未达标"


def test_compute_total_treats_error_as_zero_weighted():
    """An 'error' status still has weight, so it pulls total down."""
    results = [
        _r("a", 100, 5),
        _r("b", 0, 5, status="error"),
    ]
    # error counts: (100*5 + 0*5)/10 = 50
    assert compute_total(results) == 50.0


def test_fatal_run_error_detects_upstream_credit_limit():
    results = [
        DetectorResult(
            name="identity",
            display_name="identity",
            status="error",
            score=0,
            weight=5,
            error="HTTP 400: Your credit balance is too low to access the Anthropic API",
        )
    ]
    assert fatal_run_error(results) is not None


def test_fatal_run_error_detects_nested_usage_limit_errors():
    results = [
        DetectorResult(
            name="knowledge",
            display_name="knowledge",
            status="fail",
            score=0,
            weight=10,
            details={
                "per_question": [
                    {
                        "id": "q1",
                        "passed": False,
                        "error": "You have reached your specified API usage limits.",
                    }
                ]
            },
        )
    ]
    assert fatal_run_error(results) is not None


def test_fatal_run_error_detects_model_unavailable_errors():
    results = [
        DetectorResult(
            name="basic_request",
            display_name="basic_request",
            status="error",
            score=0,
            weight=15,
            error=(
                "HTTP 503: 分组 default 下模型 gpt-4 无可用渠道（distributor） "
                '{"code":"model_not_found"}'
            ),
        )
    ]
    msg = fatal_run_error(results)
    assert msg is not None
    assert "模型" in msg


# ---- has_critical_issues / effective_verdict ----------------------------


def test_has_critical_issues_via_critical_issue_count():
    """Passive ProtocolDetector aggregates as critical_issue_count."""
    results = [
        _r("protocol", 25, 15, status="fail", details={"critical_issue_count": 18}),
        _r("basic_request", 100, 15),
    ]
    assert has_critical_issues(results) is True


def test_has_critical_issues_via_inline_severity():
    """Some detectors put severity directly on each issue dict."""
    results = [
        _r("protocol", 50, 15, status="fail", details={
            "issues": [
                {"severity": "minor", "code": "x"},
                {"severity": "critical", "code": "y"},
            ],
        }),
    ]
    assert has_critical_issues(results) is True


def test_has_critical_issues_false_when_none_critical():
    results = [
        _r("a", 100, 15),
        _r("b", 80, 15, details={"critical_issue_count": 0}),
        _r("c", 90, 15, details={"issues": [{"severity": "major"}]}),
    ]
    assert has_critical_issues(results) is False


def test_has_critical_issues_ignores_skipped_detectors():
    """A skipped detector's accumulated state shouldn't poison the verdict."""
    results = [
        _r("p", 0, 15, status="skip", details={"critical_issue_count": 5}),
    ]
    assert has_critical_issues(results) is False


def test_effective_verdict_caps_passed_to_marginal_on_critical():
    """The smoking-gun case: 75% score that includes a critical-bearing
    detector — sunyears-style OpenAI impersonation. Without this cap the UI
    showed a green 75% circle while the report listed 18 critical issues."""
    results = [
        _r("protocol", 25, 15, status="fail", details={"critical_issue_count": 18}),
        _r("basic_request", 100, 15),
        _r("model_consistency", 80, 15),
        _r("function_calling", 100, 15),
        _r("structured_output", 10, 15),
        _r("integrity", 100, 15),
        _r("token_billing", 100, 10),
    ]
    score = compute_total(results)
    assert score >= 70.0  # raw score still in passed band
    assert verdict_for(score) == "passed"
    # but effective verdict is capped:
    assert effective_verdict(score, results) == "marginal"


def test_effective_verdict_does_not_promote_failed():
    """Critical-issue cap only DOWNgrades; it never promotes a low score
    upward. A 30% relay stays failed even if no critical issue is found."""
    results = [_r("a", 30, 100)]
    assert effective_verdict(30.0, results) == "failed"


def test_effective_verdict_unchanged_when_no_critical():
    results = [_r("a", 90, 100)]
    assert effective_verdict(90.0, results) == "passed"
