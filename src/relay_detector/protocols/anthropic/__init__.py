"""Anthropic protocol implementation."""

from __future__ import annotations

from pathlib import Path

from ...core.detectors_base import BaseDetector
from ...core.models import DetectionTier, ExecutionConfig, Mode, Protocol
from .client import AnthropicClient
from .config import MODELS
from .detectors import build_all
from .runner import Runner

PROTOCOL_NAME = Protocol.ANTHROPIC
TIER = DetectionTier.CRYPTOGRAPHIC


def model_choices() -> list[str]:
    return list(MODELS.keys())


def default_model() -> str:
    return "claude-haiku-4-5"


# Cheap-and-stable first. Used by pick_default_model() to pre-fill the form
# after a probe — Haiku is universal, Sonnet is the next-best fallback.
_PREFERRED_DEFAULTS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-haiku-3-5",
    "claude-sonnet-3-7",
)


def pick_default_model(available: list[str]) -> str | None:
    if not available:
        return None
    for pref in _PREFERRED_DEFAULTS:
        for m in available:
            if m == pref or m.startswith(pref + "-"):
                return m
    return available[0]


def build_config(mode: Mode, max_concurrent: int = 3) -> ExecutionConfig:
    return ExecutionConfig.for_mode(mode, max_concurrent=max_concurrent)


def build_detectors(mode: Mode | None = None) -> list[BaseDetector]:
    # The runner performs mode filtering. The optional argument keeps the
    # protocol contract symmetric with future protocols.
    _ = mode
    return build_all()


def make_client(base_url: str, api_key: str, timeout: float) -> AnthropicClient:
    return AnthropicClient(base_url, api_key, timeout=timeout)


def build_runner(
    client: AnthropicClient,
    detectors: list[BaseDetector],
    config: ExecutionConfig,
) -> Runner:
    return Runner(client, detectors, config)


def baseline_path(model_id: str, mode: Mode) -> Path | None:
    direct = Path("data/baselines/anthropic") / f"{model_id}_{mode.value}.json"
    if direct.is_file():
        return direct
    legacy = Path("data/baselines") / f"{model_id}_{mode.value}.json"
    return legacy if legacy.is_file() else None


def verdict_caption(score: float) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "通过"
    if score >= 50:
        return "基本合格"
    return "未达标"


def tier_banner() -> tuple[str, str]:
    return (
        "加密级验证",
        "Claude thinking signature 来自 Anthropic 服务端签名。通过该项时,它是当前检测集中最高可信度的真伪信号。",
    )
