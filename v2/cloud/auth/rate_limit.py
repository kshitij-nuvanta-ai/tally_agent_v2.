"""In-process sliding-window rate limiter (S1 spec D24: per worker, no shared store).

Used for both the per-email login limiter (§7.1, copied ``_check_rate_limit`` semantics: checked before the DB
lookup, but recorded only on a FAILED attempt — see ``backend/api/auth.py:119,125``) and the per-device sync
limiter (§8.1 check 6, where every call counts, so it uses ``hit`` = ``check`` + ``record``). A9: ``Retry-After``
is the number of seconds until the oldest hit in the window expires.

``_hits`` also carries the legacy leak guard (``backend/api/auth.py:32,44-50``, "S4: prevent memory leak"): once
the number of distinct keys passes ``cleanup_threshold`` (1000, matching legacy), keys with no hit still inside
the window are dropped. Only an unauthenticated, attacker-controlled key (e.g. an email string on ``/login``) can
grow this dict, so without a sweep it never shrinks.
"""
from __future__ import annotations

from collections import defaultdict

from v2.cloud.clock import Clock
from v2.cloud.errors import ApiError

DEFAULT_CLEANUP_THRESHOLD = 1000


class SlidingWindow:
    """``max_hits`` per ``window_s`` seconds, per key. Not thread-safe beyond the GIL; one process only (D24)."""

    def __init__(self, max_hits: int, window_s: int, clock: Clock, cleanup_threshold: int = DEFAULT_CLEANUP_THRESHOLD):
        self.max_hits = max_hits
        self.window_s = window_s
        self.clock = clock
        self.cleanup_threshold = cleanup_threshold
        self._hits: dict[str, list[float]] = defaultdict(list)

    def _sweep(self, now: float) -> None:
        """Legacy-style cleanup (``backend/api/auth.py:44-50``): only runs once the key count passes the
        threshold, and only drops keys with nothing left inside the window — never a key someone is actively
        hitting."""
        if len(self._hits) <= self.cleanup_threshold:
            return
        cutoff = now - self.window_s
        stale = [k for k, hits in self._hits.items() if not any(t > cutoff for t in hits)]
        for k in stale:
            del self._hits[k]

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_s
        return [t for t in self._hits.get(key, []) if t > cutoff]

    def check(self, key: str) -> None:
        """Raise ``ApiError(429, "rate_limited", retry_after=...)`` if ``key`` is already at capacity — without
        recording a hit. Pairs with ``record`` for callers (e.g. login) that only count some attempts."""
        now = self.clock.now().timestamp()
        self._sweep(now)
        hits = self._prune(key, now)
        self._hits[key] = hits
        if len(hits) >= self.max_hits:
            retry_after = max(int(self.window_s - (now - hits[0])), 0)
            raise ApiError(429, "rate_limited", "Too many requests", retry_after=retry_after)

    def record(self, key: str) -> None:
        """Record one hit for ``key``, regardless of capacity (call ``check`` first if a limit should apply)."""
        now = self.clock.now().timestamp()
        hits = self._prune(key, now)
        hits.append(now)
        self._hits[key] = hits

    def hit(self, key: str) -> None:
        """``check`` then unconditionally ``record`` — every call counts. Used by the per-device limiter, where
        §8.1 check 6 applies to every request that reaches it."""
        self.check(key)
        self.record(key)
