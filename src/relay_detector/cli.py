"""CLI entrypoint."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .client import AnthropicAPIError, AnthropicClient
from .detectors import build_all
from .models import (
    DetectionReport,
    DetectionTier,
    ExecutionConfig,
    Mode,
    PerformanceMetrics,
    Protocol,
    mask_api_key,
)
from .report import Report
from .runner import Runner
from .scorer import compute_total, effective_verdict, fatal_run_error, summary_text

app = typer.Typer(
    name="relay-detector",
    help="Detect quality and authenticity of Claude API relay stations.",
    no_args_is_help=True,
)
openai_app = typer.Typer(
    name="openai",
    help="OpenAI-compatible API protocol template tools.",
    no_args_is_help=True,
)
app.add_typer(openai_app, name="openai")
console = Console()


@app.command()
def version() -> None:
    """Print the version and exit."""
    console.print(f"relay-detector {__version__}")


def _load_dotenv(path: Path) -> None:
    """Lightweight .env loader (avoid python-dotenv dep for M1).

    Project-local .env values override shell environment — matches standard
    dotenv tooling semantics where the file is the source of truth for the
    project. Shell exports of irrelevant defaults won't pollute runs.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if v:  # don't blank out an existing value with an empty .env line
            os.environ[k] = v


_load_dotenv(Path.cwd() / ".env")


@app.command()
def ping(
    base_url: str = typer.Option(
        None,
        "--base-url",
        envvar="ANTHROPIC_BASE_URL",
        help="Relay station base URL, e.g. https://api.anthropic.com",
    ),
    api_key: str = typer.Option(
        None,
        "--api-key",
        envvar="ANTHROPIC_API_KEY",
        help="API key (sk-...).",
    ),
    model: str = typer.Option(
        "claude-haiku-4-5",
        "--model",
        envvar="ANTHROPIC_MODEL",
        help="Model ID to test.",
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="Request timeout in seconds."),
) -> None:
    """Send one minimal /v1/messages request to verify connectivity & response shape.

    M1 verification command. Prints the raw response fields so you can eyeball
    what the relay station actually returns.
    """
    if not base_url:
        console.print("[red]error:[/red] --base-url or ANTHROPIC_BASE_URL is required")
        raise typer.Exit(2)
    if not api_key:
        console.print("[red]error:[/red] --api-key or ANTHROPIC_API_KEY is required")
        raise typer.Exit(2)

    asyncio.run(_run_ping(base_url, api_key, model, timeout))


async def _run_ping(
    base_url: str, api_key: str, model: str, timeout: float
) -> None:
    masked = mask_api_key(api_key)
    console.print(
        f"[bold]Pinging[/bold] base_url=[cyan]{base_url}[/cyan] "
        f"model=[cyan]{model}[/cyan] key=[dim]{masked}[/dim]"
    )

    async with AnthropicClient(base_url, api_key, timeout=timeout) as client:
        try:
            req, resp, headers, latency_ms = await client.messages_create(
                model=model,
                max_tokens=64,
                messages=[
                    {
                        "role": "user",
                        "content": "Reply with exactly: pong",
                    }
                ],
            )
        except AnthropicAPIError as e:
            console.print(f"[red]HTTP {e.status}[/red]: {e.body[:500]}")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]network error[/red]: {type(e).__name__}: {e}")
            raise typer.Exit(1)

    _render_ping_result(resp, headers, latency_ms)


