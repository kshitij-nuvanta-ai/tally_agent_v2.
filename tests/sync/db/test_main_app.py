"""The one app on the one engine, with a real database (v2 merge M1, M3, M7, M9): ``backend.main``'s app, started
through its own lifespan, serves the pre-existing web routes and the sync/agent routes side by side. Each keeps its
own error shape, also for a database error."""
from __future__ import annotations

import logging
import traceback

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.engine import get_db
from backend.sync.errors import SyncRoute
from tests.sync.conftest import TEST_EMAIL_DOMAIN, make_user, make_workspace, requires_db, serve_main

pytestmark = requires_db

PASSWORD = "Passw0rd!Passw0rd"
SECRET = "NARRATION-Being-paid-to-Sharma-Traders-123456.78"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_one_app_serves_web_and_agent_routes_with_one_auth(engine, session, monkeypatch):
    email = f"oneapp{TEST_EMAIL_DOMAIN}"
    uid = await make_user(session, email=email, password=PASSWORD)
    ws = await make_workspace(session, uid, name="Books")

    async with serve_main(monkeypatch) as c:
        # pre-existing web routes
        r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        web = _bearer(r.json()["access_token"])
        me = await c.get("/api/auth/me", headers=web)
        assert me.status_code == 200 and (me.json()["id"], me.json()["email"]) == (str(uid), email)
        mine = await c.get("/api/workspaces", headers=web)
        assert mine.status_code == 200 and str(ws) in mine.text

        # agent routes, same app, same engine
        r = await c.post("/api/agent/auth/login", json={"email": email, "password": PASSWORD,
                                                        "device_name": "ACCOUNTS-PC", "agent_version": "0.1.0"})
        assert r.status_code == 200, r.text
        device_id, device = r.json()["device_id"], _bearer(r.json()["access_token"])
        r = await c.get("/api/agent/workspaces", headers=device)
        assert r.status_code == 200 and [w["id"] for w in r.json()] == [str(ws)]

        # one auth (M7): the token the WEB login issued is what the sync web routes accept
        r = await c.get("/api/devices", headers=web)
        assert r.status_code == 200 and [d["id"] for d in r.json()] == [device_id]

        # two error shapes (M9), each route family keeps its own
        r = await c.get("/api/workspaces")
        assert (r.status_code, r.json()) == (401, {"detail": "Authentication required"})
        r = await c.get("/api/devices")
        assert (r.status_code, r.json()) == (401, {"error": "token_invalid", "detail": ""})
        # a device token is not a web token, on either family (D6: separate secrets)
        r = await c.get("/api/workspaces", headers=device)
        assert (r.status_code, r.json()) == (401, {"detail": "Invalid or expired token"})
        r = await c.get("/api/devices", headers=device)
        assert (r.status_code, r.json()) == (401, {"error": "token_invalid", "detail": ""})
        # ... and a web token is not a device token
        r = await c.get("/api/agent/workspaces", headers=web)
        assert (r.status_code, r.json()["error"]) == (401, "token_invalid")

        gone = await c.get("/api/v2/health")
        assert (gone.status_code, gone.json()) == (404, {"detail": "Not Found"})

    async with engine.connect() as conn:                       # the device row really is in the one database
        n = (await conn.execute(text("SELECT count(*) FROM agent_devices WHERE user_id = :u"), {"u": uid})).scalar_one()
    assert n == 1


async def test_without_a_device_secret_the_rest_of_the_app_works(engine, session, monkeypatch, caplog):
    email = f"nosync{TEST_EMAIL_DOMAIN}"
    uid = await make_user(session, email=email, password=PASSWORD)
    ws = await make_workspace(session, uid)
    caplog.set_level(logging.WARNING)

    async with serve_main(monkeypatch, device_secret="") as c:
        r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        web = _bearer(r.json()["access_token"])
        assert (await c.get("/api/auth/me", headers=web)).json()["email"] == email
        mine = await c.get("/api/workspaces", headers=web)
        assert mine.status_code == 200 and str(ws) in mine.text
        convs = await c.get(f"/api/workspaces/{ws}/conversations", headers=web)
        assert convs.status_code == 200

        for method, path in (("POST", "/api/agent/auth/login"), ("GET", "/api/agent/workspaces"),
                             ("GET", "/api/devices"), ("POST", "/api/sync/company"),
                             ("GET", f"/api/workspaces/{ws}/sync-status"),
                             ("POST", f"/api/workspaces/{ws}/sync/commands")):
            r = await c.request(method, path, headers=web, json={})
            assert (r.status_code, r.json()) == (404, {"detail": "Not Found"}), (method, path)

        # web login is still throttled: the login limiter does not depend on the sync routes (M8)
        for _ in range(5):
            r = await c.post("/api/auth/login", json={"email": email, "password": "wrong-password"})
            assert r.status_code == 401
        r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert (r.status_code, r.json()) == (429, {"detail": "Too many login attempts. Try again later."})

    disabled = [rec.getMessage() for rec in caplog.records if "Agent sync is disabled" in rec.getMessage()]
    assert len(disabled) == 1


async def test_a_database_error_keeps_each_route_familys_own_answer(engine, monkeypatch, caplog):
    """M9: the 500 ``internal_error`` answer (and class-only logging) for a database error is scoped to the sync
    routes by their route class. The same failing statement on a route of any other router is answered exactly as
    before the merge: the app's generic handler, ``{"error": "Internal server error", "detail": null}``."""
    async def _boom(session: AsyncSession = Depends(get_db)) -> dict:
        await session.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
        return {}

    plain, sync_like = APIRouter(), APIRouter(route_class=SyncRoute)
    plain.add_api_route("/api/_test_plain_db_boom", _boom)
    sync_like.add_api_route("/api/sync/_test_db_boom", _boom)

    async with serve_main(monkeypatch, raise_app_exceptions=False) as c:
        c.app.include_router(plain)
        c.app.include_router(sync_like)

        caplog.set_level(logging.DEBUG)
        r = await c.get("/api/sync/_test_db_boom")
        assert (r.status_code, r.json()) == (500, {"error": "internal_error", "detail": ""})
        logged = caplog.text + "".join(
            "".join(traceback.format_exception(rec.exc_info[1])) for rec in caplog.records if rec.exc_info)
        assert SECRET[:20] not in logged and "sync db error" in caplog.text
        assert "Unhandled exception" not in caplog.text          # it never reached the generic handler

        caplog.clear()
        r = await c.get("/api/_test_plain_db_boom")
        assert (r.status_code, r.json()) == (500, {"error": "Internal server error", "detail": None})
        assert "Unhandled exception" in caplog.text and "sync db error" not in caplog.text
