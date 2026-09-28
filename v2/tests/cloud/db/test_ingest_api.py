"""`POST /api/sync/{ws}/batches` (S1 spec §7.9, §11, §12 all 15 steps + the performance budget, §15.2 every row,
D12, D14, D15, D23, §8.5, §8.6). Every wire object comes from an S0 / Task 0 capture through the transcoder
(`v2.tests.cloud.realdata`); where a test needs a shape no capture holds, the docstring names the variant. Every
assertion on stored state re-reads from a fresh session (`fresh(engine)`), never the request's own.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
import time
import tracemalloc
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from v2.tests.cloud import realdata
from v2.tests.cloud.conftest import requires_db, web_headers
from v2.tests.cloud.db.ingest_helpers import (
    batch, bind, bound, count, fresh, gz, one, open_run, post_batch, post_ok, rows, sw_row, table_counts,
    voucher_by_guid, voucher_state,
)

pytestmark = requires_db

B = realdata.COMPANY_B_GUID
A = realdata.COMPANY_A_GUID
FIRST_B_VOUCHER = "-00000067"            # [S0-B:103] Sale to Indore Home Needs, alterid 105, 4 lines + 1 bill


def month_09() -> list[dict]:
    return realdata.vouchers("p21_B_fy2022_month_09.xml")


async def _ingest_b_masters(app_client, ws, headers, run_id) -> dict:
    return await post_ok(app_client, ws, headers, run_id, realdata.b_masters())


# --- §15.2: masters only / vouchers only / mixed -----------------------------------------------------------------


async def test_masters_only_batch_accepted_counts_inserted(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    objs = realdata.b_masters()
    body = await post_ok(app_client, ws, headers, run_id, objs)

    assert body["status"] == "accepted" and body["replayed"] is False
    assert body["counts"] == {"inserted": 92, "updated": 0, "skipped_older": 0, "balances_applied": 27,
                              "balances_stale": 0}
    assert body["reread_ledgers"] == []

    async with fresh(engine) as s:
        assert await table_counts(s, ws) == {
            "tally_currencies": 2, "tally_groups": 30, "tally_voucher_types": 25, "tally_units": 3,
            "tally_stock_groups": 0, "tally_ledgers": 27, "tally_stock_items": 5, "tally_vouchers": 0,
            "tally_voucher_ledger_lines": 0, "tally_voucher_inventory_lines": 0, "tally_bill_allocations": 0,
            "sync_quarantine": 0}
        groups = {g["name"]: g for g in await rows(s, "SELECT * FROM tally_groups WHERE workspace_id=:w", w=ws)}
        # custom creditor sub-group, several levels deep (p18_B_group_list: National Creditors -> Sundry Creditors
        # -> Current Liabilities -> Primary)
        assert groups["National Creditors"]["nature"] == "liabilities"
        assert groups["National Creditors"]["primary_group"] == "Current Liabilities"
        assert groups["National Creditors"]["parent_guid"] == groups["Sundry Creditors"]["guid"]
        assert groups["Current Assets"]["parent_guid"] is None           # carry (a): Primary -> NULL, no missing_master
        assert groups["Current Assets"]["parent_name"] == "Primary"      # D31-cleaned
        assert all(g["nature"] is not None and g["derivation_warning"] is None for g in groups.values())

        ledgers = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w", w=ws)}
        assert ledgers["Profit & Loss A/c"]["group_guid"] is None        # carry (a) / F17 on a ledger
        assert ledgers["Chennai Components Ltd"]["group_guid"] == groups["National Creditors"]["guid"]
        assert ledgers["Gulf Office Supplies LLC (USD)"]["is_forex"] is True
        assert ledgers["Gulf Office Supplies LLC"]["is_forex"] is False
        assert ledgers["Export Sales"]["closing_balance"] is None       # F14: "" stays None, never 0
        assert ledgers["Export Sales"]["balance_source"] == "tally"
        assert ledgers["Export Sales"]["balance_text"] == {"opening": "0.00", "closing": ""}
        assert ledgers["Cash"]["closing_balance"] == Decimal("-542977.08")
        assert ledgers["Cash"]["balance_captured_at"] == datetime.fromisoformat(
            realdata.sent_at("p16_B_ledgers.xml"))

        units = {u["name"]: u for u in await rows(s, "SELECT * FROM tally_units WHERE workspace_id=:w", w=ws)}
        items = {i["name"]: i for i in await rows(s, "SELECT * FROM tally_stock_items WHERE workspace_id=:w", w=ws)}
        assert items["A4 Paper Ream"]["base_unit_guid"] == units["Box of 10 Nos"]["guid"]
        assert items["A4 Paper Ream"]["parent_guid"] is None
        assert units["Box of 10 Nos"]["conversion"] == Decimal("10")

        currencies = {c["name"]: c for c in await rows(s, "SELECT * FROM tally_currencies WHERE workspace_id=:w",
                                                         w=ws)}
        assert currencies["?"]["is_base"] is True and currencies["$"]["is_base"] is False

        sw = await sw_row(s, ws)
        assert sw["base_currency_name"] == "?"                           # D30: the base currency's NAME
        assert sw["cursor_alt_vch_id"] is None and sw["cursor_alt_mst_id"] is None   # D15: never on a batch
        b = await one(s, "SELECT * FROM sync_batches WHERE workspace_id=:w", w=ws)
        assert b["status"] == "accepted" and b["object_count"] == 92 and b["run_id"] == uuid.UUID(run_id)
        assert b["response"] == body


async def test_voucher_types_alone_in_empty_workspace_self_parent_resolves(app_client, session, engine):
    """Carry (b): on a first sync the reserved voucher types are their own parent (`Contra -> Contra`) and are not
    in the DB yet -- the batch must still resolve them (own GUID), never `missing_master`."""
    ws, headers, run_id, _ = await bound(app_client, session)
    vts = [o for o in realdata.b_masters() if o["kind"] == "voucher_type"]
    body = await post_ok(app_client, ws, headers, run_id, vts)
    assert body["counts"]["inserted"] == 25

    async with fresh(engine) as s:
        vt = {v["name"]: v for v in await rows(s, "SELECT * FROM tally_voucher_types WHERE workspace_id=:w", w=ws)}
        assert len(vt) == 25
        assert vt["Contra"]["parent_guid"] == vt["Contra"]["guid"]
        assert vt["Sales - GST"]["parent_guid"] == vt["Sales"]["guid"]
        assert vt["Sales - GST"]["base_type"] == "Sales"
        assert all(v["base_type"] is not None for v in vt.values())


async def test_vouchers_only_batch_after_masters_accepted(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    vouchers = month_09()
    body = await post_ok(app_client, ws, headers, run_id, vouchers)
    assert body["counts"] == {"inserted": 20, "updated": 0, "skipped_older": 0, "balances_applied": 0,
                              "balances_stale": 0}

    wire_lines = sum(len(v["data"]["ledger_entries"]) for v in vouchers)
    wire_inv = sum(len(v["data"].get("inventory_entries", [])) for v in vouchers)
    wire_bills = sum(len(e.get("bill_allocations", [])) for v in vouchers for e in v["data"]["ledger_entries"])
    async with fresh(engine) as s:
        counts = await table_counts(s, ws)
        assert counts["tally_vouchers"] == 20
        assert counts["tally_voucher_ledger_lines"] == wire_lines
        assert counts["tally_voucher_inventory_lines"] == wire_inv
        assert counts["tally_bill_allocations"] == wire_bills
        sums = await rows(s, "SELECT voucher_id, sum(amount) AS s FROM tally_voucher_ledger_lines WHERE "
                             "workspace_id=:w GROUP BY voucher_id", w=ws)
        assert all(r["s"] == Decimal("0.00") for r in sums)             # rung 0 survives storage
        state = await voucher_state(s, ws, B + FIRST_B_VOUCHER)
        v = state["voucher"]
        assert (v["alter_id"], v["master_id"], str(v["date"]), v["voucher_type_name"], v["base_type"]) == (
            105, 103, "2022-09-01", "Sales", "Sales")
        assert v["party_ledger_name"] == "Indore Home Needs" and v["party_ledger_guid"] == B + "-000000e1"
        assert [(x["ledger_name"], x["amount"], x["countable"]) for x in state["lines"]] == [
            ("Indore Home Needs", Decimal("-16538.66"), True), ("Domestic Sales", Decimal("14015.82"), True),
            ("Output CGST", Decimal("1261.42"), True), ("Output SGST", Decimal("1261.42"), True)]
        assert state["lines"][0]["ledger_guid"] == B + "-000000e1"
        assert [(b["bill_name"], b["bill_type"], b["amount"], b["credit_period_days"]) for b in state["bills"]] == [
            ("Inv/103", "new_ref", Decimal("-16538.66"), 30)]
        inv = state["inventory"][0]
        assert (inv["stock_item_name"], inv["actual_qty"], inv["rate"], inv["amount"], inv["is_deemed_positive"]) \
            == ("Office Stapler", Decimal("17"), Decimal("824.46"), Decimal("14015.82"), False)
        assert (await sw_row(s, ws))["cursor_alt_vch_id"] is None        # D15: cursors never move on a batch


async def test_mixed_batch_masters_applied_first(app_client, session, engine):
    """§12 step 6: a voucher that references ledgers defined LATER in the same batch body is accepted."""
    ws, headers, run_id, _ = await bound(app_client, session)
    first = voucher_by_guid(month_09(), FIRST_B_VOUCHER)
    body = await post_ok(app_client, ws, headers, run_id, [first, *realdata.b_masters()])
    assert body["counts"]["inserted"] == 93
    async with fresh(engine) as s:
        assert (await voucher_state(s, ws, B + FIRST_B_VOUCHER))["lines"][0]["ledger_guid"] == B + "-000000e1"


# --- §15.2: retryable rejections --------------------------------------------------------------------------------


async def test_unknown_ledger_422_missing_master_retryable(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    masters = [o for o in realdata.b_masters() if o["data"]["name"] != "Indore Home Needs"]
    await post_ok(app_client, ws, headers, run_id, masters)
    vouchers = month_09()
    r = await post_batch(app_client, ws, headers, batch(run_id, vouchers))
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "batch_rejected"
    users = [i for i, v in enumerate(vouchers)
             if v["data"].get("partyledgername") == "Indore Home Needs"
             or any(e["ledgername"] == "Indore Home Needs" for e in v["data"]["ledger_entries"])]
    assert users
    assert {o["index"] for o in body["objects"]} == set(users)
    for o in body["objects"]:
        assert {"index", "kind", "guid", "code"} <= set(o)
        assert o["code"] == "missing_master" and o["kind"] == "voucher"
        assert o["guid"] == vouchers[o["index"]]["data"]["guid"]
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 0                 # atomic: nothing stored
        assert await count(s, "tally_voucher_ledger_lines", ws) == 0
        rejected = await one(s, "SELECT * FROM sync_batches WHERE workspace_id=:w AND status='rejected'", w=ws)
        assert rejected["response"] == body


async def test_two_live_ledgers_same_name_422_ambiguous_master(app_client, session, engine):
    """Variant: a second live ledger named `Indore Home Needs` under another GUID (a transient rename, D13)."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    twin = copy.deepcopy(next(o for o in realdata.b_masters() if o["data"]["name"] == "Indore Home Needs"))
    twin["data"]["guid"] = B + "-9000e1e1"
    first = voucher_by_guid(month_09(), FIRST_B_VOUCHER)
    r = await post_batch(app_client, ws, headers, batch(run_id, [twin, first]))
    assert r.status_code == 422, r.text
    objs = r.json()["objects"]
    assert {(o["index"], o["code"]) for o in objs} == {(1, "ambiguous_master")}
    assert {o["detail"] for o in objs} == {"partyledgername", "ledger_entries.ledgername"}   # every offender
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 27                 # the twin was rolled back too
        assert await count(s, "tally_vouchers", ws) == 0


