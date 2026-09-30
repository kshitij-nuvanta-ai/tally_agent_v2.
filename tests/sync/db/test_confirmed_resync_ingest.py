"""Task 14b C2 (controller ruling, Task 13 re-review out-of-scope note): inside a CONFIRMED ``full_resync`` run
(whole-company or single-FY) the §12 step 9 / step 12 alter_id rule is suspended -- Tally is authoritative after a
restore / relink, which is what the user confirmed -- so an incoming master or voucher replaces the stored row even
at a LOWER ``alter_id`` and is counted ``updated``, never ``skipped_older``. Every other run kind keeps the rule.

Post-backup objects that no longer exist in the restored Tally are not deleted by ingest: the existing §7.11
reconcile path soft-deletes them (GUID absent from the agent's ``present`` list), which this file also proves.

Data: FakeBooks company B through the real endpoints (``test_parity_api.setup_b``). Every assertion re-reads from a
fresh session (§14).
"""
from __future__ import annotations

import copy
from datetime import date

from tests.sync.db.test_parity_api import FY24

from tests.sync import parity_fakebooks as pf
from tests.sync.conftest import requires_db, web_headers
from tests.sync.db.ingest_helpers import fresh, one, post_batch, batch, voucher_state
from tests.sync.db.test_parity_api import B, _company_resync, setup_b

pytestmark = requires_db

JUNE = (date(2025, 6, 1), date(2025, 6, 30))
OLD, NEW = "Cash", "Cash on Hand (Main)"


def _payment(b: B) -> dict:
    """A plain two-line June 2025 payment (party + bank, one bill)."""
    return next(copy.deepcopy(v) for v in b.cap.vouchers(*JUNE)
                if len(v["data"]["ledger_entries"]) == 2 and v["data"]["iscancelled"] == "No"
                and v["data"]["isoptional"] == "No")


def _with_amount(v: dict, amount: str, alter: int, narration: str) -> dict:
    d = v["data"]
    d["alterid"], d["narration"] = str(alter), narration
    for e in d["ledger_entries"]:
        sign = "-" if e["amount"].startswith("-") else ""
        e["amount"] = sign + amount
        for bill in e.get("bill_allocations", []):
            bill["amount"] = sign + amount
    return v


async def _post(app_client, b: B, run_id: str, objects: list[dict]) -> dict:
    r = await post_batch(app_client, b.ws, b.headers, batch(run_id, objects, company_guid=b.cap.guid))
    assert r.status_code == 200, r.text
    return r.json()["counts"]


async def _run(app_client, b: B, kind: str, counters: dict, **extra) -> str:
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": kind, "counters_at_start": counters, **extra})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


async def _complete(app_client, b: B, run_id: str, n: int, **extra) -> None:
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n, **extra})
    assert r.status_code == 200, r.text


async def _ledger(engine, b: B, guid: str) -> dict:
    async with fresh(engine) as s:
        return await one(s, "SELECT name, alter_id, is_deleted FROM tally_ledgers WHERE workspace_id=:w AND guid=:g",
                         w=b.ws, g=guid)


async def _voucher(engine, b: B, guid: str) -> dict:
    async with fresh(engine) as s:
        st = await voucher_state(s, b.ws, guid)
    return {"alter_id": st["voucher"]["alter_id"], "narration": st["voucher"]["narration"],
            "is_deleted": st["voucher"]["is_deleted"],
            "lines": sorted((l["ledger_name"], str(l["amount"])) for l in st["lines"]),
            "bills": sorted((x["bill_name"], str(x["amount"])) for x in st["bills"])}