def _render_ping_result(resp: dict, headers, latency_ms: int) -> None:
    table = Table(title="Response", show_header=False, expand=False)
    table.add_column("field", style="bold cyan", no_wrap=True)
    table.add_column("value")
    for key in ("id", "type", "role", "model", "stop_reason", "stop_sequence"):
        table.add_row(key, _fmt(resp.get(key)))
    usage = resp.get("usage", {})
    if isinstance(usage, dict):
        table.add_row("usage.input_tokens", _fmt(usage.get("input_tokens")))
        table.add_row("usage.output_tokens", _fmt(usage.get("output_tokens")))
        for opt in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if usage.get(opt) is not None:
                table.add_row(f"usage.{opt}", _fmt(usage[opt]))
    content_blocks = resp.get("content") or []
    if isinstance(content_blocks, list):
        types = [b.get("type") for b in content_blocks if isinstance(b, dict)]
        table.add_row("content.types", ", ".join(types) or "(none)")
        for i, b in enumerate(content_blocks):
            if isinstance(b, dict) and b.get("type") == "text":
                text = (b.get("text") or "").strip()
                table.add_row(f"content[{i}].text", text[:120])
    table.add_row("latency_ms", str(latency_ms))
    table.add_row("anthropic-request-id", _fmt(headers.get("anthropic-request-id")))
    console.print(table)


def _fmt(v) -> str:
    if v is None:
        return "[dim](null)[/dim]"
    return str(v)


# ---------------------------------------------------------------------------
# `detect` subcommand (M2)
# ---------------------------------------------------------------------------


@app.command()
def detect(
    base_url: str = typer.Option(
        None, "--base-url", envvar="ANTHROPIC_BASE_URL",
        help="Relay station base URL.",
    ),
    api_key: str = typer.Option(
        None, "--api-key", envvar="ANTHROPIC_API_KEY",
        help="API key (sk-...).",
    ),
    model: str = typer.Option(
        "claude-haiku-4-5", "--model", envvar="ANTHROPIC_MODEL",
        help="Model ID to test.",
    ),
    mode: Mode = typer.Option(
        Mode.STANDARD,
        "--mode",
        case_sensitive=False,
        help=(
            "quick / standard / full. Note: M2 implements only protocol/"
            "message_id/integrity, so quick may be near-empty until M3."
        ),
    ),
    max_concurrent: int = typer.Option(
        3, "--max-concurrent", help="Concurrent in-flight requests cap."
    ),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Per-request timeout (seconds).",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write JSON report to this path.",
    ),
    include_long_context: bool = typer.Option(
        False,
        "--long-context/--no-long-context",
        help=(
            "Opt in to needle-in-haystack long-context probing (32k → 100k → "
            "200k tokens). Adds ~$0.05–$0.50 per run depending on relay "
            "behaviour. Off by default."
        ),
    ),
) -> None:
    """Run a multi-detector quality check against a relay station."""
    if not base_url:
        console.print("[red]error:[/red] --base-url required")
        raise typer.Exit(2)
    if not api_key:
        console.print("[red]error:[/red] --api-key required")
        raise typer.Exit(2)

    config = ExecutionConfig.for_mode(mode, max_concurrent=max_concurrent)
    if timeout is not None:
        config.request_timeout_s = timeout
    config.include_long_context = include_long_context

    asyncio.run(_run_detect(base_url, api_key, model, config, output))


