"""Device auth API (S1 spec §7.1-7.4, §7.16, §8.1, §9): login/refresh/logout, /api/agent/workspaces,
/api/devices. Each test asserts status + the JSON `error` code and, where state changes, re-reads the row in a
fresh session (never trusts the in-memory ORM object the request handler touched)."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api import agent_auth
from backend.api.sync_dependencies import active_device
from backend.config import Settings
from backend.sync.app import create_app
from tests.sync.conftest import (
    TEST_DB,
    login_device,
    make_user,
    make_workspace,
    requires_db,
    web_headers,
)

pytestmark = requires_db


async def _device_row(session, device_id):
    row = (
        await session.execute(
            text(
                "SELECT workspace_id, refresh_hash, refresh_prev_hash, last_login_at, is_active, revoked_at, "
                "revoke_reason, refresh_expires_at FROM agent_devices WHERE id = :i"
            ),
            {"i": device_id},
        )
    ).mappings().first()
    return row


async def test_login_creates_unbound_device_and_returns_tokens(app_client, session, clock):
    uid, body, headers = await login_device(app_client, session)
    assert body["expires_in"] == 900
    assert uuid.UUID(body["device_id"])
    row = await _device_row(session, body["device_id"])
    assert row["workspace_id"] is None
    assert row["refresh_hash"] == hashlib.sha256(body["refresh_token"].encode()).hexdigest()
    assert row["last_login_at"] == clock.now()


async def test_login_wrong_password_401_invalid_credentials(app_client, session):
    uid = await make_user(session, password="Passw0rd!Passw0rd")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await app_client.post("/api/agent/auth/login", json={"email": email, "password": "wrong-password",
                                                              "device_name": "PC", "agent_version": "0.1.0"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_credentials"


async def test_login_inactive_user_403_account_inactive(app_client, session):
    uid = await make_user(session, password="Passw0rd!Passw0rd", is_active=False)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await app_client.post("/api/agent/auth/login", json={"email": email, "password": "Passw0rd!Passw0rd",
                                                              "device_name": "PC", "agent_version": "0.1.0"})
    assert r.status_code == 403 and r.json()["error"] == "account_inactive"


async def test_login_rate_limited_after_5_attempts_per_email(app_client, session):
    uid = await make_user(session, password="Passw0rd!Passw0rd")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    body = {"email": email, "password": "wrong-password", "device_name": "PC", "agent_version": "0.1.0"}
    for _ in range(5):
        r = await app_client.post("/api/agent/auth/login", json=body)
        assert r.status_code == 401
    r = await app_client.post("/api/agent/auth/login", json=body)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert "Retry-After" in r.headers


async def test_refresh_rotates_and_old_token_becomes_reuse_signal(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    r1 = body["refresh_token"]
    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": r1})
    assert r.status_code == 200
    r2 = r.json()["refresh_token"]
    assert r2 != r1

    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": r1})
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"
    row = await _device_row(session, body["device_id"])
    assert row["revoke_reason"] == "refresh_reuse"

    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": r2})
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"


async def test_refresh_expired_401_refresh_expired(app_client, session, clock):
    uid, body, headers = await login_device(app_client, session)
    clock.advance(days=90, seconds=1)
    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"] == "refresh_expired"


async def test_refresh_sliding_window_extends_expiry(app_client, session, clock):
    uid, body, headers = await login_device(app_client, session)
    clock.advance(days=89)
    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200
    row = await _device_row(session, body["device_id"])
    assert row["refresh_expires_at"] == clock.now() + timedelta(days=90)


async def test_logout_revokes_calling_device_204(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    r = await app_client.post("/api/agent/auth/logout", headers=headers)
    assert r.status_code == 204
    row = await _device_row(session, body["device_id"])
    assert row["revoked_at"] is not None and row["revoke_reason"] == "logout"

    r = await app_client.get("/api/agent/workspaces", headers=headers)
    assert r.status_code == 401
    b = r.json()
    assert b["error"] == "device_revoked" and b["reason"] == "logout"


async def test_agent_workspaces_lists_live_only_with_binding(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    await make_workspace(session, uid, name="Live")
    await make_workspace(session, uid, name="Gone", is_deleted=True)

    r = await app_client.get("/api/agent/workspaces", headers=headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Live"
    assert rows[0]["bound_company_guid"] is None
    assert rows[0]["active_device"] is None


async def test_web_token_rejected_on_device_endpoint(app_client, session):
    uid = await make_user(session)
    r = await app_client.get("/api/agent/workspaces", headers=web_headers(uid))
    assert r.status_code == 401 and r.json()["error"] == "token_invalid"


async def test_device_token_rejected_on_web_endpoint(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    r = await app_client.get("/api/devices", headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "token_invalid"


async def test_devices_list_and_delete_revokes_user_removed(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    r = await app_client.get("/api/devices", headers=web_headers(uid))
    assert r.status_code == 200
    devices = r.json()
    assert len(devices) == 1 and devices[0]["id"] == body["device_id"]

    r = await app_client.delete(f"/api/devices/{body['device_id']}", headers=web_headers(uid))
    assert r.status_code == 204
    row = await _device_row(session, body["device_id"])
    assert row["revoke_reason"] == "user_removed"

    r = await app_client.get("/api/agent/workspaces", headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"


async def test_delete_other_users_device_404(app_client, session):
    uid, body, headers = await login_device(app_client, session)
    other_uid = await make_user(session)
    r = await app_client.delete(f"/api/devices/{body['device_id']}", headers=web_headers(other_uid))
    assert r.status_code == 404


async def test_per_device_rate_limit_429(engine, clock):
    settings = Settings(_env_file=None, DATABASE_URL=TEST_DB, JWT_SECRET="w" * 32,
                        DEVICE_TOKEN_SECRET="d" * 32, DEVICE_RATE_MAX=3)
    app = create_app(settings, clock)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as client:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            uid, body, headers = await login_device(client, session)
        for _ in range(3):
            r = await client.get("/api/agent/workspaces", headers=headers)
            assert r.status_code == 200
        r = await client.get("/api/agent/workspaces", headers=headers)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    await app.state.engine.dispose()


# --- Fix round 1 ------------------------------------------------------------------------------------------
# Review findings (task-4-review.md § Important): (1) the login limiter must count only FAILED attempts —
# copied `_check_rate_limit` semantics, not "every attempt"; (2) covered at the unit level in test_rate_limit.py
# (the sweep); (3) `active_device` (§8.1 checks 3-6) needs executing tests — a test-only probe router below;
# (4) auth bodies must be pydantic-validated (422, not 500, on bad input); (5) refresh rotation must be atomic
# under concurrency; (6) covered in test_config_clock.py (distinct secrets).


async def test_login_rate_limit_never_trips_on_successful_logins(app_client, session):
    """Fix round 1 #1: repeated SUCCESSFUL logins must never count against the per-email limiter — only a
    failed attempt does (copied `_check_rate_limit` semantics, spec §7.1)."""
    uid = await make_user(session, password="Passw0rd!Passw0rd")
    email = (await session.execute(text("SELECT email FROM users WHERE id = :i"), {"i": uid})).scalar_one()
    for _ in range(6):
        r = await app_client.post(
            "/api/agent/auth/login",
            json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": "PC", "agent_version": "0.1.0"},
        )
        assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "Passw0rd!Passw0rd", "device_name": "PC"},  # missing email
        {"email": "x@example.com", "device_name": "PC"},  # missing password
        {"email": "x@example.com", "password": "Passw0rd!Passw0rd"},  # missing device_name
    ],
)
async def test_login_missing_required_field_422(app_client, payload):
    r = await app_client.post("/api/agent/auth/login", json=payload)
    assert r.status_code == 422


async def test_login_wrong_type_field_422(app_client):
    r = await app_client.post(
        "/api/agent/auth/login",
        json={"email": "x@example.com", "password": 12345, "device_name": "PC", "agent_version": "0.1.0"},
    )
    assert r.status_code == 422


async def test_login_overlong_field_422(app_client):
    r = await app_client.post(
        "/api/agent/auth/login",
        json={"email": "x@example.com", "password": "y" * 10, "device_name": "x" * 300, "agent_version": "0.1.0"},
    )
    assert r.status_code == 422


async def test_refresh_missing_field_422(app_client):
    r = await app_client.post("/api/agent/auth/refresh", json={})
    assert r.status_code == 422


async def test_refresh_wrong_type_field_422(app_client):
    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": 12345})
    assert r.status_code == 422


async def test_refresh_overlong_field_422(app_client):
    r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": "x" * 600})
    assert r.status_code == 422


class _TwoPartyBarrier:
    """Fix round 2 #5: forces two coroutines to rendezvous at a single point before either continues past it.
    Used to guarantee both concurrent ``refresh`` requests finish their read (the ``SELECT`` by ``refresh_hash``)
    before either performs its write (the conditional ``UPDATE``) — the exact overlap a non-atomic
    read-then-write race needs to be exercised deterministically, instead of hoping ``asyncio.gather`` happens
    to interleave two in-process ASGI calls that way."""

    def __init__(self, parties: int):
        self._parties = parties
        self._count = 0
        self._lock = asyncio.Lock()
        self._released = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._count += 1
            if self._count >= self._parties:
                self._released.set()
        await self._released.wait()


async def test_refresh_race_one_wins_other_becomes_reuse_signal(app_client, session, monkeypatch):
    """Fix round 1 #5 / Fix round 2 #5: two concurrent uses of the SAME refresh token must end with exactly one
    success and the other treated as reuse — the device revoked, per D6's theft signal — never two successful
    rotations.

    Round 1's version of this test drove both requests through ``asyncio.gather`` and hoped they'd interleave
    between the read and the write; the re-review showed it also passes against the pre-fix, non-atomic
    read-then-write code (both requests run to completion sequentially inside one event loop turn often enough
    that the *old* code's prev-hash path produces the same 200+401 shape by coincidence — so the test never
    actually proved atomicity). This version makes the overlap deterministic: it monkeypatches
    ``agent_auth._workspace_deleted`` — called once per refresh, strictly after the read and strictly before the
    write — with a wrapper that blocks on a 2-party barrier. Neither request's write can happen until BOTH have
    completed their read, on every run, not just probabilistically.
    """
    uid, body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)  # live (not deleted) — refresh() must call _workspace_deleted on it
    await _bind_device(session, body["device_id"], ws, is_active=True)
    r1 = body["refresh_token"]

    barrier = _TwoPartyBarrier(2)
    original = agent_auth._workspace_deleted

    async def _barriered_workspace_deleted(session_arg, workspace_id):
        await barrier.wait()  # blocks until both concurrent requests have finished their read
        return await original(session_arg, workspace_id)

    monkeypatch.setattr(agent_auth, "_workspace_deleted", _barriered_workspace_deleted)

    results = await asyncio.gather(
        app_client.post("/api/agent/auth/refresh", json={"refresh_token": r1}),
        app_client.post("/api/agent/auth/refresh", json={"refresh_token": r1}),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 401], [r.text for r in results]

    loser = next(r for r in results if r.status_code == 401)
    loser_body = loser.json()
    assert loser_body["error"] == "device_revoked" and loser_body["reason"] == "refresh_reuse"

    row = await _device_row(session, body["device_id"])
    assert row["revoke_reason"] == "refresh_reuse"


def _mount_active_device_probe(app) -> None:
    """Test-only route (Fix round 1 #3): drives `active_device` (§8.1 checks 1-6) end to end. Not a production
    route — never registered by `backend/sync/app.py`."""
    probe_router = APIRouter()

    @probe_router.get("/t/{ws}")
    async def _probe(bound: tuple = Depends(active_device)) -> dict:
        device, workspace = bound
        return {"device_id": str(device.id), "workspace_id": str(workspace.workspace_id)}

    app.include_router(probe_router)


@pytest.fixture
async def probe_client(engine, settings, clock):
    app = create_app(settings, clock)
    _mount_active_device_probe(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as c:
        c.app = app
        yield c
    await app.state.engine.dispose()


async def _seed_sync_workspace(session, workspace_id, *, active_device_id=None) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_workspaces (workspace_id, tally_company_guid, tally_company_name, books_from, "
            "sync_state, active_device_id) VALUES (:w, 'guid-1', 'Co', '2022-04-01', 'ready', :ad)"
        ),
        {"w": workspace_id, "ad": active_device_id},
    )
    await session.commit()


async def _bind_device(session, device_id, workspace_id, *, is_active: bool) -> None:
    await session.execute(
        text("UPDATE agent_devices SET workspace_id = :w, is_active = :a WHERE id = :d"),
        {"w": workspace_id, "a": is_active, "d": device_id},
    )
    await session.commit()


async def test_active_device_missing_token_401(probe_client):
    r = await probe_client.get(f"/t/{uuid.uuid4()}")
    assert r.status_code == 401 and r.json()["error"] == "token_invalid"


async def test_active_device_revoked_device_401(probe_client, session):
    uid, body, headers = await login_device(probe_client, session)
    ws = await make_workspace(session, uid)
    await _bind_device(session, body["device_id"], ws, is_active=True)
    await _seed_sync_workspace(session, ws, active_device_id=uuid.UUID(body["device_id"]))
    await session.execute(
        text("UPDATE agent_devices SET revoked_at = now(), revoke_reason = 'logout', is_active = false "
             "WHERE id = :d"),
        {"d": body["device_id"]},
    )
    await session.commit()

    r = await probe_client.get(f"/t/{ws}", headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked" and r.json()["reason"] == "logout"


async def test_active_device_wrong_workspace_403(probe_client, session):
    uid, body, headers = await login_device(probe_client, session)
    ws_a = await make_workspace(session, uid, name="A")
    ws_b = await make_workspace(session, uid, name="B")
    await _bind_device(session, body["device_id"], ws_a, is_active=True)
    await _seed_sync_workspace(session, ws_a, active_device_id=uuid.UUID(body["device_id"]))

    r = await probe_client.get(f"/t/{ws_b}", headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "wrong_workspace"


async def test_active_device_deleted_workspace_410_revokes(probe_client, session):
    uid, body, headers = await login_device(probe_client, session)
    ws = await make_workspace(session, uid, is_deleted=True)
    await _bind_device(session, body["device_id"], ws, is_active=True)
    await _seed_sync_workspace(session, ws, active_device_id=uuid.UUID(body["device_id"]))

    r = await probe_client.get(f"/t/{ws}", headers=headers)
    assert r.status_code == 410 and r.json()["error"] == "workspace_deleted"
    row = await _device_row(session, body["device_id"])
    assert row["revoke_reason"] == "workspace_deleted"


async def test_active_device_not_active_409(probe_client, session):
    uid, body, headers = await login_device(probe_client, session)
    ws = await make_workspace(session, uid)
    await _bind_device(session, body["device_id"], ws, is_active=False)  # bound but not the active device
    await _seed_sync_workspace(session, ws, active_device_id=None)

    r = await probe_client.get(f"/t/{ws}", headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "not_active_device"


async def test_active_device_active_200(probe_client, session):
    uid, body, headers = await login_device(probe_client, session)
    ws = await make_workspace(session, uid)
    await _bind_device(session, body["device_id"], ws, is_active=True)
    await _seed_sync_workspace(session, ws, active_device_id=uuid.UUID(body["device_id"]))

    r = await probe_client.get(f"/t/{ws}", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"device_id": body["device_id"], "workspace_id": str(ws)}


async def test_active_device_ordering_wrong_workspace_wins_over_deleted(probe_client, session):
    """Ordering case: two checks fail at once — the device isn't bound to `ws` (check 3) AND `ws` itself is
    deleted (check 4). Check 3 must win, so a cross-tenant probe against a deleted workspace learns nothing
    about that workspace's deletion state, and the device is never revoked for a workspace it isn't bound to."""
    uid, body, headers = await login_device(probe_client, session)
    ws_a = await make_workspace(session, uid, name="A")
    ws_deleted = await make_workspace(session, uid, name="Deleted", is_deleted=True)
    await _bind_device(session, body["device_id"], ws_a, is_active=True)
    await _seed_sync_workspace(session, ws_a, active_device_id=uuid.UUID(body["device_id"]))

    r = await probe_client.get(f"/t/{ws_deleted}", headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "wrong_workspace"

    row = await _device_row(session, body["device_id"])
    assert row["revoked_at"] is None  # check 4's revoke side effect must not have run


async def test_active_device_rate_limit_checked_after_not_active(engine, clock):
    """§8.1 check 6 (rate limit) is last: with `DEVICE_RATE_MAX=1` and a device that always fails check 5
    (bound, not the workspace's active device), repeated calls must keep returning 409 — never 429 — because
    execution never reaches the limiter once an earlier check has already failed."""
    settings = Settings(_env_file=None, DATABASE_URL=TEST_DB, JWT_SECRET="w" * 32,
                        DEVICE_TOKEN_SECRET="d" * 32, DEVICE_RATE_MAX=1)
    app = create_app(settings, clock)
    _mount_active_device_probe(app)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as client:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            uid, body, headers = await login_device(client, session)
            ws = await make_workspace(session, uid)
            await _bind_device(session, body["device_id"], ws, is_active=False)
            await _seed_sync_workspace(session, ws, active_device_id=None)

        for _ in range(3):
            r = await client.get(f"/t/{ws}", headers=headers)
            assert r.status_code == 409 and r.json()["error"] == "not_active_device"
    await app.state.engine.dispose()


async def test_active_device_rate_limit_429_with_retry_after(engine, clock):
    """Fix round 2 #3(a): §8.1 check 6, reached through `active_device` (not `any_device`). With
    `DEVICE_RATE_MAX=1` and a device that passes every earlier check (bound, active, live workspace), the first
    call succeeds and the second is 429 `rate_limited` with an integer `Retry-After` header — proving the
    limiter is actually wired into `active_device`'s own code path, not just asserted by omission elsewhere."""
    settings = Settings(_env_file=None, DATABASE_URL=TEST_DB, JWT_SECRET="w" * 32,
                        DEVICE_TOKEN_SECRET="d" * 32, DEVICE_RATE_MAX=1)
    app = create_app(settings, clock)
    _mount_active_device_probe(app)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as client:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            uid, body, headers = await login_device(client, session)
            ws = await make_workspace(session, uid)
            await _bind_device(session, body["device_id"], ws, is_active=True)
            await _seed_sync_workspace(session, ws, active_device_id=uuid.UUID(body["device_id"]))

        r1 = await client.get(f"/t/{ws}", headers=headers)
        assert r1.status_code == 200

        r2 = await client.get(f"/t/{ws}", headers=headers)
        assert r2.status_code == 429
        assert r2.json()["error"] == "rate_limited"
        assert "Retry-After" in r2.headers
        assert int(r2.headers["Retry-After"]) >= 0  # header value must parse as an integer
    await app.state.engine.dispose()


async def test_active_device_invalid_bearer_401(probe_client):
    """Fix round 2 #3(b): a malformed/garbage bearer on the probe route (not just a missing header)."""
    r = await probe_client.get(f"/t/{uuid.uuid4()}", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401 and r.json()["error"] == "token_invalid"


async def test_active_device_wrongly_signed_bearer_401(probe_client, session):
    """Fix round 2 #3(b): a well-formed JWT signed with the WEB secret (not the device secret) — the `typ`
    check must still reject it even though it decodes as valid JSON/JWT."""
    uid = await make_user(session)
    r = await probe_client.get(f"/t/{uuid.uuid4()}", headers=web_headers(uid))
    assert r.status_code == 401 and r.json()["error"] == "token_invalid"