# --- §15.2: deterministic rejections ------------------------------------------------------------------------------


async def test_unbalanced_422_deterministic_and_nothing_stored(app_client, session, engine):
    """§14.10 first half. Variant: the real first voucher with one line moved by 0.01."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    vouchers = month_09()[:10]
    vouchers[3]["data"]["ledger_entries"][0]["amount"] = str(
        Decimal(vouchers[3]["data"]["ledger_entries"][0]["amount"]) + Decimal("0.01"))
    r = await post_batch(app_client, ws, headers, batch(run_id, vouchers))
    assert r.status_code == 422, r.text
    assert [(o["index"], o["code"]) for o in r.json()["objects"]] == [(3, "unbalanced_voucher")]
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 0
        assert (await sw_row(s, ws))["quarantine_count"] == 0


async def test_forex_voucher_stored_with_fx_columns(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    usd = voucher_by_guid(realdata.vouchers("p22_B_forex_sales.xml"), "-000003c1")
    await post_ok(app_client, ws, headers, run_id, [usd])
    async with fresh(engine) as s:
        state = await voucher_state(s, ws, B + "-000003c1")
        assert state["voucher"]["has_forex"] is True
        assert [(x["amount"], x["fx_amount"], x["fx_rate"], x["fx_currency"]) for x in state["lines"]] == [
            (Decimal("-37216.04"), Decimal("-448.44"), Decimal("82.99"), "$"),
            (Decimal("37216.04"), Decimal("448.44"), Decimal("82.99"), "$")]


async def test_cancelled_voucher_no_lines_empty_party_accepted(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    cancelled = voucher_by_guid(realdata.vouchers("p03_B_flagged_month_2023_02.xml"), "-000000c9")
    await post_ok(app_client, ws, headers, run_id, [cancelled])
    async with fresh(engine) as s:
        state = await voucher_state(s, ws, B + "-000000c9")
        v = state["voucher"]
        assert (v["is_cancelled"], v["party_ledger_name"], v["party_ledger_guid"]) == (True, "", None)
        assert state["lines"] == [] and state["inventory"] == [] and state["bills"] == []


async def test_optional_voucher_lines_not_countable(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    optional = voucher_by_guid(realdata.vouchers("p03_B_flagged_month_2023_07.xml"), "-0000012d")
    await post_ok(app_client, ws, headers, run_id, [optional])
    async with fresh(engine) as s:
        state = await voucher_state(s, ws, B + "-0000012d")
        assert state["voucher"]["is_optional"] is True
        assert len(state["lines"]) == 4 and all(x["countable"] is False for x in state["lines"])


async def test_post_dated_voucher_lines_countable(app_client, session, engine):
    """Company A (`p16_A_post_dated_voucher.xml`, assembled per `realdata.a_post_dated_voucher`)."""
    ws, headers, run_id, _ = await bound(app_client, session, "A")
    await post_ok(app_client, ws, headers, run_id, realdata.a_masters(), company_guid=A)
    pd = realdata.a_post_dated_voucher()
    await post_ok(app_client, ws, headers, run_id, [pd], company_guid=A)
    async with fresh(engine) as s:
        state = await voucher_state(s, ws, pd["data"]["guid"])
        assert state["voucher"]["is_post_dated"] is True and str(state["voucher"]["date"]) == "2026-03-31"
        assert [(x["ledger_name"], x["amount"], x["countable"]) for x in state["lines"]] == [
            ("Electricity", Decimal("-1.00"), True), ("Cash", Decimal("1.00"), True)]


async def test_older_alter_id_skipped_older(app_client, session, engine):
    """§14.5: v@105 stored, v@104 (same GUID, variant narration) -> `skipped_older`, stored state unchanged."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    v105 = voucher_by_guid(month_09(), FIRST_B_VOUCHER)
    await post_ok(app_client, ws, headers, run_id, [v105])
    async with fresh(engine) as s:
        before = await voucher_state(s, ws, B + FIRST_B_VOUCHER)

    v104 = copy.deepcopy(v105)
    v104["data"]["alterid"] = " 104"
    v104["data"]["narration"] = "older copy"
    body = await post_ok(app_client, ws, headers, run_id, [v104])
    assert body["counts"]["skipped_older"] == 1 and body["counts"]["inserted"] == 0
    async with fresh(engine) as s:
        after = await voucher_state(s, ws, B + FIRST_B_VOUCHER)
        assert after == before
        assert after["voucher"]["alter_id"] == 105


