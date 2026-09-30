"""Sync DB harness. TEST_DATABASE_URL only (never .env, never the dev DB).

One metadata, one database, one pytest session (v2 merge T4). The sync tests use the real tables of the single
``Base.metadata`` — the same 30 tables the app's own DB fixtures (``tests/integration/conftest.py`` and the
``tests/e2e/test_db_*`` modules) build with ``create_all`` and remove with ``drop_all`` around each of their tests.
Both harnesses therefore work on the same schema and can run in one session, in any order:

- ``engine`` (per test) makes sure the tables exist — one catalog query, and a ``create_all`` only when an app
  test has just dropped them — then empties the sync tables and removes this harness's own test users;
- ``sync_schema`` (per session) notes which tables the database held before the first sync test and, at the end
  of the session, puts the database back as it found it (``restore_as_found``).

The sync routes take their sessions from the backend's one engine (``backend.db.engine``, v2 merge M3), so
``app_factory`` initialises that engine on the test database for the test and closes it afterwards — what the real
app's lifespan does. Apps are built by ``build_sync_app``: a bare FastAPI plus ``backend.sync.wiring.install_sync``,
the same function ``backend/main.py`` uses, with the test's own settings and a fixed clock.

The migration chain itself is tested in throwaway databases (``tests/sync/db/test_migration.py``), which also
proves the migrated schema equals this metadata.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import Settings
from backend.db import engine as app_engine
from backend.db.models import Base
from backend.db.sync_models import SYNC_TABLES
from backend.sync.clock import Clock, FixedClock
from backend.sync.wiring import install_sync
from backend.utils.auth import hash_password

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set — skipping sync DB tests")
TEST_EMAIL_DOMAIN = "@v2test.invalid"

_DELETE_TEST_ROWS = (
    f"DELETE FROM workspaces WHERE user_id IN (SELECT id FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}')",
    f"DELETE FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'",
)


async def _existing(url: str) -> set[str]:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        rows = await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        names = {r[0] for r in rows}
    await eng.dispose()
    return names


async def ensure_schema(conn) -> None:
    """Create whichever of the metadata's tables are missing. Cheap when none is: one catalog query."""
    present = (
        await conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:names)"),
            {"names": list(Base.metadata.tables)},
        )
    ).scalar_one()
    if present != len(Base.metadata.tables):
        await conn.run_sync(Base.metadata.create_all)


async def _restore_as_found(url: str, before: set[str]) -> None:
    eng = create_async_engine(url)
    async with eng.begin() as c:
        now = {r[0] for r in await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        kept = [t for t in SYNC_TABLES if t in now and t in before]
        if kept:
            await c.execute(text("TRUNCATE " + ", ".join(kept) + " CASCADE"))
        if {"users", "workspaces"} <= (now & before):
            for sql in _DELETE_TEST_ROWS:
                await c.execute(text(sql))
        created = [t for name, t in Base.metadata.tables.items() if name in now and name not in before]
        await c.run_sync(lambda sync_conn: Base.metadata.drop_all(sync_conn, tables=created))
    await eng.dispose()


def restore_as_found(url: str, before: set[str]) -> None:
    """The harness's session teardown: put the database back as it was when ``before`` (its table names) was
    taken. Every metadata table that was not there is dropped. A table that was already there stays, with the
    rows other users of the database put in it; only what this harness writes is removed from it — the sync
    tables are emptied (the ``engine`` fixture empties them before every test anyway) and the
    ``@v2test.invalid`` users and their workspaces are deleted. Used by ``sync_schema`` below and exercised
    directly by ``test_migration.py::test_harness_teardown_leaves_the_database_as_found``."""
    asyncio.run(_restore_as_found(url, before))


@pytest.fixture(scope="session")
def sync_schema():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    before = asyncio.run(_existing(TEST_DB))
    yield {"tables_before": before}
    restore_as_found(TEST_DB, before)


@pytest.fixture
async def engine(sync_schema):
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as c:           # every test starts from empty sync tables + no test users
        await ensure_schema(c)
        await c.execute(text("TRUNCATE " + ", ".join(SYNC_TABLES) + " CASCADE"))
        for sql in _DELETE_TEST_ROWS:
            await c.execute(text(sql))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


async def make_user(session: AsyncSession, email: str | None = None, password: str = "Passw0rd!Passw0rd",
                    is_active: bool = True) -> uuid.UUID:
    uid = uuid.uuid4()
    await session.execute(text("INSERT INTO users (id, email, password_hash, name, is_active, created_at, updated_at) "
                               "VALUES (:id, :e, :h, 'Owner', :a, now(), now())"),
                          {"id": uid, "e": email or f"{uid.hex[:8]}{TEST_EMAIL_DOMAIN}",
                           "h": hash_password(password), "a": is_active})
    await session.commit()
    return uid


async def make_workspace(session: AsyncSession, user_id: uuid.UUID, name: str = "W",
                         is_deleted: bool = False) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(text("INSERT INTO workspaces (id, user_id, name, agent_type, config, memory, is_deleted, "
                               "created_at, updated_at) VALUES (:id, :u, :n, 'tally', '{}', '{}', :d, now(), now())"),
                          {"id": wid, "u": user_id, "n": name, "d": is_deleted})
    await session.commit()
    return wid


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc))


