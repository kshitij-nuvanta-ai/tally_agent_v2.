"""Restart round-trips (CLAUDE.md "Test reality" rule 7, adapted per S1 spec §16: there is no page to refresh, so
after every state-changing call the test disposes the app's engine, builds a NEW ``create_app`` on the same DB and
re-asserts the WHOLE user-visible state -- ``GET /state`` (device), ``GET sync-status`` (web) and direct row counts of
every v2 table for the workspace -- not only the attribute the call changed).

Each test: act -> ``whole()`` on the old app -> ``restart()`` -> ``whole()`` on the new app -> equal, plus the exact
expected values, plus exactly one row / message / command per action.
"""
from __future__ import annotations

import copy
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from v2.cloud.main import create_app
from v2.cloud.models import V2_TABLES
from v2.tests.cloud import realdata
from v2.tests.cloud.conftest import requires_db, web_headers
from v2.tests.cloud.db.ingest_helpers import B_BIND, COUNTERS, batch, bind, fresh, open_run, post_batch
from v2.tests.cloud.db.test_end_to_end_fakeb import FY25, FY26, first_sync, fb_session_fake

pytestmark = requires_db

NEW_GUID = "new-guid-0000"


@pytest.fixture
async def restart(settings, clock):
    made: list[httpx.AsyncClient] = []

    async def _restart(client: httpx.AsyncClient) -> httpx.AsyncClient:
        await client.app.state.engine.dispose()
        app = create_app(settings, clock)
        c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2")
        c2.app = app
        made.append(c2)
        return c2

    yield _restart
    for c in made:
        await c.aclose()
        await c.app.state.engine.dispose()


async def whole(client, engine, ws, headers: dict, uid) -> dict:
    """Everything a user or the agent can see about ``ws``, read through a FRESH session / the given app."""
    st = await client.get(f"/api/sync/{ws}/state", headers=headers)
    ss = await client.get(f"/api/workspaces/{ws}/sync-status", headers=web_headers(uid))
    assert ss.status_code == 200, ss.text
    async with fresh(engine) as s:
        counts = {t: (await s.execute(text(f"SELECT count(*) FROM {t} WHERE workspace_id = :w"), {"w": ws}))
                  .scalar_one() for t in V2_TABLES}
        sw = dict((await s.execute(text("SELECT * FROM sync_workspaces WHERE workspace_id = :w"), {"w": ws}))
                  .mappings().one())
        devices = [dict(r) for r in (await s.execute(text(
            "SELECT id, device_name, workspace_id, is_active, revoked_at, revoke_reason FROM agent_devices "
            "WHERE user_id = :u ORDER BY created_at, id"), {"u": uid})).mappings()]
        commands = [dict(r) for r in (await s.execute(text(
            "SELECT id, type, status FROM sync_commands WHERE workspace_id = :w ORDER BY created_at, id"),
            {"w": ws})).mappings()]
        runs = [dict(r) for r in (await s.execute(text(
            "SELECT id, kind, status, cursor_after FROM sync_runs WHERE workspace_id = :w ORDER BY started_at, id"),
            {"w": ws})).mappings()]
    return {"state_status": st.status_code, "state": st.json(), "status": ss.json(), "counts": counts, "sw": sw,
            "devices": devices, "commands": commands, "runs": runs}


async def roundtrip(client, restart, engine, ws, headers, uid) -> tuple[httpx.AsyncClient, dict]:
    before = await whole(client, engine, ws, headers, uid)
    c2 = await restart(client)
    after = await whole(c2, engine, ws, headers, uid)
    assert after == before
    return c2, after


def cov(after: dict) -> dict[str, tuple]:
    return {r["fy_start"]: (r["state"], r["months_complete"]) for r in after["state"]["coverage"]}


PENDING_5 = {f"{y}-04-01": ("pending", 0) for y in range(2022, 2027)}


async def _uid_of(session, ws) -> uuid.UUID:
    return (await session.execute(text("SELECT user_id FROM workspaces WHERE id = :w"), {"w": ws})).scalar_one()


# --- bind / take-over -------------------------------------------------------------------------------------------


