"""S1 DB harness. TEST_DATABASE_URL only (never .env, never the dev DB). The DB is shared with the current app's
suite (plan ambiguity A2): stand-in users/workspaces are created only if absent, and teardown leaves the DB as
found."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from v2.cloud.auth.passwords import _pwd_context
from v2.cloud.cli import downgrade, migrate
from v2.cloud.clock import FixedClock
from v2.cloud.config import V2Settings
from v2.cloud.main import create_app
from v2.cloud.models import V2_TABLES

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set — skipping v2 DB tests")
TEST_EMAIL_DOMAIN = "@v2test.invalid"

# Copied shape from: backend/db/models.py @ 9335469 (User, Workspace) — test-only stand-ins, created only if absent.
STANDIN_DDL = (
    """CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY, email varchar(255) UNIQUE NOT NULL,
       password_hash varchar(255) NOT NULL, name varchar(255) NOT NULL, is_active boolean DEFAULT true,
       created_at timestamptz, updated_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS workspaces (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
       name varchar(255) NOT NULL, agent_type varchar(50) NOT NULL DEFAULT 'tally', config jsonb NOT NULL DEFAULT '{}',
       memory jsonb NOT NULL DEFAULT '{}', is_deleted boolean DEFAULT false, created_at timestamptz,
       updated_at timestamptz)""",
)


async def _existing(url: str) -> set[str]:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        rows = await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        names = {r[0] for r in rows}
    await eng.dispose()
    return names


async def _exec(url: str, *sql: str) -> None:
    eng = create_async_engine(url)
    async with eng.begin() as c:
        for s in sql:
            await c.execute(text(s))
    await eng.dispose()


def teardown_v2(url: str, created: list[str]) -> None:
    """The harness's real teardown (Review Focus 2 / controller ruling 1): runs the v2 Alembic chain down to
    ``base``, drops ``alembic_version_v2``, removes every test user/workspace this session's tests created
    (``@v2test.invalid`` emails), and drops any stand-in ``users``/``workspaces`` tables the harness itself
    created (a pre-existing ``users``/``workspaces`` — the normal case, shared with the current app's suite — is
    left alone). Used both by the session fixture below and directly by
    ``test_session_teardown_leaves_no_v2_objects``.
    """
    downgrade(url, "base")
    asyncio.run(
        _exec(
            url,
            "DROP TABLE IF EXISTS alembic_version_v2",
            f"DELETE FROM workspaces WHERE user_id IN (SELECT id FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}')",
            f"DELETE FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'",
            *[f"DROP TABLE IF EXISTS {t}" for t in reversed(created)],
        )
    )


@pytest.fixture(scope="session")
def v2_schema():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    before = asyncio.run(_existing(TEST_DB))
    created = [t for t in ("users", "workspaces") if t not in before]
    asyncio.run(_exec(TEST_DB, *STANDIN_DDL))
    migrate(TEST_DB)
    yield {"created_standins": created}
    teardown_v2(TEST_DB, created)


@pytest.fixture
async def engine(v2_schema):
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as c:           # every test starts from empty v2 tables + no test users
        await c.execute(text("TRUNCATE " + ", ".join(V2_TABLES) + " CASCADE"))
        await c.execute(text(f"DELETE FROM workspaces WHERE user_id IN (SELECT id FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}')"))
        await c.execute(text(f"DELETE FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'"))
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
                           "h": _pwd_context.hash(password), "a": is_active})
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
    return V2Settings(_env_file=None, database_url=TEST_DB, web_jwt_secret="w" * 32, device_token_secret="d" * 32)


@pytest.fixture
async def app_client(engine, settings, clock):
    app = create_app(settings, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as c:
        c.app = app
        yield c
    await app.state.engine.dispose()


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
