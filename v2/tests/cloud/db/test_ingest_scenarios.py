"""§14 DB persistence scenarios owned by S1 task 8c (4, 5, 6, 8, 9, 10, 11, 19 -- 4/5/10/11 live in
`test_ingest_api.py` next to their §15.2 rows; this file holds 6, 8, 9, 19 plus step 10, D3 and §4.9). Each one acts
through the API, then re-reads the WHOLE stored state from a fresh session -- row counts included.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal

from v2.contract import transcode
from v2.tests.cloud import realdata
from v2.tests.cloud.conftest import requires_db
from v2.tests.cloud.db.ingest_helpers import (
    bound, count, fresh, one, post_ok, rows, table_counts, voucher_by_guid, voucher_state,
)

pytestmark = requires_db

B = realdata.COMPANY_B_GUID
A = realdata.COMPANY_A_GUID
FIRST_B_VOUCHER = "-00000067"


async def _b_with_masters(app_client, session):
    ws, headers, run_id, uid = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    return ws, headers, run_id


async def _a_with_masters(app_client, session):
    ws, headers, run_id, uid = await bound(app_client, session, "A")
    await post_ok(app_client, ws, headers, run_id, realdata.a_masters(), company_guid=A)
    return ws, headers, run_id


def _strip_volatile(state: dict) -> dict:
    """Voucher state minus the columns that legitimately change on a replace (row ids, timestamps)."""
    out = {"voucher": {k: v for k, v in state["voucher"].items() if k not in ("updated_at",)}}
    for key in ("lines", "inventory", "bills"):
        out[key] = [{k: v for k, v in r.items() if k not in ("id", "created_at", "voucher_id")} for r in state[key]]
    return out


async def test_voucher_edit_replaces_lines_and_bills_exactly(app_client, session, engine):
    """§14.6. v@105 = the real first B voucher (4 lines, 1 bill). v@106 is a variant edit of it: `Output SGST`
    removed and the party line / bill re-totalled so it still balances (3 lines, different amounts)."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    v105 = voucher_by_guid(realdata.vouchers("p21_B_fy2022_month_09.xml"), FIRST_B_VOUCHER)
    await post_ok(app_client, ws, headers, run_id, [v105])
    async with fresh(engine) as s:
        before = await voucher_state(s, ws, B + FIRST_B_VOUCHER)
        assert (len(before["lines"]), len(before["bills"]), len(before["inventory"])) == (4, 1, 1)

    v106 = copy.deepcopy(v105)
    d = v106["data"]
    d["alterid"] = " 106"
    d["ledger_entries"] = [e for e in d["ledger_entries"] if e["ledgername"] != "Output SGST"]
    d["ledger_entries"][0]["amount"] = "-15277.24"
    d["ledger_entries"][0]["bill_allocations"][0]["amount"] = "-15277.24"
    body = await post_ok(app_client, ws, headers, run_id, [v106])
    assert body["counts"]["updated"] == 1

    async with fresh(engine) as s:
        after = await voucher_state(s, ws, B + FIRST_B_VOUCHER)
        assert after["voucher"]["id"] == before["voucher"]["id"] and after["voucher"]["alter_id"] == 106
        assert [(x["line_no"], x["ledger_name"], x["amount"]) for x in after["lines"]] == [
            (0, "Indore Home Needs", Decimal("-15277.24")), (1, "Domestic Sales", Decimal("14015.82")),
            (2, "Output CGST", Decimal("1261.42"))]
        assert [(b["bill_name"], b["amount"], b["ledger_line_no"]) for b in after["bills"]] == [
            ("Inv/103", Decimal("-15277.24"), 0)]
        assert {b["id"] for b in after["bills"]}.isdisjoint({b["id"] for b in before["bills"]})   # replaced
        counts = await table_counts(s, ws)
        assert (counts["tally_vouchers"], counts["tally_voucher_ledger_lines"], counts["tally_bill_allocations"],
                counts["tally_voucher_inventory_lines"]) == (1, 3, 1, 1)