async def test_restart_after_bind(app_client, session, engine, restart):
    uid, ws, headers = await bind(app_client, session)
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    st = after["state"]
    assert (after["state_status"], st["sync_state"], st["cursors"], st["books_from"], st["base_currency_name"],
            st["open_runs"], st["commands"]) == (200, "awaiting_first_connection",
                                                 {"alt_vch_id": None, "alt_mst_id": None}, "2022-04-01", "INR", [], [])
    assert cov(after) == PENDING_5
    assert after["status"]["agent"]["device_name"] == "ACCOUNTS-PC" and after["status"]["last_parity"] is None
    assert after["sw"]["tally_company_guid"] == realdata.COMPANY_B_GUID
    assert [(d["is_active"], d["revoked_at"]) for d in after["devices"]] == [(True, None)]
    assert after["counts"] == {**{t: 0 for t in V2_TABLES}, "sync_workspaces": 1, "agent_devices": 1,
                               "sync_fy_coverage": 5}
    # the re-bind on the new app is a no-op: no second row of anything
    r = await c2.post("/api/sync/company", json={**B_BIND, "workspace_id": str(ws)}, headers=headers)
    assert r.status_code == 200, r.text
    assert (await whole(c2, engine, ws, headers, uid))["counts"] == after["counts"]


async def test_restart_after_takeover(app_client, session, engine, restart):
    uid, ws, h1 = await bind(app_client, session)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await app_client.post("/api/agent/auth/login", json={"email": email, "password": "Passw0rd!Passw0rd",
                                                             "device_name": "LAPTOP", "agent_version": "0.1.0"})
    h2 = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await app_client.post("/api/sync/company", json={**B_BIND, "workspace_id": str(ws), "takeover": True},
                              headers=h2)
    assert r.status_code == 200, r.text
    c2, after = await roundtrip(app_client, restart, engine, ws, h2, uid)
    assert after["state_status"] == 200 and after["status"]["agent"]["device_name"] == "LAPTOP"
    assert [(d["device_name"], d["is_active"], d["revoke_reason"]) for d in after["devices"]] == \
        [("ACCOUNTS-PC", False, "taken_over"), ("LAPTOP", True, None)]
    assert after["sw"]["active_device_id"] == after["devices"][1]["id"]
    old = await c2.get(f"/api/sync/{ws}/state", headers=h1)                  # the old device stays revoked
    assert (old.status_code, old.json()["error"]) == (401, "device_revoked")
    assert after["counts"]["agent_devices"] == 2 and after["counts"]["sync_workspaces"] == 1


# --- ingest --------------------------------------------------------------------------------------------------------


async def test_restart_after_batch_and_replay_still_replays(app_client, session, engine, restart):
    uid, ws, headers = await bind(app_client, session)
    run_id = await open_run(app_client, ws, headers)
    body = batch(run_id, realdata.b_masters())
    r1 = await post_batch(app_client, ws, headers, body)
    assert r1.status_code == 200 and r1.json()["replayed"] is False, r1.text
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    masters_n = len(realdata.b_masters())
    assert r1.json()["counts"]["inserted"] == masters_n
    assert sum(after["counts"][t] for t in ("tally_currencies", "tally_groups", "tally_voucher_types",
                                            "tally_units", "tally_ledgers", "tally_stock_items")) == masters_n
    assert after["counts"]["sync_batches"] == 1 and after["counts"]["sync_runs"] == 1
    assert after["state"]["sync_state"] == "first_sync" and len(after["state"]["open_runs"]) == 1
    assert after["status"]["last_synced_at"] is not None
    r2 = await post_batch(c2, ws, headers, body)                              # the replay survives the restart
    assert r2.status_code == 200, r2.text
    assert r2.json() == {**r1.json(), "replayed": True}
    assert (await whole(c2, engine, ws, headers, uid))["counts"] == after["counts"]


async def test_restart_after_coverage_ack(app_client, session, engine, restart):
    uid, ws, headers = await bind(app_client, session)
    run_id = await open_run(app_client, ws, headers)
    r = await app_client.patch(f"/api/sync/{ws}/coverage", headers=headers,
                               json={"fy_start": "2025-04-01", "month": "2025-04", "run_id": run_id})
    assert r.status_code == 200, r.text
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    assert cov(after) == {**PENDING_5, "2025-04-01": ("running", 1)}
    assert after["sw"]["oldest_complete_fy"] is None
    r = await c2.patch(f"/api/sync/{ws}/coverage", headers=headers,          # the same ack again: counted once
                       json={"fy_start": "2025-04-01", "month": "2025-04", "run_id": run_id})
    assert r.status_code == 200, r.text
    assert cov(await whole(c2, engine, ws, headers, uid)) == cov(after)


