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


BOUND = {**{t: 0 for t in V2_TABLES}, "sync_workspaces": 1, "agent_devices": 1, "sync_fy_coverage": 5}
MASTER_TABLES = {"currency": "tally_currencies", "group": "tally_groups", "voucher_type": "tally_voucher_types",
                 "unit": "tally_units", "stock_group": "tally_stock_groups", "ledger": "tally_ledgers",
                 "stock_item": "tally_stock_items"}


def data_counts(posted: list[dict]) -> dict[str, int]:
    """The rows the accepted wire objects must have produced -- one per master / voucher / ledger line / bill."""
    out = {t: 0 for t in MASTER_TABLES.values()}
    vouchers = {}
    for o in posted:
        if o["kind"] in MASTER_TABLES:
            out[MASTER_TABLES[o["kind"]]] += 1
        elif o["kind"] == "voucher":
            vouchers[o["data"]["guid"]] = o["data"]
    out["tally_vouchers"] = len(vouchers)
    out["tally_voucher_ledger_lines"] = sum(len(v.get("ledger_entries", [])) for v in vouchers.values())
    out["tally_bill_allocations"] = sum(len(e.get("bill_allocations", [])) for v in vouchers.values()
                                        for e in v.get("ledger_entries", []))
    out["tally_voucher_inventory_lines"] = sum(len(v.get("inventory_entries", [])) for v in vouchers.values())
    return out


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
    assert after["counts"] == {**BOUND, "agent_devices": 2}                    # no run, command or coverage row


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
    assert after["counts"] == {**BOUND, "sync_runs": 1}                        # the ack adds no row
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
    assert after["counts"] == {**BOUND, "sync_runs": 1}                        # detection writes no command/run


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
    assert after["counts"] == BOUND                                            # relink queues no command or run


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
    async with fresh(engine) as s:
        lines_compared = (await s.execute(text("SELECT lines_compared FROM parity_runs WHERE workspace_id = :w"),
                                          {"w": flow.ws})).scalar_one()
    assert lines_compared > 0
    assert after["counts"] == {**BOUND, **data_counts(flow.posted), "sync_runs": 1, "sync_batches": flow.batches,
                               "tally_report_snapshots": 3, "parity_runs": 1, "parity_lines": lines_compared}
    flow.client = c2
    again = await flow.parity(remediation_done=[r["id"] for r in res["remediation"]])
    assert again["status"] == "alert" and again["ladder"] == {"state": "alert", "heal_attempts": 1,
                                                              "resync_offered_fy": None}
    later = await whole(c2, engine, flow.ws, flow.headers, flow.uid)
    assert later["status"]["last_parity"]["state"] == "alert"
    # exactly one more run, comparing the same ledgers/groups again; nothing else written
    assert later["counts"] == {**after["counts"], "parity_runs": 2, "parity_lines": 2 * lines_compared}


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



# --- S1 review I3 / I4: the D16 restore -> confirm -> deliver -> ack -> resync path, and its resolution -------------


async def _restore_detected(client, ws, headers, clock) -> dict:
    below = {"alt_vch_id": COUNTERS["alt_vch_id"] - 1, "alt_mst_id": COUNTERS["alt_mst_id"]}
    r = await client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "ours", "pc_clock": clock.now().isoformat(), "counters": below,
        "seen_company": {"guid": realdata.COMPANY_B_GUID, "name": B_BIND["company_name"]}})
    assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text
    return below


async def _hb(client, ws, headers, clock, acked=()):
    r = await client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "closed", "pc_clock": clock.now().isoformat(), "acked_commands": list(acked)})
    assert r.status_code == 200, r.text
    return r.json()


