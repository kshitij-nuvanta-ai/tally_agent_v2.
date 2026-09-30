"""Temporary sync app factory (S1 spec §6.1, A13), kept until the sync routers mount in ``backend/main.py``
(v2 merge T5). Mounts the agent-auth, devices (S1 task 4), sync (S1 task 5) and web_sync (S1 task 6) routers.
It reads the one ``backend.config.Settings`` class (M2)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api import agent_auth, devices, sync
from backend.api import workspace_sync as web_sync
from backend.utils.rate_limit import SlidingWindow
from backend.sync.clock import Clock, SystemClock
from backend.config import Settings
from backend.sync.db import make_engine
from backend.sync.errors import install_error_handler


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    """Build a fresh app instance (A13: tests inject their own settings/clock; uvicorn uses the defaults below).

    Never calls ``settings.validate_for_serving()`` at import/construction time — only in the lifespan startup,
    and only when ``DATABASE_URL`` is set, so a DB-less app (e.g. this module's own health smoke test) stays
    importable and servable.
    """
    settings = settings or Settings()
    clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.settings.DATABASE_URL:
            app.state.settings.validate_for_serving()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock

    # The rate limiters live on app.state (D24: in-process, per worker) so each app instance — each test's own
    # ``create_app`` call — gets its own independent limiter, sized from this instance's own settings.
    app.state.login_rate_limiter = SlidingWindow(settings.LOGIN_RATE_MAX, settings.LOGIN_RATE_WINDOW_S, clock)
    app.state.device_rate_limiter = SlidingWindow(settings.DEVICE_RATE_MAX, settings.DEVICE_RATE_WINDOW_S, clock)

    if settings.DATABASE_URL:
        app.state.engine = make_engine(settings.DATABASE_URL)
        app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    else:
        app.state.engine = None
        app.state.sessionmaker = None

    install_error_handler(app)
    app.include_router(agent_auth.router)
    app.include_router(devices.router)
    app.include_router(sync.router)
    app.include_router(web_sync.router)

    @app.get("/api/v2/health")
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