async def _post_backup_changes(app_client, engine, b: B) -> tuple[dict, dict, dict, dict]:
    """After the (implicit) backup: rename `Cash`, alter a June payment, add a new June payment -- all at alter_ids
    above the backup's counters -- through a normal incremental. Returns (cash master, original payment,
    new voucher, the mirror's pre-restore view of the payment)."""
    pre = dict(b.counters)
    cash = next(m for m in b.cap.masters() if m["kind"] == "ledger" and m["data"]["name"] == OLD)
    pay = _payment(b)
    moved = {"alt_vch_id": pre["alt_vch_id"] + 5, "alt_mst_id": pre["alt_mst_id"] + 5}
    renamed = {"kind": "ledger", "data": {**cash["data"], "name": NEW, "alterid": str(moved["alt_mst_id"])}}
    altered = _with_amount(copy.deepcopy(pay), "3000.00", moved["alt_vch_id"], "altered after the backup")
    extra = copy.deepcopy(pay)
    extra["data"]["guid"] = extra["data"]["guid"][:-8] + "0000ffff"
    extra["data"]["masterid"] = extra["data"]["vouchernumber"] = "65535"
    extra["data"]["alterid"] = str(moved["alt_vch_id"] - 1)
    extra["data"]["narration"] = "created after the backup"
    run_id = await _run(app_client, b, "incremental", pre)
    counts = await _post(app_client, b, run_id, [renamed, altered, extra])
    assert (counts["inserted"], counts["updated"], counts["skipped_older"]) == (1, 2, 0)
    await _complete(app_client, b, run_id, 1, cursor_after=moved)
    b.counters = moved
    assert (await _ledger(engine, b, cash["data"]["guid"]))["name"] == NEW
    return cash, pay, extra, await _voucher(engine, b, pay["data"]["guid"])


async def test_confirmed_company_resync_after_restore_replaces_higher_alter_ids_with_the_restored_objects(
        app_client, session, engine):
    """C2 restore round-trip: the mirror holds post-backup (higher) alter_ids -- a renamed ledger, an altered
    voucher, a new voucher. Tally is restored (counters fall back) -> `restore_detected`; the user confirms a
    company resync; the agent posts the restored (lower-alter_id) objects: they REPLACE the mirror rows (name,
    amounts, alter_ids equal the restored data) and count as `updated`, not `skipped_older`. The post-backup voucher
    that no longer exists in Tally goes through the existing reconcile path (soft-deleted), not ingest."""
    b = await setup_b(app_client, session)
    restored = dict(b.counters)
    orig_pay = await _voucher(engine, b, _payment(b)["data"]["guid"])
    cash, pay, extra, altered_view = await _post_backup_changes(app_client, engine, b)
    assert altered_view != orig_pay and altered_view["alter_id"] > orig_pay["alter_id"]

    r = await app_client.post(f"/api/sync/{b.ws}/heartbeat", headers=b.headers, json={
        "tally_status": "ours", "pc_clock": "2026-09-25T06:30:00+00:00", "counters": restored,
        "seen_company": {"guid": b.cap.guid, "name": pf.B_NAME}})
    assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text

    seen: dict = {}

    async def during(run_id: str) -> int:
        june = b.cap.vouchers(*JUNE)                                   # the restored Tally's June
        seen["counts"] = await _post(app_client, b, run_id, [cash, *june])
        r = await app_client.post(f"/api/sync/{b.ws}/reconcile", headers=b.headers, json={
            "run_id": run_id, "scope": {"kind": "vouchers", "from": JUNE[0].isoformat(), "to": JUNE[1].isoformat()},
            "present": [{"guid": v["data"]["guid"], "alter_id": int(v["data"]["alterid"])} for v in june],
            "present_count": len(june)})
        assert r.status_code == 200, r.text
        seen["reconcile"] = r.json()
        return 1

    await _company_resync(app_client, b, restored, during)
    # the renamed ledger and the altered payment are `updated` (lower alter_id accepted); the rest of June is equal
    assert seen["counts"]["skipped_older"] == 0
    assert seen["counts"]["updated"] == 1 + len(b.cap.vouchers(*JUNE)) and seen["counts"]["inserted"] == 0
    assert seen["reconcile"]["soft_deleted"] == 1 and seen["reconcile"]["refetch"] == []

    assert await _ledger(engine, b, cash["data"]["guid"]) == \
        {"name": OLD, "alter_id": int(cash["data"]["alterid"]), "is_deleted": False}
    assert await _voucher(engine, b, pay["data"]["guid"]) == orig_pay           # amounts, bills, narration, alter_id
    assert (await _voucher(engine, b, extra["data"]["guid"]))["is_deleted"] is True
    async with fresh(engine) as s:
        sw = await one(s, "SELECT sync_state, cursor_alt_vch_id, cursor_alt_mst_id FROM sync_workspaces "
                          "WHERE workspace_id=:w", w=b.ws)
    assert (sw["sync_state"], sw["cursor_alt_vch_id"], sw["cursor_alt_mst_id"]) == \
        ("ready", restored["alt_vch_id"], restored["alt_mst_id"])


