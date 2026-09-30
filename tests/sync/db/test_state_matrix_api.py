"""The full S1 spec §15.1 matrix -- endpoint group × auth state -- at API level (``httpx`` + ASGI, DB-backed).

Columns (auth states), each built on top of a REAL login + bind (never a hand-seeded "valid" row):
  valid_active    the bound, active device D1
  expired_access  D1 after its 15-minute access token lapsed (the shared app clock advanced 16 minutes)
  revoked         D1 revoked (``revoked_at`` set)
  unbound         a second device of the same user, logged in, never bound (``workspace_id`` NULL)
  wrong_ws        D1 against another workspace of the same user it isn't bound to
  deleted_ws      D1's workspace soft-deleted (``workspaces.is_deleted``)
  not_active      a second device bound to D1's workspace but ``is_active = false``
  web_jwt         the owner's web JWT presented instead of a device token

Cell values are exactly the spec table's. ``—`` cells are omitted (each omission is commented at its row). Row
"``/api/sync/{ws}/*``" is run for every ``{ws}`` route: heartbeat, state, runs, batches, coverage, reconcile,
snapshots, parity, relink. For the "valid active" cell each route gets a well-formed body and must answer 200/204.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
import pytest
from sqlalchemy import text

from contract import transcode
from tests.sync import realdata
from tests.sync.conftest import login_device, make_user, make_workspace, requires_db, web_headers
from tests.sync.db.ingest_helpers import B_BIND, COUNTERS, batch, gz

pytestmark = requires_db

PASSWORD = "Passw0rd!Passw0rd"
COLUMNS = ("valid_active", "expired_access", "revoked", "unbound", "wrong_ws", "deleted_ws", "not_active", "web_jwt")
NEW_GUID = "new-guid-0000"


@dataclass
class Env:
    uid: uuid.UUID
    email: str
    ws: uuid.UUID
    run_id: str
    d1: dict                    # D1's login body
    headers: dict               # D1's device headers
    other_ws: uuid.UUID | None = None
    d2: dict | None = None      # the unbound / not-active second device's login body


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, email: str, device_name: str) -> dict:
    r = await client.post("/api/agent/auth/login", json={"email": email, "password": PASSWORD,
                                                         "device_name": device_name, "agent_version": "0.1.0"})
    assert r.status_code == 200, r.text
    return r.json()


async def setup(client, session, clock, variant: str) -> tuple[Env, dict]:
    """A real bind of company B + an open first_sync run + a relink prompt (so relink's valid cell has one), then
    ``variant`` applied. Returns the env and the headers the request is made with."""
    uid, d1, headers = await login_device(client, session)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r = await client.post("/api/sync/company", json={**B_BIND, "workspace_id": str(ws)}, headers=headers)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": COUNTERS},
                          headers=headers)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    r = await client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "other_company_same_name", "pc_clock": clock.now().isoformat(),
        "seen_company": {"guid": NEW_GUID, "name": B_BIND["company_name"]}})
    assert r.status_code == 200, r.text
    env = Env(uid, email, ws, run_id, d1, headers)

    if variant == "valid_active":
        return env, headers
    if variant == "expired_access":
        clock.advance(minutes=16)                         # device_access_minutes = 15
        return env, headers
    if variant == "revoked":
        await session.execute(text("UPDATE agent_devices SET revoked_at = now(), revoke_reason = 'logout', "
                                   "is_active = false WHERE id = :d"), {"d": uuid.UUID(d1["device_id"])})
        await session.commit()
        return env, headers
    if variant == "unbound":
        env.d2 = await _login(client, email, "UNBOUND-PC")
        return env, bearer(env.d2["access_token"])
    if variant == "wrong_ws":
        env.other_ws = await make_workspace(session, uid, name="Other")
        return env, headers
    if variant == "deleted_ws":
        await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id = :w"), {"w": ws})
        await session.commit()
        return env, headers
    if variant == "not_active":
        env.d2 = await _login(client, email, "LAPTOP")
        await session.execute(text("UPDATE agent_devices SET workspace_id = :w, is_active = false WHERE id = :d"),
                              {"w": ws, "d": uuid.UUID(env.d2["device_id"])})
        await session.commit()
        return env, bearer(env.d2["access_token"])
    if variant == "web_jwt":
        return env, web_headers(uid)
    raise AssertionError(variant)


def _error(r) -> str | None:
    try:
        return r.json().get("error")
    except Exception:
        return None


# --- row 1: agent auth (refresh / logout) ---------------------------------------------------------------------
# spec: valid ✓ | expired refresh ✓ | revoked 401 | unbound ✓ | wrong ws — | deleted ws 410 | not active ✓ | web 401
# `wrong ws` is omitted (—: these calls name no workspace). `expired access` is specified for refresh only ("refresh
# ✓"), so logout × expired is omitted too.
AUTH_ROW = {"valid_active": 200, "expired_access": 200, "revoked": 401, "unbound": 200, "deleted_ws": 410,
            "not_active": 200, "web_jwt": 401}


@pytest.mark.parametrize("call, variant", [(c, v) for c in ("refresh", "logout") for v in AUTH_ROW
                                            if not (c == "logout" and v == "expired_access")])
async def test_auth_matrix(app_client, session, clock, call, variant):
    env, headers = await setup(app_client, session, clock, variant)
    device = env.d2 if variant in ("unbound", "not_active") else env.d1
    if call == "refresh":
        token = headers["Authorization"][len("Bearer "):] if variant == "web_jwt" else device["refresh_token"]
        r = await app_client.post("/api/agent/auth/refresh", json={"refresh_token": token})
        want = AUTH_ROW[variant]
    else:
        r = await app_client.post("/api/agent/auth/logout", headers=headers)
        want = 204 if AUTH_ROW[variant] == 200 else AUTH_ROW[variant]
    assert r.status_code == want, r.text
    if want == 410:
        assert _error(r) == "workspace_deleted"
    if call == "refresh" and want == 200:
        assert r.json()["device_id"] == device["device_id"] and r.json()["access_token"]


# --- row 2: GET /api/agent/workspaces ---------------------------------------------------------------------------
# spec: valid ✓ | expired 401 | revoked 401 | unbound ✓ | wrong ws — | deleted ws "lists only live" | not active ✓ |
# web 401. (`wrong ws` omitted: the call names no workspace.)
WORKSPACES_ROW = {"valid_active": 200, "expired_access": 401, "revoked": 401, "unbound": 200, "deleted_ws": 200,
                  "not_active": 200, "web_jwt": 401}


@pytest.mark.parametrize("variant", list(WORKSPACES_ROW))
async def test_agent_workspaces_matrix(app_client, session, clock, variant):
    env, headers = await setup(app_client, session, clock, variant)
    r = await app_client.get("/api/agent/workspaces", headers=headers)
    assert r.status_code == WORKSPACES_ROW[variant], r.text
    if r.status_code == 200:
        ids = {w["id"] for w in r.json()}
        if variant == "deleted_ws":
            assert str(env.ws) not in ids                      # "lists only live"
        else:
            assert str(env.ws) in ids


# --- row 3: POST /api/sync/company ------------------------------------------------------------------------------
# spec: valid ✓ | expired 401 | revoked 401 | unbound ✓ | wrong ws — | deleted ws 410 | not active "take-over
# rules" | web 401. (`wrong ws` omitted: binding names the workspace it binds.) "Take-over rules" (§9.3): without
# `takeover` -> 409 (another device is active); with `takeover` and a fresh login -> 200 and it becomes active.
BIND_ROW = {"valid_active": 200, "expired_access": 401, "revoked": 401, "unbound": 200, "deleted_ws": 410,
            "not_active": "takeover_rules", "web_jwt": 401}


@pytest.mark.parametrize("variant", list(BIND_ROW))
async def test_bind_matrix(app_client, session, clock, variant):
    env, headers = await setup(app_client, session, clock, variant)
    body = {**B_BIND, "workspace_id": str(env.ws)}
    if variant == "unbound":                               # an unbound device binds a workspace of its own
        body["workspace_id"] = str(await make_workspace(session, env.uid, name="Second"))
        body["company_guid"] = "unbound-device-company-guid"
    if BIND_ROW[variant] == "takeover_rules":
        r = await app_client.post("/api/sync/company", json=body, headers=headers)
        assert r.status_code == 409, r.text
        r = await app_client.post("/api/sync/company", json={**body, "takeover": True}, headers=headers)
        assert r.status_code == 200, r.text
        row = (await session.execute(text("SELECT is_active FROM agent_devices WHERE id = :d"),
                                     {"d": uuid.UUID(env.d2["device_id"])})).scalar_one()
        assert row is True
        return
    r = await app_client.post("/api/sync/company", json=body, headers=headers)
    assert r.status_code == BIND_ROW[variant], r.text
    if r.status_code == 410:
        assert _error(r) == "workspace_deleted"


# --- row 4: /api/sync/{ws}/* ------------------------------------------------------------------------------------
# spec: valid ✓ | expired 401 | revoked 401 | unbound 403 | wrong ws 403 | deleted ws 410 | not active 409 | web 401
SYNC_ROW = {"valid_active": 200, "expired_access": 401, "revoked": 401, "unbound": 403, "wrong_ws": 403,
            "deleted_ws": 410, "not_active": 409, "web_jwt": 401}
SYNC_ERRORS = {"expired_access": "token_expired", "revoked": "device_revoked", "unbound": "wrong_workspace",
               "wrong_ws": "wrong_workspace", "deleted_ws": "workspace_deleted", "not_active": "not_active_device",
               "web_jwt": "token_invalid"}


def _snapshot_body(clock) -> dict:
    return {"report_type": "trial_balance", "from_date": "01-04-2022", "as_on_date": "31-03-2023",
            "request_flags": {"EXPLODEFLAG": "Yes"}, "purpose": "parity", "captured_at": clock.now().isoformat(),
            "counters": COUNTERS,
            "cells": transcode.report_cells(realdata.read_capture("p18_B_tb_asof_2023-03-31.xml"), "trial_balance")}


async def _sync_call(client, route: str, ws, env: Env, headers: dict, clock):
    base = f"/api/sync/{ws}"
    if route == "heartbeat":
        return await client.post(f"{base}/heartbeat", headers=headers,
                                 json={"tally_status": "ok", "pc_clock": clock.now().isoformat()})
    if route == "state":
        return await client.get(f"{base}/state", headers=headers)
    if route == "runs":
        return await client.post(f"{base}/runs", headers=headers,
                                 json={"kind": "first_sync", "counters_at_start": COUNTERS})
    if route == "batches":
        body = batch(env.run_id, [m for m in realdata.b_masters() if m["kind"] == "currency"])
        return await client.post(f"{base}/batches", content=gz(body), headers={
            **headers, "Content-Encoding": "gzip", "Content-Type": "application/json"})
    if route == "coverage":
        return await client.patch(f"{base}/coverage", headers=headers,
                                  json={"fy_start": "2025-04-01", "month": "2025-04", "run_id": env.run_id})
    if route == "reconcile":
        return await client.post(f"{base}/reconcile", headers=headers, json={
            "run_id": env.run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
            "present": [], "present_count": 0})
    if route == "snapshots":
        return await client.post(f"{base}/snapshots", headers=headers, json=_snapshot_body(clock))
    if route == "parity":
        return await client.post(f"{base}/parity", headers=headers, json={
            "scope": "daily", "as_on_date": "31-03-2026", "capture_started_at": clock.now().isoformat(),
            "counters_before": COUNTERS, "counters_after": COUNTERS})
    if route == "relink":
        return await client.post(f"{base}/relink", headers=headers, json={
            "new_company_guid": NEW_GUID, "company_name": B_BIND["company_name"], "password": PASSWORD})
    raise AssertionError(route)


SYNC_ROUTES = ("heartbeat", "state", "runs", "batches", "coverage", "reconcile", "snapshots", "parity", "relink")


@pytest.mark.parametrize("variant", COLUMNS)
@pytest.mark.parametrize("route", SYNC_ROUTES)
async def test_sync_endpoints_matrix(app_client, session, clock, route, variant):
    env, headers = await setup(app_client, session, clock, variant)
    ws = env.other_ws if variant == "wrong_ws" else env.ws
    r = await _sync_call(app_client, route, ws, env, headers, clock)
    assert r.status_code == SYNC_ROW[variant], (route, variant, r.text)
    if variant != "valid_active":
        assert _error(r) == SYNC_ERRORS[variant], (route, variant, r.text)
    if variant == "deleted_ws":                            # §8.1 check 4: the device is revoked on the way out
        reason = (await session.execute(text("SELECT revoke_reason FROM agent_devices WHERE id = :d"),
                                        {"d": uuid.UUID(env.d1["device_id"])})).scalar_one()
        assert reason == "workspace_deleted"


# --- row 5: web sync-status, commands, devices ------------------------------------------------------------------
# spec: valid active — | expired 401 | revoked — | unbound — | wrong ws 404 (not owner) | deleted ws 404 |
# not active — | web JWT ✓. The four `—` cells are omitted. `expired` = an expired WEB JWT (the column is the web
# caller's token here). `wrong ws` = another user's JWT on this workspace (devices: deleting this user's device with
# another user's JWT). `deleted ws` applies to the workspace-keyed routes only: `/api/devices` names no workspace, so
# that cell is omitted for devices.
WEB_ROW = {"expired_access": 401, "wrong_ws": 404, "deleted_ws": 404, "web_jwt": 200}


def _expired_web(uid) -> dict:
    tok = jwt.encode({"sub": str(uid), "type": "access", "exp": 1}, "w" * 32, algorithm="HS256")
    return bearer(tok)


@pytest.mark.parametrize("route, variant", [(r, v) for r in ("sync_status", "commands", "devices") for v in WEB_ROW
                                             if not (r == "devices" and v == "deleted_ws")])
async def test_web_matrix(app_client, session, clock, route, variant):
    env, _ = await setup(app_client, session, clock, "deleted_ws" if variant == "deleted_ws" else "valid_active")
    if variant == "expired_access":
        headers = _expired_web(env.uid)
    elif variant == "wrong_ws":
        headers = web_headers(await make_user(session))
    else:
        headers = web_headers(env.uid)
    if route == "sync_status":
        r = await app_client.get(f"/api/workspaces/{env.ws}/sync-status", headers=headers)
    elif route == "commands":
        r = await app_client.post(f"/api/workspaces/{env.ws}/sync/commands", json={"type": "recheck_now"},
                                  headers=headers)
    elif variant == "wrong_ws":
        r = await app_client.delete(f"/api/devices/{env.d1['device_id']}", headers=headers)
    else:
        r = await app_client.get("/api/devices", headers=headers)
    assert r.status_code == WEB_ROW[variant], (route, variant, r.text)
    if variant == "expired_access":
        assert _error(r) == "token_expired"
    if r.status_code == 404:
        assert _error(r) in ("workspace_not_found", "device_not_found")




# --- S1 review I8: §14 scenario 17 as specified — another user's BOUND workspace with data, re-read unchanged -------


async def _tenant_rows(engine, ws, uid) -> dict:
    """Every v2 row of ``ws`` (whole rows, not counts) + its owner's devices + the workspace row, from a FRESH
    session."""
    from backend.db.sync_models import V2_TABLES
    from tests.sync.db.ingest_helpers import fresh
    out = {}
    async with fresh(engine) as s:
        for t in V2_TABLES:
            rows = (await s.execute(text(f"SELECT * FROM {t} WHERE workspace_id = :w"), {"w": ws})).mappings().all()
            out[t] = sorted((dict(r) for r in rows), key=repr)
        out["owner_devices"] = [dict(r) for r in (await s.execute(text(
            "SELECT * FROM agent_devices WHERE user_id = :u ORDER BY id"), {"u": uid})).mappings()]
        out["workspace"] = dict((await s.execute(text("SELECT * FROM workspaces WHERE id = :w"), {"w": ws}))
                                .mappings().one())
    return out


async def test_cross_tenant_device_refused_on_every_route_and_other_tenant_unchanged(app_client, session, engine,
                                                                                    clock):
    """§14.17 / R19: D1 (user 1, bound to W1) against W2 — another USER's workspace, BOUND, holding data (masters,
    an open run, a relink prompt) — gets 403 ``wrong_workspace`` on every ``{ws}`` route, the web routes answer 404
    to user 1, and a fresh-session re-read of all of W2's rows is identical before and after."""
    env1, h1 = await setup(app_client, session, clock, "valid_active")           # user 1: D1 bound to W1
    env2, h2 = await setup(app_client, session, clock, "valid_active")           # user 2: D2 bound to W2
    assert env1.uid != env2.uid
    r = await app_client.post(f"/api/sync/{env2.ws}/batches",                    # W2 holds real data
                              content=gz(batch(env2.run_id, realdata.b_masters())),
                              headers={**h2, "Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    before = await _tenant_rows(engine, env2.ws, env2.uid)
    assert before["tally_ledgers"] and before["sync_runs"] and before["sync_batches"]

    for route in SYNC_ROUTES:
        # D1's token, W2's path — and W2's own run id wherever a body names a run.
        r = await _sync_call(app_client, route, env2.ws, env2, h1, clock)
        assert (r.status_code, _error(r)) == (403, "wrong_workspace"), (route, r.text)
    r = await app_client.patch(f"/api/sync/{env2.ws}/runs/{env2.run_id}", headers=h1,
                               json={"status": "failed", "error_code": "unrecoverable"})
    assert (r.status_code, _error(r)) == (403, "wrong_workspace")
    for method, path, body in (("get", f"/api/workspaces/{env2.ws}/sync-status", None),
                               ("post", f"/api/workspaces/{env2.ws}/sync/commands", {"type": "recheck_now"}),
                               ("post", f"/api/workspaces/{env2.ws}/sync/commands",
                                {"type": "confirm_resync", "scope": "company"}),
                               ("post", f"/api/workspaces/{env2.ws}/sync/commands",
                                {"type": "confirm_relink", "password": PASSWORD})):
        r = await getattr(app_client, method)(path, headers=web_headers(env1.uid),
                                              **({"json": body} if body else {}))
        assert (r.status_code, _error(r)) == (404, "workspace_not_found"), (path, r.text)
    r = await app_client.delete(f"/api/devices/{env2.d1['device_id']}", headers=web_headers(env1.uid))
    assert r.status_code == 404, r.text

    assert await _tenant_rows(engine, env2.ws, env2.uid) == before
