"""One app (v2 merge M1): ``backend.main`` serves the pre-existing routes and, in DB mode with a device-token
secret, the sync/agent routes — through ``backend.sync.wiring.install_sync``, the function the sync tests use too.
No database is needed here: building the app and running its lifespan opens no connection.

The two route tables below are written out on purpose. ``PRE_EXISTING_*`` is what ``backend.main:app`` served
before the merge step (taken from the app at ``7d5d64e``); ``SYNC_ROUTES`` is the agent's wire contract.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Match

from backend.config import Settings
from backend.db import engine as app_engine
from backend.sync.clock import FixedClock, SystemClock
from backend.sync.errors import SyncRoute
from backend.sync.wiring import install_state, install_sync
from tests.sync.conftest import configure_main_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
# Never connected to: the engine is lazy, and these tests send no request that reaches the database.
UNUSED_DB = "postgresql+asyncpg://nobody@localhost/tallyagent_test_never_connected"

_DOCS = {("GET", "/docs"), ("HEAD", "/docs"), ("GET", "/docs/oauth2-redirect"), ("HEAD", "/docs/oauth2-redirect"),
         ("GET", "/openapi.json"), ("HEAD", "/openapi.json"), ("GET", "/redoc"), ("HEAD", "/redoc")}
PRE_EXISTING_LEGACY = _DOCS | {
    ("GET", "/api/companies"), ("GET", "/api/health"), ("GET", "/api/reports/{name}"), ("GET", "/api/tally-mode"),
    ("POST", "/api/chat"), ("POST", "/api/chat/upload"), ("POST", "/api/chat/voucher-action"),
    ("POST", "/api/tally-mode"), ("POST", "/api/tally/test-connection"),
}
PRE_EXISTING_DB = PRE_EXISTING_LEGACY | {
    ("POST", "/api/auth/register"), ("POST", "/api/auth/login"), ("POST", "/api/auth/refresh"),
    ("GET", "/api/auth/me"), ("POST", "/api/auth/logout"),
    ("GET", "/api/workspaces"), ("POST", "/api/workspaces"),
    ("PATCH", "/api/workspaces/{workspace_id}"), ("DELETE", "/api/workspaces/{workspace_id}"),
    ("GET", "/api/workspaces/{workspace_id}/conversations"),
    ("POST", "/api/workspaces/{workspace_id}/conversations"),
    ("GET", "/api/workspaces/{workspace_id}/conversations/{conversation_id}"),
    ("PATCH", "/api/workspaces/{workspace_id}/conversations/{conversation_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/conversations/{conversation_id}"),
    ("GET", "/api/usage"),
}
SYNC_ROUTES = {
    ("POST", "/api/agent/auth/login"), ("POST", "/api/agent/auth/refresh"), ("POST", "/api/agent/auth/logout"),
    ("GET", "/api/agent/workspaces"),
    ("POST", "/api/sync/company"), ("POST", "/api/sync/{ws}/heartbeat"), ("GET", "/api/sync/{ws}/state"),
    ("POST", "/api/sync/{ws}/relink"), ("POST", "/api/sync/{ws}/runs"), ("PATCH", "/api/sync/{ws}/runs/{run_id}"),
    ("PATCH", "/api/sync/{ws}/coverage"), ("POST", "/api/sync/{ws}/batches"), ("POST", "/api/sync/{ws}/reconcile"),
    ("POST", "/api/sync/{ws}/snapshots"), ("POST", "/api/sync/{ws}/parity"),
    ("GET", "/api/devices"), ("DELETE", "/api/devices/{device_id}"),
    ("GET", "/api/workspaces/{ws}/sync-status"), ("POST", "/api/workspaces/{ws}/sync/commands"),
}
SYNC_MODULES = {"backend.api.agent_auth", "backend.api.devices", "backend.api.sync", "backend.api.workspace_sync"}
WARNING = "Agent sync is disabled"


def route_list(app: FastAPI) -> list[tuple[str, str]]:
    return [(m, r.path) for r in app.routes for m in sorted(getattr(r, "methods", None) or [])]


def first_match(app: FastAPI, method: str, path: str):
    """The route the app's router picks for a concrete request (the first full match, as Starlette does)."""
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
    for route in app.routes:
        if route.matches(scope)[0] is Match.FULL:
            return route
    return None


def _main_app(monkeypatch, **cfg) -> FastAPI:
    from backend.main import build_app

    configure_main_settings(monkeypatch, **cfg)
    return build_app()


def _sync_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if WARNING in r.getMessage()]


# --- DB mode, device secret set: everything is served ---------------------------------------------------------------


def test_main_app_in_db_mode_serves_every_sync_route_and_every_pre_existing_route(monkeypatch):
    app = _main_app(monkeypatch, database_url=UNUSED_DB)
    served = route_list(app)
    assert len(SYNC_ROUTES) == 19 and not (SYNC_ROUTES & PRE_EXISTING_DB)
    assert set(served) == PRE_EXISTING_DB | SYNC_ROUTES
    assert len(served) == len(set(served)), "a method + path is registered twice"
    assert app_engine.engine is None and app_engine.async_session_factory is None   # building made no engine