async def test_wrong_company_guid_409_company_mismatch_nothing_stored(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    other = realdata.transcode.counters_from_xml(realdata.read_capture("p02_A_active_b.xml"))["guid"]
    assert other != B
    r = await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters(), company_guid=other))
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "company_mismatch"
    async with fresh(engine) as s:
        assert sum((await table_counts(s, ws)).values()) == 0
        assert await count(s, "sync_batches", ws) == 0


# --- §7.9 / §14.4 idempotency --------------------------------------------------------------------------------------


async def test_replay_same_body_returns_stored_response_replayed_true(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    body = batch(run_id, realdata.b_masters())
    r1 = await post_batch(app_client, ws, headers, body)
    r2 = await post_batch(app_client, ws, headers, body)
    assert r1.status_code == r2.status_code == 200
    assert r2.json() == {**r1.json(), "replayed": True}
    async with fresh(engine) as s:
        assert (await table_counts(s, ws))["tally_ledgers"] == 27
        assert await count(s, "sync_batches", ws) == 1


async def test_reused_batch_id_different_body_409_batch_id_reused(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    body = batch(run_id, realdata.b_masters())
    assert (await post_batch(app_client, ws, headers, body)).status_code == 200
    other = {**body, "objects": body["objects"][:5]}
    r = await post_batch(app_client, ws, headers, other)
    assert r.status_code == 409 and r.json()["error"] == "batch_id_reused"
    async with fresh(engine) as s:
        assert (await table_counts(s, ws))["tally_ledgers"] == 27
        assert await count(s, "sync_batches", ws) == 1


async def test_rejected_batch_same_body_replays_stored_422(app_client, session, engine):
    """F10: a rejected batch replayed with the SAME body returns the stored 422 (idempotent)."""
    ws, headers, run_id, _ = await bound(app_client, session)
    body = batch(run_id, month_09())                                     # no masters yet -> missing_master
    r1 = await post_batch(app_client, ws, headers, body)
    r2 = await post_batch(app_client, ws, headers, body)
    assert r1.status_code == r2.status_code == 422
    assert r2.json()["objects"] == r1.json()["objects"]
    async with fresh(engine) as s:
        assert await count(s, "sync_batches", ws) == 1
        assert await count(s, "tally_vouchers", ws) == 0


async def test_rejected_batch_id_retried_with_new_body_is_a_new_attempt(app_client, session, engine):
    """F10: idempotent replay binds only ACCEPTED batches -- a `rejected` row is replaced by a different body."""
    ws, headers, run_id, _ = await bound(app_client, session)
    batch_id = uuid.uuid4().hex
    r1 = await post_batch(app_client, ws, headers, batch(run_id, month_09(), batch_id=batch_id))
    assert r1.status_code == 422
    r2 = await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters() + month_09(),
                                                          batch_id=batch_id))
    assert r2.status_code == 200, r2.text
    async with fresh(engine) as s:
        b = await one(s, "SELECT * FROM sync_batches WHERE workspace_id=:w", w=ws)
        assert b["status"] == "accepted" and b["object_count"] == 112 and b["response"] == r2.json()
        assert await count(s, "sync_batches", ws) == 1
        assert await count(s, "tally_vouchers", ws) == 20