async def _run_detect(
    base_url: str,
    api_key: str,
    model: str,
    config: ExecutionConfig,
    output_path: Optional[Path],
) -> None:
    masked = mask_api_key(api_key)
    console.print(
        f"[bold]Detecting[/bold] base_url=[cyan]{base_url}[/cyan] "
        f"model=[cyan]{model}[/cyan] mode=[cyan]{config.mode.value}[/cyan] "
        f"key=[dim]{masked}[/dim]"
    )

    detectors = build_all()

    async with AnthropicClient(
        base_url, api_key, timeout=config.request_timeout_s
    ) as client:
        runner = Runner(client, detectors, config)
        outcome = await runner.run(model)

    run_error = fatal_run_error(outcome.results)
    total = 0.0 if run_error else compute_total(outcome.results)
    verdict = effective_verdict(total, outcome.results)
    summary = run_error or summary_text(total, verdict)

    # Pull the model's self-reported identity from the IdentityDetector
    # so callers can read it from the JSON top-level without digging into
    # results[*].details.
    identity_result = next(
        (r for r in outcome.results if r.name == "identity"), None
    )
    self_id: str | None = None
    detected_brands: list[str] = []
    if identity_result is not None and isinstance(identity_result.details, dict):
        text = identity_result.details.get("response_text")
        if isinstance(text, str) and text.strip():
            self_id = text.strip()
        brands = identity_result.details.get("detected_non_anthropic_brands")
        if isinstance(brands, list):
            detected_brands = [b for b in brands if isinstance(b, str)]

    report = DetectionReport(
        protocol=Protocol.ANTHROPIC,
        tier=DetectionTier.CRYPTOGRAPHIC,
        tier_title="加密级验证",
        tier_message=(
            "Claude thinking signature 来自 Anthropic 服务端签名。"
            "通过该项时,它是当前检测集中最高可信度的真伪信号。"
        ),
        base_url=base_url,
        api_key_masked=masked,
        target_model=model,
        mode=config.mode,
        timestamp=datetime.now(timezone.utc),
        total_score=total,
        verdict=verdict,
        results=outcome.results,
        performance=outcome.performance,
        summary=summary,
        run_error=run_error,
        self_reported_identity=self_id,
        detected_non_anthropic_brands=detected_brands,
    )

    Report(console).render_terminal(report)

    if output_path:
        Report().write_json(report, output_path)
        console.print(f"[dim]Wrote JSON report to {output_path}[/dim]")


# ---------------------------------------------------------------------------
# `compare` subcommand — diff a relay report against an official baseline
# ---------------------------------------------------------------------------


_SEVERITY_COLOR = {
    "ok": "green",
    "minor": "yellow",
    "major": "red",
    "critical": "bold red",
}
_SEVERITY_LABEL = {
    "ok": "✓ 一致",
    "minor": "▲ 轻微",
    "major": "⚠ 重大",
    "critical": "✗ 严重",
}


