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
    lim.hit("b")