async def test_concurrent_identical_batches_store_once(app_client, session, engine):
    """Review Focus 5: two identical posts race; the claim row serialises them -- one stores, one replays."""
    ws, headers, run_id, _ = await bound(app_client, session)
    body = batch(run_id, realdata.b_masters() + month_09())
    r1, r2 = await asyncio.gather(post_batch(app_client, ws, headers, body), post_batch(app_client, ws, headers, body))
    assert {r1.status_code, r2.status_code} == {200}
    assert sorted([r1.json()["replayed"], r2.json()["replayed"]]) == [False, True]
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 20
        assert await count(s, "tally_ledgers", ws) == 27
        assert await count(s, "sync_batches", ws) == 1


# --- §12 step 1 limits ---------------------------------------------------------------------------------------------


async def test_oversize_gzip_413(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    body = batch(run_id, realdata.b_masters())
    app_client.app.state.settings.ingest_max_gzip_bytes = len(gz(body)) - 1
    r = await post_batch(app_client, ws, headers, body)
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 0


async def test_too_many_objects_413(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    v = month_09()[0]
    r = await post_batch(app_client, ws, headers, batch(run_id, [copy.deepcopy(v) for _ in range(501)]))
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"


async def test_zip_bomb_is_413_before_full_inflate(app_client, session, engine):
    """A 60 MB zero-filled JSON string gzips to ~60 KB; the inflater must stop at the 50 MB cap, streaming."""
    ws, headers, run_id, _ = await bound(app_client, session)
    raw = b'{"batch_id": "x", "pad": "' + b"0" * (60 * 1024 * 1024) + b'"}'
    bomb = gzip.compress(raw, 9)
    del raw
    assert len(bomb) < 200_000
    tracemalloc.start()
    try:
        r = await app_client.post(f"/api/sync/{ws}/batches", content=bomb,
                                  headers={**headers, "Content-Encoding": "gzip", "Content-Type": "application/json"})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    assert peak < 70 * 1024 * 1024, peak


async def test_uncompressed_json_body_is_accepted(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    r = await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters()), compress=False)
    assert r.status_code == 200, r.text
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 27


# --- §12 step 4 / §8.6: run and state gates --------------------------------------------------------------------


async def _to_ready_with_cursors(session, ws) -> None:
    await session.execute(text("UPDATE sync_workspaces SET sync_state='ready', cursor_alt_vch_id=900, "
                               "cursor_alt_mst_id=400, cursor_set_at=now() WHERE workspace_id=:w"), {"w": ws})
    await session.commit()


async def test_incremental_batch_in_restore_detected_409(app_client, session, engine):
    uid, ws, headers = await bind(app_client, session)
    await _to_ready_with_cursors(session, ws)
    run_id = await open_run(app_client, ws, headers, "incremental")
    await session.execute(text("UPDATE sync_workspaces SET sync_state='restore_detected', "
                               "restore_reason='counters_backwards' WHERE workspace_id=:w"), {"w": ws})
    await session.commit()
    r = await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters()))
    assert r.status_code == 409 and r.json()["error"] == "restore_detected"
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 0 and await count(s, "sync_batches", ws) == 0