@pytest.fixture
def settings():
    return Settings(_env_file=None, DATABASE_URL=TEST_DB, JWT_SECRET="w" * 32, DEVICE_TOKEN_SECRET="d" * 32)


def build_sync_app(settings: Settings, clock: Clock) -> FastAPI:
    """An app serving the sync/agent routes through the production wiring (``install_sync``), with injected
    settings and clock. It owns no engine: its routes use ``backend.db.engine.get_db``."""
    app = FastAPI()
    install_sync(app, settings, clock)
    return app


async def restart_app_engine() -> None:
    """What a server restart does to the database side: dispose the backend's engine and build a new one."""
    await app_engine.close_engine()
    app_engine.init_engine(TEST_DB)


@pytest.fixture
async def app_factory(engine):
    """``build_sync_app`` with the backend's one engine initialised on the test database for this test."""
    app_engine.init_engine(TEST_DB)
    yield build_sync_app
    await app_engine.close_engine()


@pytest.fixture
async def app_client(app_factory, settings, clock):
    app = app_factory(settings, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as c:
        c.app = app
        yield c


async def login_device(client, session, *, email=None, password="Passw0rd!Passw0rd", device_name="ACCOUNTS-PC"):
    uid = await make_user(session, email=email, password=password)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await client.post("/api/agent/auth/login", json={"email": email, "password": password,
                                                        "device_name": device_name, "agent_version": "0.1.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    return uid, body, {"Authorization": f"Bearer {body['access_token']}"}


def web_headers(user_id, secret="w" * 32):
    tok = jwt.encode({"sub": str(user_id), "type": "access", "exp": 4102444800}, secret, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


# --- the real app (backend.main) under a chosen configuration ---------------------------------------------------

MAIN_JWT_SECRET = "w" * 32            # the secret ``web_headers`` signs with
MAIN_DEVICE_SECRET = "d" * 32


def configure_main_settings(monkeypatch, *, database_url: str | None, device_secret: str = MAIN_DEVICE_SECRET,
                            jwt_secret: str | None = MAIN_JWT_SECRET) -> None:
    """Set the ``backend.config.settings`` singleton — what ``backend/main.py`` and every pre-existing route read
    — for one test (restored by ``monkeypatch``). Never the repo's ``.env`` values: every field the app wiring
    reads is set here. Tally is the built-in mock and Langfuse is off, so nothing leaves the process."""
    from backend.config import settings as live

    values = {"DATABASE_URL": database_url, "JWT_SECRET": jwt_secret, "DEVICE_TOKEN_SECRET": device_secret,
              "TALLY_MODE": "mock", "LANGFUSE_PUBLIC_KEY": "", "CORS_ORIGINS": "",
              "LOGIN_RATE_MAX": 5, "LOGIN_RATE_WINDOW_S": 900, "DEVICE_RATE_MAX": 600, "DEVICE_RATE_WINDOW_S": 60}
    for name, value in values.items():
        monkeypatch.setattr(live, name, value)


@asynccontextmanager
async def serve_main(monkeypatch, *, database_url: str | None = TEST_DB, device_secret: str = MAIN_DEVICE_SECRET,
                     raise_app_exceptions: bool = True):
    """The app ``backend.main`` builds for this configuration, started through its real lifespan (which creates
    the engine and closes it again), behind an httpx client. ``client.app`` is the app."""
    from backend.main import build_app

    configure_main_settings(monkeypatch, database_url=database_url, device_secret=device_secret)
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://main") as c:
            c.app = app
            yield c
