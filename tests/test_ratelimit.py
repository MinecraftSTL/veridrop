"""Sliding-window rate limiter tests."""

from __future__ import annotations

import time

import pytest

from web.ratelimit import check_rate, reset


@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()


def test_under_limit_always_allowed():
    for _ in range(5):
        ok, retry = check_rate("1.1.1.1", limit=10, window_s=60.0)
        assert ok is True
        assert retry == 0.0


def test_at_limit_denies_with_retry_after():
    for _ in range(5):
        check_rate("2.2.2.2", limit=5, window_s=60.0)
    ok, retry = check_rate("2.2.2.2", limit=5, window_s=60.0)
    assert ok is False
    # window is 60s and bucket was just filled, so retry should be close to 60
    assert 50.0 <= retry <= 60.0


def test_separate_ips_have_separate_buckets():
    for _ in range(3):
        check_rate("3.3.3.3", limit=3, window_s=60.0)
    # 3.3.3.3 is now full
    ok_a, _ = check_rate("3.3.3.3", limit=3, window_s=60.0)
    assert ok_a is False
    # ...but 4.4.4.4 has its own bucket
    ok_b, _ = check_rate("4.4.4.4", limit=3, window_s=60.0)
    assert ok_b is True


def test_window_slides_old_hits_expire():
    """After the window passes, old hits should no longer count."""
    # Window of 0.2s, limit 2
    check_rate("5.5.5.5", limit=2, window_s=0.2)
    check_rate("5.5.5.5", limit=2, window_s=0.2)
    ok, _ = check_rate("5.5.5.5", limit=2, window_s=0.2)
    assert ok is False
    time.sleep(0.25)
    ok, _ = check_rate("5.5.5.5", limit=2, window_s=0.2)
    assert ok is True


def test_unbounded_memory_protection():
    """Buckets shouldn't grow without bound when many denied requests come in."""
    # Limit 3, fire 100 attempts. Bucket should stay capped near limit + 5.
    for _ in range(100):
        check_rate("6.6.6.6", limit=3, window_s=60.0)
    from web.ratelimit import _HITS  # noqa: PLC0415 — internal-state assertion
    assert len(_HITS["6.6.6.6"]) <= 8
