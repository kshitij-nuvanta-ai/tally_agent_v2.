"""V2 cloud FastAPI app factory (S1 spec §6.1, A13). Port 8100 (V2Settings.port); mounts the agent-auth,
devices (S1 task 4) and sync (S1 task 5) routers — later tasks add ``web_sync`` here."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from v2.cloud.api import agent_auth, devices, sync
from v2.cloud.auth.rate_limit import SlidingWindow
from v2.cloud.clock import Clock, SystemClock
from v2.cloud.config import V2Settings
from v2.cloud.db import make_engine
from v2.cloud.errors import install_error_handler


def create_app(settings: V2Settings | None = None, clock: Clock | None = None) -> FastAPI:
    """Build a fresh app instance (A13: tests inject their own settings/clock; uvicorn uses the defaults below).

    Never calls ``settings.validate_for_serving()`` at import/construction time — only in the lifespan startup,
    and only when ``database_url`` is set, so a DB-less app (e.g. this module's own health smoke test) stays
    importable and servable.
    """
    settings = settings or V2Settings()
    clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.settings.database_url:
            app.state.settings.validate_for_serving()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock

    # The rate limiters live on app.state (D24: in-process, per worker) so each app instance — each test's own
    # ``create_app`` call — gets its own independent limiter, sized from this instance's own settings.
    app.state.login_rate_limiter = SlidingWindow(settings.login_rate_max, settings.login_rate_window_s, clock)
    app.state.device_rate_limiter = SlidingWindow(settings.device_rate_max, settings.device_rate_window_s, clock)

    if settings.database_url:
        app.state.engine = make_engine(settings.database_url)
        app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    else:
        app.state.engine = None
        app.state.sessionmaker = None

    install_error_handler(app)
    app.include_router(agent_auth.router)
    app.include_router(devices.router)
    app.include_router(sync.router)

    @app.get("/api/v2/health")
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
