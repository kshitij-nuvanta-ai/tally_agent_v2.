"""``POST /api/sync/{ws}/reconcile`` and ``POST /api/sync/{ws}/snapshots`` (S1 spec §7.11, §7.12, §14 scenarios 7
and 15, D8, D19, D28, LESSONS rule 30). Every assertion on stored state re-reads from a fresh session.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from v2.cloud.clock import FixedClock
from v2.cloud.ingest import snapshots as snap_mod
from v2.cloud.models import SyncWorkspace
from v2.contract import transcode
from v2.contract.models import SnapshotRequest
from v2.tests.cloud import realdata
from v2.tests.cloud.conftest import requires_db
from v2.tests.cloud.db.ingest_helpers import bind, bound, fresh, one, post_ok, rows, voucher_by_guid

pytestmark = requires_db

B = realdata.COMPANY_B_GUID


def month(n: str) -> list[dict]:
    return realdata.vouchers(f"p21_B_fy2022_month_{n}.xml")


FIRST_B_VOUCHER = "-00000067"


async def _b_with_masters(app_client, session):
    ws, headers, run_id, uid = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    return ws, headers, run_id


async def _post_reconcile(app_client, ws, headers, body: dict):
    return await app_client.post(f"/api/sync/{ws}/reconcile", json=body, headers=headers)


def _present(objs: list[dict], exclude: set[str] = frozenset()) -> tuple[list[dict], int]:
    present = [{"guid": o["data"]["guid"], "alter_id": int(str(o["data"]["alterid"]).strip())}
               for o in objs if o["data"]["guid"] not in exclude]
    return present, len(present)


# --- §14.7 / §7.11: voucher soft-delete + reread_ledgers -------------------------------------------------------


async def test_reconcile_soft_deletes_absent_voucher_and_returns_touched_ledgers(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    v = voucher_by_guid(month("09"), FIRST_B_VOUCHER)
    guid = v["data"]["guid"]
    await post_ok(app_client, ws, headers, run_id, [v])

    body = {"run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
            "present": [], "present_count": 0, "confirm_large": False}
    r = await _post_reconcile(app_client, ws, headers, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["soft_deleted"] == 1 and out["refetch"] == []
    names = {x["name"] for x in out["reread_ledgers"]}
    assert names == {"Indore Home Needs", "Domestic Sales", "Output CGST", "Output SGST"}
    assert (B + "-000000e1", "Indore Home Needs") in {(x["guid"], x["name"]) for x in out["reread_ledgers"]}

    async with fresh(engine) as s:
        voucher = await one(s, "SELECT * FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws, g=guid)
        assert voucher["is_deleted"] is True and voucher["deleted_at"] is not None
        vid = voucher["id"]
        assert await rows(s, "SELECT 1 FROM tally_voucher_ledger_lines WHERE voucher_id=:v", v=vid) == []
        assert await rows(s, "SELECT 1 FROM tally_voucher_inventory_lines WHERE voucher_id=:v", v=vid) == []
        assert await rows(s, "SELECT 1 FROM tally_bill_allocations WHERE voucher_id=:v", v=vid) == []


async def test_resent_deleted_voucher_is_undeleted(app_client, session, engine):
    """S1-R8: soft-delete is reversible -- a re-sent voucher is re-stored with ``is_deleted = false``."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    v = voucher_by_guid(month("09"), FIRST_B_VOUCHER)
    guid = v["data"]["guid"]
    await post_ok(app_client, ws, headers, run_id, [v])
    await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": [], "present_count": 0, "confirm_large": False})

    async with fresh(engine) as s:
        assert (await one(s, "SELECT is_deleted FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=guid))["is_deleted"] is True

    await post_ok(app_client, ws, headers, run_id, [v])         # the agent's next batch carries it again

    async with fresh(engine) as s:
        voucher = await one(s, "SELECT * FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws, g=guid)
        assert voucher["is_deleted"] is False and voucher["deleted_at"] is None
        lines = await rows(s, "SELECT * FROM tally_voucher_ledger_lines WHERE voucher_id=:v", v=voucher["id"])
        assert len(lines) == 4                                  # lines re-inserted


async def test_reconcile_scope_limits_to_date_range(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    await post_ok(app_client, ws, headers, run_id, month("08"))
    await post_ok(app_client, ws, headers, run_id, month("09"))

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": [], "present_count": 0, "confirm_large": False})
    assert r.status_code == 200, r.text
    assert r.json()["soft_deleted"] == 20                       # only September's 20, never August's

    async with fresh(engine) as s:
        sept_deleted = await rows(s, "SELECT is_deleted FROM tally_vouchers WHERE workspace_id=:w AND "
                                     "date >= '2022-09-01' AND date <= '2022-09-30'", w=ws)
        assert all(x["is_deleted"] for x in sept_deleted) and len(sept_deleted) == 20
        aug_deleted = await rows(s, "SELECT is_deleted FROM tally_vouchers WHERE workspace_id=:w AND "
                                    "date >= '2022-08-01' AND date <= '2022-08-31'", w=ws)
        assert not any(x["is_deleted"] for x in aug_deleted) and len(aug_deleted) == 20


async def test_reconcile_refetch_for_unknown_or_newer_alter_id(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    vouchers = month("09")
    await post_ok(app_client, ws, headers, run_id, vouchers)

    present, _ = _present(vouchers)
    bumped_guid = present[0]["guid"]
    present[0] = {"guid": bumped_guid, "alter_id": present[0]["alter_id"] + 1000}     # Tally has a newer edit
    unknown_guid = B + "-9fffffff"
    present.append({"guid": unknown_guid, "alter_id": 1})

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": present, "present_count": len(present), "confirm_large": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["soft_deleted"] == 0                              # both GUIDs counted as "present" -- never absent
    assert out["refetch"] == sorted([bumped_guid, unknown_guid])


async def test_reconcile_refetches_and_undeletes_a_wrongly_soft_deleted_voucher(app_client, session, engine):
    """Fix round 1, I2: a voucher soft-deleted by an earlier (wrong) reconcile must be `refetch`-ed the moment
    it's `present` again, even at the SAME `alter_id` -- our own stored copy is stale (deleted), so it can never
    be treated as "known". The round trip: reconcile deletes it -> present lists it again -> it's in `refetch`
    -> the agent's next batch re-sends it -> it is live again, with its lines, re-read from a fresh session."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    v = voucher_by_guid(month("09"), FIRST_B_VOUCHER)
    guid = v["data"]["guid"]
    alter_id = int(str(v["data"]["alterid"]).strip())
    await post_ok(app_client, ws, headers, run_id, [v])

    r1 = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": [], "present_count": 0, "confirm_large": False})
    assert r1.status_code == 200 and r1.json()["soft_deleted"] == 1

    async with fresh(engine) as s:
        assert (await one(s, "SELECT is_deleted FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=guid))["is_deleted"] is True

    # Tally still has it (it was wrongly reconciled): present it again, same alter_id.
    r2 = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": [{"guid": guid, "alter_id": alter_id}], "present_count": 1, "confirm_large": False})
    assert r2.status_code == 200, r2.text
    out2 = r2.json()
    assert out2["soft_deleted"] == 0 and out2["refetch"] == [guid]

    async with fresh(engine) as s:                                # still soft-deleted -- refetch, not magic revive
        assert (await one(s, "SELECT is_deleted FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=guid))["is_deleted"] is True

    await post_ok(app_client, ws, headers, run_id, [v])            # the agent's next batch carries it again

    async with fresh(engine) as s:
        voucher = await one(s, "SELECT * FROM tally_vouchers WHERE workspace_id=:w AND guid=:g", w=ws, g=guid)
        assert voucher["is_deleted"] is False and voucher["deleted_at"] is None
        lines = await rows(s, "SELECT * FROM tally_voucher_ledger_lines WHERE voucher_id=:v", v=voucher["id"])
        assert len(lines) == 4


# --- D28 guard --------------------------------------------------------------------------------------------------


async def test_reconcile_guard_20pct_and_50_rows_409_reconcile_too_large(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    for n in ("04", "05", "06", "07", "08", "09"):               # 6 * 20 = 120 vouchers in scope
        await post_ok(app_client, ws, headers, run_id, month(n))

    body = {"run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-04-01", "to": "2022-09-30"},
            "present": [], "present_count": 0, "confirm_large": False}
    r = await _post_reconcile(app_client, ws, headers, body)
    assert r.status_code == 409, r.text
    out = r.json()
    assert out["error"] == "reconcile_too_large" and out["would_delete"] == 120 and out["scope_total"] == 120

    async with fresh(engine) as s:
        assert (await rows(s, "SELECT 1 FROM tally_vouchers WHERE workspace_id=:w AND is_deleted", w=ws)) == []


async def test_reconcile_confirm_large_allows(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    for n in ("04", "05", "06", "07", "08", "09"):
        await post_ok(app_client, ws, headers, run_id, month(n))

    body = {"run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-04-01", "to": "2022-09-30"},
            "present": [], "present_count": 0, "confirm_large": True}
    r = await _post_reconcile(app_client, ws, headers, body)
    assert r.status_code == 200, r.text
    assert r.json()["soft_deleted"] == 120

    async with fresh(engine) as s:
        deleted = await rows(s, "SELECT 1 FROM tally_vouchers WHERE workspace_id=:w AND is_deleted", w=ws)
        assert len(deleted) == 120


# --- present_count (fix round 1, I3) ------------------------------------------------------------------------------


async def test_reconcile_present_count_mismatch_422_nothing_changed(app_client, session, engine):
    """Controller ruling I3: `len(present) != present_count` is refused 422 `reconcile_list_incomplete` --
    catches a truncated upload before D28's percentage/row-count thresholds would ever see it (a truncation to
    19 of 20 real GUIDs, on a 20-voucher scope, never crosses D28's 50-row floor)."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    vouchers = month("09")
    await post_ok(app_client, ws, headers, run_id, vouchers)
    present, real_count = _present(vouchers)
    assert real_count == 20

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "vouchers", "from": "2022-09-01", "to": "2022-09-30"},
        "present": present, "present_count": real_count + 1, "confirm_large": False})   # claims one more than sent
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "reconcile_list_incomplete"

    async with fresh(engine) as s:
        assert (await rows(s, "SELECT 1 FROM tally_vouchers WHERE workspace_id=:w AND is_deleted", w=ws)) == []


async def test_reconcile_present_count_mismatch_masters_scope_422(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    ledgers = [o for o in realdata.b_masters() if o["kind"] == "ledger"]
    present, real_count = _present(ledgers)

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "masters", "master_type": "ledger"},
        "present": present[:-1], "present_count": real_count, "confirm_large": False})  # sent one fewer than claimed
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "reconcile_list_incomplete"

    async with fresh(engine) as s:
        assert (await rows(s, "SELECT 1 FROM tally_ledgers WHERE workspace_id=:w AND is_deleted", w=ws)) == []


