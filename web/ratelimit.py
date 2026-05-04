"""Per-IP sliding-window rate limiter.

In-memory (deque per IP) — adequate for a single-uvicorn-worker deployment.
If we ever scale to multiple workers, swap the backend for Redis.

Used to shield endpoints that take a base_url + api_key from being abused as
key-scanning oracles. Cheap reads (probe) get a permissive limit; expensive
operations (detect — burns tokens, shared upstream quota) get a stricter
one.
"""

from __future__ import annotations

import time
from collections import deque

# {ip: deque of monotonic timestamps for hits inside the longest window we care about}
_HITS: dict[str, deque[float]] = {}


def check_rate(ip: str, *, limit: int, window_s: float) -> tuple[bool, float]:
    """Return ``(allowed, retry_after_seconds)`` for one hit on this IP.

    Sliding-window: counts hits in the trailing ``window_s`` seconds. If the
    count is at or above ``limit``, the call is denied and ``retry_after``
    tells the caller how many seconds until the oldest in-window hit expires.

    A `True` return implicitly records the hit.
    """
    now = time.monotonic()
    bucket = _HITS.setdefault(ip, deque())
    cutoff = now - window_s
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        retry = bucket[0] + window_s - now
        return False, max(retry, 0.1)
    bucket.append(now)
    # Opportunistic shrink: avoid unbounded memory if many one-off IPs hit
    # us. Cap each bucket at ``limit + 5`` recent entries.
    if len(bucket) > limit + 5:
        for _ in range(len(bucket) - (limit + 5)):
            bucket.popleft()
    return True, 0.0


def reset() -> None:
    """Test helper — wipe all buckets."""
    _HITS.clear()
