"""中转站红黑榜:聚合所有公开检测报告,按域名分组排序。

每份 /r/{job_id} 报告都是公开可分享的;leaderboard 只是把它们按 base_url 的
域名聚合起来,展示每个中转站被多少人测过、平均分多少、最近一次什么 verdict。

SEO 价值:用户搜「XX 中转站怎么样」时,leaderboard 页面包含该域名 + 评分
摘要,可以直接命中长尾搜索。详细报告通过链接跳到具体的 /r/{job_id}。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPORT_DIRS = [
    Path("/opt/veridrop/web_data/jobs/anthropic"),
    Path("/opt/veridrop/web_data/jobs/openai"),
    Path("/opt/veridrop/web_data/jobs/gemini"),
    Path("/opt/veridrop/web_data/jobs"),  # legacy top-level
]

PROTOCOL_LABELS = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini"}
VERDICT_LABELS = {"passed": "通过", "marginal": "存在风险", "failed": "未达标"}

# A domain with only 1 detection is statistically meaningless for ranking.
# Still surface it in the list (good for SEO long-tail), but mark it as
# "single sample" so users know not to over-interpret.
_MIN_RANKED_SAMPLES = 2


@dataclass
class ProtocolStats:
    """Per-protocol stats for one relay domain."""
    protocol: str
    count: int = 0
    scores: list[float] = field(default_factory=list)
    last_job_id: str = ""
    last_score: float = 0.0
    last_verdict: str = ""
    last_checked: datetime | None = None
    failed_detectors: Counter = field(default_factory=Counter)

    @property
    def avg(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.scores) if self.scores else 0.0


@dataclass
class RelayStats:
    """Aggregated stats for one relay domain across all protocols + checks."""
    domain: str
    by_protocol: dict[str, ProtocolStats] = field(default_factory=dict)

    @property
    def total_count(self) -> int:
        return sum(p.count for p in self.by_protocol.values())

    @property
    def overall_score(self) -> float:
        """Weighted by per-protocol detection count — tiebreaker for ranking."""
        all_scores = []
        for p in self.by_protocol.values():
            all_scores.extend(p.scores)
        return sum(all_scores) / len(all_scores) if all_scores else 0.0

    @property
    def overall_median(self) -> float:
        all_scores = []
        for p in self.by_protocol.values():
            all_scores.extend(p.scores)
        return statistics.median(all_scores) if all_scores else 0.0

    @property
    def last_checked(self) -> datetime | None:
        ts = [p.last_checked for p in self.by_protocol.values() if p.last_checked]
        return max(ts) if ts else None

    @property
    def is_ranked(self) -> bool:
        """≥ 2 samples means the score is statistically meaningful."""
        return self.total_count >= _MIN_RANKED_SAMPLES

    @property
    def protocols_label(self) -> str:
        labels = [PROTOCOL_LABELS.get(p, p) for p in sorted(self.by_protocol.keys())]
        return " · ".join(labels)

    @property
    def verdict_class(self) -> str:
        """CSS class for color coding the score badge."""
        score = self.overall_median
        if score >= 85: return "ok"
        if score >= 70: return "good"
        if score >= 50: return "warn"
        return "fail"


def _extract_domain(base_url: str) -> str:
    """Strip protocol + path, keep host. example: https://api.x.com/v1 → api.x.com."""
    if not base_url:
        return ""
    if "://" not in base_url:
        base_url = "https://" + base_url
    try:
        host = urlparse(base_url).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def _parse_timestamp(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        # Handle both `Z` suffix and `+00:00` offset
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_report(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def aggregate() -> tuple[list[RelayStats], dict[str, int]]:
    """Scan all report JSONs, return (sorted relay stats, summary metrics).

    Sort:
      1. Ranked relays (≥ 2 detections) first, by overall_median desc
      2. Single-sample relays at the bottom, by last_checked desc (recency)
    """
    by_domain: dict[str, RelayStats] = defaultdict(lambda: RelayStats(domain=""))
    total_reports = 0

    for dir_path in REPORT_DIRS:
        if not dir_path.is_dir():
            continue
        for json_path in dir_path.glob("*.json"):
            report = _load_report(json_path)
            if not report:
                continue
            domain = _extract_domain(report.get("base_url", ""))
            if not domain:
                continue
            total_reports += 1
            protocol = str(report.get("protocol") or "anthropic")
            score = float(report.get("total_score") or 0)
            verdict = str(report.get("verdict") or "failed")
            ts = _parse_timestamp(report.get("timestamp"))
            job_id = json_path.stem

            relay = by_domain[domain]
            relay.domain = domain
            ps = relay.by_protocol.setdefault(protocol, ProtocolStats(protocol=protocol))
            ps.count += 1
            ps.scores.append(score)
            for r in report.get("results") or []:
                if isinstance(r, dict) and r.get("status") == "fail":
                    name = r.get("name")
                    if isinstance(name, str):
                        ps.failed_detectors[name] += 1
            # Track most recent — by timestamp if available, else by file mtime
            if ts and (ps.last_checked is None or ts > ps.last_checked):
                ps.last_checked = ts
                ps.last_job_id = job_id
                ps.last_score = score
                ps.last_verdict = verdict
            elif ps.last_checked is None:
                ps.last_job_id = job_id
                ps.last_score = score
                ps.last_verdict = verdict
                try:
                    mtime = datetime.fromtimestamp(json_path.stat().st_mtime, tz=timezone.utc)
                    ps.last_checked = mtime
                except OSError:
                    pass

    relays = list(by_domain.values())
    # Stable sort: ranked first (by median desc), then unranked (by recency desc)
    relays.sort(key=lambda r: (
        not r.is_ranked,                  # False (ranked) sorts before True
        -r.overall_median,                # Higher score first
        -(r.last_checked.timestamp() if r.last_checked else 0),
    ))

    summary = {
        "total_reports": total_reports,
        "total_relays": len(relays),
        "ranked_relays": sum(1 for r in relays if r.is_ranked),
    }
    return relays, summary
