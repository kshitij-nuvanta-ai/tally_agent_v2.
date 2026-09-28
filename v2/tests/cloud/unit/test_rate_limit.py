from datetime import datetime, timezone

import pytest

from v2.cloud.auth.rate_limit import SlidingWindow
from v2.cloud.clock import FixedClock
from v2.cloud.errors import ApiError


def test_limit_then_retry_after_then_recovers():
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(3, 60, clock)
    for _ in range(3):
        lim.hit("k")
    with pytest.raises(ApiError) as e:
        lim.hit("k")
    assert e.value.status == 429 and e.value.extra["retry_after"] == 60
    clock.advance(seconds=61)
    lim.hit("k")


def test_keys_are_independent():
    lim = SlidingWindow(1, 60, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
    lim.hit("a")
    lim.hit("b")  # must not raise — "a"'s hit doesn't count against "b"
    with pytest.raises(ApiError):
        lim.hit("a")
    with pytest.raises(ApiError):
        lim.hit("b")


def test_check_then_record_only_on_failure_never_locks_out_successes():
    """Copied ``_check_rate_limit`` semantics (spec §7.1): the check runs on every attempt, but only a caller
    that decides the attempt failed calls ``record`` — so repeated successes never trip the limiter."""
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(5, 900, clock)
    for _ in range(10):
        lim.check("owner@example.com")  # every "successful login" only checks, never records
    lim.check("owner@example.com")  # still fine after 10 successes


def test_check_raises_once_failures_hit_the_limit():
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(5, 900, clock)
    for _ in range(5):
        lim.check("owner@example.com")
        lim.record("owner@example.com")  # simulates 5 failed attempts
    with pytest.raises(ApiError) as e:
        lim.check("owner@example.com")
    assert e.value.status == 429 and e.value.code == "rate_limited"


def test_sweep_evicts_stale_keys_once_threshold_exceeded():
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(5, 60, clock, cleanup_threshold=3)
    for k in ("a", "b", "c"):
        lim.hit(k)
    assert len(lim._hits) == 3

    clock.advance(seconds=61)  # a, b, c are now stale (outside the window)
    lim.hit("d")  # dict size before this call is 3, not > threshold(3) yet — no sweep
    assert len(lim._hits) == 4

    lim.hit("e")  # dict size before this call is 4 > threshold(3) — sweep runs, drops the stale a/b/c
    assert set(lim._hits) == {"d", "e"}


def test_sweep_never_drops_a_key_still_inside_the_window():
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(5, 60, clock, cleanup_threshold=1)
    lim.hit("a")             # dict size 0 -> 1
    lim.hit("still-active")  # before-call size 1, not > threshold(1) — no sweep yet; after: size 2
    lim.hit("c")             # before-call size 2 > threshold(1) — sweep runs, but nothing is stale yet
    assert {"a", "still-active", "c"} <= set(lim._hits)  # all hit just now — none pruned
