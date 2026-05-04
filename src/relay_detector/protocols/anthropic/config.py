"""Detector weights, mode-to-detector mapping, model parameter table.

Cross-references DESIGN.md §2 (weights), §6.1 (mode mapping), Appendix B (models).
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.models import Mode


# --- Weights (DESIGN.md §2) -----------------------------------------------

DETECTOR_WEIGHTS: dict[str, float] = {
    "identity": 5.0,
    "behavioral_signature": 15.0,
    "thinking_signature": 25.0,
    "consistency": 10.0,
    "knowledge": 10.0,
    "pdf": 8.0,
    "structured_output": 12.0,
    "protocol": 5.0,
    "integrity": 5.0,
    "token_usage": 10.0,
    "message_id": 5.0,
}


# --- Mode -> detector membership (DESIGN.md §6.1) -------------------------

MODE_DETECTORS: dict[Mode, set[str]] = {
    Mode.QUICK: {
        "identity",
        "thinking_signature",
        "consistency",
        "protocol",
        "message_id",
    },
    Mode.STANDARD: {
        "identity",
        "thinking_signature",
        "consistency",
        "knowledge",
        "structured_output",
        "protocol",
        "integrity",
        "token_usage",
        "message_id",
    },
    Mode.FULL: set(DETECTOR_WEIGHTS.keys()),
}


# --- Model parameter table (DESIGN.md Appendix B) -------------------------

@dataclass(frozen=True)
class ModelInfo:
    alias: str
    aliases: tuple[str, ...]  # accepted ID prefixes for double-prefix matching
    context_tokens: int
    max_output_tokens: int
    pdf_page_max: int
    supports_extended_thinking: bool
    supports_adaptive_thinking: bool
    new_tokenizer: bool = False
    deprecated: bool = False


MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-7": ModelInfo(
        alias="claude-opus-4-7",
        aliases=("claude-opus-4-7",),
        context_tokens=1_000_000,
        max_output_tokens=128_000,
        pdf_page_max=600,
        supports_extended_thinking=False,
        supports_adaptive_thinking=True,
        new_tokenizer=True,
    ),
    "claude-sonnet-4-6": ModelInfo(
        alias="claude-sonnet-4-6",
        aliases=("claude-sonnet-4-6",),
        context_tokens=1_000_000,
        max_output_tokens=64_000,
        pdf_page_max=600,
        supports_extended_thinking=True,
        supports_adaptive_thinking=True,
    ),
    "claude-haiku-4-5": ModelInfo(
        alias="claude-haiku-4-5",
        aliases=("claude-haiku-4-5",),
        context_tokens=200_000,
        max_output_tokens=64_000,
        pdf_page_max=100,
        supports_extended_thinking=True,
        supports_adaptive_thinking=False,
    ),
    "claude-opus-4-6": ModelInfo(
        alias="claude-opus-4-6",
        aliases=("claude-opus-4-6",),
        context_tokens=1_000_000,
        max_output_tokens=128_000,
        pdf_page_max=600,
        supports_extended_thinking=True,
        supports_adaptive_thinking=False,
    ),
    "claude-sonnet-4-5": ModelInfo(
        alias="claude-sonnet-4-5",
        aliases=("claude-sonnet-4-5",),
        context_tokens=200_000,
        max_output_tokens=64_000,
        pdf_page_max=100,
        supports_extended_thinking=True,
        supports_adaptive_thinking=False,
    ),
    "claude-opus-4-5": ModelInfo(
        alias="claude-opus-4-5",
        aliases=("claude-opus-4-5",),
        context_tokens=200_000,
        max_output_tokens=64_000,
        pdf_page_max=100,
        supports_extended_thinking=True,
        supports_adaptive_thinking=False,
    ),
    "claude-opus-4-1": ModelInfo(
        alias="claude-opus-4-1",
        aliases=("claude-opus-4-1",),
        context_tokens=200_000,
        max_output_tokens=32_000,
        pdf_page_max=100,
        supports_extended_thinking=True,
        supports_adaptive_thinking=False,
    ),
}


def lookup_model(model_id: str) -> ModelInfo | None:
    """Match the user-supplied model ID against known aliases (double-prefix).

    Accepts both alias (`claude-opus-4-7`) and snapshot
    (`claude-haiku-4-5-20251001`) forms.
    """
    for info in MODELS.values():
        for alias in info.aliases:
            if model_id.startswith(alias) or alias.startswith(model_id):
                return info
    return None


def models_match(request_model: str, response_model: str) -> bool:
    """Bidirectional prefix match (alias <-> snapshot tolerance)."""
    if not request_model or not response_model:
        return False
    return response_model.startswith(request_model) or request_model.startswith(
        response_model
    )