async def test_rename_keeps_guid_and_old_lines_join(app_client, session, engine):
    """§14.8 (probe 8, company A): `Rajesh Computers` (GUID …e2, alter 228) renamed `Rajesh Computers S0` (alter
    266). Old lines keep `ledger_name` as exported and still join by GUID; the renamed-export vouchers (same
    AlterIDs, probe 8) resolve to the same GUID under the new name."""
    ws, headers, run_id = await _a_with_masters(app_client, session)
    before_ledger = realdata.masters("p08_A_rename_before.xml", "ledger")
    after_ledger = realdata.masters("p08_A_rename_after.xml", "ledger")
    guid = before_ledger[0]["data"]["guid"]
    await post_ok(app_client, ws, headers, run_id, before_ledger, company_guid=A)
    before_vouchers = realdata.a_vouchers("p08_A_rename_before_vouchers.xml")
    await post_ok(app_client, ws, headers, run_id, before_vouchers, company_guid=A)
    users = [v["data"]["guid"] for v in before_vouchers
             if any(e["ledgername"] == "Rajesh Computers" for e in v["data"]["ledger_entries"])]
    assert len(users) == 3

    await post_ok(app_client, ws, headers, run_id, after_ledger, company_guid=A)
    async with fresh(engine) as s:
        led = await rows(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=ws, g=guid)
        assert len(led) == 1 and led[0]["name"] == "Rajesh Computers S0" and led[0]["alter_id"] == 266
        assert await count(s, "tally_ledgers", ws) == 35
        lines = await rows(s, "SELECT ledger_name FROM tally_voucher_ledger_lines WHERE workspace_id=:w AND "
                              "ledger_guid=:g", w=ws, g=guid)
        assert lines and {x["ledger_name"] for x in lines} == {"Rajesh Computers"}   # as exported, not rewritten
        n_lines = await count(s, "tally_voucher_ledger_lines", ws)

    after_vouchers = realdata.a_vouchers("p08_A_rename_after_vouchers.xml")
    body = await post_ok(app_client, ws, headers, run_id, after_vouchers, company_guid=A)
    assert body["counts"]["inserted"] == 0                             # same GUIDs: no second row
    async with fresh(engine) as s:
        assert await count(s, "tally_vouchers", ws) == 50
        assert await count(s, "tally_voucher_ledger_lines", ws) == n_lines
        lines = await rows(s, "SELECT ledger_name FROM tally_voucher_ledger_lines WHERE workspace_id=:w AND "
                              "ledger_guid=:g", w=ws, g=guid)
        assert len(lines) == 3 and {x["ledger_name"] for x in lines} == {"Rajesh Computers S0"}


