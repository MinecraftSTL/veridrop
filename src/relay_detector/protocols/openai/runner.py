"""OpenAI Chat Completions runner wiring."""

from __future__ import annotations

from ...core.detectors_base import BaseDetector, PassiveDetector
from ...core.models import ExecutionConfig
from ...core.runner import Runner as CoreRunner
from .client import OpenAIChatClient, ThrottledOpenAIClient
from .config import MODE_DETECTORS


def _make_throttled_client(
    base_client: OpenAIChatClient,
    passive_detectors: list[PassiveDetector],
    max_concurrent: int,
) -> ThrottledOpenAIClient:
    return ThrottledOpenAIClient(
        base_client,
        passive_detectors=passive_detectors,
        max_concurrent=max_concurrent,
    )


async def _ttft_probe(client: ThrottledOpenAIClient, model: str) -> None:
    async for _chunk, _elapsed in client.chat_completions_stream(
        model=model,
        max_completion_tokens=8,
        messages=[{"role": "user", "content": "Reply with: ok"}],
        stream_options={"include_usage": True},
    ):
        pass


class Runner(CoreRunner):
    def __init__(
        self,
        base_client: OpenAIChatClient,
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