async def test_batch_on_completed_run_409_run_closed(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    r = await app_client.patch(f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "batches_declared": 0},
                               headers=headers)
    assert r.status_code == 200, r.text
    r = await post_batch(app_client, ws, headers, batch(run_id, realdata.b_masters()))
    assert r.status_code == 409 and r.json()["error"] == "run_closed"
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 0 and await count(s, "sync_batches", ws) == 0


async def test_batch_on_other_devices_run_403(app_client, session, engine):
    ws, headers_a, run_id, uid = await bound(app_client, session)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await app_client.post("/api/agent/auth/login", json={"email": email, "password": "Passw0rd!Passw0rd",
                                                            "device_name": "LAPTOP", "agent_version": "0.1.0"})
    headers_b = {"Authorization": f"Bearer {r.json()['access_token']}"}
    from v2.tests.cloud.db.ingest_helpers import B_BIND
    r = await app_client.post("/api/sync/company", json={**B_BIND, "workspace_id": str(ws), "takeover": True},
                              headers=headers_b)
    assert r.status_code == 200, r.text
    r = await post_batch(app_client, ws, headers_b, batch(run_id, realdata.b_masters()))
    assert r.status_code == 403 and r.json()["error"] == "wrong_workspace"
    async with fresh(engine) as s:
        assert await count(s, "tally_ledgers", ws) == 0


async def test_unknown_run_404(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    r = await post_batch(app_client, ws, headers, batch(str(uuid.uuid4()), realdata.b_masters()))
    assert r.status_code == 404 and r.json()["error"] == "run_not_found"


# --- §12 step 14 warnings ------------------------------------------------------------------------------------------


async def test_guid_prefix_foreign_is_warning_not_error(app_client, session, engine):
    """D14. Variant: the real first voucher re-keyed under another company's GUID prefix (identity only)."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    v = voucher_by_guid(month_09(), FIRST_B_VOUCHER)
    v["data"]["guid"] = A + FIRST_B_VOUCHER
    body = await post_ok(app_client, ws, headers, run_id, [v])
    assert {"index": 0, "code": "guid_prefix_foreign"} in body["warnings"]
    async with fresh(engine) as s:
        assert (await voucher_state(s, ws, A + FIRST_B_VOUCHER))["voucher"]["guid"] == A + FIRST_B_VOUCHER


async def test_ledger_guid_cross_check_mismatch_warns(app_client, session, engine):
    """Step 14 / D13: a line's `ledgerguid` is a cross-check only. Variant: one line carries the right GUID (no
    warning), another carries a different ledger's GUID (warning; the NAME still decides)."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    vouchers = month_09()[:2]
    vouchers[0]["data"]["ledger_entries"][0]["ledgerguid"] = B + "-000000e1"          # right (Indore Home Needs)
    vouchers[1]["data"]["ledger_entries"][0]["ledgerguid"] = B + "-0000001f"          # Cash: wrong
    body = await post_ok(app_client, ws, headers, run_id, vouchers)
    assert [w for w in body["warnings"] if w["code"] == "ledger_guid_mismatch"] == [
        {"index": 1, "code": "ledger_guid_mismatch"}]
    async with fresh(engine) as s:
        state = await voucher_state(s, ws, vouchers[1]["data"]["guid"])
        assert state["lines"][0]["ledger_guid"] != B + "-0000001f"


