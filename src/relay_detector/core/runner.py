"""Detector orchestrator — see DESIGN.md §6.2.

Responsibility split:
- Filter detectors by mode + applies_to(model).
- Split into Active vs Passive.
- Wrap base client in ThrottledClient that broadcasts to passives and
  accumulates per-request usage automatically.
- asyncio.gather() the actives; finalize() the passives at the end.
- Wall-clock the entire run for total_latency_ms.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .detectors_base import ActiveDetector, BaseDetector, PassiveDetector
from .models import (
    DetectorResult,
    ExecutionConfig,
    PerformanceMetrics,
)


ThrottledClientFactory = Callable[
    [Any, list[PassiveDetector], int],
    Any,
]
TTFTProbe = Callable[[Any, str], Awaitable[None]]


@dataclass
class RunOutcome:
    results: list[DetectorResult]
    performance: PerformanceMetrics


class Runner:
    def __init__(
        self,
        base_client: Any,
        detectors: list[BaseDetector],
        config: ExecutionConfig,
        *,
        mode_detectors: Mapping[Any, set[str]],
        throttled_client_factory: ThrottledClientFactory,
        ttft_probe: TTFTProbe | None = None,
    ):
        self._base_client = base_client
        self._all_detectors = detectors
        self._config = config
        self._mode_detectors = mode_detectors
        self._throttled_client_factory = throttled_client_factory
        self._ttft_probe = ttft_probe

    def _select_detectors(self, model: str) -> list[BaseDetector]:
        mode_set = self._mode_detectors[self._config.mode]
        selected: list[BaseDetector] = []
        for d in self._all_detectors:
            if d.name not in mode_set:
                continue
            if not d.applies_to(model):
                continue
            selected.append(d)
        return selected

    async def run(self, model: str) -> RunOutcome:
        selected = self._select_detectors(model)
        active = [d for d in selected if isinstance(d, ActiveDetector)]
        passive = [d for d in selected if isinstance(d, PassiveDetector)]

        client = self._throttled_client_factory(
            self._base_client,
            passive,
            self._config.max_concurrent,
        )

        skipped: list[DetectorResult] = []
        selected_names = {d.name for d in selected}
        for d in self._all_detectors:
            if d.name in selected_names:
                continue
            reason = (
                "mode-excluded"
                if d.name not in self._mode_detectors[self._config.mode]
                else "model-excluded"
            )
            skipped.append(d.skip(reason))

        # Inject config for detectors that branch on mode (e.g. ConsistencyDetector)
        for d in selected:
            d.config = self._config

        async def run_one(d: ActiveDetector) -> DetectorResult:
            return await d._timed_run(client, model)

        run_started = time.perf_counter()
        try:
            active_results = await asyncio.wait_for(
                asyncio.gather(*(run_one(d) for d in active)),
                timeout=self._config.overall_timeout_s,
            )
        except asyncio.TimeoutError:
            active_results = [
                d._result(
                    "error",
                    0.0,
                    {"timeout_s": self._config.overall_timeout_s},
                    error="overall timeout",
                )
                for d in active
            ]
        total_latency_ms = int((time.perf_counter() - run_started) * 1000)

        # Quick mode contains no streaming detector (integrity is the only one,
        # and it's standard/full only). Without a streaming request the
        # ThrottledClient has no chance to record a content_block_delta time,
        # so TTFT would show as null. Fire one minimal streaming probe here as
        # a fallback — costs ~5 output tokens (~$0.0005 on Haiku). Skip when
        # samples already exist so standard/full pay nothing extra.
        if self._ttft_probe is not None and not getattr(client, "_ttft_samples_ms", []):
            try:
                await self._ttft_probe(client, model)
            except Exception:  # noqa: BLE001 — probe failures must not crash run
                pass

        passive_results = [d.finalize() for d in passive]

        # TTFT: best-case first-token latency observed across all streamed
        # requests. None when no detector did a stream (e.g. quick mode with
        # only non-stream probes selected).
        ttft_ms: int | None = (
            min(client._ttft_samples_ms)
            if getattr(client, "_ttft_samples_ms", None)
            else None
        )
        perf = PerformanceMetrics(
            usage=getattr(client, "total_usage", PerformanceMetrics().usage),
            request_count=getattr(client, "request_count", 0),
            backoff_events=getattr(client, "backoff_events", 0),
            total_latency_ms=total_latency_ms,
            ttft_ms=ttft_ms,
        )

        return RunOutcome(
            results=[*active_results, *passive_results, *skipped],
            performance=perf,
        )
