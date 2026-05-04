"""Compare a relay-station detect report against an official-API baseline.

The point isn't just "score difference" — it's *what specifically* diverges.
Per-detector field-level comparisons surface the smoking guns: thinking block
missing, tool_use stripped, UUID instead of msg_ id, etc. A small score gap
with a critical-severity finding is more damning than a big gap from minor
formatting drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .config import models_match


class Severity(str, Enum):
    OK = "ok"             # within tolerance / matches baseline
    MINOR = "minor"       # small drift; usable
    MAJOR = "major"       # capability degraded
    CRITICAL = "critical"  # not the model it claims to be


_SEVERITY_ORDER = {
    Severity.OK: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
}


def _max_severity(*items: Severity) -> Severity:
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


@dataclass
class ComparisonReport:
    baseline_path: str
    relay_path: str
    baseline_meta: dict[str, Any]
    relay_meta: dict[str, Any]
    detectors: list[DetectorComparison]
    overall_severity: Severity
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_path": self.baseline_path,
            "relay_path": self.relay_path,
            "baseline_meta": self.baseline_meta,
            "relay_meta": self.relay_meta,
            "overall_severity": self.overall_severity.value,
            "summary": self.summary,
            "detectors": [d.to_dict() for d in self.detectors],
        }


# ---------------------------------------------------------------------------
# Loading + auto-discovery
# ---------------------------------------------------------------------------


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_baseline_for(
    model: str, mode: str, baseline_dir: Path
) -> Path | None:
    """Auto-discover baseline file by model + mode in `baseline_dir`."""
    if not baseline_dir.is_dir():
        return None
    direct = baseline_dir / f"{model}_{mode}.json"
    if direct.is_file():
        return direct
    for f in sorted(baseline_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("mode") == mode and models_match(
            model, d.get("target_model", "")
        ):
            return f
    return None


# ---------------------------------------------------------------------------
# Per-detector comparators — each returns (severity, findings)
# ---------------------------------------------------------------------------


def _index_results(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in report.get("results", []) if "name" in r}


def _details(r: dict[str, Any] | None) -> dict[str, Any]:
    if r is None:
        return {}
    d = r.get("details")
    return d if isinstance(d, dict) else {}


def _cmp_thinking_signature(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    sev = Severity.OK

    b_seen = bd.get("thinking_block_seen")
    r_seen = rd.get("thinking_block_seen")
    if b_seen and not r_seen:
        findings.append(
            "thinking 块完全没返回 — 中转站可能未启用 thinking,或后台模型不支持"
        )
        sev = Severity.CRITICAL

    b_sig = int(bd.get("signature_length") or 0)
    r_sig = int(rd.get("signature_length") or 0)
    if b_sig > 0 and r_sig == 0:
        findings.append(f"signature 缺失 (baseline 长度 {b_sig} chars)")
        sev = Severity.CRITICAL
    elif b_sig > 0 and 0 < r_sig < b_sig * 0.3:
        findings.append(
            f"signature 异常短: relay {r_sig} chars vs baseline {b_sig}"
        )
        sev = _max_severity(sev, Severity.MAJOR)

    return sev, findings


def _cmp_identity(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    sev = Severity.OK

    b_required = len(bd.get("required_hits") or [])
    r_required = len(rd.get("required_hits") or [])
    if b_required > r_required:
        findings.append(
            f"身份关键词命中减少: baseline {b_required}/2, relay {r_required}/2"
        )
        sev = Severity.MAJOR

    competitors = rd.get("competitor_hits") or []
    if competitors:
        findings.append(f"出现竞品关键词: {competitors}")
        sev = Severity.CRITICAL

    # Specific backend brands detected on the relay but not on baseline —
    # the strongest possible identity signal (e.g. "Amazon Q" / "AWS Bedrock"
    # / "ChatGPT" — pinpoints actual upstream).
    b_brands = set(bd.get("detected_non_anthropic_brands") or [])
    r_brands = set(rd.get("detected_non_anthropic_brands") or [])
    new_brands = sorted(r_brands - b_brands)
    if new_brands:
        findings.append(
            f"⚠ 检测到非 Anthropic 后端品牌: {', '.join(new_brands)}"
        )
        sev = Severity.CRITICAL

    return sev, findings


def _cmp_behavioral(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    b_hits = bd.get("hits") or 0
    r_hits = rd.get("hits") or 0
    if b_hits > r_hits:
        failed = [
            s.get("id")
            for s in rd.get("signatures") or []
            if isinstance(s, dict) and not s.get("hit")
        ]
        findings.append(
            f"行为指纹缺失 {len(failed)} 项 ({b_hits}/{bd.get('total')} → "
            f"{r_hits}/{rd.get('total')}): {', '.join(failed)}"
        )
        return Severity.MINOR, findings
    return Severity.OK, findings


def _cmp_consistency(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    sev = Severity.OK

    if rd.get("model_match") is False:
        findings.append(
            f"response.model 不匹配请求: req={rd.get('request_model')!r}, "
            f"resp={rd.get('response_model')!r}"
        )
        sev = Severity.CRITICAL

    b_cv = bd.get("stability_cv")
    r_cv = rd.get("stability_cv")
    b_seq = bd.get("output_tokens_seq")
    r_seq = rd.get("output_tokens_seq")

    if isinstance(b_cv, (int, float)) and isinstance(r_cv, (int, float)):
        # Always surface the seq + cv pair when the relay's stability falls
        # into "suspicious" (0.10-0.30) or "unstable" (>0.30) range, OR when
        # it's noticeably worse than baseline. This way score drops always
        # come with a concrete number to look at.
        cv_label = (
            "高度不稳定" if r_cv > 0.30
            else "可疑波动" if r_cv > 0.10
            else "略有波动" if r_cv > b_cv * 2 + 0.03
            else None
        )
        if cv_label:
            findings.append(
                f"输出稳定性 {cv_label}: relay CV={r_cv:.3f} vs baseline CV={b_cv:.3f}"
                + (f"; output_tokens seq baseline={b_seq} relay={r_seq}"
                   if b_seq is not None or r_seq is not None else "")
            )
            sev = _max_severity(
                sev, Severity.MAJOR if r_cv > 0.30 else Severity.MINOR
            )
    return sev, findings


def _cmp_knowledge(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    b_passes = bd.get("passes") or 0
    r_passes = rd.get("passes") or 0
    if b_passes > r_passes:
        failed_with_answers: list[str] = []
        for q in rd.get("per_question") or []:
            if not isinstance(q, dict) or q.get("passed"):
                continue
            qid = q.get("id", "?")
            ans = (q.get("answer") or "").strip().replace("\n", " ")
            if len(ans) > 60:
                ans = ans[:60] + "…"
            failed_with_answers.append(
                f"{qid}={ans!r}" if ans else f"{qid}"
            )
        findings.append(
            f"知识题失败 {len(failed_with_answers)} 道 "
            f"({b_passes}/{bd.get('total')} → {r_passes}/{rd.get('total')}): "
            + "; ".join(failed_with_answers)
        )
        return Severity.MINOR, findings
    return Severity.OK, findings


def _cmp_pdf(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []
    b_eval = bd.get("evaluation")
    r_eval = rd.get("evaluation")
    if b_eval == "magic_found" and r_eval != "magic_found":
        findings.append(
            f"PDF 识别失败: relay 评估={r_eval!r} (baseline=magic_found)"
        )
        return Severity.CRITICAL, findings
    return Severity.OK, findings


def _cmp_structured_output(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    findings: list[str] = []

    b_blocks = bd.get("content_block_types") or []
    r_blocks = rd.get("content_block_types") or []
    if "tool_use" in b_blocks and "tool_use" not in r_blocks:
        findings.append(
            f"tool_use 块缺失 — relay 仅返回 {r_blocks},"
            f" stop_reason={rd.get('stop_reason')!r}"
        )
        # Surface the model's actual text response so we can see *why* it
        # didn't call the tool (e.g. relay stripped the tools param and the
        # model answered the prompt as plain conversation).
        text = rd.get("text_response")
        if isinstance(text, str) and text.strip():
            excerpt = text.strip().replace("\n", " ")
            if len(excerpt) > 200:
                excerpt = excerpt[:200] + "…"
            findings.append(f"  模型实际回复: {excerpt!r}")
        return Severity.CRITICAL, findings

    # tool_use block present — compare individual sub_checks for fakery
    # (wrong id prefix, wrong tool name, wrong input schema, wrong stop_reason)
    b_sub = bd.get("sub_checks") or {}
    r_sub = rd.get("sub_checks") or {}
    failed_details = []
    for key, b_check in b_sub.items():
        if not isinstance(b_check, dict):
            continue
        r_check = r_sub.get(key, {}) if isinstance(r_sub.get(key), dict) else {}
        if b_check.get("pass") and not r_check.get("pass"):
            r_val = r_check.get("value")
            failed_details.append(f"{key}={r_val!r}")
    if failed_details:
        findings.append(
            f"tool_use 子检查失败: {', '.join(failed_details)}"
        )
        # Wrong id prefix is a strong protocol-level forgery signal.
        sev = (
            Severity.MAJOR
            if any(d.startswith("id_prefix=") for d in failed_details)
            else Severity.MINOR
        )
        return sev, findings

    return Severity.OK, findings


def _cmp_integrity(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    b_subs = bd.get("sub_checks") or {}
    r_subs = rd.get("sub_checks") or {}
    failed = []
    for key, b_sub in b_subs.items():
        if not isinstance(b_sub, dict):
            continue
        r_sub = r_subs.get(key, {}) if isinstance(r_subs.get(key), dict) else {}
        if b_sub.get("pass") and not r_sub.get("pass"):
            failed.append(f"{key}{_integrity_subcheck_detail(key, r_sub)}")
    if failed:
        return Severity.MINOR, [f"integrity 子检查失败: {'; '.join(failed)}"]
    return Severity.OK, []


def _integrity_subcheck_detail(key: str, sub: dict) -> str:
    """Render a per-sub-check value snippet, e.g. (ns=58, stream=30, diff=28)."""
    if not isinstance(sub, dict):
        return ""
    if key == "input_tokens":
        ns = sub.get("ns")
        stream = sub.get("stream")
        diff = sub.get("diff")
        tol = sub.get("tolerance")
        return f" (ns={ns}, stream={stream}, diff={diff}, tolerance={tol})"
    if key == "char_per_token":
        return f" (value={sub.get('value')})"
    if key == "stream_output_tokens":
        return f" (stream={sub.get('stream')}, ns={sub.get('ns')})"
    if key == "similarity":
        return f" (ratio={sub.get('ratio')})"
    if key == "stop_reason":
        return f" (stop_reason={sub.get('value')!r})"
    val = sub.get("value")
    return f" (value={val!r})" if val is not None else ""


def _cmp_protocol(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    b_issues = set(bd.get("issues") or [])
    r_issues = set(rd.get("issues") or [])
    new = sorted(r_issues - b_issues)
    if new:
        sample = ", ".join(new[:3])
        more = f" (+{len(new) - 3})" if len(new) > 3 else ""
        return Severity.MINOR, [f"协议偏差 {len(new)} 项: {sample}{more}"]
    return Severity.OK, []


def _cmp_message_id(b: dict, r: dict) -> tuple[Severity, list[str]]:
    bd, rd = _details(b), _details(r)
    b_v = set(bd.get("violations") or [])
    r_v = set(rd.get("violations") or [])
    new = sorted(r_v - b_v)
    if not new:
        return Severity.OK, []
    findings = [f"ID 前缀违规: {', '.join(new)}"]
    samples = rd.get("samples") or {}
    for v in new[:2]:
        if samples.get(v):
            findings.append(f"  样本 ({v}): {samples[v]}")
    # id_prefix_invalid 是核心(msg_ 前缀错),其余协议小问题。
    sev = (
        Severity.MAJOR
        if "id_prefix_invalid" in new
        else Severity.MINOR
    )
    return sev, findings


_PER_DETECTOR = {
    "thinking_signature": _cmp_thinking_signature,
    "identity": _cmp_identity,
    "behavioral_signature": _cmp_behavioral,
    "consistency": _cmp_consistency,
    "knowledge": _cmp_knowledge,
    "pdf": _cmp_pdf,
    "structured_output": _cmp_structured_output,
    "integrity": _cmp_integrity,
    "protocol": _cmp_protocol,
    "message_id": _cmp_message_id,
}


def _compare_one(
    name: str,
    b: dict | None,
    r: dict | None,
) -> DetectorComparison:
    """Build a DetectorComparison for one detector.

    Either side may be None (e.g. detector skipped on one run). When the relay
    skipped a detector that the baseline ran, we treat that as a strong signal
    (the relay couldn't even attempt the check)."""
    b_score = float(b.get("score", 0.0)) if b else 0.0
    r_score = float(r.get("score", 0.0)) if r else 0.0
    diff = r_score - b_score
    display_name = (
        (r or {}).get("display_name") or (b or {}).get("display_name") or name
    )

    findings: list[str] = []
    severity = Severity.OK

    # If only one side exists, that's already a signal.
    if b is None and r is not None:
        findings.append("baseline 缺少该检测项")
        severity = Severity.MINOR
    elif r is None and b is not None:
        findings.append("relay 报告缺少该检测项 (检测可能 skipped)")
        severity = Severity.MAJOR
    elif b is not None and r is not None:
        if r.get("status") == "skip" and b.get("status") != "skip":
            findings.append(
                f"relay 跳过该检测项 (skip_reason="
                f"{_details(r).get('skip_reason')!r})"
            )
            severity = Severity.MAJOR
        elif r.get("status") == "error":
            findings.append(
                f"relay 检测出错: {(r.get('error') or '')[:120]}"
            )
            severity = Severity.MAJOR
        else:
            specialized = _PER_DETECTOR.get(name)
            if specialized is not None:
                sev2, f2 = specialized(b, r)
                severity = _max_severity(severity, sev2)
                findings.extend(f2)

    # generic fallback: large unexplained score gap → at least minor
    if not findings and abs(diff) >= 10:
        if diff < -25:
            severity = _max_severity(severity, Severity.MAJOR)
        elif diff < 0:
            severity = _max_severity(severity, Severity.MINOR)
        findings.append(f"分数差距 {diff:+.0f} (无具体差异定位)")

    return DetectorComparison(
        name=name,
        display_name=display_name,
        baseline_score=b_score,
        relay_score=r_score,
        score_diff=diff,
        severity=severity,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Top-level compare()
# ---------------------------------------------------------------------------


def compare(
    baseline: dict[str, Any],
    relay: dict[str, Any],
    baseline_path: str = "",
    relay_path: str = "",
) -> ComparisonReport:
    b_idx = _index_results(baseline)
    r_idx = _index_results(relay)

    all_names = list(b_idx.keys())
    for n in r_idx:
        if n not in all_names:
            all_names.append(n)

    detector_comparisons = [
        _compare_one(n, b_idx.get(n), r_idx.get(n)) for n in all_names
    ]

    overall = Severity.OK
    for d in detector_comparisons:
        overall = _max_severity(overall, d.severity)

    b_total = float(baseline.get("total_score") or 0.0)
    r_total = float(relay.get("total_score") or 0.0)

    summary = _build_summary(
        b_total, r_total, overall, detector_comparisons,
        baseline.get("target_model", ""),
        relay.get("target_model", ""),
    )

    return ComparisonReport(
        baseline_path=baseline_path,
        relay_path=relay_path,
        baseline_meta={
            "model": baseline.get("target_model"),
            "mode": baseline.get("mode"),
            "total_score": b_total,
            "verdict": baseline.get("verdict"),
            "timestamp": baseline.get("timestamp"),
            "base_url": baseline.get("base_url"),
            "self_reported_identity": baseline.get("self_reported_identity"),
            "detected_non_anthropic_brands":
                baseline.get("detected_non_anthropic_brands") or [],
        },
        relay_meta={
            "model": relay.get("target_model"),
            "mode": relay.get("mode"),
            "total_score": r_total,
            "verdict": relay.get("verdict"),
            "timestamp": relay.get("timestamp"),
            "base_url": relay.get("base_url"),
            "self_reported_identity": relay.get("self_reported_identity"),
            "detected_non_anthropic_brands":
                relay.get("detected_non_anthropic_brands") or [],
        },
        detectors=detector_comparisons,
        overall_severity=overall,
        summary=summary,
    )


def _build_summary(
    b_total: float,
    r_total: float,
    overall: Severity,
    detectors: list[DetectorComparison],
    b_model: str,
    r_model: str,
) -> str:
    diff = r_total - b_total
    crit = sum(1 for d in detectors if d.severity == Severity.CRITICAL)
    major = sum(1 for d in detectors if d.severity == Severity.MAJOR)
    minor = sum(1 for d in detectors if d.severity == Severity.MINOR)

    if not models_match(b_model, r_model):
        return (
            f"模型不一致: baseline={b_model}, relay={r_model} — 无法可靠对比"
        )
    if overall == Severity.OK:
        return f"中转站行为与官方基线一致 (总分 {r_total:.1f} vs {b_total:.1f})"
    parts = [f"总分 {r_total:.1f} vs baseline {b_total:.1f} ({diff:+.1f})"]
    sev_bits = []
    if crit:
        sev_bits.append(f"{crit} 项严重 (critical)")
    if major:
        sev_bits.append(f"{major} 项重大 (major)")
    if minor:
        sev_bits.append(f"{minor} 项轻微 (minor)")
    if sev_bits:
        parts.append("; ".join(sev_bits))

    if overall == Severity.CRITICAL:
        parts.append("中转站极有可能不是声称的 Claude 模型")
    elif overall == Severity.MAJOR:
        parts.append("中转站存在能力剥离或协议显著偏差")
    return " — ".join(parts)