async def test_inr_ledger_with_currency_name_not_forex_usd_is(app_client, session, engine):
    """Carry (c): `is_forex_ledger` gets the workspace's real base-currency NAME (`?`, D30), never None. Variant: a
    real B ledger (`Domestic Sales`) carrying `currencyname: "?"` -- the base currency's exported NAME
    (`p22_B_currencies.xml`), the same field `s1_B_usd_ledger.xml` carries as `$`."""
    ws, headers, run_id, _ = await bound(app_client, session)
    objs = realdata.b_masters()
    for o in objs:
        if o["data"]["name"] == "Domestic Sales":
            o["data"]["currencyname"] = "?"
    await post_ok(app_client, ws, headers, run_id, objs)
    async with fresh(engine) as s:
        led = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w", w=ws)}
        assert led["Domestic Sales"]["currency_name"] == "?" and led["Domestic Sales"]["is_forex"] is False
        assert led["Gulf Office Supplies LLC (USD)"]["currency_name"] == "$"
        assert led["Gulf Office Supplies LLC (USD)"]["is_forex"] is True


# --- D12 / §14.10 quarantine ----------------------------------------------------------------------------------------


async def test_quarantine_resend_stores_rest_and_records_row(app_client, session, engine):
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    good = month_09()[:10]
    bad = copy.deepcopy(good)
    bad[4]["data"]["ledger_entries"][0]["amount"] = str(
        Decimal(bad[4]["data"]["ledger_entries"][0]["amount"]) + Decimal("0.01"))
    r = await post_batch(app_client, ws, headers, batch(run_id, bad))
    assert r.status_code == 422 and r.json()["objects"][0]["code"] == "unbalanced_voucher"

    q_guid = bad[4]["data"]["guid"]
    resend = [v for i, v in enumerate(bad) if i != 4]
    quarantine = [{"kind": "voucher", "guid": q_guid, "code": "unbalanced_voucher",
                   "voucher_date": bad[4]["data"]["date"]}]
    body = await post_ok(app_client, ws, headers, run_id, resend, quarantine=quarantine)
    assert body["counts"]["inserted"] == 9
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 9
        q = await rows(s, "SELECT * FROM sync_quarantine WHERE workspace_id=:w", w=ws)
        assert len(q) == 1
        assert (q[0]["kind"], q[0]["guid"], q[0]["code"], str(q[0]["voucher_date"]), q[0]["resolved_at"],
                q[0]["times_seen"]) == ("voucher", q_guid, "unbalanced_voucher", "2022-09-01", None, 1)
        assert (await sw_row(s, ws))["quarantine_count"] == 1
        status = (await app_client.get(f"/api/workspaces/{ws}/sync-status",
                                       headers=web_headers((await sw_owner(s, ws))))).json()
        assert status["quarantine_count"] == 1

    await post_ok(app_client, ws, headers, run_id, [good[4]])                     # a later good copy resolves it
    async with fresh(engine) as s:
        q = await one(s, "SELECT * FROM sync_quarantine WHERE workspace_id=:w", w=ws)
        assert q["resolved_at"] is not None
        assert (await sw_row(s, ws))["quarantine_count"] == 0
        assert await count(s, "tally_vouchers", ws) == 10


async def sw_owner(s, ws):
    return (await s.execute(text("SELECT user_id FROM workspaces WHERE id=:w"), {"w": ws})).scalar_one()


async def test_quarantine_retryable_code_refused(app_client, session, engine):
    """A7: a code outside the deterministic set (`missing_master` is retryable) -> 422 `quarantine_code_not_allowed`."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    v = month_09()[:3]
    q = [{"kind": "voucher", "guid": v[2]["data"]["guid"], "code": "missing_master"}]
    r = await post_batch(app_client, ws, headers, batch(run_id, v[:2], quarantine=q))
    assert r.status_code == 422
    assert r.json()["error"] == "batch_rejected"
    assert [(o["index"], o["code"]) for o in r.json()["objects"]] == [(0, "quarantine_code_not_allowed")]
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 0 and await count(s, "sync_quarantine", ws) == 0


async def test_quarantine_accepts_every_task_8a_deterministic_code(app_client, session, engine):
    """The 8a codes (`invalid_counter`, `invalid_captured_at`, `invalid_field_type`, `unexpected_parse_error`) are
    deterministic and therefore quarantinable, alongside §11's own set."""
    ws, headers, run_id, _ = await bound(app_client, session)
    codes = ["invalid_counter", "invalid_captured_at", "invalid_field_type", "unexpected_parse_error",
             "unbalanced_voucher", "unparseable_amount", "forex_base_missing", "invalid_date", "invalid_logical",
             "missing_field", "duplicate_posting_list", "unknown_kind"]
    q = [{"kind": "voucher", "guid": f"{B}-9{i:07d}", "code": c} for i, c in enumerate(codes)]
    await post_ok(app_client, ws, headers, run_id, [], quarantine=q)
    async with fresh(engine) as s:
        assert await count(s, "sync_quarantine", ws) == len(codes)
        assert (await sw_row(s, ws))["quarantine_count"] == len(codes)


