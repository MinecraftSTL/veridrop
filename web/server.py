"""Veridrop FastAPI app — POST /api/detect, GET /r/{id}, GET /r/{id}.jpg."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from relay_detector.config import MODELS

from . import jobs
from .image_report import render_report_jpg


HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

logger = logging.getLogger("veridrop")
logger.setLevel(logging.INFO)

app = FastAPI(title="Veridrop", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


_VALID_MODES = {"quick", "standard", "full"}


def _model_choices() -> list[dict[str, str]]:
    """Curated dropdown — 4 most-tested model names. Free-form input still
    accepts anything; lookup_model() prefix-matches snapshot/alias forms.

    We keep it short on purpose:
    - Opus 4.7 + Opus 4.6: top-tier choices
    - Sonnet 4.6: most popular
    - Haiku 4.5 in snapshot form (-20251001) because some relays only route
      the snapshot ID, not the bare alias.
    """
    suggestions = [
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]
    return [{"id": s, "label": s} for s in suggestions]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"models": _model_choices()},
    )


@app.post("/api/detect")
async def api_detect(
    base_url: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(...),
    mode: str = Form("full"),
) -> JSONResponse:
    base_url = base_url.strip()
    api_key = api_key.strip()
    model = model.strip()
    mode = mode.strip().lower()

    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http(s)://")
    if not api_key or len(api_key) < 8:
        raise HTTPException(status_code=400, detail="api_key looks invalid")
    # Permissive model validation: relays often expose custom names like
    # "claude-opus-4-7-thinking" or vendor-prefixed variants. Detectors
    # use lookup_model() which does double-prefix matching and gracefully
    # skips thinking/PDF probes for unknown models, so we let anything
    # reasonable through and let the upstream relay decide what's valid.
    if not model or len(model) > 200:
        raise HTTPException(status_code=400, detail="model must be 1–200 chars")
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {_VALID_MODES}")

    job_id = await jobs.submit(base_url, api_key, model, mode)
    # NOTE: never echo api_key back in the response
    return JSONResponse({"job_id": job_id, "status_url": f"/api/status/{job_id}"})


@app.get("/api/status/{job_id}")
async def api_status(job_id: str) -> JSONResponse:
    j = await jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = {
        "job_id": j.id,
        "status": j.status,
        "base_url": j.base_url,
        "target_model": j.target_model,
        "mode": j.mode,
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
    }
    if j.status == "done":
        payload["result_url"] = f"/r/{j.id}"
        payload["image_url"] = f"/r/{j.id}.jpg"
        payload["json_url"] = f"/api/result/{j.id}.json"
    elif j.status == "error":
        payload["error"] = j.error
    return JSONResponse(payload)


@app.get("/api/result/{job_id}.json")
async def api_result_json(job_id: str) -> JSONResponse:
    j = await jobs.get(job_id)
    if j is None or j.report is None:
        raise HTTPException(status_code=404, detail="result not ready")
    return JSONResponse(j.report)


# NOTE: declare .jpg route BEFORE the bare /r/{job_id} HTML route. Starlette
# path params match `[^/]+` greedily so `/r/{job_id}` would otherwise swallow
# `/r/foo.jpg` (job_id="foo.jpg") and shadow the image endpoint.
@app.get("/r/{job_id}.jpg")
async def result_jpg(job_id: str) -> Response:
    j = await jobs.get(job_id)
    if j is None or j.status != "done" or j.report is None:
        raise HTTPException(status_code=404, detail="result not ready")

    # Cache the JPG next to the JSON. Detection report is immutable, so once
    # generated we always serve from disk to spare the CPU.
    cache_path = jobs.JOBS_DIR / f"{job_id}.jpg"
    if not cache_path.exists():
        png_bytes = render_report_jpg(j.report)
        cache_path.write_bytes(png_bytes)
    return Response(
        content=cache_path.read_bytes(),
        media_type="image/jpeg",
        headers={
            "Content-Disposition": f'inline; filename="veridrop-{job_id}.jpg"',
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.get("/r/{job_id}", response_class=HTMLResponse)
async def result_page(request: Request, job_id: str) -> HTMLResponse:
    j = await jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    if j.status != "done" or j.report is None:
        return templates.TemplateResponse(
            request, "running.html", {"job_id": job_id, "job": j},
        )
    return templates.TemplateResponse(
        request, "result.html",
        {"job_id": job_id, "report": j.report, "rows": _result_rows(j.report)},
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "ts": time.time()})


# ----- helpers shared with templates -----


_DETECTOR_DISPLAY = [
    ("identity", "身份一致性"),
    ("behavioral_signature", "行为签名验证"),
    ("thinking_signature", "思维签名验证"),
    ("consistency", "模型一致性"),
    ("knowledge", "知识准确度"),
    ("pdf", "PDF 文档识别"),
    ("structured_output", "结构化输出"),
    ("protocol", "协议规范性"),
    ("integrity", "响应完整性"),
    ("message_id", "消息标识规范"),
]


def _result_rows(report: dict) -> list[dict]:
    """Flatten results into the order/labels the result template expects."""
    by_name = {
        r.get("name"): r for r in report.get("results") or []
        if isinstance(r, dict)
    }
    out = []
    for name, label in _DETECTOR_DISPLAY:
        r = by_name.get(name) or {"status": "skip", "score": 0.0}
        status = str(r.get("status") or "skip")
        score = float(r.get("score") or 0.0)
        if status == "pass":
            label_short, css = "通过", "ok"
        elif status == "skip":
            label_short, css = "跳过", "muted"
        elif status == "error":
            label_short, css = "异常", "fail"
        elif score >= 70:
            label_short, css = "警告", "warn"
        else:
            label_short, css = "未通过", "fail"
        out.append({
            "name": name,
            "label": label,
            "status": status,
            "label_short": label_short,
            "css": css,
            "score": score,
        })
    return out
