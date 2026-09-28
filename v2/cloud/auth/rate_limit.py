"""In-process sliding-window rate limiter (S1 spec D24: per worker, no shared store).

Used for both the per-email login limiter (§7.1, copied ``_check_rate_limit`` semantics — every attempt, success
or failure, counts) and the per-device sync limiter (§8.1 check 6). A9: ``Retry-After`` is the number of seconds
until the oldest hit in the window expires.
"""
from __future__ import annotations

from collections import defaultdict

from v2.cloud.clock import Clock
from v2.cloud.errors import ApiError


class SlidingWindow:
    """``max_hits`` per ``window_s`` seconds, per key. Not thread-safe beyond the GIL; one process only (D24)."""

    def __init__(self, max_hits: int, window_s: int, clock: Clock):
        self.max_hits = max_hits
        self.window_s = window_s
        self.clock = clock
        self._hits: dict[str, list[float]] = defaultdict(list)

    def hit(self, key: str) -> None:
        now = self.clock.now().timestamp()
        cutoff = now - self.window_s
        hits = [t for t in self._hits[key] if t > cutoff]
        if len(hits) >= self.max_hits:
            self._hits[key] = hits
            retry_after = max(int(self.window_s - (now - hits[0])), 0)
            raise ApiError(429, "rate_limited", "Too many requests", retry_after=retry_after)
        hits.append(now)
        self._hits[key] = hits