async def _complete_empty_first_sync(client, ws, headers) -> str:
    run_id = await open_run(client, ws, headers)
    r = await client.get(f"/api/sync/{ws}/state", headers=headers)
    totals = {c["fy_start"]: c["months_total"] for c in r.json()["coverage"]}
    for fy in (FY25, FY26):
        y, m = fy.year, 4
        for _ in range(totals[fy.isoformat()]):
            r = await client.patch(f"/api/sync/{ws}/coverage", headers=headers,
                                   json={"fy_start": fy.isoformat(), "month": f"{y:04d}-{m:02d}", "run_id": run_id})
            assert r.status_code == 200, r.text
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    r = await client.patch(f"/api/sync/{ws}/runs/{run_id}", headers=headers, json={
        "status": "completed", "progress_done": 18, "progress_total": 18, "batches_declared": 0})
    assert r.status_code == 200, r.text
    return run_id


async def test_restart_after_run_completion_cursor_persisted(app_client, session, engine, restart):
    uid, ws, headers = await bind(app_client, session)
    run_id = await _complete_empty_first_sync(app_client, ws, headers)
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    assert (after["state"]["sync_state"], after["state"]["cursors"], after["state"]["open_runs"]) == \
        ("ready", COUNTERS, [])
    assert cov(after) == {**PENDING_5, "2025-04-01": ("complete", 12), "2026-04-01": ("complete", 6)}
    assert [(r["id"], r["kind"], r["status"], r["cursor_after"]) for r in after["runs"]] == \
        [(uuid.UUID(run_id), "first_sync", "completed", COUNTERS)]
    assert (after["status"]["sync_state"], after["status"]["first_sync"],
            after["status"]["backfill"]["oldest_complete_fy"]) == ("ready", None, "2025-04-01")
    assert after["sw"]["cursor_set_at"] is not None


async def test_restart_after_restore_detected(app_client, session, engine, restart, clock):
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    below = {"alt_vch_id": COUNTERS["alt_vch_id"] - 1, "alt_mst_id": COUNTERS["alt_mst_id"]}
    r = await app_client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "ours", "pc_clock": clock.now().isoformat(), "counters": below,
        "seen_company": {"guid": realdata.COMPANY_B_GUID, "name": B_BIND["company_name"]}})
    assert r.status_code == 200, r.text
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    assert after["state"]["sync_state"] == "restore_detected" and after["state"]["cursors"] == COUNTERS
    assert (after["status"]["sync_state"], after["status"]["restore_reason"], after["status"]["resync_offered"]) == \
        ("restore_detected", "counters_backwards", {"scope": "company", "reason": "restore"})
    r = await c2.post(f"/api/sync/{ws}/runs", headers=headers,              # still enforced on the new app
                      json={"kind": "incremental", "counters_at_start": below})
    assert (r.status_code, r.json()["error"]) == (409, "restore_detected")
    assert after["counts"]["sync_runs"] == 1


async def test_restart_after_relink(app_client, session, engine, restart, clock):
    uid, ws, headers = await bind(app_client, session)
    r = await app_client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "other_company_same_name", "pc_clock": clock.now().isoformat(),
        "seen_company": {"guid": NEW_GUID, "name": B_BIND["company_name"]}})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{ws}/relink", headers=headers, json={
        "new_company_guid": NEW_GUID, "company_name": B_BIND["company_name"], "password": "Passw0rd!Passw0rd"})
    assert r.status_code == 200, r.text
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    sw = after["sw"]
    assert (sw["tally_company_guid"], sw["previous_company_guids"], sw["relink_prompt"], sw["restore_reason"]) == \
        (NEW_GUID, [realdata.COMPANY_B_GUID], None, "relink")
    assert after["status"]["relink_prompt"] is None and after["status"]["restore_reason"] == "relink"
    assert after["status"]["resync_offered"]["reason"] == "relink"
    assert after["state"]["sync_state"] == r.json()["sync_state"]
    assert after["counts"]["sync_workspaces"] == 1