async def test_quarantine_over_threshold_moves_first_sync_to_error(app_client, session, engine):
    """§8.2 / A8: quarantine > `V2_QUARANTINE_ERROR_THRESHOLD` during a first sync -> `sync_state = error`."""
    ws, headers, run_id, _ = await bound(app_client, session)
    app_client.app.state.settings.quarantine_error_threshold = 1
    q = [{"kind": "voucher", "guid": f"{B}-9{i:07d}", "code": "unbalanced_voucher"} for i in range(2)]
    await post_ok(app_client, ws, headers, run_id, [], quarantine=q[:1])
    async with fresh(engine) as s:
        assert (await sw_row(s, ws))["sync_state"] == "first_sync"
    await post_ok(app_client, ws, headers, run_id, [], quarantine=q[1:])
    async with fresh(engine) as s:
        sw = await sw_row(s, ws)
        assert (sw["sync_state"], sw["quarantine_count"]) == ("error", 2)


# --- §8.5 / §14.11 / §15.3 last_synced_at; D15 cursors -----------------------------------------------------------


async def test_last_synced_at_moved_by_first_sync_and_incremental_not_backfill(app_client, session, engine, clock):
    ws, headers, run_id, uid = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    async with fresh(engine) as s:
        first_at = (await sw_row(s, ws))["last_synced_at"]
        assert first_at == clock.now()                                              # first_sync moves it

    await _to_ready_with_cursors(session, ws)
    clock.advance(minutes=1)
    backfill = await open_run(app_client, ws, headers, "backfill", scope={"fy_start": "2022-04-01"})
    await post_ok(app_client, ws, headers, backfill, month_09()[:2], chunk={"from": "2022-09-01", "to": "2022-09-02"})
    async with fresh(engine) as s:
        sw = await sw_row(s, ws)
        assert sw["last_synced_at"] == first_at                                        # backfill: unchanged
        assert (sw["cursor_alt_vch_id"], sw["cursor_alt_mst_id"]) == (900, 400)       # D15

    clock.advance(minutes=1)
    inc = await open_run(app_client, ws, headers, "incremental")
    await post_ok(app_client, ws, headers, inc, month_09()[2:4])
    async with fresh(engine) as s:
        sw = await sw_row(s, ws)
        assert sw["last_synced_at"] == clock.now()                                    # incremental moves it
        assert (sw["cursor_alt_vch_id"], sw["cursor_alt_mst_id"]) == (900, 400)       # D15: only on completion
        moved_at = sw["last_synced_at"]

    # company full_resync: an out-of-window chunk without masters leaves it; an in-window chunk moves it; masters
    # move it whatever the chunk.
    r = await app_client.post(f"/api/workspaces/{ws}/sync/commands", json={"type": "confirm_resync",
                                                                          "scope": "company"},
                              headers=web_headers(uid))
    assert r.status_code == 200, r.text
    resync = await open_run(app_client, ws, headers, "full_resync", scope={"company": True},
                            command_id=r.json()["id"])
    clock.advance(minutes=1)
    await post_ok(app_client, ws, headers, resync, month_09()[4:6], chunk={"from": "2022-09-01", "to": "2022-09-02"})
    async with fresh(engine) as s:
        assert (await sw_row(s, ws))["last_synced_at"] == moved_at
    clock.advance(minutes=1)
    await post_ok(app_client, ws, headers, resync, [], chunk={"from": "2026-09-01", "to": "2026-09-02"})
    async with fresh(engine) as s:
        assert (await sw_row(s, ws))["last_synced_at"] == clock.now()
    clock.advance(minutes=1)
    await post_ok(app_client, ws, headers, resync, realdata.b_masters()[:2],
                  chunk={"from": "2022-09-01", "to": "2022-09-02"})
    async with fresh(engine) as s:
        assert (await sw_row(s, ws))["last_synced_at"] == clock.now()


async def test_sync_status_first_sync_progress_with_real_run_and_batches(app_client, session, engine):
    """Carried from Task 6: `sync-status.first_sync` read from a REAL open first_sync run (opened through the API
    with `progress_total`), with accepted batches on it."""
    uid, ws, headers = await bind(app_client, session)
    run_id = await open_run(app_client, ws, headers, progress_total=24)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    await post_ok(app_client, ws, headers, run_id, month_09())
    r = await app_client.get(f"/api/workspaces/{ws}/sync-status", headers=web_headers(uid))
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["sync_state"] == "first_sync"
    assert st["first_sync"] == {"percent": 0.0, "done": 0, "total": 24}
    assert st["last_synced_at"] is not None and st["quarantine_count"] == 0