async def test_confirmed_single_fy_resync_replaces_a_higher_alter_id_voucher(app_client, session, engine):
    """C2, single-FY form: a confirmed FY 2025-26 `full_resync` also replaces a stored voucher at a lower
    alter_id (`updated`)."""
    b = await setup_b(app_client, session)
    orig_pay = await _voucher(engine, b, _payment(b)["data"]["guid"])
    _, pay, _, _ = await _post_backup_changes(app_client, engine, b)
    r = await app_client.post(f"/api/workspaces/{b.ws}/sync/commands", headers=web_headers(b.uid),
                              json={"type": "confirm_resync", "scope": "fy", "fy_start": "2025-04-01"})
    assert r.status_code == 200, r.text
    run_id = await _run(app_client, b, "full_resync", b.counters, scope={"fy_start": "2025-04-01"},
                        command_id=r.json()["id"])
    counts = await _post(app_client, b, run_id, [pay])
    assert (counts["updated"], counts["skipped_older"]) == (1, 0)
    assert await _voucher(engine, b, pay["data"]["guid"]) == orig_pay


async def test_incremental_with_a_lower_alter_id_is_still_skipped_older(app_client, session, engine):
    """C2 leaves every other run kind on the §12 rule: an incremental re-sending the pre-change objects (lower
    alter_ids) is `skipped_older` and the mirror keeps the newer rows."""
    b = await setup_b(app_client, session)
    cash, pay, _, altered_view = await _post_backup_changes(app_client, engine, b)
    run_id = await _run(app_client, b, "incremental", b.counters)
    counts = await _post(app_client, b, run_id, [cash, pay])
    assert (counts["updated"], counts["skipped_older"]) == (0, 2)
    assert (await _ledger(engine, b, cash["data"]["guid"]))["name"] == NEW
    assert await _voucher(engine, b, pay["data"]["guid"]) == altered_view


async def test_fy_scoped_resync_is_authoritative_only_for_its_own_fy_vouchers(app_client, session, engine):
    """C2 follow-up (controller ruling): authority is decided per object. In an FY-scoped (FY 2025-26) confirmed
    resync, masters keep the alter_id rule (a lower-alter_id master is `skipped_older`), and so does a voucher dated
    in ANOTHER FY; only a voucher of the run's own FY replaces the stored row at a lower alter_id."""
    b = await setup_b(app_client, session, edge=FY24)
    june24 = (date(2024, 6, 1), date(2024, 6, 30))
    other = next(copy.deepcopy(v) for v in b.cap.vouchers(*june24)
                 if len(v["data"]["ledger_entries"]) == 2 and v["data"]["iscancelled"] == "No"
                 and v["data"]["isoptional"] == "No")
    orig_pay = await _voucher(engine, b, _payment(b)["data"]["guid"])
    cash, pay, _, _ = await _post_backup_changes(app_client, engine, b)
    # also move an FY 2024-25 voucher above the backup (a second incremental)
    moved = {"alt_vch_id": b.counters["alt_vch_id"] + 1, "alt_mst_id": b.counters["alt_mst_id"]}
    run_id = await _run(app_client, b, "incremental", b.counters)
    altered_other = _with_amount(copy.deepcopy(other), "4000.00", moved["alt_vch_id"], "other FY, after the backup")
    assert (await _post(app_client, b, run_id, [altered_other]))["updated"] == 1
    await _complete(app_client, b, run_id, 1, cursor_after=moved)
    b.counters = moved
    other_view = await _voucher(engine, b, other["data"]["guid"])

    r = await app_client.post(f"/api/workspaces/{b.ws}/sync/commands", headers=web_headers(b.uid),
                              json={"type": "confirm_resync", "scope": "fy", "fy_start": "2025-04-01"})
    assert r.status_code == 200, r.text
    run_id = await _run(app_client, b, "full_resync", b.counters, scope={"fy_start": "2025-04-01"},
                        command_id=r.json()["id"])
    counts = await _post(app_client, b, run_id, [cash, other, pay])
    assert (counts["updated"], counts["skipped_older"]) == (1, 2), counts
    assert (await _ledger(engine, b, cash["data"]["guid"]))["name"] == NEW            # master: rule kept
    assert await _voucher(engine, b, other["data"]["guid"]) == other_view            # other FY: rule kept
    assert await _voucher(engine, b, pay["data"]["guid"]) == orig_pay                # own FY: replaced