def test_no_pre_existing_route_shadows_a_sync_route_or_the_reverse(monkeypatch):
    """Equal strings are ruled out above; this rules out one PATTERN swallowing the other's requests
    (``/api/workspaces/{workspace_id}/...`` and ``/api/workspaces/{ws}/sync-status`` share a prefix)."""
    app = _main_app(monkeypatch, database_url=UNUSED_DB)
    fill = {"{ws}": "11111111-1111-4111-8111-111111111111", "{run_id}": "22222222-2222-4222-8222-222222222222",
            "{device_id}": "33333333-3333-4333-8333-333333333333",
            "{workspace_id}": "44444444-4444-4444-8444-444444444444",
            "{conversation_id}": "55555555-5555-4555-8555-555555555555", "{name}": "trial_balance"}

    def concrete(path: str) -> str:
        for k, v in fill.items():
            path = path.replace(k, v)
        return path

    for method, path in sorted(SYNC_ROUTES):
        route = first_match(app, method, concrete(path))
        assert route is not None and route.path == path, (method, path, route)
        assert route.endpoint.__module__ in SYNC_MODULES and isinstance(route, SyncRoute), (method, path)
    for method, path in sorted(PRE_EXISTING_DB - _DOCS):
        route = first_match(app, method, concrete(path))
        assert route is not None and route.path == path, (method, path, route)
        assert route.endpoint.__module__ not in SYNC_MODULES and not isinstance(route, SyncRoute), (method, path)


def test_main_app_puts_settings_clock_and_limiters_on_state(monkeypatch):
    from backend.config import settings as live

    app = _main_app(monkeypatch, database_url=UNUSED_DB)
    assert app.state.settings is live and isinstance(app.state.clock, SystemClock)
    assert (app.state.login_rate_limiter.max_hits, app.state.login_rate_limiter.window_s) == (5, 900)
    assert (app.state.device_rate_limiter.max_hits, app.state.device_rate_limiter.window_s) == (600, 60)


async def test_lifespan_creates_the_engine_and_closes_it(monkeypatch, caplog):
    app = _main_app(monkeypatch, database_url=UNUSED_DB)
    caplog.set_level(logging.WARNING)
    assert app_engine.engine is None
    async with app.router.lifespan_context(app):
        assert app_engine.engine is not None and app_engine.async_session_factory is not None
        assert app_engine.engine.sync_engine.hide_parameters is True and app_engine.engine.pool._pre_ping is True
    assert app_engine.engine is None and app_engine.async_session_factory is None
    assert _sync_warnings(caplog) == []


async def test_main_app_with_sync_builds_and_answers_health_without_an_engine(monkeypatch):
    """Was ``test_health_route_without_db`` on the separate sync app's ``GET /api/v2/health`` (both are gone, M1):
    the app with the sync routes mounted is servable before any engine exists, and its health route is the one
    app's own ``GET /api/health``."""
    from backend.agents.context import SessionStore
    from backend.api.dependencies import get_current_user
    from backend.tally_bridge.client import TallyClient

    app = _main_app(monkeypatch, database_url=UNUSED_DB)
    tally = TallyClient(host="localhost", port=9000)
    tally.mock_mode = True
    app.state.tally_client, app.state.session_store = tally, SessionStore(ttl_minutes=60)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ok = await c.get("/api/health")
        gone = await c.get("/api/v2/health")
    await tally.close()
    assert ok.status_code == 200 and ok.json()["tally_connected"] is True
    assert gone.status_code == 404 and gone.json() == {"detail": "Not Found"}
    assert app_engine.engine is None


# --- DB mode, no device secret: the app starts as before, without the sync routes ---------------------------------


async def test_without_a_device_secret_sync_routes_are_absent_and_one_warning_is_logged(monkeypatch, caplog):
    app = _main_app(monkeypatch, database_url=UNUSED_DB, device_secret="")
    served = route_list(app)
    assert set(served) == PRE_EXISTING_DB and len(served) == len(set(served))
    for method, path in SYNC_ROUTES:
        route = first_match(app, method, path.replace("{ws}", "11111111-1111-4111-8111-111111111111"))
        assert route is None or route.endpoint.__module__ not in SYNC_MODULES
    assert app.state.login_rate_limiter.max_hits == 5          # web login's limiter is there all the same (M8)

    caplog.set_level(logging.WARNING)
    async with app.router.lifespan_context(app):               # starts: an existing deployment does not break
        assert app_engine.engine is not None
    assert app_engine.engine is None
    warnings = _sync_warnings(caplog)
    assert len(warnings) == 1 and "DEVICE_TOKEN_SECRET" in warnings[0]


# --- DB mode, unusable device secret: startup fails ----------------------------------------------------------------


