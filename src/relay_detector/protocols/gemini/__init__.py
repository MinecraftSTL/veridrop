"""Gemini OpenAI-compatible protocol package.

Veridrop tests Gemini relays through the OpenAI Chat Completions wire shape.
There is no native `/v1beta/models/X:generateContent` path — almost every
third-party relay exposes Gemini under `/v1/chat/completions`, and Google's
own OpenAI-compat endpoint at `/v1beta/openai/chat/completions` speaks the
same shape.
"""

from __future__ import annotations

from pathlib import Path

from ...core.detectors_base import BaseDetector
from ...core.models import DetectionTier, ExecutionConfig, Mode, Protocol
from .client import DEFAULT_GEMINI_OPENAI_BASE_URL, GeminiClient
from .config import GEMINI_MODEL_CHOICES
from .detectors import build_all
from .runner import Runner

PROTOCOL_NAME = Protocol.GEMINI
TIER = DetectionTier.PROTOCOL


def model_choices() -> list[str]:
    return list(GEMINI_MODEL_CHOICES)


def default_model() -> str:
    return GEMINI_MODEL_CHOICES[0]


# Stable Google-direct aliases first, then 3.x previews. Multi-protocol
# relays (8864k etc.) often carry only the preview series, so we still want
# a sensible fall-through. Used by pick_default_model() — see the OpenAI
# version for the matching philosophy.
_PREFERRED_DEFAULTS = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
)


def pick_default_model(available: list[str]) -> str | None:
    if not available:
        return None
    # Strip Google's `models/` prefix when matching so a relay returning the
    # qualified id still maps to a preferred alias.
    def _bare(name: str) -> str:
        return name.removeprefix("models/")

    for pref in _PREFERRED_DEFAULTS:
        for m in available:
            bare = _bare(m)
            if bare == pref or bare.startswith(pref + "-"):
                return m
    return available[0]


def default_base_url() -> str:
    """Suggested base URL placeholder for the Gemini submission form."""
    return DEFAULT_GEMINI_OPENAI_BASE_URL


def build_config(mode: Mode, max_concurrent: int = 3) -> ExecutionConfig:
    return ExecutionConfig.for_mode(mode, max_concurrent=max_concurrent)


def build_detectors(mode: Mode | None = None) -> list[BaseDetector]:
    _ = mode
    return build_all()


def make_client(base_url: str, api_key: str, timeout: float) -> GeminiClient:
    return GeminiClient(base_url, api_key, timeout=timeout)


def build_runner(
    client: GeminiClient,
    detectors: list[BaseDetector],
    config: ExecutionConfig,
) -> Runner:
    return Runner(client, detectors, config)


def baseline_path(model_id: str, mode: Mode) -> Path | None:
    _ = model_id, mode
    return None


def verdict_caption(score: float) -> str:
    if score >= 85:
        return "协议表现良好"
    if score >= 70:
        return "基本通过"
    if score >= 50:
        return "存在风险"
    return "未达标"


def tier_banner() -> tuple[str, str]:
    return (
        "协议级验证",
        (
            "本检测通过 OpenAI 兼容协议 (POST /chat/completions) 探测 Gemini 中转站,"
            "验证响应字段、tool 调用、结构化输出、流式一致性和 usage 字段是否符合 OpenAI 规范。"
            "它不提供加密级模型真伪证明。"
        ),
    )
