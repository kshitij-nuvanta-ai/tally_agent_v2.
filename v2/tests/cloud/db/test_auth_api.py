"""Device auth API (S1 spec §7.1-7.4, §7.16, §8.1, §9): login/refresh/logout, /api/agent/workspaces,
/api/devices. Each test asserts status + the JSON `error` code and, where state changes, re-reads the row in a
fresh session (never trusts the in-memory ORM object the request handler touched)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from v2.cloud.config import V2Settings
from v2.cloud.main import create_app
from v2.tests.cloud.conftest import (
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
    settings = V2Settings(_env_file=None, database_url=TEST_DB, web_jwt_secret="w" * 32,
                           device_token_secret="d" * 32, device_rate_max=3)
    app = create_app(settings, clock)
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as client:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            uid, body, headers = await login_device(client, session)
        for _ in range(3):
            r = await client.get("/api/agent/workspaces", headers=headers)
            assert r.status_code == 200
        r = await client.get("/api/agent/workspaces", headers=headers)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    await app.state.engine.dispose()
