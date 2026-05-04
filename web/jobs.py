"""Job queue for HTTP-driven detections.

Wraps the same Runner the CLI uses. Single uvicorn worker, in-process state
guarded by an asyncio lock. Done jobs are persisted to disk so a server
restart keeps the shareable result URLs alive. API keys are NEVER persisted —
the masked form goes to disk; the raw key lives only in the Job until the run
finishes, then is dropped from memory.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from relay_detector.models import (
    DetectionReport,
    DetectionTier,
    ExecutionConfig,
    Mode,
    Protocol,
    mask_api_key,
)
from relay_detector.scorer import (
    compute_total,
    effective_verdict,
    fatal_run_error,
    summary_text,
)


JobStatus = Literal["queued", "running", "done", "error"]

JOBS_DIR = Path("/opt/veridrop/web_data/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Cap concurrent detections so a flood of submissions doesn't exhaust file
# descriptors or get the upstream Anthropic API rate-limited. Each detection
# already runs ~13 outbound requests in parallel, so 6 inflight = ~78 sockets.
_MAX_INFLIGHT = 6
_SEMA = asyncio.Semaphore(_MAX_INFLIGHT)


@dataclass
class Job:
    id: str
    protocol: str = "anthropic"
    status: JobStatus = "queued"
    base_url: str = ""
    target_model: str = ""
    mode: str = "full"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


_JOBS: dict[str, Job] = {}
_LOCK = asyncio.Lock()


def _new_job_id() -> str:
    # 8-char URL-safe id; secrets gives ~48 bits of entropy, fine for an
    # unguessable shareable link without auth.
    return secrets.token_urlsafe(6)


async def submit(
    base_url: str,
    api_key: str,
    model: str,
    mode: str,
    protocol: str = "anthropic",
) -> str:
    """Queue a detection job and return the job id immediately."""
    job_id = _new_job_id()
    job = Job(
        id=job_id,
        protocol=protocol,
        base_url=base_url,
        target_model=model,
        mode=mode,
    )
    async with _LOCK:
        _JOBS[job_id] = job
    # fire-and-forget; the asyncio task lives until the runner finishes.
    asyncio.create_task(_run(job_id, base_url, api_key, model, mode, protocol))
    return job_id


async def get(job_id: str) -> Job | None:
    """Look up a job by id. Falls back to disk for jobs that survived a restart."""
    async with _LOCK:
        j = _JOBS.get(job_id)
    if j is not None:
        return j
    report = None
    for path in _report_candidates(job_id):
        if not path.exists():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            break
        except (json.JSONDecodeError, OSError):
            continue
    if report is None:
        return None
    return Job(
        id=job_id,
        status="done",
        protocol=report.get("protocol", "anthropic"),
        base_url=report.get("base_url", ""),
        target_model=report.get("target_model", ""),
        mode=report.get("mode", "full"),
        created_at=time.time(),
        finished_at=time.time(),
        report=report,
    )


def report_path(job_id: str, protocol: str) -> Path:
    protocol_dir = JOBS_DIR / protocol
    protocol_dir.mkdir(parents=True, exist_ok=True)
    return protocol_dir / f"{job_id}.json"


def image_path(job_id: str, protocol: str) -> Path:
    protocol_dir = JOBS_DIR / protocol
    protocol_dir.mkdir(parents=True, exist_ok=True)
    return protocol_dir / f"{job_id}.jpg"


def _report_candidates(job_id: str) -> list[Path]:
    return [
        JOBS_DIR / f"{job_id}.json",
        JOBS_DIR / "anthropic" / f"{job_id}.json",
        JOBS_DIR / "openai" / f"{job_id}.json",
        JOBS_DIR / "gemini" / f"{job_id}.json",
    ]


async def _run(
    job_id: str,
    base_url: str,
    api_key: str,
    model: str,
    mode: str,
    protocol: str,
) -> None:
    async with _SEMA:
        async with _LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j.status = "running"
            j.started_at = time.time()

        try:
            cfg = ExecutionConfig.for_mode(Mode(mode), max_concurrent=3)
            if protocol == "openai":
                outcome = await _run_openai(base_url, api_key, model, cfg)
                report_protocol = Protocol.OPENAI
                report_tier = DetectionTier.BEHAVIORAL
                tier_title = "行为/协议级验证"
                tier_message = (
                    "本检测无法可靠区分高配模型真品与低配模型伪装。"
                    "我们检测的是中转站接口是否符合 OpenAI Chat Completions 协议规范、"
                    "能力是否完整、usage 字段是否符合官方响应形状。"
                )
            elif protocol == "gemini":
                outcome = await _run_gemini(base_url, api_key, model, cfg)
                report_protocol = Protocol.GEMINI
                report_tier = DetectionTier.PROTOCOL
                tier_title = "协议级验证"
                tier_message = (
                    "本检测通过 OpenAI 兼容协议 (POST /chat/completions) 探测 Gemini 中转站,"
                    "验证响应字段、tool 调用、结构化输出、流式一致性和 usage 字段是否符合 OpenAI 规范。"
                    "它不提供加密级模型真伪证明。"
                )
            elif protocol == "anthropic":
                outcome = await _run_anthropic(base_url, api_key, model, cfg)
                report_protocol = Protocol.ANTHROPIC
                report_tier = DetectionTier.CRYPTOGRAPHIC
                tier_title = "加密级验证"
                tier_message = (
                    "Claude thinking signature 来自 Anthropic 服务端签名。"
                    "通过该项时,它是当前检测集中最高可信度的真伪信号。"
                )
            else:
                raise ValueError(f"unsupported protocol: {protocol}")

            run_error = fatal_run_error(outcome.results)
            score = 0.0 if run_error else compute_total(outcome.results)
            verdict = effective_verdict(score, outcome.results)
            summary = run_error or summary_text(score, verdict)

            self_id: str | None = None
            brands: list[str] = []
            for r in outcome.results:
                if r.name != "identity" or not isinstance(r.details, dict):
                    continue
                text = r.details.get("response_text")
                if isinstance(text, str) and text.strip():
                    self_id = text.strip()
                b = r.details.get("detected_non_anthropic_brands")
                if isinstance(b, list):
                    brands = [x for x in b if isinstance(x, str)]
                break

            report = DetectionReport(
                protocol=report_protocol,
                tier=report_tier,
                tier_title=tier_title,
                tier_message=tier_message,
                base_url=base_url,
                api_key_masked=mask_api_key(api_key),
                target_model=model,
                mode=Mode(mode),
                timestamp=datetime.now(timezone.utc),
                total_score=score,
                verdict=verdict,
                results=outcome.results,
                performance=outcome.performance,
                summary=summary,
                run_error=run_error,
                self_reported_identity=self_id,
                detected_non_anthropic_brands=brands,
            )
            report_dict = json.loads(report.model_dump_json())
            report_path(job_id, protocol).write_text(
                json.dumps(report_dict, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            async with _LOCK:
                if job_id in _JOBS:
                    _JOBS[job_id].status = "done"
                    _JOBS[job_id].protocol = protocol
                    _JOBS[job_id].report = report_dict
                    _JOBS[job_id].finished_at = time.time()

        except Exception as e:  # noqa: BLE001 — bubble error into job state
            async with _LOCK:
                if job_id in _JOBS:
                    _JOBS[job_id].status = "error"
                    _JOBS[job_id].error = f"{type(e).__name__}: {e}"
                    _JOBS[job_id].finished_at = time.time()


async def _run_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
):
    from relay_detector.protocols.anthropic import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model)


async def _run_openai(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
):
    from relay_detector.protocols.openai import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model)


async def _run_gemini(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
):
    from relay_detector.protocols.gemini import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model)
