"""How the sync/agent routes are put on an app (v2 merge M1). ``backend/main.py`` calls these for the real app;
the sync tests call the same functions with their own settings and a fixed clock, so they run the production
wiring. Sessions come from ``backend.db.engine.get_db`` — nothing here builds an engine.
"""
from __future__ import annotations

from fastapi import FastAPI

from backend.config import Settings
from backend.sync.clock import Clock
from backend.sync.errors import install_error_handler
from backend.utils.rate_limit import SlidingWindow

def install_state(app: FastAPI, settings: Settings, clock: Clock) -> None:
    """Put ``settings``, ``clock`` and the two rate limiters on ``app.state``.

    The limiters are in-process and per app (D24), sized from ``settings``. ``login_rate_limiter`` is the one
    per-email login limiter web login and agent login share (M8), so it is installed for every DB-mode app —
    also one that does not serve the sync routes.
    """
    app.state.settings = settings
    app.state.clock = clock
    app.state.login_rate_limiter = SlidingWindow(settings.LOGIN_RATE_MAX, settings.LOGIN_RATE_WINDOW_S, clock)
    app.state.device_rate_limiter = SlidingWindow(settings.DEVICE_RATE_MAX, settings.DEVICE_RATE_WINDOW_S, clock)


def install_sync(app: FastAPI, settings: Settings, clock: Clock) -> None:
    """Serve the sync/agent API on ``app``: state, the ``ApiError`` handler and the four routers (each carries
    its full ``/api/...`` prefix and the ``SyncRoute`` class). Does not validate ``settings`` — the caller
    decides when (``backend/main.py``: in the lifespan, never at import)."""
    from backend.api import agent_auth, devices, sync, workspace_sync

    install_state(app, settings, clock)
    install_error_handler(app)
    app.include_router(agent_auth.router)
    app.include_router(devices.router)
    app.include_router(sync.router)
    app.include_router(workspace_sync.router)