@app.command(name="compare")
def compare_cmd(
    relay_report: Path = typer.Argument(
        ..., help="relay-detector detect 输出的 JSON 报告"
    ),
    baseline: Optional[Path] = typer.Option(
        None,
        "--baseline",
        "-b",
        help="基线 JSON 文件。不传则自动从 data/baselines/ 按 model+mode 查找。",
    ),
    baseline_dir: Path = typer.Option(
        Path("data/baselines"),
        "--baseline-dir",
        help="自动查找基线时的目录",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="JSON 比对报告输出路径"
    ),
) -> None:
    """对比一份 relay detect 报告与一个官方基线,输出字段级差异。"""
    from .comparator import (
        compare,
        find_baseline_for,
        load_report,
    )

    if not relay_report.is_file():
        console.print(f"[red]找不到 relay 报告:[/red] {relay_report}")
        raise typer.Exit(2)

    relay = load_report(relay_report)

    # Resolve baseline
    if baseline is None:
        target_model = relay.get("target_model", "")
        mode = relay.get("mode", "")
        found = find_baseline_for(target_model, mode, baseline_dir)
        if found is None:
            console.print(
                f"[red]找不到基线文件:[/red] model={target_model!r}, "
                f"mode={mode!r} 在 {baseline_dir}"
            )
            console.print(
                "[dim]显式指定 --baseline,或先用 ./bench.sh 收集官方基线[/dim]"
            )
            raise typer.Exit(2)
        baseline = found
        console.print(f"[dim]自动选择基线: {baseline}[/dim]")

    baseline_data = load_report(baseline)
    cmp = compare(
        baseline_data, relay,
        baseline_path=str(baseline), relay_path=str(relay_report),
    )

    _render_comparison(cmp)

    if output is not None:
        import json
        output.write_text(
            json.dumps(cmp.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"[dim]JSON 比对报告写入: {output}[/dim]")


def _render_comparison(cmp) -> None:
    """Render a side-by-side comparison table to the terminal."""
    color = _SEVERITY_COLOR.get(cmp.overall_severity.value, "white")
    bm, rm = cmp.baseline_meta, cmp.relay_meta

    panel_text = (
        f"[bold]model[/bold]:    {rm.get('model')} (vs baseline {bm.get('model')})\n"
        f"[bold]mode[/bold]:     {rm.get('mode')}\n"
        f"[bold]baseline[/bold]: {bm.get('base_url')}  "
        f"score={bm.get('total_score'):.1f}\n"
        f"[bold]relay[/bold]:    {rm.get('base_url')}  "
        f"score={rm.get('total_score'):.1f}"
    )

    # Side-by-side self-reported identity (the "smoking gun" in many cases —
    # e.g. relay says "I'm Amazon Q" while baseline says "I'm Claude").
    b_self = (bm.get("self_reported_identity") or "").strip()
    r_self = (rm.get("self_reported_identity") or "").strip()
    if b_self or r_self:
        panel_text += "\n\n[bold]模型自报身份对比:[/bold]"
        if b_self:
            panel_text += f"\n  [dim]baseline:[/dim] {_truncate(b_self, 180)}"
        if r_self:
            panel_text += f"\n  [dim]relay   :[/dim] {_truncate(r_self, 180)}"

    # Detected non-Anthropic backend brands → red alert
    b_brands = set(bm.get("detected_non_anthropic_brands") or [])
    r_brands = set(rm.get("detected_non_anthropic_brands") or [])
    new_brands = sorted(r_brands - b_brands)
    if new_brands:
        panel_text += (
            f"\n\n[bold red]⚠ 检测到非 Anthropic 后端品牌:[/bold red] "
            f"[red]{', '.join(new_brands)}[/red]"
        )

    panel_text += (
        f"\n\n[{color}]{_SEVERITY_LABEL.get(cmp.overall_severity.value, '?')}[/{color}]: "
        f"{cmp.summary}"
    )
    console.print(Panel(panel_text, title="基线对比报告", expand=False))

    table = Table(show_header=True, expand=False)
    table.add_column("项", style="bold")
    table.add_column("baseline", justify="right")
    table.add_column("relay", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("级别", justify="center")
    table.add_column("差异详情")

    for d in cmp.detectors:
        sev = d.severity.value
        sev_text = f"[{_SEVERITY_COLOR[sev]}]{_SEVERITY_LABEL[sev]}[/{_SEVERITY_COLOR[sev]}]"
        diff_str = f"{d.score_diff:+.0f}" if d.score_diff != 0 else "0"
        diff_color = (
            "red" if d.score_diff < -10
            else "yellow" if d.score_diff < 0
            else "dim"
        )
        if d.findings:
            note = "\n".join(d.findings)
        else:
            note = "[dim]—[/dim]"
        table.add_row(
            d.display_name,
            f"{d.baseline_score:.0f}",
            f"{d.relay_score:.0f}",
            f"[{diff_color}]{diff_str}[/{diff_color}]",
            sev_text,
            note,
        )
    console.print(table)


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------------------
# OpenAI protocol template tools
# ---------------------------------------------------------------------------


@openai_app.command("validate")
def openai_validate(
    response_json: Path = typer.Argument(
        ..., help="Raw OpenAI API response JSON to validate."
    ),
    wire_api: str = typer.Option(
        "responses",
        "--wire-api",
        help="responses / chat-completions",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Requested model ID. If provided, response.model is checked.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write template validation result as JSON.",
    ),
) -> None:
    """Validate a raw OpenAI response against the protocol template."""
    import json

    from .openai import validate_openai_payload

    if not response_json.is_file():
        console.print(f"[red]找不到响应 JSON:[/red] {response_json}")
        raise typer.Exit(2)

    normalized_wire_api = wire_api.strip().lower().replace("-", "_")
    if normalized_wire_api not in ("responses", "chat_completions"):
        console.print(
            "[red]error:[/red] --wire-api must be responses or chat-completions"
        )
        raise typer.Exit(2)

    try:
        payload = json.loads(response_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        console.print(f"[red]JSON 解析失败:[/red] {e}")
        raise typer.Exit(2)
    if not isinstance(payload, dict):
        console.print("[red]error:[/red] response JSON top-level must be an object")
        raise typer.Exit(2)

    result = validate_openai_payload(
        normalized_wire_api,  # type: ignore[arg-type]
        payload,
        request_model=model,
    )
    _render_openai_validation(result)

    if output is not None:
        output.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"[dim]JSON 校验报告写入: {output}[/dim]")


def _render_openai_validation(result) -> None:
    color = "green" if result.passed else "red"
    header = (
        f"[bold]wire_api[/bold]:  {result.wire_api}\n"
        f"[bold]template[/bold]:  {result.template_name}\n"
        f"[bold]score[/bold]:     [{color}]{result.score:.1f}[/{color}]\n"
        f"[bold]passed[/bold]:    {result.passed}\n"
        f"[bold]fingerprints[/bold]: {result.fingerprints}"
    )
    console.print(Panel(header, title="OpenAI 协议模板校验", expand=False))

    if not result.issues:
        console.print("[green]✓ 未发现协议形状问题[/green]")
        return

    table = Table(show_header=True, expand=False)
    table.add_column("级别", style="bold")
    table.add_column("代码")
    table.add_column("路径")
    table.add_column("说明")
    table.add_column("actual")
    table.add_column("expected")
    for issue in result.issues:
        sev_color = (
            "bold red"
            if issue.severity == "critical"
            else "red"
            if issue.severity == "major"
            else "yellow"
        )
        table.add_row(
            f"[{sev_color}]{issue.severity}[/{sev_color}]",
            issue.code,
            issue.path,
            issue.message,
            _truncate(repr(issue.actual), 80),
            _truncate(repr(issue.expected), 80),
        )
    console.print(table)


@openai_app.command("baseline")
def openai_baseline(
    base_url: str = typer.Option(
        "https://api.openai.com/v1",
        "--base-url",
        envvar="OPENAI_BASE_URL",
        help="OpenAI API base URL. Official baseline defaults to https://api.openai.com/v1.",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="OPENAI_API_KEY",
        help="OpenAI API key. Prefer OPENAI_API_KEY or .env instead of typing it here.",
    ),
    model: str = typer.Option(
        "gpt-5.5",
        "--model",
        envvar="OPENAI_MODEL",
        help="Model ID to probe. Use the exact model you want as the official baseline.",
    ),
    wire_api: str = typer.Option(
        "both",
        "--wire-api",
        help="responses / chat-completions / both",
    ),
    probe_set: str = typer.Option(
        "full",
        "--probe-set",
        help="smoke / full. full adds structured output and tool-call probes.",
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="Request timeout in seconds."),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write official baseline report as JSON.",
    ),
) -> None:
    """Collect an official OpenAI protocol baseline from live API responses."""

    if not api_key:
        console.print("[red]error:[/red] --api-key or OPENAI_API_KEY is required")
        console.print("[dim]建议把 key 放到环境变量或项目 .env,不要粘贴到聊天里。[/dim]")
        raise typer.Exit(2)

    normalized_wire_api = wire_api.strip().lower().replace("-", "_")
    if normalized_wire_api not in ("responses", "chat_completions", "both"):
        console.print(
            "[red]error:[/red] --wire-api must be responses, chat-completions, or both"
        )
        raise typer.Exit(2)

    normalized_probe_set = probe_set.strip().lower()
    if normalized_probe_set not in ("smoke", "full"):
        console.print("[red]error:[/red] --probe-set must be smoke or full")
        raise typer.Exit(2)

    asyncio.run(
        _run_openai_baseline(
            base_url=base_url,
            api_key=api_key,
            model=model,
            wire_api=normalized_wire_api,
            probe_set=normalized_probe_set,
            timeout=timeout,
            output_path=output,
        )
    )


async def _run_openai_baseline(
    *,
    base_url: str,
    api_key: str,
    model: str,
    wire_api: str,
    probe_set: str,
    timeout: float,
    output_path: Optional[Path],
) -> None:
    import json

    from .openai import OpenAIClient, collect_openai_official_baseline

    masked = mask_api_key(api_key)
    console.print(
        f"[bold]Collecting OpenAI baseline[/bold] base_url=[cyan]{base_url}[/cyan] "
        f"model=[cyan]{model}[/cyan] wire_api=[cyan]{wire_api}[/cyan] "
        f"probe_set=[cyan]{probe_set}[/cyan] key=[dim]{masked}[/dim]"
    )

    async with OpenAIClient(base_url, api_key, timeout=timeout) as client:
        report = await collect_openai_official_baseline(
            client,
            base_url=base_url,
            api_key_masked=masked,
            model=model,
            wire_api=wire_api,  # type: ignore[arg-type]
            probe_set=probe_set,  # type: ignore[arg-type]
        )

    _render_openai_baseline(report)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"[dim]OpenAI 官方 baseline 写入: {output_path}[/dim]")