async def test_mirrored_balance_newer_wins_stale_counted(app_client, session, engine):
    """§14.9 / F23: G5 (`s1_B_ledgers_touched.xml`, captured_at = sidecar sent_at, t2) then an older mirrored
    capture of the same ledger (`p16_B_ledgers_asof_2023-03-31.xml`, its own sent_at t1 < t2) -> t2 stays."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    g5 = realdata.g5_ledger_balances()
    t2 = datetime.fromisoformat(realdata.sent_at("s1_B_ledgers_touched.xml"))
    body = await post_ok(app_client, ws, headers, run_id, g5)
    assert (body["counts"]["balances_applied"], body["counts"]["balances_stale"]) == (4, 0)

    stale_name = "p16_B_ledgers_asof_2023-03-31.xml"
    t1 = datetime.fromisoformat(realdata.sent_at(stale_name))
    assert t1 < t2
    stale = [o for o in transcode.balances_from_xml(realdata.read_capture(stale_name), realdata.sent_at(stale_name))
             if o["data"]["name"] == "Gulf Office Supplies LLC"]
    assert stale[0]["data"]["closingbalance"] == "-192264.86"
    body = await post_ok(app_client, ws, headers, run_id, stale)
    assert (body["counts"]["balances_applied"], body["counts"]["balances_stale"]) == (0, 1)

    async with fresh(engine) as s:
        led = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w", w=ws)}
        gulf = led["Gulf Office Supplies LLC"]
        assert (gulf["closing_balance"], gulf["balance_captured_at"]) == (Decimal("-351248.14"), t2)
        assert gulf["balance_text"] == {"opening": "-178008.58", "closing": "-351248.14"}
        usd = led["Gulf Office Supplies LLC (USD)"]
        assert (usd["closing_balance"], usd["closing_fx_amount"], usd["closing_fx_rate"], usd["fx_currency"],
                usd["balance_source"], usd["balance_captured_at"]) == (
            Decimal("-132929.85"), Decimal("-1609.71"), Decimal("82.58"), "$", "tally", t2)
        export = led["Export Sales"]
        assert (export["closing_balance"], export["opening_balance"], export["balance_captured_at"]) == (
            None, Decimal("0.00"), t2)                                                      # F14: "" -> None
        untouched = led["Cash"]
        assert untouched["balance_captured_at"] == datetime.fromisoformat(realdata.sent_at("p16_B_ledgers.xml"))
        assert await count(s, "tally_ledgers", ws) == 27


async def test_numeric_round_trip_exact(app_client, session, engine):
    """§14.19: `-16538.66`, `132929.85`, face `-1609.71`, rate `82.58` read back as equal `Decimal`s (all real). The
    `0.01` value is a variant: a real Receipt voucher from month 09 re-keyed (…-90000001) with its two lines set to
    -0.01 / 0.01, since no capture holds a one-paisa posting."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    vouchers = realdata.vouchers("p21_B_fy2022_month_09.xml")
    paisa = copy.deepcopy(next(v for v in vouchers if len(v["data"]["ledger_entries"]) == 2
                               and not v["data"].get("inventory_entries")))
    paisa["data"]["guid"] = B + "-90000001"
    paisa["data"]["ledger_entries"][0]["amount"] = "-0.01" if paisa["data"]["ledger_entries"][0]["isdeemedpositive"] \
        == "Yes" else "0.01"
    paisa["data"]["ledger_entries"][1]["amount"] = "0.01" if paisa["data"]["ledger_entries"][0]["amount"] == "-0.01" \
        else "-0.01"
    for e in paisa["data"]["ledger_entries"]:
        for b in e.get("bill_allocations", []):
            b["amount"] = e["amount"]
    await post_ok(app_client, ws, headers, run_id, [voucher_by_guid(vouchers, FIRST_B_VOUCHER), paisa])
    async with fresh(engine) as s:
        first = await voucher_state(s, ws, B + FIRST_B_VOUCHER)
        assert first["lines"][0]["amount"] == Decimal("-16538.66")
        assert {x["amount"] for x in (await voucher_state(s, ws, B + "-90000001"))["lines"]} == {
            Decimal("-0.01"), Decimal("0.01")}
        usd = await one(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w AND name=:n", w=ws,
                        n="Gulf Office Supplies LLC (USD)")
        assert (-usd["closing_balance"], usd["closing_fx_amount"], usd["closing_fx_rate"]) == (
            Decimal("132929.85"), Decimal("-1609.71"), Decimal("82.58"))
        assert str(usd["closing_fx_rate"]) == "82.580000" and str(usd["closing_balance"]) == "-132929.85"


