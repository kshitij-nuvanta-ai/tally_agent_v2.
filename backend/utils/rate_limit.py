"""In-process sliding-window rate limiter (S1 spec D24: per worker, no shared store).

Used for both the per-email login limiter (checked before the DB lookup, but recorded only on a FAILED attempt)
and the per-device sync limiter (§8.1 check 6, where every call counts, so it uses ``hit`` = ``check`` + ``record``). A9: ``Retry-After``
is the number of seconds until the oldest hit in the window expires.

The per-email login limiter is ONE store per app (v2 merge M8): web login (``POST /api/auth/login``), agent login
(``POST /api/agent/auth/login``) and the relink password re-check all count against
``app.state.login_rate_limiter``, reached through ``login_limiter(app)``. Each endpoint keeps its own response
at the limit: the agent routes call ``check`` (``ApiError`` 429 ``rate_limited`` + ``Retry-After``), web login
calls ``blocked_for`` and raises its own ``HTTPException``.

``_hits`` also carries the leak guard ("S4: prevent memory leak"): once the number of distinct keys passes
``cleanup_threshold`` (1000), keys with no hit still inside the window are dropped. Only an unauthenticated, attacker-controlled key (e.g. an email string on ``/login``) can
grow this dict, so without a sweep it never shrinks.
"""
from __future__ import annotations

from collections import defaultdict

from backend.sync.clock import Clock, SystemClock
from backend.sync.errors import ApiError

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
        """Cleanup: only runs once the key count passes the
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

    def blocked_for(self, key: str) -> int | None:
        """Seconds until ``key`` has capacity again, or ``None`` if it has capacity now — without recording a
        hit. For a caller that answers the limit in its own error shape (web login)."""
        now = self.clock.now().timestamp()
        self._sweep(now)
        hits = self._prune(key, now)
        self._hits[key] = hits
        if len(hits) >= self.max_hits:
            return max(int(self.window_s - (now - hits[0])), 0)
        return None

    def check(self, key: str) -> None:
        """Raise ``ApiError(429, "rate_limited", retry_after=...)`` if ``key`` is already at capacity — without
        recording a hit. Pairs with ``record`` for callers (e.g. login) that only count some attempts."""
        retry_after = self.blocked_for(key)
        if retry_after is not None:
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


def login_limiter(app) -> SlidingWindow:
    """The app's one per-email login limiter (M8). ``backend.sync.wiring.install_state`` puts it on
    ``app.state``; an app assembled without that wiring (a test that mounts the auth router by hand) gets one on
    first use, sized from the settings, so there is still exactly one store per app."""
    limiter = getattr(app.state, "login_rate_limiter", None)
    if limiter is None:
        from backend.config import settings

        limiter = SlidingWindow(settings.LOGIN_RATE_MAX, settings.LOGIN_RATE_WINDOW_S, SystemClock())
        app.state.login_rate_limiter = limiter
    return limiter