def _render_openai_baseline(report: dict) -> None:
    summary = report.get("summary", {})
    panel_text = (
        f"[bold]base_url[/bold]:  {report.get('base_url')}\n"
        f"[bold]model[/bold]:     {report.get('target_model')}\n"
        f"[bold]wire_api[/bold]:  {report.get('wire_api')}\n"
        f"[bold]probe_set[/bold]: {report.get('probe_set')}\n"
        f"[bold]probes[/bold]:    {summary.get('ok_count')}/{summary.get('probe_count')} ok, "
        f"{summary.get('passed_count')} passed\n"
        f"[bold]avg_score[/bold]: {summary.get('average_validation_score')}"
    )
    console.print(Panel(panel_text, title="OpenAI 官方 baseline", expand=False))

    table = Table(show_header=True, expand=False)
    table.add_column("probe", style="bold")
    table.add_column("api")
    table.add_column("status")
    table.add_column("score", justify="right")
    table.add_column("latency", justify="right")
    table.add_column("features")

    for probe in report.get("probes", []):
        if not probe.get("ok"):
            error = probe.get("error", {})
            table.add_row(
                str(probe.get("name")),
                str(probe.get("wire_api")),
                "[red]error[/red]",
                "-",
                "-",
                _truncate(
                    f"{error.get('type')}: {error.get('status') or error.get('message')}",
                    120,
                ),
            )
            continue

        validation = probe.get("validation", {})
        score = validation.get("score")
        passed = validation.get("passed") is True
        status = (
            "[green]pass[/green]"
            if passed and score == 100.0
            else "[yellow]warn[/yellow]"
            if passed
            else "[red]fail[/red]"
        )
        table.add_row(
            str(probe.get("name")),
            str(probe.get("wire_api")),
            status,
            f"{score:.1f}" if isinstance(score, (int, float)) else "-",
            f"{probe.get('latency_ms')}ms",
            _truncate(_openai_probe_feature_summary(probe), 180),
        )
    console.print(table)


def _openai_probe_feature_summary(probe: dict) -> str:
    features = probe.get("features", {})
    if probe.get("wire_api") == "responses":
        return (
            f"id={features.get('id_prefix')} object={features.get('object')} "
            f"status={features.get('status')} output={features.get('output_item_types')} "
            f"content={features.get('content_item_types')} "
            f"tool_call={features.get('function_call_seen')} "
            f"json={features.get('first_output_text_is_json_object')}"
        )
    return (
        f"id={features.get('id_prefix')} object={features.get('object')} "
        f"finish={features.get('finish_reasons')} "
        f"tool_call={features.get('tool_call_seen')} "
        f"json={features.get('first_message_text_is_json_object')} "
        f"fp={features.get('system_fingerprint_prefix')}"
    )


# ---------------------------------------------------------------------------


if __name__ == "__main__":
    app()