# --- parity / quarantine / commands ---------------------------------------------------------------------------------


async def test_restart_after_parity_ladder_state(app_client, session, engine, restart):
    fb = fb_session_fake()
    gst_sale = next(v for v in fb.month_vouchers(FY25, "2025-06")
                    if v["data"]["iscancelled"] == "No" and v["data"]["isoptional"] == "No"
                    and {"Domestic Sales", "Output CGST"} <= {e["ledgername"] for e in v["data"]["ledger_entries"]})
    omit = gst_sale["data"]["guid"]
    real_month_vouchers = fb.month_vouchers
    fb.month_vouchers = lambda fy, month: [v for v in real_month_vouchers(fy, month) if v["data"]["guid"] != omit]
    flow = await first_sync(app_client, session, fb)
    res = await flow.parity()
    assert res["status"] == "suspect", res
    c2, after = await roundtrip(app_client, restart, engine, flow.ws, flow.headers, flow.uid)
    assert after["sw"]["ladder"]["state"] == "suspect"
    assert after["status"]["last_parity"]["state"] == "ok"                     # suspect is invisible
    assert after["status"]["last_parity"]["mismatch_count"] == 0
    assert after["counts"]["parity_runs"] == 1 and after["counts"]["parity_lines"] > 0
    flow.client = c2
    again = await flow.parity(remediation_done=[r["id"] for r in res["remediation"]])
    assert again["status"] == "alert" and again["ladder"] == {"state": "alert", "heal_attempts": 1,
                                                              "resync_offered_fy": None}
    later = await whole(c2, engine, flow.ws, flow.headers, flow.uid)
    assert later["counts"]["parity_runs"] == 2 and later["status"]["last_parity"]["state"] == "alert"


async def test_restart_after_quarantine_count(app_client, session, engine, restart):
    uid, ws, headers = await bind(app_client, session)
    run_id = await open_run(app_client, ws, headers)
    assert (await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters()))).status_code == 200
    good = realdata.vouchers("p21_B_fy2022_month_09.xml")[:10]
    bad = copy.deepcopy(good[4])
    entry = bad["data"]["ledger_entries"][0]
    entry["amount"] = str(Decimal(entry["amount"]) + Decimal("0.01"))
    q = [{"kind": "voucher", "guid": bad["data"]["guid"], "code": "unbalanced_voucher",
          "voucher_date": bad["data"]["date"]}]
    r = await post_batch(app_client, ws, headers, batch(run_id, good[:4] + good[5:], quarantine=q))
    assert r.status_code == 200, r.text
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    assert after["status"]["quarantine_count"] == 1 and after["sw"]["quarantine_count"] == 1
    assert (after["counts"]["sync_quarantine"], after["counts"]["tally_vouchers"], after["counts"]["sync_batches"]) \
        == (1, 9, 2)


async def test_restart_after_command_delivery_not_redelivered(app_client, session, engine, restart, clock):
    uid, ws, headers = await bind(app_client, session)
    r = await app_client.post(f"/api/workspaces/{ws}/sync/commands", json={"type": "recheck_now"},
                              headers=web_headers(uid))
    assert r.status_code == 200, r.text
    cmd_id = r.json()["id"]
    hb = {"tally_status": "ok", "pc_clock": clock.now().isoformat()}
    first = await app_client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json=hb)
    assert first.json()["commands"] == [{"id": cmd_id, "type": "recheck_now", "params": {}}]
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    assert [(str(c["id"]), c["type"], c["status"]) for c in after["commands"]] == [(cmd_id, "recheck_now",
                                                                                    "delivered")]
    assert after["state"]["commands"] == []                                     # only `pending` ones are listed
    second = await c2.post(f"/api/sync/{ws}/heartbeat", headers=headers, json=hb)
    assert second.status_code == 200 and second.json()["commands"] == []       # not redelivered after the restart
    later = await whole(c2, engine, ws, headers, uid)
    assert later["commands"] == after["commands"] and later["counts"]["sync_commands"] == 1