@pytest.mark.parametrize("secret, message", [
    ("short", "DEVICE_TOKEN_SECRET must be at least 32 characters"),
    ("d" * 31, "DEVICE_TOKEN_SECRET must be at least 32 characters"),
    ("w" * 32, "DEVICE_TOKEN_SECRET must differ from JWT_SECRET (D6)"),      # equal to the JWT secret
])
async def test_an_invalid_device_secret_fails_startup(monkeypatch, secret, message):
    app = _main_app(monkeypatch, database_url=UNUSED_DB, device_secret=secret)   # importable / buildable ...
    with pytest.raises(ValueError) as err:
        async with app.router.lifespan_context(app):                             # ... but it does not start
            pytest.fail("the app started with an unusable DEVICE_TOKEN_SECRET")
    await app.state.tally_client.close()
    assert str(err.value) == message
    assert app_engine.engine is None


async def test_a_short_jwt_secret_still_fails_with_its_own_message(monkeypatch):
    from backend.main import build_app

    configure_main_settings(monkeypatch, database_url=UNUSED_DB, jwt_secret="short")
    app = build_app()
    with pytest.raises(ValueError) as err:
        async with app.router.lifespan_context(app):
            pytest.fail("the app started with a short JWT_SECRET")
    await app.state.tally_client.close()
    assert str(err.value) == "JWT_SECRET must be at least 32 characters when DATABASE_URL is set"


# --- legacy mode (no DATABASE_URL): unaffected ---------------------------------------------------------------------


async def test_legacy_mode_is_unaffected(monkeypatch, caplog):
    """No database: exactly the legacy routes, no sync state on the app, no engine, no sync warning — whatever the
    device secret is (it is only looked at in DB mode)."""
    for secret in ("", "d" * 32, "short"):
        app = _main_app(monkeypatch, database_url=None, device_secret=secret)
        served = route_list(app)
        assert set(served) == PRE_EXISTING_LEGACY and len(served) == len(set(served))
        for name in ("settings", "clock", "login_rate_limiter", "device_rate_limiter"):
            assert not hasattr(app.state, name)
        caplog.clear()
        caplog.set_level(logging.WARNING)
        async with app.router.lifespan_context(app):
            assert app_engine.engine is None
            assert app.state.tally_client.mock_mode is True
        assert _sync_warnings(caplog) == []


# --- importing backend.main creates no engine -------------------------------------------------------------------------


_IMPORT_PROBE = """
import sqlalchemy.ext.asyncio as sa
calls = []
real = sa.create_async_engine
def spy(*args, **kwargs):
    calls.append(args)
    return real(*args, **kwargs)
sa.create_async_engine = spy

import backend.main
import backend.db.engine as e
assert e.create_async_engine is spy, "the spy is not the function backend.db.engine calls"
paths = {r.path for r in backend.main.app.routes}
assert "/api/auth/login" in paths and "/api/agent/auth/login" in paths, "not DB mode with sync"
assert calls == [], calls
assert e.engine is None and e.async_session_factory is None
e.init_engine()
assert len(calls) == 1
print("no-engine-at-import")
"""


def test_importing_backend_main_creates_no_engine(tmp_path):
    """The real import path, in a fresh interpreter, in DB mode with the sync routes on: importing the module
    (which builds ``app``) must not call ``create_async_engine``. Run from an empty directory so no ``.env`` is
    read; ``init_engine()`` at the end proves the spy would have seen a call."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("V2_")}
    env.update({"PYTHONPATH": str(REPO_ROOT), "DATABASE_URL": UNUSED_DB, "JWT_SECRET": "w" * 32,
                "DEVICE_TOKEN_SECRET": "d" * 32, "ANTHROPIC_API_KEY": "test-key", "TALLY_MODE": "mock",
                "LANGFUSE_PUBLIC_KEY": ""})
    done = subprocess.run([sys.executable, "-c", _IMPORT_PROBE], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("no-engine-at-import")


# --- the wiring function itself ---------------------------------------------------------------------------------------


def test_install_sync_mounts_exactly_the_sync_routes_with_the_injected_settings_and_clock():
    from datetime import datetime, timezone

    settings = Settings(_env_file=None, DATABASE_URL="", JWT_SECRET="w" * 32, DEVICE_TOKEN_SECRET="d" * 32,
                        LOGIN_RATE_MAX=3, DEVICE_RATE_MAX=7)
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_sync(app, settings, clock)
    assert set(route_list(app)) == SYNC_ROUTES and len(route_list(app)) == 19
    assert app.state.settings is settings and app.state.clock is clock
    assert (app.state.login_rate_limiter.max_hits, app.state.login_rate_limiter.clock) == (3, clock)
    assert (app.state.device_rate_limiter.max_hits, app.state.device_rate_limiter.clock) == (7, clock)


def test_install_state_alone_mounts_no_route():
    settings = Settings(_env_file=None, DATABASE_URL="", JWT_SECRET="w" * 32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_state(app, settings, SystemClock())
    assert route_list(app) == [] and app.state.login_rate_limiter is not None
