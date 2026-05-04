"""Report rendering — basic version for M2.

M5 will replace this with a `rich.live.Live` streaming renderer (DESIGN §6.6).
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import DetectionReport


_VERDICT_COLOR = {
    "passed": "green",
    "marginal": "yellow",
    "failed": "red",
}

_STATUS_LABEL = {
    "pass": "[green]✓ 通过[/green]",
    "fail": "[red]✗ 失败[/red]",
    "skip": "[dim]— 跳过[/dim]",
    "error": "[red]! 错误[/red]",
}


class Report:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def render_terminal(self, report: DetectionReport) -> None:
        color = _VERDICT_COLOR.get(report.verdict, "white")

        header = (
            f"[bold]base_url[/bold]: {report.base_url}\n"
            f"[bold]model[/bold]:    {report.target_model}    "
            f"[bold]mode[/bold]: {report.mode.value}\n"
            f"[bold]api_key[/bold]:  {report.api_key_masked}    "
            f"[bold]time[/bold]: {report.timestamp.isoformat(timespec='seconds')}\n"
            f"\n[bold]总分:[/bold] [{color}]{report.total_score:.1f}[/{color}] "
            f"([{color}]{report.summary}[/{color}])"
        )
        if report.self_reported_identity:
            snippet = report.self_reported_identity.replace("\n", " ")
            if len(snippet) > 220:
                snippet = snippet[:220] + "…"
            header += f"\n\n[bold]模型自报[/bold]: [dim]{snippet}[/dim]"
        if report.tier_title or report.tier_message:
            header += (
                f"\n\n[bold]{report.tier_title or report.tier.value}[/bold]: "
                f"[dim]{report.tier_message}[/dim]"
            )
        self.console.print(Panel(header, title="中转站检测报告", expand=False))

        # Detector results
        table = Table(show_header=True, expand=False)
        table.add_column("项", style="bold")
        table.add_column("状态", justify="center")
        table.add_column("分数", justify="right")
        table.add_column("权重", justify="right")
        table.add_column("耗时", justify="right")
        table.add_column("备注")

        for r in report.results:
            status_str = _STATUS_LABEL.get(r.status, r.status)
            note = self._note_for(r)
            table.add_row(
                r.display_name,
                status_str,
                f"{r.score:.0f}",
                f"{r.weight:.0f}%",
                f"{r.duration_ms}ms" if r.duration_ms else "—",
                note,
            )
        self.console.print(table)

        # Performance footer
        perf = report.performance
        usage = perf.usage
        usage_bits = [
            f"input={usage.input_tokens}",
            f"output={usage.output_tokens}",
        ]
        if usage.cache_read_input_tokens is not None:
            usage_bits.append(f"cache_read={usage.cache_read_input_tokens}")
        self.console.print(
            "[dim]性能 · "
            f"requests={perf.request_count} "
            f"backoffs={perf.backoff_events} "
            f"latency={perf.total_latency_ms}ms "
            f"tokens: {' '.join(usage_bits)}"
            "[/dim]"
        )

    def _note_for(self, r) -> str:
        if r.status == "error":
            return f"[red]{(r.error or '')[:60]}[/red]"
        if r.status == "skip":
            reason = r.details.get("skip_reason") or r.details.get("reason") or ""
            return f"[dim]{reason}[/dim]"
        if r.name == "protocol":
            issues = r.details.get("issues") or []
            if not issues:
                return "[dim]协议合规[/dim]"
            preview = ", ".join(issues[:2])
            more = "" if len(issues) <= 2 else f" (+{len(issues) - 2})"
            return f"[yellow]{preview}{more}[/yellow]"
        if r.name == "message_id":
            v = r.details.get("violations") or []
            if not v:
                return "[dim]ID 规范[/dim]"
            samples = r.details.get("samples") or {}
            first = v[0]
            ex = samples.get(first)
            return f"[yellow]{first}[/yellow]" + (f" e.g. {ex}" if ex else "")
        if r.name == "integrity":
            sub = r.details.get("sub_checks") or {}
            failed = [k for k, v in sub.items() if not v.get("pass")]
            if not failed:
                return "[dim]4/4 sub-checks pass[/dim]"
            return f"[yellow]failing: {', '.join(failed)}[/yellow]"
        return ""

    def to_json(self, report: DetectionReport) -> str:
        return report.model_dump_json(indent=2)

    def write_json(self, report: DetectionReport, path: Path) -> None:
        path.write_text(self.to_json(report), encoding="utf-8")
