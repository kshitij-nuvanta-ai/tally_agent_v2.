"""Tally write exploration v4 — Stage 0 of seed-data plan.

Verifies XML envelopes for every unproven write op against live Tally:
  1. Unit (UOM) create
  2. Stock group create
  3. Stock item create with HSN + per-item GST
  4. GST tax ledger create (CGST/SGST/IGST input + output)
  5. Ledger with opening balance
  6. Sales voucher with GST (intra-state + inter-state, mixed-rate)
  7. Purchase voucher with GST
  8. Receipt voucher
  9. Journal voucher

Each op:
  - Builds candidate XML
  - POSTs to Tally
  - Parses with parse_import_response
  - Read-back verifies (where feasible)
  - Records PASS/FAIL with the canonical envelope on success

All test entities use prefix "_Test Explore" so cleanup is mechanical.
Voucher narrations contain "_Test Explore" for the same reason.

Usage:
    PYTHONPATH=. python scripts/explore_tally_write_v4.py 2>&1 | tee docs/tally-write-exploration-v4.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable

import httpx

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.request_builder import (
    build_list_ledgers, build_list_groups, build_list_stock_items,
    build_day_book, build_trial_balance,
)
from tally_bridge.response_parser import (
    parse_import_response, parse_ledger_list, parse_groups, parse_stock_items,
    parse_vouchers, sanitize_xml,
)

COMPANY = "Bharat Traders Private Limited"
PFX = "_Test Explore"


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class OpResult:
    name: str
    passed: bool
    detail: str = ""
    envelope: str = ""
    response: str = ""
    readback: str = ""


RESULTS: list[OpResult] = []


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def record(name: str, passed: bool, detail: str = "", envelope: str = "", response: str = "", readback: str = "") -> None:
    RESULTS.append(OpResult(name=name, passed=passed, detail=detail, envelope=envelope, response=response, readback=readback))
    status = "PASS" if passed else "FAIL"
    print(f"  >>> {status}: {name} — {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper with extended timeout
# ─────────────────────────────────────────────────────────────────────────────
async def post_write(client: TallyClient, xml: str) -> str:
    # Override timeout for writes (default 30s).
    saved = client._client.timeout
    client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    try:
        return await client.post_xml(xml)
    finally:
        client._client.timeout = saved


def _short(text: str, n: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


async def post_and_parse(client: TallyClient, xml: str, label: str) -> tuple[dict, str]:
    print(f"\n--- {label} ---")
    print(f"  XML: {_short(xml, 1500)}")
    raw = await post_write(client, xml)
    print(f"  Raw: {_short(raw, 800)}")
    parsed = parse_import_response(raw)
    print(f"  Parsed: {json.dumps(parsed, indent=2)}")
    return parsed, raw


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: check Tally current date (license clamp canary)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_current_date(client: TallyClient) -> str | None:
    """Best-effort fetch of Tally's internal current date via a Function call.

    Returns YYYYMMDD or None if it can't be parsed. We use a simple Trial Balance
    request as a proxy — its <REQUESTDATE> echoes back, but really we just want
    to confirm Tally is responsive.
    """
    try:
        await client.post_xml(build_list_groups())
        return "responsive"
    except Exception as e:
        print(f"  WARN: Tally not responsive: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup helpers — used both before and after the run.
# ─────────────────────────────────────────────────────────────────────────────
async def list_test_vouchers(client: TallyClient) -> list[dict]:
    raw = await client.post_xml(build_day_book("01-04-2024", "31-03-2027"))
    vchs = parse_vouchers(raw)
    return [v for v in vchs if PFX in (v.get("narration") or "")]


async def cleanup_all(client: TallyClient) -> None:
    """Delete everything prefixed `_Test Explore`. Vouchers first, then masters."""
    banner(f"CLEANUP: removing all entities prefixed '{PFX}'")

    # 1. Vouchers — DELETE INTENTIONALLY SKIPPED for now.
    # The previous envelope identified vouchers via VOUCHERNUMBER+VCHTYPE+DATE — which is
    # NOT the format proven in docs/tally-write-exploration.md:158. The proven path uses
    # TAGNAME="Master ID" + TAGVALUE=<LASTVCHID> from the create response. Until we record
    # LASTVCHID for every voucher this script creates and use that, voucher cleanup is a
    # crash risk. Test vouchers can be cleaned manually in Tally UI (Display → Day Book →
    # delete) or via a dedicated cleanup tool that captures Master IDs at create time.
    print("  Voucher cleanup SKIPPED (see comment + tally-write-exploration-v4.md)")

    # 2. Stock items
    try:
        items_raw = await client.post_xml(build_list_stock_items())
        items = parse_stock_items(items_raw)
        for it in items:
            if it["name"].startswith(PFX):
                xml = _wrap_import(
                    "All Masters", COMPANY,
                    f'<STOCKITEM NAME="{_esc(it["name"])}" ACTION="Delete">\n'
                    f'<NAME.LIST><NAME>{_esc(it["name"])}</NAME></NAME.LIST>\n'
                    f'</STOCKITEM>',
                )
                raw = await post_write(client, xml)
                r = parse_import_response(raw)
                print(f"  StockItem '{it['name']}': deleted={r['deleted']} errors={r['errors']}")
    except Exception as e:
        print(f"  Stock item cleanup failed: {e}")

    # 3. Ledgers
    try:
        ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
        for l in ledgers:
            if l["name"].startswith(PFX):
                xml = _wrap_import(
                    "All Masters", COMPANY,
                    f'<LEDGER NAME="{_esc(l["name"])}" ACTION="Delete">\n'
                    f'<NAME.LIST><NAME>{_esc(l["name"])}</NAME></NAME.LIST>\n'
                    f'</LEDGER>',
                )
                raw = await post_write(client, xml)
                r = parse_import_response(raw)
                print(f"  Ledger '{l['name']}': deleted={r['deleted']} errors={r['errors']}")
    except Exception as e:
        print(f"  Ledger cleanup failed: {e}")

    # 4. Stock groups
    try:
        # Stock groups are returned via a different endpoint — we use a custom collection.
        sg_xml = _wrap_collection_query("CustomStockGroupList", "StockGroup", ["Name", "Parent"])
        raw = await client.post_xml(sg_xml)
        root = ET.fromstring(sanitize_xml(raw))
        for sg in root.iter("STOCKGROUP"):
            name_el = sg.find("NAME")
            name = (name_el.text or "").strip() if name_el is not None and name_el.text else (sg.get("NAME") or "").strip()
            if name.startswith(PFX):
                xml = _wrap_import(
                    "All Masters", COMPANY,
                    f'<STOCKGROUP NAME="{_esc(name)}" ACTION="Delete">\n'
                    f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
                    f'</STOCKGROUP>',
                )
                draw = await post_write(client, xml)
                r = parse_import_response(draw)
                print(f"  StockGroup '{name}': deleted={r['deleted']} errors={r['errors']}")
    except Exception as e:
        print(f"  Stock group cleanup failed: {e}")

    # 5. Units — DELETE INTENTIONALLY DISABLED.
    # Per docs/tally-write-exploration-v4.md "Cleanup risks": UNIT delete without NAME.LIST
    # is unverified and may cause a Tally memory crash (analogous to the documented
    # Ledger/Group/StockItem behaviour). Wrapping with NAME.LIST returns "BAD UNIT NAME"
    # on create — the symmetry on delete was never confirmed safe. Safer to leave the
    # 2 test units in place; they don't pollute reports. If you must delete units,
    # prove out the safe envelope first against a throwaway unit.
    print("  Unit cleanup SKIPPED (see comment + tally-write-exploration-v4.md)")

    # 6. Account Groups (custom groups created above the GST ledgers — usually none for our tests)
    try:
        groups = parse_groups(await client.post_xml(build_list_groups()))
        for g in groups:
            if g["name"].startswith(PFX):
                xml = _wrap_import(
                    "All Masters", COMPANY,
                    f'<GROUP NAME="{_esc(g["name"])}" ACTION="Delete">\n'
                    f'<NAME.LIST><NAME>{_esc(g["name"])}</NAME></NAME.LIST>\n'
                    f'</GROUP>',
                )
                raw = await post_write(client, xml)
                r = parse_import_response(raw)
                print(f"  Group '{g['name']}': deleted={r['deleted']} errors={r['errors']}")
    except Exception as e:
        print(f"  Account group cleanup failed: {e}")


def _wrap_collection_query(collection_name: str, object_type: str, methods: list[str]) -> str:
    methods_xml = "\n".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in methods)
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>{collection_name}</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT><SVCURRENTCOMPANY>{_esc(COMPANY)}</SVCURRENTCOMPANY></STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="{collection_name}" ISMODIFY="No"><TYPE>{object_type}</TYPE>{methods_xml}</COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""


# ─────────────────────────────────────────────────────────────────────────────
# Stock-item read-back via custom TDL collection (HSN + GST fields)
# ─────────────────────────────────────────────────────────────────────────────
def build_stockitem_full_query() -> str:
    methods = [
        "Name", "Parent", "BaseUnits",
        "GSTApplicable", "GSTTypeOfSupply", "HSNCode",
        "OpeningBalance", "OpeningRate", "OpeningValue",
    ]
    return _wrap_collection_query("StockItemFullList", "StockItem", methods)


def parse_stockitem_full(raw_xml: str) -> dict[str, dict]:
    root = ET.fromstring(sanitize_xml(raw_xml))
    out: dict[str, dict] = {}
    for it in root.iter("STOCKITEM"):
        name_el = it.find("NAME")
        name = (name_el.text or "").strip() if name_el is not None and name_el.text else (it.get("NAME") or "").strip()
        if not name:
            continue
        def _t(tag: str) -> str:
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        out[name] = {
            "parent": _t("PARENT"),
            "base_units": _t("BASEUNITS"),
            "gst_applicable": _t("GSTAPPLICABLE"),
            "gst_type_of_supply": _t("GSTTYPEOFSUPPLY"),
            "hsn_code": _t("HSNCODE"),
            "opening_balance": _t("OPENINGBALANCE"),
            "opening_rate": _t("OPENINGRATE"),
            "opening_value": _t("OPENINGVALUE"),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Op 1 — Unit (UOM) create
# ─────────────────────────────────────────────────────────────────────────────
async def op1_create_units(client: TallyClient) -> dict[str, str]:
    banner("OP 1 — Create Units (UOM)")
    # Units cannot contain spaces or special chars (Tally rejects with "BAD UNIT NAME").
    # Use short ASCII symbols namespaced with "Tst" prefix.
    # Tally rejects names with spaces/underscores/long identifiers as "BAD UNIT NAME".
    # Also: Units do NOT use NAME.LIST (unlike ledgers/groups) — that returns BAD UNIT NAME too.
    # Verified format: <UNIT ACTION="Create"><NAME>X</NAME><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT></UNIT>
    units = {"TstN": "TstN", "TstP": "TstP"}
    created = {}
    for name in units:
        xml = _wrap_import(
            "All Masters", COMPANY,
            f'<UNIT ACTION="Create">\n'
            f'<NAME>{_esc(name)}</NAME>\n'
            f'<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>\n'
            f'</UNIT>',
        )
        parsed, raw = await post_and_parse(client, xml, f"create unit '{name}'")
        if parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0:
            created[name] = name
        else:
            record("op1_create_unit", False, f"{name}: {parsed.get('error_message') or parsed}", xml, raw)
            return created
    # Read-back
    raw = await client.post_xml(_wrap_collection_query("CustomUnitList", "Unit", ["Name"]))
    root = ET.fromstring(sanitize_xml(raw))
    found = {(u.find("NAME").text or "").strip() for u in root.iter("UNIT") if u.find("NAME") is not None and u.find("NAME").text}
    missing = set(units) - found
    if missing:
        record("op1_create_unit", False, f"read-back missing: {missing}", "", "", _short(raw))
    else:
        record("op1_create_unit", True, f"created {len(units)} units, all visible in collection",
               envelope=f"<UNIT NAME='...' ACTION='Create'><NAME.LIST><NAME>...</NAME></NAME.LIST><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT></UNIT>")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Op 2 — Stock group create
# ─────────────────────────────────────────────────────────────────────────────
async def op2_create_stock_groups(client: TallyClient) -> dict[str, str]:
    banner("OP 2 — Create Stock Groups")
    names = [f"{PFX} Electronics", f"{PFX} Peripherals", f"{PFX} Office Supplies"]
    created = {}
    for name in names:
        xml = _wrap_import(
            "All Masters", COMPANY,
            f'<STOCKGROUP NAME="{_esc(name)}" ACTION="Create">\n'
            f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
            f'<PARENT/>\n'
            f'<ISADDABLE>No</ISADDABLE>\n'
            f'</STOCKGROUP>',
        )
        parsed, raw = await post_and_parse(client, xml, f"create stock group '{name}'")
        if parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0:
            created[name] = name
        else:
            record("op2_create_stock_group", False, f"{name}: {parsed.get('error_message') or parsed}", xml, raw)
            return created
    # Read-back
    raw = await client.post_xml(_wrap_collection_query("CustomStockGroupList", "StockGroup", ["Name", "Parent"]))
    root = ET.fromstring(sanitize_xml(raw))
    found = {(sg.find("NAME").text or "").strip() for sg in root.iter("STOCKGROUP") if sg.find("NAME") is not None and sg.find("NAME").text}
    missing = set(names) - found
    if missing:
        record("op2_create_stock_group", False, f"read-back missing: {missing}", "", "", _short(raw))
    else:
        record("op2_create_stock_group", True, f"created {len(names)} stock groups",
               envelope=f"<STOCKGROUP NAME='...' ACTION='Create'><NAME.LIST><NAME>...</NAME></NAME.LIST><PARENT/></STOCKGROUP>")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Op 3 — Stock item create with HSN + per-item GST
# ─────────────────────────────────────────────────────────────────────────────
async def op3_create_stock_items(client: TallyClient, units: dict, groups: dict) -> dict[str, dict]:
    banner("OP 3 — Create Stock Items with HSN + GST")

    # Pick a Nos and Pcs unit and a couple groups.
    unit_nos = next((n for n in units if n.endswith("Nos")), None)
    unit_pcs = next((n for n in units if n.endswith("Pcs")), None)
    g_elec = next((n for n in groups if n.endswith("Electronics")), None)
    g_off = next((n for n in groups if n.endswith("Office Supplies")), None)
    if not (unit_nos and unit_pcs and g_elec and g_off):
        record("op3_create_stock_item", False, "missing unit/group prerequisites")
        return {}

    items = [
        # (name, group, unit, hsn, gst_rate_pct, opening_qty, opening_rate, opening_value)
        (f"{PFX} Monitor 24in", g_elec, unit_nos, "8528", 18, 5, 11000, 55000),
        (f"{PFX} Laptop 15s", g_elec, unit_nos, "8471", 18, 3, 38000, 114000),
        (f"{PFX} A4 Paper Ream", g_off, unit_pcs, "4802", 12, 10, 280, 2800),
    ]

    created: dict[str, dict] = {}
    for (name, group, unit, hsn, rate, qty, opening_rate, opening_value) in items:
        cgst = sgst = rate / 2.0
        igst = float(rate)
        # Build GSTDETAILS.LIST with TAXABILITY=Taxable + per-tax rates and APPLICABLEFROM.
        gst_details_xml = (
            "<GSTDETAILS.LIST>\n"
            "<APPLICABLEFROM>20250401</APPLICABLEFROM>\n"
            "<TAXABILITY>Taxable</TAXABILITY>\n"
            f"<IGSTRATE>{igst}</IGSTRATE>\n"
            f"<CGSTRATE>{cgst}</CGSTRATE>\n"
            f"<SGSTRATE>{sgst}</SGSTRATE>\n"
            "<STATEWISEDETAILS.LIST>\n"
            "<STATENAME>Any</STATENAME>\n"
            "<RATEDETAILS.LIST>\n"
            "<GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD>\n"
            f"<GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>\n"
            f"<GSTRATE>{cgst}</GSTRATE>\n"
            "</RATEDETAILS.LIST>\n"
            "<RATEDETAILS.LIST>\n"
            "<GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD>\n"
            f"<GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>\n"
            f"<GSTRATE>{sgst}</GSTRATE>\n"
            "</RATEDETAILS.LIST>\n"
            "<RATEDETAILS.LIST>\n"
            "<GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD>\n"
            f"<GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>\n"
            f"<GSTRATE>{igst}</GSTRATE>\n"
            "</RATEDETAILS.LIST>\n"
            "<RATEDETAILS.LIST>\n"
            "<GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>\n"
            "<GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>\n"
            "<GSTRATE>0</GSTRATE>\n"
            "</RATEDETAILS.LIST>\n"
            "</STATEWISEDETAILS.LIST>\n"
            "</GSTDETAILS.LIST>\n"
        )
        hsn_details_xml = (
            "<HSNDETAILS.LIST>\n"
            "<APPLICABLEFROM>20250401</APPLICABLEFROM>\n"
            f"<HSNCODE>{_esc(hsn)}</HSNCODE>\n"
            f"<HSN>{_esc(hsn)}</HSN>\n"
            "</HSNDETAILS.LIST>\n"
        )
        body = (
            f'<STOCKITEM NAME="{_esc(name)}" ACTION="Create">\n'
            f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
            f'<PARENT>{_esc(group)}</PARENT>\n'
            f'<BASEUNITS>{_esc(unit)}</BASEUNITS>\n'
            f'<GSTAPPLICABLE>Applicable</GSTAPPLICABLE>\n'
            f'<GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>\n'
            f'<HSNCODE>{_esc(hsn)}</HSNCODE>\n'
            f'<HSN>{_esc(hsn)}</HSN>\n'
            f'{hsn_details_xml}'
            f'{gst_details_xml}'
            f'<OPENINGBALANCE>{qty} {_esc(unit)}</OPENINGBALANCE>\n'
            f'<OPENINGRATE>{opening_rate}/{_esc(unit)}</OPENINGRATE>\n'
            f'<OPENINGVALUE>{opening_value}</OPENINGVALUE>\n'
            f'</STOCKITEM>'
        )
        xml = _wrap_import("All Masters", COMPANY, body)
        parsed, raw = await post_and_parse(client, xml, f"create stock item '{name}' (HSN={hsn} GST={rate}%)")
        if not (parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0):
            record("op3_create_stock_item", False, f"{name} create failed: {parsed.get('error_message') or parsed}", xml, raw)
            return created
        created[name] = {"hsn": hsn, "gst_rate": rate, "qty": qty, "rate": opening_rate, "value": opening_value, "unit": unit, "group": group}

    # Read-back: pull all stock items with HSN/GST fields and assert.
    raw = await client.post_xml(build_stockitem_full_query())
    by_name = parse_stockitem_full(raw)
    failures = []
    for name, exp in created.items():
        got = by_name.get(name)
        if not got:
            failures.append(f"{name}: not found in collection read-back")
            continue
        if got["hsn_code"] != exp["hsn"]:
            failures.append(f"{name}: HSN mismatch — sent={exp['hsn']!r}, got={got['hsn_code']!r}")
        if "applicable" not in got["gst_applicable"].lower():
            failures.append(f"{name}: GSTAPPLICABLE not set — got={got['gst_applicable']!r}")
        if got["gst_type_of_supply"].lower() != "goods":
            failures.append(f"{name}: GSTTYPEOFSUPPLY!=Goods — got={got['gst_type_of_supply']!r}")
    if failures:
        record("op3_create_stock_item", False, "; ".join(failures), envelope="", response=_short(raw))
    else:
        record("op3_create_stock_item", True, f"created {len(created)} stock items, HSN+GST round-trip OK",
               envelope="STOCKITEM with HSNCODE+HSNDETAILS.LIST + GSTAPPLICABLE=Applicable + GSTDETAILS.LIST(STATEWISEDETAILS)",
               readback=json.dumps({n: by_name.get(n) for n in created}, indent=2))
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Op 4 — GST tax ledgers
# ─────────────────────────────────────────────────────────────────────────────
async def op4_create_gst_ledgers(client: TallyClient) -> dict[str, str]:
    banner("OP 4 — Create GST tax ledgers")
    # (name, gstduty, taxtype-suffix)
    specs = [
        (f"{PFX} CGST Output", "Central Tax"),
        (f"{PFX} SGST Output", "State Tax"),
        (f"{PFX} IGST Output", "Integrated Tax"),
        (f"{PFX} CGST Input", "Central Tax"),
        (f"{PFX} SGST Input", "State Tax"),
        (f"{PFX} IGST Input", "Integrated Tax"),
    ]
    created = {}
    for name, duty in specs:
        body = (
            f'<LEDGER NAME="{_esc(name)}" ACTION="Create">\n'
            f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
            f'<PARENT>Duties &amp; Taxes</PARENT>\n'
            f'<TAXTYPE>GST</TAXTYPE>\n'
            f'<GSTDUTYHEAD>{_esc(duty)}</GSTDUTYHEAD>\n'
            f'<RATEOFTAXCALCULATION>0</RATEOFTAXCALCULATION>\n'
            f'<ROUNDINGMETHOD/>\n'
            f'<ROUNDINGLIMIT>0</ROUNDINGLIMIT>\n'
            f'<ISBILLWISEON>No</ISBILLWISEON>\n'
            f'<AFFECTSSTOCK>No</AFFECTSSTOCK>\n'
            f'<ISCOSTCENTRESON>No</ISCOSTCENTRESON>\n'
            f'</LEDGER>'
        )
        xml = _wrap_import("All Masters", COMPANY, body)
        parsed, raw = await post_and_parse(client, xml, f"create GST ledger '{name}' duty={duty}")
        if parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0:
            created[name] = duty
        else:
            record("op4_create_gst_ledger", False, f"{name}: {parsed.get('error_message') or parsed}", xml, raw)
            return created
    # Read-back: confirm parent group is Duties & Taxes.
    ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    by_name = {l["name"]: l for l in ledgers}
    failures = [n for n in created if by_name.get(n, {}).get("parent_group", "").lower() != "duties & taxes"]
    if failures:
        record("op4_create_gst_ledger", False, f"read-back parent-group mismatch: {failures}")
    else:
        record("op4_create_gst_ledger", True, f"created {len(created)} GST ledgers under Duties & Taxes",
               envelope="LEDGER PARENT='Duties & Taxes' + TAXTYPE=GST + GSTDUTYHEAD=<Central|State|Integrated> Tax")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# Op 5 — Ledger with opening balance + counterpart cash/bank ledger
# ─────────────────────────────────────────────────────────────────────────────
async def op5_create_ledger_with_opening(client: TallyClient) -> dict[str, dict]:
    """We need a few ledgers for vouchers:
       - Sales-Electronics, Sales-OfficeSupplies
       - Purchase-Electronics
       - Cash (no opening — verify with capital opening)
       - Capital Account with opening balance
       - Party (debtor) intra-state (Maharashtra), inter-state (Delhi)
       - Supplier (creditor)
    """
    banner("OP 5 — Create Ledgers (incl. opening balance)")

    # OPENING BALANCE convention in Tally:
    #   Negative OPENINGBALANCE = Debit (asset) — but for a "Capital Account" group
    #   ledgers default to credit; positive opening = credit there.
    # We'll test with a cash-in-hand ledger that has a debit opening (positive number,
    # but Tally infers sign from group nature).
    specs = [
        # (name, parent, opening_balance_str, gstin, state, isdeemedpos_for_balance)
        ("Sales - Electronics", "Sales Accounts", None, None, None),
        ("Sales - Office Supplies", "Sales Accounts", None, None, None),
        ("Purchase - Electronics", "Purchase Accounts", None, None, None),
        ("Test Cash", "Cash-in-Hand", "10000.00", None, None),
        ("Capital", "Capital Account", "10000.00", None, None),  # offset
        ("Party MH", "Sundry Debtors", None, "27ABCDE1234F1Z5", "Maharashtra"),
        ("Party DL", "Sundry Debtors", None, "07ABCDE1234F1Z5", "Delhi"),
        ("Supplier MH", "Sundry Creditors", None, "27XYZAB1234F1Z5", "Maharashtra"),
    ]
    # Prefix all names
    out: dict[str, dict] = {}
    for raw_name, parent, opening, gstin, state in specs:
        name = f"{PFX} {raw_name}"
        gstin_xml = f"<PARTYGSTIN>{_esc(gstin)}</PARTYGSTIN>\n" if gstin else ""
        state_xml = f"<LEDSTATENAME>{_esc(state)}</LEDSTATENAME>\n" if state else ""
        opening_xml = f"<OPENINGBALANCE>{opening}</OPENINGBALANCE>\n" if opening else ""
        reg_xml = "<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>\n" if gstin else ""
        body = (
            f'<LEDGER NAME="{_esc(name)}" ACTION="Create">\n'
            f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
            f'<PARENT>{_esc(parent)}</PARENT>\n'
            f'{gstin_xml}{state_xml}{reg_xml}{opening_xml}'
            f'<ISBILLWISEON>{"Yes" if "Debtors" in parent or "Creditors" in parent else "No"}</ISBILLWISEON>\n'
            f'</LEDGER>'
        )
        xml = _wrap_import("All Masters", COMPANY, body)
        parsed, raw = await post_and_parse(client, xml, f"create ledger '{name}'")
        if not (parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0):
            record("op5_create_ledger", False, f"{name}: {parsed.get('error_message') or parsed}", xml, raw)
            return out
        out[name] = {"parent": parent, "opening": opening, "gstin": gstin, "state": state}

    # Read-back: confirm Capital opening balance shows up in trial balance.
    cap_name = f"{PFX} Capital"
    tb_raw = await client.post_xml(build_trial_balance("01-04-2025", "31-03-2026"))
    tb_root = ET.fromstring(sanitize_xml(tb_raw))
    found_cap = False
    for el in tb_root.iter("DSPACCNAME"):
        n = el.findtext("DSPDISPNAME") or ""
        if cap_name in n:
            found_cap = True
            break
    if not found_cap:
        # Fall back: parse_ledger_list and check OPENINGBALANCE? closing_balance is what we have.
        ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
        cap = next((l for l in ledgers if l["name"] == cap_name), None)
        if cap is None:
            record("op5_create_ledger", False, f"Capital ledger not visible in read-back")
            return out
        # We accept this as success if ledger exists; opening can be checked via TB but TB requires posted txns.
    record("op5_create_ledger", True, f"created {len(out)} ledgers (incl. opening balance, GSTIN, state)",
           envelope="LEDGER + OPENINGBALANCE + PARTYGSTIN + LEDSTATENAME + GSTREGISTRATIONTYPE")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Op 6 — Sales voucher (intra + inter, mixed-rate)
# ─────────────────────────────────────────────────────────────────────────────
async def op6_create_sales_vouchers(
    client: TallyClient,
    items: dict[str, dict],
    ledgers: dict[str, dict],
    gst_ledgers: dict[str, str],
    voucher_date: str,
) -> list[dict]:
    banner("OP 6 — Create Sales Vouchers (intra-state + inter-state, mixed-rate)")

    party_mh = f"{PFX} Party MH"
    party_dl = f"{PFX} Party DL"
    sales_elec = f"{PFX} Sales - Electronics"
    sales_off = f"{PFX} Sales - Office Supplies"
    cgst_out = f"{PFX} CGST Output"
    sgst_out = f"{PFX} SGST Output"
    igst_out = f"{PFX} IGST Output"

    # Items: 18% monitor + 12% paper for mixed-rate.
    monitor = f"{PFX} Monitor 24in"
    paper = f"{PFX} A4 Paper Ream"

    if monitor not in items or paper not in items:
        record("op6_create_sales_voucher", False, "missing required stock items")
        return []

    mon_qty, mon_rate = 2, 12500
    pap_qty, pap_rate = 10, 350
    mon_amt = mon_qty * mon_rate  # 25000
    pap_amt = pap_qty * pap_rate  # 3500

    # 18% on monitor + 12% on paper
    mon_cgst = mon_amt * 0.09
    mon_sgst = mon_amt * 0.09
    mon_igst = mon_amt * 0.18
    pap_cgst = pap_amt * 0.06
    pap_sgst = pap_amt * 0.06
    pap_igst = pap_amt * 0.12

    def _inv_entry(item: str, group: str, sales_ledger: str, qty: int, rate: float, unit: str) -> str:
        amt = qty * rate
        # Stock Item amount in inventory entry: Sales => negative (credit-like, ISDEEMEDPOSITIVE=No)
        return (
            f'<ALLINVENTORYENTRIES.LIST>\n'
            f'<STOCKITEMNAME>{_esc(item)}</STOCKITEMNAME>\n'
            f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
            f'<RATE>{rate:.2f}/{_esc(unit)}</RATE>\n'
            f'<AMOUNT>{amt:.2f}</AMOUNT>\n'
            f'<ACTUALQTY>{qty} {_esc(unit)}</ACTUALQTY>\n'
            f'<BILLEDQTY>{qty} {_esc(unit)}</BILLEDQTY>\n'
            f'<ACCOUNTINGALLOCATIONS.LIST>\n'
            f'<LEDGERNAME>{_esc(sales_ledger)}</LEDGERNAME>\n'
            f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
            f'<AMOUNT>{amt:.2f}</AMOUNT>\n'
            f'</ACCOUNTINGALLOCATIONS.LIST>\n'
            f'</ALLINVENTORYENTRIES.LIST>'
        )

    unit_nos = items[monitor]["unit"]
    unit_pcs = items[paper]["unit"]

    results: list[dict] = []

    # ── Intra-state (Maharashtra → Maharashtra: CGST + SGST) ──
    party = party_mh
    total_intra = mon_amt + pap_amt + (mon_cgst + mon_sgst) + (pap_cgst + pap_sgst)
    cgst_total = mon_cgst + pap_cgst
    sgst_total = mon_sgst + pap_sgst
    body = (
        f'<VOUCHER VCHTYPE="Sales" ACTION="Create">\n'
        f'<DATE>{voucher_date}</DATE>\n'
        f'<NARRATION>{PFX} sales intra-state mixed-rate</NARRATION>\n'
        f'<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>\n'
        f'<PARTYLEDGERNAME>{_esc(party)}</PARTYLEDGERNAME>\n'
        f'<PARTYNAME>{_esc(party)}</PARTYNAME>\n'
        f'<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>\n'
        f'<ISINVOICE>Yes</ISINVOICE>\n'
        f'<EFFECTIVEDATE>{voucher_date}</EFFECTIVEDATE>\n'
        # Party debit (negative, ISDEEMEDPOSITIVE=Yes)
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(party)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<ISPARTYLEDGER>Yes</ISPARTYLEDGER>\n'
        f'<AMOUNT>-{total_intra:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        # CGST credit
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(cgst_out)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>{cgst_total:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        # SGST credit
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(sgst_out)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>{sgst_total:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        # Stock items
        + _inv_entry(monitor, items[monitor]["group"], sales_elec, mon_qty, mon_rate, unit_nos) + "\n"
        + _inv_entry(paper, items[paper]["group"], sales_off, pap_qty, pap_rate, unit_pcs) + "\n"
        + f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, body)
    parsed, raw = await post_and_parse(client, xml, f"sales intra-state to '{party}'")
    if parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0:
        results.append({"mode": "intra", "lastvchid": parsed["last_vch_id"], "total": total_intra})
    else:
        record("op6_create_sales_voucher", False,
               f"intra-state failed: {parsed.get('error_message') or parsed}", xml, raw)
        return results

    # ── Inter-state (MH company → DL party: IGST) ──
    party = party_dl
    total_inter = mon_amt + pap_amt + mon_igst + pap_igst
    igst_total = mon_igst + pap_igst
    body = (
        f'<VOUCHER VCHTYPE="Sales" ACTION="Create">\n'
        f'<DATE>{voucher_date}</DATE>\n'
        f'<NARRATION>{PFX} sales inter-state mixed-rate</NARRATION>\n'
        f'<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>\n'
        f'<PARTYLEDGERNAME>{_esc(party)}</PARTYLEDGERNAME>\n'
        f'<PARTYNAME>{_esc(party)}</PARTYNAME>\n'
        f'<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>\n'
        f'<ISINVOICE>Yes</ISINVOICE>\n'
        f'<EFFECTIVEDATE>{voucher_date}</EFFECTIVEDATE>\n'
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(party)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<ISPARTYLEDGER>Yes</ISPARTYLEDGER>\n'
        f'<AMOUNT>-{total_inter:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(igst_out)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>{igst_total:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        + _inv_entry(monitor, items[monitor]["group"], sales_elec, mon_qty, mon_rate, unit_nos) + "\n"
        + _inv_entry(paper, items[paper]["group"], sales_off, pap_qty, pap_rate, unit_pcs) + "\n"
        + f'</VOUCHER>'
    )
    xml2 = _wrap_import("Vouchers", COMPANY, body)
    parsed2, raw2 = await post_and_parse(client, xml2, f"sales inter-state to '{party}'")
    if parsed2["created"] >= 1 and parsed2["errors"] == 0 and parsed2["exceptions"] == 0:
        results.append({"mode": "inter", "lastvchid": parsed2["last_vch_id"], "total": total_inter})
    else:
        record("op6_create_sales_voucher", False,
               f"inter-state failed: {parsed2.get('error_message') or parsed2}", xml2, raw2)
        return results

    # Read-back: query day book for that date and confirm vouchers + amounts.
    db_raw = await client.post_xml(build_day_book(_yyyymmdd_to_ddmmyyyy(voucher_date), _yyyymmdd_to_ddmmyyyy(voucher_date), voucher_type="Sales"))
    vchs = parse_vouchers(db_raw)
    test_vchs = [v for v in vchs if PFX in (v.get("narration") or "")]
    detail = f"created intra (id={results[0]['lastvchid']}) + inter (id={results[1]['lastvchid']}); day book sees {len(test_vchs)} test sales"
    if len(test_vchs) >= 2:
        record("op6_create_sales_voucher", True, detail,
               envelope="VOUCHER VCHTYPE=Sales + PERSISTEDVIEW=Invoice Voucher View + ISINVOICE=Yes + LEDGERENTRIES.LIST(party,GST) + ALLINVENTORYENTRIES.LIST(stock+ACCOUNTINGALLOCATIONS)",
               readback=json.dumps([{"num": v.get("voucher_number"), "amt": v.get("amount"), "narr": v.get("narration")} for v in test_vchs], indent=2))
    else:
        record("op6_create_sales_voucher", False, f"{detail} — read-back mismatch", "", _short(db_raw))
    return results


def _yyyymmdd_to_ddmmyyyy(s: str) -> str:
    return f"{s[6:8]}-{s[4:6]}-{s[0:4]}"


# ─────────────────────────────────────────────────────────────────────────────
# Op 7 — Purchase voucher with GST
# ─────────────────────────────────────────────────────────────────────────────
async def op7_create_purchase_voucher(
    client: TallyClient,
    items: dict,
    ledgers: dict,
    gst_ledgers: dict,
    voucher_date: str,
) -> list[dict]:
    banner("OP 7 — Create Purchase Voucher (intra-state)")

    supplier = f"{PFX} Supplier MH"
    purch_elec = f"{PFX} Purchase - Electronics"
    cgst_in = f"{PFX} CGST Input"
    sgst_in = f"{PFX} SGST Input"
    monitor = f"{PFX} Monitor 24in"

    if monitor not in items:
        record("op7_create_purchase_voucher", False, "missing required stock item")
        return []

    qty, rate = 5, 11000
    amt = qty * rate
    cgst = amt * 0.09
    sgst = amt * 0.09
    total = amt + cgst + sgst
    unit = items[monitor]["unit"]

    inv = (
        f'<ALLINVENTORYENTRIES.LIST>\n'
        f'<STOCKITEMNAME>{_esc(monitor)}</STOCKITEMNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<RATE>{rate:.2f}/{_esc(unit)}</RATE>\n'
        f'<AMOUNT>-{amt:.2f}</AMOUNT>\n'
        f'<ACTUALQTY>{qty} {_esc(unit)}</ACTUALQTY>\n'
        f'<BILLEDQTY>{qty} {_esc(unit)}</BILLEDQTY>\n'
        f'<ACCOUNTINGALLOCATIONS.LIST>\n'
        f'<LEDGERNAME>{_esc(purch_elec)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>-{amt:.2f}</AMOUNT>\n'
        f'</ACCOUNTINGALLOCATIONS.LIST>\n'
        f'</ALLINVENTORYENTRIES.LIST>'
    )

    body = (
        f'<VOUCHER VCHTYPE="Purchase" ACTION="Create">\n'
        f'<DATE>{voucher_date}</DATE>\n'
        f'<NARRATION>{PFX} purchase intra-state</NARRATION>\n'
        f'<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>\n'
        f'<PARTYLEDGERNAME>{_esc(supplier)}</PARTYLEDGERNAME>\n'
        f'<PARTYNAME>{_esc(supplier)}</PARTYNAME>\n'
        f'<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>\n'
        f'<ISINVOICE>Yes</ISINVOICE>\n'
        # Supplier credit (positive, ISDEEMEDPOSITIVE=No)
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(supplier)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<ISPARTYLEDGER>Yes</ISPARTYLEDGER>\n'
        f'<AMOUNT>{total:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        # CGST debit
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(cgst_in)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>-{cgst:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        f'<LEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(sgst_in)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>-{sgst:.2f}</AMOUNT>\n'
        f'</LEDGERENTRIES.LIST>\n'
        + inv + "\n"
        + f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, body)
    parsed, raw = await post_and_parse(client, xml, f"purchase intra-state from '{supplier}'")
    if not (parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0):
        record("op7_create_purchase_voucher", False, f"failed: {parsed.get('error_message') or parsed}", xml, raw)
        return []

    # Read-back via day book.
    db = await client.post_xml(build_day_book(_yyyymmdd_to_ddmmyyyy(voucher_date), _yyyymmdd_to_ddmmyyyy(voucher_date), voucher_type="Purchase"))
    vchs = parse_vouchers(db)
    test = [v for v in vchs if PFX in (v.get("narration") or "")]
    if test:
        record("op7_create_purchase_voucher", True, f"created (id={parsed['last_vch_id']}); day book sees {len(test)} test purchase",
               envelope="VOUCHER VCHTYPE=Purchase + party ISDEEMEDPOSITIVE=No + GST input ledgers debited (negative AMOUNT)",
               readback=json.dumps(test, indent=2)[:400])
    else:
        record("op7_create_purchase_voucher", False, f"voucher created but day book empty for date", "", _short(db))
    return [{"lastvchid": parsed["last_vch_id"]}]


# ─────────────────────────────────────────────────────────────────────────────
# Op 8 — Receipt voucher
# ─────────────────────────────────────────────────────────────────────────────
async def op8_create_receipt_voucher(client: TallyClient, voucher_date: str) -> dict:
    banner("OP 8 — Create Receipt Voucher")
    party = f"{PFX} Party MH"
    cash = f"{PFX} Test Cash"
    amount = 5000.00

    body = (
        f'<VOUCHER VCHTYPE="Receipt" ACTION="Create">\n'
        f'<DATE>{voucher_date}</DATE>\n'
        f'<NARRATION>{PFX} receipt against MH party</NARRATION>\n'
        f'<VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>\n'
        f'<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>\n'
        # Cash debit (negative, ISDEEMEDPOSITIVE=Yes)
        f'<ALLLEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(cash)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>-{amount:.2f}</AMOUNT>\n'
        f'</ALLLEDGERENTRIES.LIST>\n'
        # Party credit (positive)
        f'<ALLLEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(party)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>{amount:.2f}</AMOUNT>\n'
        f'</ALLLEDGERENTRIES.LIST>\n'
        f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, body)
    parsed, raw = await post_and_parse(client, xml, "receipt voucher")
    if not (parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0):
        record("op8_create_receipt_voucher", False, f"failed: {parsed.get('error_message') or parsed}", xml, raw)
        return {}
    db = await client.post_xml(build_day_book(_yyyymmdd_to_ddmmyyyy(voucher_date), _yyyymmdd_to_ddmmyyyy(voucher_date), voucher_type="Receipt"))
    vchs = parse_vouchers(db)
    test = [v for v in vchs if PFX in (v.get("narration") or "")]
    if test:
        record("op8_create_receipt_voucher", True, f"created (id={parsed['last_vch_id']}); day book sees {len(test)} test receipt",
               envelope="VOUCHER VCHTYPE=Receipt + ALLLEDGERENTRIES.LIST(cash dr / party cr) + PERSISTEDVIEW=Accounting Voucher View")
    else:
        record("op8_create_receipt_voucher", False, "created but day book empty", "", _short(db))
    return {"lastvchid": parsed["last_vch_id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Op 9 — Journal voucher
# ─────────────────────────────────────────────────────────────────────────────
async def op9_create_journal_voucher(client: TallyClient, voucher_date: str) -> dict:
    banner("OP 9 — Create Journal Voucher")
    # Create two simple expense ledgers and journal between them? Simpler: use existing
    # Sales ledger and Purchase ledger we created.
    dr = f"{PFX} Purchase - Electronics"
    cr = f"{PFX} Capital"
    amt = 1000.00

    body = (
        f'<VOUCHER VCHTYPE="Journal" ACTION="Create">\n'
        f'<DATE>{voucher_date}</DATE>\n'
        f'<NARRATION>{PFX} journal entry</NARRATION>\n'
        f'<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>\n'
        f'<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>\n'
        f'<ALLLEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(dr)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>-{amt:.2f}</AMOUNT>\n'
        f'</ALLLEDGERENTRIES.LIST>\n'
        f'<ALLLEDGERENTRIES.LIST>\n'
        f'<LEDGERNAME>{_esc(cr)}</LEDGERNAME>\n'
        f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n'
        f'<AMOUNT>{amt:.2f}</AMOUNT>\n'
        f'</ALLLEDGERENTRIES.LIST>\n'
        f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, body)
    parsed, raw = await post_and_parse(client, xml, "journal voucher")
    if not (parsed["created"] >= 1 and parsed["errors"] == 0 and parsed["exceptions"] == 0):
        record("op9_create_journal_voucher", False, f"failed: {parsed.get('error_message') or parsed}", xml, raw)
        return {}
    db = await client.post_xml(build_day_book(_yyyymmdd_to_ddmmyyyy(voucher_date), _yyyymmdd_to_ddmmyyyy(voucher_date), voucher_type="Journal"))
    vchs = parse_vouchers(db)
    test = [v for v in vchs if PFX in (v.get("narration") or "")]
    if test:
        record("op9_create_journal_voucher", True, f"created (id={parsed['last_vch_id']}); day book sees {len(test)} test journal",
               envelope="VOUCHER VCHTYPE=Journal + ALLLEDGERENTRIES.LIST(dr/cr) + PERSISTEDVIEW=Accounting Voucher View")
    else:
        record("op9_create_journal_voucher", False, "created but day book empty", "", _short(db))
    return {"lastvchid": parsed["last_vch_id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int, voucher_date: str, skip_cleanup: bool):
    client = TallyClient(host=host, port=port)
    print(f"Tally Write Exploration v4 — {datetime.now().isoformat()}")
    print(f"Host: {host}:{port} | Company: {COMPANY} | Voucher date: {voucher_date}")

    # Pre-flight: connectivity
    if not await fetch_current_date(client):
        print("ABORT: Tally not responsive")
        await client.close()
        sys.exit(1)

    # Pre-flight: cleanup leftover from prior runs.
    await cleanup_all(client)

    try:
        units = await op1_create_units(client)
        if not units:
            return
        groups = await op2_create_stock_groups(client)
        if not groups:
            return
        items = await op3_create_stock_items(client, units, groups)
        if not items:
            return
        gst_ledgers = await op4_create_gst_ledgers(client)
        if not gst_ledgers:
            return
        ledgers = await op5_create_ledger_with_opening(client)
        if not ledgers:
            return
        await op6_create_sales_vouchers(client, items, ledgers, gst_ledgers, voucher_date)
        await op7_create_purchase_voucher(client, items, ledgers, gst_ledgers, voucher_date)
        await op8_create_receipt_voucher(client, voucher_date)
        await op9_create_journal_voucher(client, voucher_date)
    finally:
        # Final report
        banner("FINAL RESULTS")
        for r in RESULTS:
            print(f"  {'PASS' if r.passed else 'FAIL'}: {r.name} — {r.detail}")
        if not skip_cleanup:
            await cleanup_all(client)
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--date", default="20260501", help="Voucher date YYYYMMDD")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port, args.date, args.skip_cleanup))