# --- performance budget ----------------------------------------------------------------------------------------------


async def test_ingest_performance_500_vouchers_under_5s(app_client, session, engine):
    """§12: 500 vouchers (~4 lines each) < 5 s. The only synthetic content is identity (GUIDs `…-9xxxxxxx`) and
    dates within Sept 2022; everything else is cloned from `p21_B_fy2022_month_09.xml`."""
    ws, headers, run_id, _ = await bound(app_client, session)
    await _ingest_b_masters(app_client, ws, headers, run_id)
    src = month_09()
    objs = []
    for i in range(500):
        v = copy.deepcopy(src[i % len(src)])
        v["data"]["guid"] = f"{B}-9{i:07d}"
        v["data"]["date"] = f"202209{(i % 30) + 1:02d}"
        objs.append(v)
    body = batch(run_id, objs)
    t0 = time.perf_counter()
    r = await post_batch(app_client, ws, headers, body)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["inserted"] == 500
    assert elapsed < 5.0, elapsed
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 500
        assert await count(s, "tally_voucher_ledger_lines", ws) == sum(len(o["data"]["ledger_entries"]) for o in objs)


# --- review checkpoint: the claim really serialises; a mid-batch failure leaves zero rows -------------------------


async def test_claim_row_blocks_a_concurrent_identical_batch_until_the_winner_commits(app_client, session, engine,
                                                                                      clock):
    """Review Focus 5, deterministic: session 1 holds an uncommitted claim; session 2's claim for the same
    `batch_id` must BLOCK on the unique index (not race past it), then replay once session 1 commits."""
    from v2.cloud.ingest import pipeline
    from v2.contract.models import BatchRequest

    ws, headers, run_id, _ = await bound(app_client, session)
    body = BatchRequest.model_validate(batch(run_id, []))
    async with fresh(engine) as s1, fresh(engine) as s2:
        c1 = await pipeline._claim_batch_id(s1, ws, uuid.UUID(run_id), body, "sha-1", clock)
        assert c1.row_id is not None and c1.replay is None
        loser = asyncio.create_task(pipeline._claim_batch_id(s2, ws, uuid.UUID(run_id), body, "sha-1", clock))
        await asyncio.sleep(0.5)
        assert not loser.done()                                        # blocked on session 1's claim row
        stored = {"batch_id": body.batch_id, "status": "accepted", "replayed": False}
        await pipeline._finish_claim(s1, c1, "accepted", 0, stored)
        await s1.commit()
        c2 = await asyncio.wait_for(loser, 5)
        assert (c2.replay_status, c2.replay) == (200, {**stored, "replayed": True})
        await s2.rollback()


async def test_claim_taken_over_when_the_concurrent_winner_rolls_back(app_client, session, engine, clock):
    from v2.cloud.ingest import pipeline
    from v2.contract.models import BatchRequest

    ws, headers, run_id, _ = await bound(app_client, session)
    body = BatchRequest.model_validate(batch(run_id, []))
    async with fresh(engine) as s1, fresh(engine) as s2:
        await pipeline._claim_batch_id(s1, ws, uuid.UUID(run_id), body, "sha-1", clock)
        loser = asyncio.create_task(pipeline._claim_batch_id(s2, ws, uuid.UUID(run_id), body, "sha-1", clock))
        await asyncio.sleep(0.3)
        assert not loser.done()
        await s1.rollback()
        c2 = await asyncio.wait_for(loser, 5)
        assert c2.row_id is not None and c2.replay is None             # the claim is now session 2's
        await s2.rollback()


async def test_mid_batch_failure_leaves_zero_rows(app_client, session, engine, monkeypatch):
    """Atomicity: masters are already flushed when the voucher store blows up -> the whole batch rolls back, the
    claim row included (so a retry with the same `batch_id` is a fresh attempt, §11 "a 500 is always retried")."""
    import pytest

    from v2.cloud.ingest import store

    ws, headers, run_id, _ = await bound(app_client, session)

    async def boom(*args, **kwargs):
        raise RuntimeError("injected store failure")

    monkeypatch.setattr(store, "upsert_vouchers", boom)
    body = batch(run_id, realdata.b_masters() + month_09())
    with pytest.raises(RuntimeError, match="injected"):
        await post_batch(app_client, ws, headers, body)
    async with fresh(engine) as s:
        assert sum((await table_counts(s, ws)).values()) == 0
        assert await count(s, "sync_batches", ws) == 0
        assert (await sw_row(s, ws))["base_currency_name"] == "INR"    # sync_workspaces untouched too

    monkeypatch.undo()
    r = await post_batch(app_client, ws, headers, body)                 # the retry is a fresh, successful attempt
    assert r.status_code == 200 and r.json()["replayed"] is False