# --- masters scope + LESSONS rule 30 -----------------------------------------------------------------------------


async def test_reconcile_master_in_use_409(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    v = voucher_by_guid(month("09"), FIRST_B_VOUCHER)
    await post_ok(app_client, ws, headers, run_id, [v])

    ledgers = [o for o in realdata.b_masters() if o["kind"] == "ledger"]
    in_use_guid = B + "-000000e1"                                 # "Indore Home Needs", the party of that voucher
    present, count_ = _present(ledgers, exclude={in_use_guid})    # every OTHER live ledger is present

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "masters", "master_type": "ledger"},
        "present": present, "present_count": count_, "confirm_large": False})
    assert r.status_code == 409, r.text
    out = r.json()
    assert out["error"] == "master_in_use" and out["guid"] == in_use_guid and out["name"] == "Indore Home Needs"

    async with fresh(engine) as s:
        assert (await one(s, "SELECT is_deleted FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=in_use_guid))["is_deleted"] is False   # refused atomically -- nothing soft-deleted


async def test_reconcile_masters_soft_delete_when_not_in_use(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    ledgers = [o for o in realdata.b_masters() if o["kind"] == "ledger"]
    unused_guid = next(o["data"]["guid"] for o in ledgers if o["data"]["name"] == "Export Sales")
    present, count_ = _present(ledgers, exclude={unused_guid})

    r = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "masters", "master_type": "ledger"},
        "present": present, "present_count": count_, "confirm_large": False})
    assert r.status_code == 200, r.text
    assert r.json()["soft_deleted"] == 1

    async with fresh(engine) as s:
        assert (await one(s, "SELECT is_deleted FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=unused_guid))["is_deleted"] is True


async def test_reconcile_refetches_a_soft_deleted_master_when_present_again(app_client, session, engine):
    """Fix round 1, I2, masters variant: "the same applies to masters" (review Important #2)."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    ledgers = [o for o in realdata.b_masters() if o["kind"] == "ledger"]
    unused = next(o for o in ledgers if o["data"]["name"] == "Export Sales")
    guid, alter_id = unused["data"]["guid"], int(str(unused["data"]["alterid"]).strip())
    present, count_ = _present(ledgers, exclude={guid})
    r1 = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "masters", "master_type": "ledger"},
        "present": present, "present_count": count_, "confirm_large": False})
    assert r1.status_code == 200 and r1.json()["soft_deleted"] == 1

    all_present, all_count = _present(ledgers)                    # Tally lists it again, same alter_id
    r2 = await _post_reconcile(app_client, ws, headers, {
        "run_id": run_id, "scope": {"kind": "masters", "master_type": "ledger"},
        "present": all_present, "present_count": all_count, "confirm_large": False})
    assert r2.status_code == 200, r2.text
    out2 = r2.json()
    assert out2["soft_deleted"] == 0 and out2["refetch"] == [guid]

    async with fresh(engine) as s:                                # still soft-deleted -- refetch is the signal
        assert (await one(s, "SELECT is_deleted FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=ws,
                          g=guid))["is_deleted"] is True

    await post_ok(app_client, ws, headers, run_id, [unused])       # the agent re-sends the master
    async with fresh(engine) as s:
        row = await one(s, "SELECT is_deleted, alter_id FROM tally_ledgers WHERE workspace_id=:w AND guid=:g",
                        w=ws, g=guid)
        assert row["is_deleted"] is False and row["alter_id"] == alter_id


# --- §7.12 snapshots -----------------------------------------------------------------------------------------


def _tb_cells() -> list[dict]:
    return transcode.report_cells(realdata.read_capture("p18_B_tb_asof_2023-03-31.xml"), "trial_balance")


def _snapshot_body(*, captured_at: str, from_date: str = "01-04-2022", as_on_date: str = "31-03-2023",
                   cells: list[dict] | None = None) -> dict:
    return {"report_type": "trial_balance", "from_date": from_date, "as_on_date": as_on_date,
            "request_flags": {"EXPLODEFLAG": "Yes"}, "purpose": "anchor", "captured_at": captured_at,
            "counters": {"alt_vch_id": 965, "alt_mst_id": 412}, "cells": cells if cells is not None else _tb_cells()}


async def _post_snapshot(app_client, ws, headers, body: dict):
    return await app_client.post(f"/api/sync/{ws}/snapshots", json=body, headers=headers)


async def test_snapshot_recapture_replaces_and_round_trips(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    body1 = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30")
    r1 = await _post_snapshot(app_client, ws, headers, body1)
    assert r1.status_code == 200, r1.text
    out1 = r1.json()
    assert out1 == {"stored": True, "replaced": False, "row_count": 18,
                    "synthetic_rows": ["Opening Stock", "Unadjusted Forex Gain/Loss"], "imbalance": "0.00",
                    "warnings": []}

    body2 = _snapshot_body(captured_at="2026-09-25T17:00:00+05:30")     # newer captured_at
    r2 = await _post_snapshot(app_client, ws, headers, body2)
    assert r2.status_code == 200, r2.text
    assert r2.json()["stored"] is True and r2.json()["replaced"] is True

    async with fresh(engine) as s:
        snaps = await rows(s, "SELECT * FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)
        assert len(snaps) == 1                                   # one row on the (ws, report_type, as_on_date) key
        snap = snaps[0]
        assert snap["cells"] == body2["cells"]
        assert snap["row_count"] == 18
        assert snap["imbalance"] == 0
        assert sorted(snap["synthetic_rows"]) == ["Opening Stock", "Unadjusted Forex Gain/Loss"]
        assert [r["name"] for r in snap["rows"]][:2] == ["Capital Account", "Capital Account"]


async def test_older_capture_does_not_replace(app_client, session, engine):
    ws, headers, run_id = await _b_with_masters(app_client, session)
    newer = _snapshot_body(captured_at="2026-09-25T17:00:00+05:30")
    r1 = await _post_snapshot(app_client, ws, headers, newer)
    assert r1.status_code == 200 and r1.json()["replaced"] is False

    older = _snapshot_body(captured_at="2026-09-25T16:00:00+05:30")     # older than what's stored
    r2 = await _post_snapshot(app_client, ws, headers, older)
    assert r2.status_code == 200, r2.text
    assert r2.json()["stored"] is False and r2.json()["replaced"] is False

    async with fresh(engine) as s:
        snap = await one(s, "SELECT captured_at FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)
        assert snap["captured_at"] == datetime.fromisoformat("2026-09-25T17:00:00+05:30")  # the newer one, untouched


async def test_snapshot_bad_period_422(app_client, session, engine):
    """D8: ``from_date`` must be the FY start containing ``as_on_date`` -- 01-05-2022 is not 01-04-2022."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30", from_date="01-05-2022")
    r = await _post_snapshot(app_client, ws, headers, body)
    assert r.status_code == 422 and r.json()["error"] == "bad_period"

    async with fresh(engine) as s:
        assert (await rows(s, "SELECT 1 FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)) == []


async def test_snapshot_before_books_from_422_bad_period(app_client, session, engine):
    """``as_on_date`` before ``books_from`` (2022-04-01 for company B) is refused even though the FY-start
    convention holds for that earlier date."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30", from_date="01-04-2021", as_on_date="31-03-2022")
    r = await _post_snapshot(app_client, ws, headers, body)
    assert r.status_code == 422 and r.json()["error"] == "bad_period"


async def test_snapshot_company_mismatch_409(app_client, session, engine):
    """The snapshot body carries no company GUID, so the only check is the bound workspace: a device bound to a
    DIFFERENT workspace gets 403 ``wrong_workspace`` before the handler ever runs."""
    ws1, headers1, run_id1 = await _b_with_masters(app_client, session)
    uid2, ws2, headers2 = await bind(app_client, session, "B", device_name="OTHER-PC")

    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30")
    r = await _post_snapshot(app_client, ws1, headers2, body)
    assert r.status_code == 403 and r.json()["error"] == "wrong_workspace"

    async with fresh(engine) as s:
        assert (await rows(s, "SELECT 1 FROM tally_report_snapshots WHERE workspace_id=:w", w=ws1)) == []


# --- I1: atomic newer-wins under real concurrency -----------------------------------------------------------------


async def test_snapshot_newer_wins_under_concurrent_writes(app_client, session, engine):
    """Fix round 1, I1: two overlapping posts (e.g. an agent retry after a timeout) must never let an older
    capture win, however Postgres happens to interleave/serialize their writes. A barrier forces both writes to
    be genuinely in flight together (not accidentally fully serialized by asyncio's own scheduling) before
    either touches the DB, and the OLDER write's coroutine is dispatched to `gather` FIRST -- so if call/dispatch
    order ever decided the winner (the pre-fix bug: whichever statement resolves the ON CONFLICT last wins,
    unconditionally), this would expose it. The atomic `WHERE captured_at < excluded.captured_at` guarantees the
    NEWER capture wins regardless."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    cells = _tb_cells()
    older = SnapshotRequest.model_validate(_snapshot_body(captured_at="2026-09-25T16:00:00+05:30", cells=cells))
    newer = SnapshotRequest.model_validate(_snapshot_body(captured_at="2026-09-25T17:00:00+05:30", cells=cells))
    barrier = asyncio.Barrier(2)

    async def write(body: SnapshotRequest) -> dict:
        async with fresh(engine) as s:
            sw = await s.get(SyncWorkspace, ws)
            await barrier.wait()
            result = await snap_mod.store(s, sw, body, FixedClock(datetime.now(timezone.utc)))
            await s.commit()
            return result

    results = await asyncio.wait_for(asyncio.gather(write(older), write(newer)), timeout=10)
    assert results[1]["stored"] is True                            # the newer write always applies
    assert results[0]["stored"] in (True, False)                   # the loser reports cleanly either way

    async with fresh(engine) as s:
        snap = await one(s, "SELECT captured_at FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)
        assert snap["captured_at"] == datetime.fromisoformat("2026-09-25T17:00:00+05:30")


# --- I5: empty / primary-less TB -> NULL imbalance, never 0 -------------------------------------------------------


async def test_snapshot_empty_tb_imbalance_is_null_with_warning(app_client, session, engine):
    """Fix round 1, I5 (controller ruling): a TB snapshot with no top-level group rows at all must never report
    imbalance `0` -- that would become a false D10 baseline, silently discarding every later real TB at the same
    `alt_mst_id` as `discarded_stale`. It IS stored (the agent's capture is still useful data), but `imbalance`
    is `None`/NULL and a warning is returned."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    cells = [{"dspdispname": "Some Random Ledger", "dspcldramta": "-5.00", "dspclcramta": ""}]  # no primary rows
    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30", cells=cells)
    r = await _post_snapshot(app_client, ws, headers, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stored"] is True and out["imbalance"] is None
    assert out["warnings"] == ["tb_no_top_level_group_rows"]

    async with fresh(engine) as s:
        snap = await one(s, "SELECT imbalance FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)
        assert snap["imbalance"] is None                            # NULL in the DB, never Decimal("0")


# --- I6: custom top-level groups (and the no-masters-stored fallback) ---------------------------------------------


async def test_snapshot_imbalance_includes_custom_top_level_group(app_client, session, engine):
    """Fix round 1, I6 (controller ruling): a group directly under Primary that is NOT one of the 15 reserved
    names -- a real user-created top-level group -- must still be netted, using the workspace's own STORED group
    masters (`parent_guid IS NULL`) rather than the reserved-name list alone. The real B capture is edited to
    move ₹1,000 out of `Current Liabilities` into a new top-level group `Suspense A/c (Custom)`; the capture
    must still net to 0.00 once that group's master is stored."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    custom_group = {"kind": "group", "data": {"guid": B + "-9c000001", "alterid": "1",
                    "name": "Suspense A/c (Custom)", "parent": "Primary"}}
    await post_ok(app_client, ws, headers, run_id, [custom_group])

    cells = _tb_cells()
    for c in cells:
        if c["dspdispname"] == "Current Liabilities":
            c["dspclcramta"] = str(Decimal(c["dspclcramta"]) - Decimal("1000.00"))
    cells.append({"dspdispname": "Suspense A/c (Custom)", "dspcldramta": "", "dspclcramta": "1000.00"})

    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30", cells=cells)
    r = await _post_snapshot(app_client, ws, headers, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["imbalance"] == "0.00"                                # would be "-1000.00" if left out
    assert out["warnings"] == []

    async with fresh(engine) as s:
        snap = await one(s, "SELECT imbalance FROM tally_report_snapshots WHERE workspace_id=:w", w=ws)
        assert snap["imbalance"] == 0


async def test_snapshot_imbalance_falls_back_to_reserved_names_with_no_group_masters_stored(app_client, session,
                                                                                             engine):
    """Fix round 1, I6: with NO group masters stored yet (a snapshot captured before the first masters batch),
    the imbalance formula falls back to the 15 reserved primary-group names -- identical to the pre-fix-round-1
    behaviour and to every existing pure-unit test in `test_snapshot_rows.py`."""
    ws, headers, run_id, uid = await bound(app_client, session)     # bound, but NO masters batch posted at all
    body = _snapshot_body(captured_at="2026-09-25T16:52:49+05:30")
    r = await _post_snapshot(app_client, ws, headers, body)
    assert r.status_code == 200, r.text
    assert r.json()["imbalance"] == "0.00"
