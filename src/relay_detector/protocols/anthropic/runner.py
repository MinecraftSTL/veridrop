"""Anthropic runner wiring around the protocol-agnostic core runner."""

from __future__ import annotations

from ...core.detectors_base import BaseDetector, PassiveDetector
from ...core.models import ExecutionConfig
from ...core.runner import Runner as CoreRunner
from .client import AnthropicClient, ThrottledClient
from .config import MODE_DETECTORS


def _make_throttled_client(
    base_client: AnthropicClient,
    passive_detectors: list[PassiveDetector],
    max_concurrent: int,
) -> ThrottledClient:
    return ThrottledClient(
        base_client,
        passive_detectors=passive_detectors,
        max_concurrent=max_concurrent,
    )


async def _ttft_probe(client: ThrottledClient, model: str) -> None:
    async for _ in client.messages_stream(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": "ok"}],
    ):
        pass


class Runner(CoreRunner):
    """Anthropic-compatible runner preserving the v1 constructor."""

    def __init__(
        self,
        base_client: AnthropicClient,
        detectors: list[BaseDetector],
        config: ExecutionConfig,
    ):
        super().__init__(
            base_client,
            detectors,
            config,
            mode_detectors=MODE_DETECTORS,
            throttled_client_factory=_make_throttled_client,
            ttft_probe=_ttft_probe,
        )