async def _confirm(client, ws, uid) -> str:
    r = await client.post(f"/api/workspaces/{ws}/sync/commands", headers=web_headers(uid),
                          json={"type": "confirm_resync", "scope": "company"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _cmd_status(engine, cmd_id) -> str:
    async with fresh(engine) as s:
        return (await s.execute(text("SELECT status FROM sync_commands WHERE id = :i"), {"i": cmd_id})).scalar_one()


async def _run_company_resync(client, ws, headers, cmd_id, counters) -> str:
    r = await client.post(f"/api/sync/{ws}/runs", headers=headers, json={
        "kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id, "counters_at_start": counters})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    for c in r.json()["coverage"]:                        # re-ack every month of every FY (whole-company pass)
        y, m = int(c["fy_start"][:4]), int(c["fy_start"][5:7])
        for _ in range(c["months_total"]):
            rr = await client.patch(f"/api/sync/{ws}/coverage", headers=headers,
                                    json={"fy_start": c["fy_start"], "month": f"{y:04d}-{m:02d}", "run_id": run_id})
            assert rr.status_code == 200, rr.text
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    r = await client.patch(f"/api/sync/{ws}/runs/{run_id}", headers=headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0})
    assert r.status_code == 200, r.text
    return run_id


async def test_acked_confirm_resync_still_opens_the_resync_and_completion_marks_it_done(
        app_client, session, engine, clock):
    """I3: a heartbeat ack means "received", not "executed" — acking a delivered `confirm_resync` must leave it
    usable for the `full_resync` it authorises; only that run's completion closes it (`done`)."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    below = await _restore_detected(app_client, ws, headers, clock)
    cmd_id = await _confirm(app_client, ws, uid)
    assert [c["id"] for c in (await _hb(app_client, ws, headers, clock))["commands"]] == [cmd_id]
    assert (await _hb(app_client, ws, headers, clock, acked=[cmd_id]))["commands"] == []
    assert await _cmd_status(engine, cmd_id) == "delivered"          # acked, still open
    run_id = await _run_company_resync(app_client, ws, headers, cmd_id, below)
    assert await _cmd_status(engine, cmd_id) == "done"
    async with fresh(engine) as s:
        run = (await s.execute(text("SELECT status, command_id FROM sync_runs WHERE id = :i"), {"i": run_id})).one()
    assert (run.status, run.command_id) == ("completed", uuid.UUID(cmd_id))


async def test_acked_recheck_now_still_goes_done(app_client, session, engine, clock):
    """I3: other command kinds keep ack -> done."""
    uid, ws, headers = await bind(app_client, session)
    r = await app_client.post(f"/api/workspaces/{ws}/sync/commands", headers=web_headers(uid),
                              json={"type": "recheck_now"})
    cmd_id = r.json()["id"]
    await _hb(app_client, ws, headers, clock)
    await _hb(app_client, ws, headers, clock, acked=[cmd_id])
    assert await _cmd_status(engine, cmd_id) == "done"


async def test_superseding_confirm_resync_cancels_the_open_one(app_client, session, engine, clock):
    """I3: a newer user confirm supersedes an open (pending/delivered, not running) `confirm_resync`."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    below = await _restore_detected(app_client, ws, headers, clock)
    old = await _confirm(app_client, ws, uid)
    await _hb(app_client, ws, headers, clock)
    await _hb(app_client, ws, headers, clock, acked=[old])
    new = await _confirm(app_client, ws, uid)
    assert await _cmd_status(engine, old) == "cancelled"
    r = await app_client.post(f"/api/sync/{ws}/runs", headers=headers, json={
        "kind": "full_resync", "scope": {"company": True}, "command_id": old, "counters_at_start": below})
    assert (r.status_code, r.json()["error"]) == (409, "resync_not_confirmed")
    await _run_company_resync(app_client, ws, headers, new, below)
    assert (await _cmd_status(engine, old), await _cmd_status(engine, new)) == ("cancelled", "done")


@pytest.mark.parametrize("cause", ["restore", "relink"])
async def test_restart_after_confirmed_resync_resolves_restore_or_relink(app_client, session, engine, restart, clock,
                                                                         cause):
    """I4: once the confirmed whole-company resync completes, `restore_reason` and the restore/relink resync offer
    are cleared (and stay cleared across a restart) — sync-status no longer offers a resync already done; any other
    open `confirm_resync` is cancelled."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    if cause == "restore":
        counters = await _restore_detected(app_client, ws, headers, clock)
    else:
        r = await app_client.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
            "tally_status": "other_company_same_name", "pc_clock": clock.now().isoformat(),
            "seen_company": {"guid": NEW_GUID, "name": B_BIND["company_name"]}})
        assert r.status_code == 200, r.text
        r = await app_client.post(f"/api/sync/{ws}/relink", headers=headers, json={
            "new_company_guid": NEW_GUID, "company_name": B_BIND["company_name"], "password": "Passw0rd!Passw0rd"})
        assert r.status_code == 200, r.text
        counters = {"alt_vch_id": 3, "alt_mst_id": 3}
    status = (await app_client.get(f"/api/workspaces/{ws}/sync-status", headers=web_headers(uid))).json()
    assert status["resync_offered"] == {"scope": "company", "reason": cause}
    cmd_id = await _confirm(app_client, ws, uid)
    await _hb(app_client, ws, headers, clock)
    await _hb(app_client, ws, headers, clock, acked=[cmd_id])
    await _run_company_resync(app_client, ws, headers, cmd_id, counters)
    c2, after = await roundtrip(app_client, restart, engine, ws, headers, uid)
    st = after["status"]
    assert (st["sync_state"], st["restore_reason"], st["resync_offered"], st["relink_prompt"]) == \
        ("ready", None, None, None)
    assert "resync_offered" not in (after["sw"]["ladder"] or {})
    assert after["state"]["cursors"] == counters and after["state"]["commands"] == []
    assert [(c["type"], c["status"]) for c in after["commands"]] == [("confirm_resync", "done")]
    assert all(r["status"] == "completed" for r in after["runs"])


async def test_confirm_issued_during_the_running_resync_is_cancelled_on_completion(app_client, session, engine, clock):
    """I4: a confirm the user clicks WHILE the whole-company resync runs is pending (the running one's command is in
    use, so it isn't superseded); once the resync completes there is nothing left to resync, so it is cancelled —
    never delivered afterwards to trigger a second full re-read."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    below = await _restore_detected(app_client, ws, headers, clock)
    first = await _confirm(app_client, ws, uid)
    r = await app_client.post(f"/api/sync/{ws}/runs", headers=headers, json={
        "kind": "full_resync", "scope": {"company": True}, "command_id": first, "counters_at_start": below})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    second = await _confirm(app_client, ws, uid)
    assert (await _cmd_status(engine, first), await _cmd_status(engine, second)) == ("pending", "pending")
    r = await app_client.patch(f"/api/sync/{ws}/runs/{run_id}", headers=headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0})
    assert r.status_code == 200, r.text
    assert (await _cmd_status(engine, first), await _cmd_status(engine, second)) == ("done", "cancelled")
    assert (await _hb(app_client, ws, headers, clock))["commands"] == []


# --- Task 14b C3: a confirm supersedes open confirms of the SAME or a NARROWER scope only -------------------------


async def _confirm_fy(client, ws, uid, fy) -> str:
    r = await client.post(f"/api/workspaces/{ws}/sync/commands", headers=web_headers(uid),
                          json={"type": "confirm_resync", "scope": "fy", "fy_start": fy.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_fy_confirm_leaves_an_open_company_confirm_usable(app_client, session, engine, clock):
    """C3: after a restore the user confirms the company resync (delivered + acked), then an FY-scoped confirm —
    the narrower FY confirm must NOT cancel the broader company one; the agent's company `full_resync` still
    opens with it and resolves the restore."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    below = await _restore_detected(app_client, ws, headers, clock)
    company = await _confirm(app_client, ws, uid)
    await _hb(app_client, ws, headers, clock)
    await _hb(app_client, ws, headers, clock, acked=[company])
    fy = await _confirm_fy(app_client, ws, uid, FY25)
    assert (await _cmd_status(engine, company), await _cmd_status(engine, fy)) == ("delivered", "pending")
    await _run_company_resync(app_client, ws, headers, company, below)
    # the company resync is the widest re-read: completing it closes the FY confirm too (I4)
    assert (await _cmd_status(engine, company), await _cmd_status(engine, fy)) == ("done", "cancelled")


async def test_company_confirm_cancels_open_fy_confirms(app_client, session, engine, clock):
    """C3: a company confirm supersedes every open FY confirm (company ⊇ any FY)."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    fy25, fy26 = await _confirm_fy(app_client, ws, uid, FY25), await _confirm_fy(app_client, ws, uid, FY26)
    await _hb(app_client, ws, headers, clock)                           # fy25 + fy26 delivered
    company = await _confirm(app_client, ws, uid)
    assert [await _cmd_status(engine, c) for c in (fy25, fy26, company)] == ["cancelled", "cancelled", "pending"]


async def test_fy_confirm_supersedes_the_same_fy_only(app_client, session, engine, clock):
    """C3: FY X supersedes an open FY X confirm, never an FY Y one."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    fy25, fy26 = await _confirm_fy(app_client, ws, uid, FY25), await _confirm_fy(app_client, ws, uid, FY26)
    again = await _confirm_fy(app_client, ws, uid, FY25)
    assert [await _cmd_status(engine, c) for c in (fy25, fy26, again)] == ["cancelled", "pending", "pending"]


async def test_a_running_resyncs_command_is_never_cancelled_by_a_wider_confirm(app_client, session, engine, clock):
    """C3: an FY resync is running on its command; a company confirm (wider) must not cancel that in-use
    command — the running run completes normally and marks it `done`."""
    uid, ws, headers = await bind(app_client, session)
    await _complete_empty_first_sync(app_client, ws, headers)
    fy = await _confirm_fy(app_client, ws, uid, FY25)
    r = await app_client.post(f"/api/sync/{ws}/runs", headers=headers, json={
        "kind": "full_resync", "scope": {"fy_start": FY25.isoformat()}, "command_id": fy,
        "counters_at_start": COUNTERS})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    company = await _confirm(app_client, ws, uid)
    assert (await _cmd_status(engine, fy), await _cmd_status(engine, company)) == ("pending", "pending")
    r = await app_client.patch(f"/api/sync/{ws}/runs/{run_id}", headers=headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0})
    assert r.status_code == 200, r.text
    assert (await _cmd_status(engine, fy), await _cmd_status(engine, company)) == ("done", "pending")