async def test_group_parent_change_rederives_descendants(app_client, session, engine):
    """§12 step 10 (company A). Variant parents (identity + parent only; alterid bumped): `North Zone Debtors`
    moved under `Sundry Creditors` -> liabilities; `Sundry Creditors` moved under `Sundry Debtors` -> it AND its
    real custom sub-groups `Local Creditors` / `National Creditors` (and now North Zone) become assets."""
    ws, headers, run_id = await _a_with_masters(app_client, session)
    groups = {o["data"]["name"]: o for o in realdata.a_masters() if o["kind"] == "group"}

    async with fresh(engine) as s:
        g = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_groups WHERE workspace_id=:w", w=ws)}
        assert g["North Zone Debtors"]["nature"] == "assets" and g["Local Creditors"]["nature"] == "liabilities"

    north = copy.deepcopy(groups["North Zone Debtors"])
    north["data"]["parent"] = "Sundry Creditors"
    north["data"]["alterid"] = str(int(north["data"]["alterid"]) + 1000)
    await post_ok(app_client, ws, headers, run_id, [north], company_guid=A)
    async with fresh(engine) as s:
        g = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_groups WHERE workspace_id=:w", w=ws)}
        assert (g["North Zone Debtors"]["nature"], g["North Zone Debtors"]["primary_group"],
                g["North Zone Debtors"]["parent_guid"]) == ("liabilities", "Current Liabilities",
                                                             g["Sundry Creditors"]["guid"])

    creditors = copy.deepcopy(groups["Sundry Creditors"])
    creditors["data"]["parent"] = "Sundry Debtors"
    creditors["data"]["alterid"] = str(int(creditors["data"]["alterid"]) + 1000)
    body = await post_ok(app_client, ws, headers, run_id, [creditors], company_guid=A)
    assert {"index": 0, "code": "is_revenue_disagrees"} not in body["warnings"]
    async with fresh(engine) as s:
        g = {x["name"]: x for x in await rows(s, "SELECT * FROM tally_groups WHERE workspace_id=:w", w=ws)}
        for name in ("Sundry Creditors", "Local Creditors", "National Creditors", "North Zone Debtors"):
            assert (g[name]["nature"], g[name]["primary_group"]) == ("assets", "Current Assets"), name
        assert g["South Zone Debtors"]["nature"] == "assets" and g["Sales Accounts"]["nature"] == "income"
        assert await count(s, "tally_groups", ws) == 32


async def test_ledger_balance_without_stated_base_stored_null_needs_tb(app_client, session, engine):
    """D3. Variant: the real G5 USD re-read with the stated `= -? 132929.85` base cut off (an expression Tally can
    render without a base) -> money NULL, `balance_source = needs_tb`, face/rate/currency kept."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    usd = [o for o in realdata.g5_ledger_balances() if o["data"]["name"] == "Gulf Office Supplies LLC (USD)"]
    usd[0]["data"]["closingbalance"] = usd[0]["data"]["closingbalance"].split(" = ")[0]
    assert usd[0]["data"]["closingbalance"] == "-$1609.71 @ ? 82.58/$"
    body = await post_ok(app_client, ws, headers, run_id, usd)
    assert body["counts"]["balances_applied"] == 1
    async with fresh(engine) as s:
        led = await one(s, "SELECT * FROM tally_ledgers WHERE workspace_id=:w AND name=:n", w=ws,
                        n="Gulf Office Supplies LLC (USD)")
        assert (led["closing_balance"], led["balance_source"], led["closing_fx_amount"], led["closing_fx_rate"],
                led["fx_currency"], led["is_forex"]) == (None, "needs_tb", Decimal("-1609.71"), Decimal("82.58"),
                                                         "$", True)
        assert led["balance_text"]["closing"] == "-$1609.71 @ ? 82.58/$"


async def test_raw_kept_only_for_newest_two_fys_at_insert(app_client, session, engine):
    """§4.9: coverage runs FY 2022-23 .. FY 2026-27 (clock 2026-09-25 IST); newest two = 2026-27, 2025-26. A FY
    2022-23 voucher is stored with `raw = NULL`; a FY 2025-26 one keeps `raw` (both from `p03_B_vouchers_flags.xml`,
    whose month request fetched no ledger entries -- `ledger_entries: []`)."""
    ws, headers, run_id = await _b_with_masters(app_client, session)
    flags = realdata.vouchers("p03_B_vouchers_flags.xml")
    old = next(v for v in flags if v["data"]["date"].startswith("2022") and v["data"]["iscancelled"] == "No")
    new = next(v for v in flags if v["data"]["date"] >= "20250401" and v["data"]["date"] < "20260401"
               and v["data"]["iscancelled"] == "No")
    await post_ok(app_client, ws, headers, run_id, [old, new])
    async with fresh(engine) as s:
        o = (await voucher_state(s, ws, old["data"]["guid"]))["voucher"]
        n = (await voucher_state(s, ws, new["data"]["guid"]))["voucher"]
        assert o["raw"] is None
        assert n["raw"] == new["data"]
        assert await count(s, "tally_vouchers", ws) == 2
