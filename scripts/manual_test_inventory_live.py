"""Phase 2 INVENTORY WRITE-PATH manual test against a LIVE Tally — proves stock-grid posting.

Goal: PROVE that the Phase 2 inventory write path puts voucher line items into the
TallyPrime **stock grid** (qty/rate per ALLINVENTORYENTRIES.LIST) — NOT merely into the
narration — and that a brand-new stock item is *created first* when a line is unmatched.
It exercises the EXACT writer methods that ``backend/api/chat.py`` ``_write_inventory_voucher``
(the helper the voucher_action inventory branch dispatches to) calls:

    writer.create_unit(...)         — idempotent UoM pre-flight (for the create-new line)
    writer.create_stock_item(...)   — idempotent stock-master pre-flight (create-new line)
    writer.create_purchase_voucher( — STOCK-BASED Purchase with (name,qty,rate,ledger,uom,gst)
        items=[...], gst_mode="intra", bill_allocations=[New Ref gross], reference=...,
        reference_date=...)            tuples — the same call _write_inventory_voucher makes

The voucher carries TWO inventory lines:
  * a MATCHED line on an existing seeded item  — "A4 Paper Ream 500 sheets" (Pcs, gst 12), and
  * a CREATE-NEW line on a test item           — "_MTINV Widget" (Nos, gst 18), created first.

PROOF step: after the write we re-read the voucher with a day-book-style Collection that
pulls the native ``AllInventoryEntries`` method, scoped to the voucher date, and matched by
narration. We assert it has exactly TWO ``ALLINVENTORYENTRIES.LIST`` blocks whose
``STOCKITEMNAME`` are the two item names, each with the right ``ACTUALQTY`` (10 Pcs / 2 Nos)
and ``RATE``. Inventory entries living in the stock grid (not the narration string) is the
proof the Phase 2 path posts real stock-ledger lines.

Cleanup (``finally``): delete the voucher by Master ID (DD-MMM-YYYY date), then delete the
created "_MTINV Widget" stock item via an ``All Masters`` ``<STOCKITEM ... ACTION="Delete">``
envelope (mirroring ``scripts/explore_tally_write_v4.py``). The seeded item "A4 Paper Ream
500 sheets", the unit "Nos" and group "Primary" pre-exist, so they are left in place. The
supplier payable is re-read and asserted back to baseline.

Grounded in:
  - backend/api/chat.py                 (_write_inventory_voucher — exact call shapes mirrored)
  - tally_bridge/writer.py      (create_unit / create_stock_item / create_purchase_voucher)
  - tally_bridge/import_builder.py (_esc / _wrap_import / item tuple order)
  - tally_bridge/queries/reports.py (bills_payable → list[OutstandingBill])
  - scripts/manual_test_group_b_live.py (client/arg/cleanup/results patterns, FY date 20250620)
  - scripts/manual_test_pdf_flows_live.py (delete-by-Master-ID, post_write timeout)
  - scripts/explore_tally_write_v4.py   (STOCKITEM ACTION="Delete" via All Masters envelope)
  - scripts/probe_supplier_invoice_date.py (day-book Collection w/ NATIVEMETHOD AllInventoryEntries)
  - LESSONS.md §15                       (write safety: read-back, DD-MMM-YYYY delete date)

WARNING: this WRITES to a live Tally. It targets "Bharat Traders Private Limited", uses a
"_MTINV" prefix and small amounts, and cleans up after itself (voucher + created item).

Usage:
    TALLY_WRITE_ENABLED=true PYTHONPATH=. uv run python scripts/manual_test_inventory_live.py \
        --host localhost --port 9000 2>&1 | tee docs/manual-test-inventory-live.log

Import check (safe — no Tally, no run):
    PYTHONPATH=. uv run python -c "import scripts.manual_test_inventory_live"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import httpx

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.models import OutstandingBill
from tally_bridge.queries.masters import list_stock_items
from tally_bridge.queries.reports import bills_payable
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import parse_import_response
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
NPFX = "_MTINV"  # narration / item prefix so leftovers are mechanically identifiable

# FY-internal date (seed company FY = Apr 2025 – Mar 2026).
# Tally import dates are YYYYMMDD; the day-book read + delete envelope want DD-MMM-YYYY.
VCH_DATE = "20250620"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 20-Jun-2025
# bills_payable takes a DD-MM-YYYY "as on" date (request_builder format).
AS_ON = "31-03-2026"

# Supplier (pre-existing seed Sundry Creditor) and purchase/expense ledger.
SUPPLIER = "Bharat Paper Supplies"
PURCHASE_LEDGER = "Purchase - Office Supplies"

# Line 1 — MATCHED, existing seeded stock item.
MATCHED_ITEM = "A4 Paper Ream 500 sheets"
MATCHED_QTY = 10.0
MATCHED_RATE = 280.0
MATCHED_UOM = "Pcs"
MATCHED_GST = 12

# Line 2 — CREATE-NEW test item (deleted in cleanup).
NEW_ITEM = f"{NPFX} Widget"   # "_MTINV Widget"
NEW_QTY = 2.0
NEW_RATE = 500.0
NEW_UOM = "Nos"
NEW_GROUP = "AI Imported Items"
NEW_GST = 18

# Voucher identity.
VCH_NUMBER = f"{NPFX}-1"            # "_MTINV-1"
NARRATION = f"{NPFX} inventory test"
REFERENCE = f"{NPFX}-REF-1"        # "_MTINV-REF-1"

# Gross = sum of line bases + GST. base = 10*280 + 2*500 = 2800 + 1000 = 3800.
LINE1_BASE = MATCHED_QTY * MATCHED_RATE   # 2,800
LINE2_BASE = NEW_QTY * NEW_RATE           # 1,000
TOTAL_BASE = LINE1_BASE + LINE2_BASE      # 3,800
# GST per line (intra → split CGST/SGST, but only the gross magnitude matters for the bill).
GST_TOTAL = LINE1_BASE * MATCHED_GST / 100 + LINE2_BASE * NEW_GST / 100  # 336 + 180 = 516
GROSS = TOTAL_BASE + GST_TOTAL            # 4,316

TOLERANCE = 1.0  # rupees — accept ±1 rounding noise on read-backs
QTY_TOLERANCE = 0.001


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking (mirrors manual_test_group_b_live.py)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    step: str
    expected: str
    observed: str
    passed: bool


RESULTS: list[StepResult] = []
MASTER_ID: str | None = None  # captured purchase voucher Master ID for cleanup


def record(step: str, expected: str, observed: str, passed: bool) -> None:
    RESULTS.append(StepResult(step, expected, observed, passed))
    print(f"\n  >>> {'PASS' if passed else 'FAIL'}: {step}")
    print(f"      expected: {expected}")
    print(f"      observed: {observed}")


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _short(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — extend write timeout (mirrors manual_test_group_b_live.post_write)
# ─────────────────────────────────────────────────────────────────────────────
async def post_write(client: TallyClient, xml: str) -> str:
    saved = client._client.timeout
    client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    try:
        return await client.post_xml(xml)
    finally:
        client._client.timeout = saved


# ─────────────────────────────────────────────────────────────────────────────
# Read-back helpers
# ─────────────────────────────────────────────────────────────────────────────
def _party_pending(bills: list[OutstandingBill], party: str) -> float:
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


async def read_payable(client: TallyClient, supplier: str) -> float:
    bills = await bills_payable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, supplier)
    print(f"  [bills_payable as-on {AS_ON}] {supplier!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills)")
    return pending


# ─────────────────────────────────────────────────────────────────────────────
# Inventory read-back — day-book-style Collection pulling the NATIVE AllInventoryEntries
# method, scoped to VCH_DATE, matched by narration. This is the PROOF the lines live in
# the stock grid (ALLINVENTORYENTRIES.LIST), not the narration string.
# (mirrors scripts/probe_supplier_invoice_date.build_day_book_full)
# ─────────────────────────────────────────────────────────────────────────────
def build_inventory_readback(from_date: str, to_date: str, company: str) -> str:
    """Day-book Collection over Purchase vouchers in [from,to], pulling stock grid + ids."""
    fields = [
        "Date",
        "VoucherTypeName",
        "VoucherNumber",
        "PartyLedgerName",
        "Narration",
        "Reference",
        "MasterID",
        "AllInventoryEntries",
    ]
    natives = "\n".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fields)
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>MtinvInventoryReadback</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
<SVCurrentCompany>{_esc(company)}</SVCurrentCompany>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="MtinvInventoryReadback" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FILTER>MtinvPurchFilter</FILTER>
{natives}
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="MtinvPurchFilter">$VoucherTypeName = "Purchase"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


@dataclass
class InvLine:
    stock_item: str
    actual_qty: str   # raw Tally string, e.g. "10 Pcs"
    qty_num: float    # parsed numeric magnitude
    rate: str         # raw Tally string, e.g. "280.00/Pcs"
    rate_num: float   # parsed numeric magnitude
    amount: str


def _num(text: str) -> float:
    """Extract the leading signed numeric magnitude from a Tally qty/rate string."""
    import re
    m = re.search(r"-?\d[\d,]*\.?\d*", (text or "").replace(",", ""))
    return abs(float(m.group(0))) if m else 0.0


def _parse_inventory_entries(raw: str, narration_match: str) -> tuple[list[InvLine], str | None]:
    """Find the VOUCHER whose NARRATION contains `narration_match`; return its parsed
    ALLINVENTORYENTRIES.LIST blocks + its MASTERID.

    Looks for inventory line items in the stock-grid blocks — NOT the narration.
    """
    from tally_bridge.response_parser import sanitize_xml
    root = ET.fromstring(sanitize_xml(raw))
    for vch in root.iter("VOUCHER"):
        narr = (vch.findtext("NARRATION") or "")
        if narration_match.lower() not in narr.lower():
            continue
        master_id = vch.findtext("MASTERID")
        lines: list[InvLine] = []
        for inv in vch.iter("ALLINVENTORYENTRIES.LIST"):
            name = (inv.findtext("STOCKITEMNAME") or "").strip()
            qty = (inv.findtext("ACTUALQTY") or inv.findtext("BILLEDQTY") or "").strip()
            rate = (inv.findtext("RATE") or "").strip()
            amount = (inv.findtext("AMOUNT") or "").strip()
            lines.append(InvLine(
                stock_item=name, actual_qty=qty, qty_num=_num(qty),
                rate=rate, rate_num=_num(rate), amount=amount,
            ))
        return lines, master_id
    return [], None


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup — delete the voucher by Master ID, then delete the created stock item.
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None) -> None:
    if not master_id or master_id == "0":
        print(f"  [skip voucher cleanup — no Master ID]")
        return
    voucher_xml = (
        f'<VOUCHER DATE="{_esc(VCH_DATE_DISPLAY)}" TAGNAME="Master ID" '
        f'TAGVALUE="{_esc(master_id)}" VCHTYPE="{_esc(voucher_type)}" ACTION="Delete">\n'
        f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, voucher_xml)
    try:
        raw = await post_write(client, xml)
        parsed = parse_import_response(raw)
        if parsed.get("deleted", 0) >= 1:
            print(f"  [cleanup OK] deleted {voucher_type} (mid={master_id}, "
                  f"deleted={parsed['deleted']})")
        else:
            print(f"  [cleanup WARN] {voucher_type} (mid={master_id}) NOT deleted: "
                  f"{_short(raw, 300)}")
    except Exception as e:  # noqa: BLE001 — cleanup must never abort the run
        print(f"  [cleanup ERROR] voucher {voucher_type}: {type(e).__name__}: {e}")


async def delete_stock_item(client: TallyClient, name: str) -> bool:
    """Delete a stock-item master via an All Masters STOCKITEM ACTION="Delete" envelope
    (mirrors scripts/explore_tally_write_v4 stock-item cleanup). Returns True if deleted."""
    xml = _wrap_import(
        "All Masters", COMPANY,
        f'<STOCKITEM NAME="{_esc(name)}" ACTION="Delete">\n'
        f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
        f'</STOCKITEM>',
    )
    try:
        raw = await post_write(client, xml)
        parsed = parse_import_response(raw)
        deleted = parsed.get("deleted", 0)
        if deleted >= 1:
            print(f"  [cleanup OK] deleted stock item {name!r} (deleted={deleted})")
            return True
        print(f"  [cleanup WARN] stock item {name!r} NOT deleted "
              f"(deleted={deleted}, errors={parsed.get('errors')}): {_short(raw, 300)}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [cleanup ERROR] stock item {name!r}: {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main sequence
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    global MASTER_ID
    client = TallyClient(host=host, port=port)
    print(f"Phase 2 INVENTORY write-path manual test — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Voucher date: {VCH_DATE} (display {VCH_DATE_DISPLAY}) | payable as-on: {AS_ON}")
    print(f"Lines: matched {MATCHED_ITEM!r} ({MATCHED_QTY:g} {MATCHED_UOM} @ {MATCHED_RATE:g}); "
          f"create-new {NEW_ITEM!r} ({NEW_QTY:g} {NEW_UOM} @ {NEW_RATE:g})")
    print(f"Expected gross ≈ {GROSS:,.2f} (base {TOTAL_BASE:,.2f} + GST {GST_TOTAL:,.2f})")

    # Connectivity check.
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    base_payable = 0.0
    new_item_created = False
    writer = TallyWriter(client, COMPANY)

    try:
        # ── Step 1: Baseline payable ───────────────────────────────────────
        banner("Step 1 — Baseline supplier payable")
        base_payable = await read_payable(client, SUPPLIER)
        record("1-baseline",
               "capture baseline supplier payable",
               f"{SUPPLIER!r} payable baseline = {base_payable:,.2f}",
               True)

        # ── Step 2: Create-new masters (unit + stock item) — pre-flight ────
        # Mirrors _write_inventory_voucher's ROOT-CAUSE FIX: NEVER send a CREATE
        # for a master that ALREADY EXISTS — this Tally answers a duplicate-master
        # CREATE with a BLOCKING MODAL that freezes the gateway. So fetch existing
        # stock items once, derive known units/groups/item-names, and create only
        # genuinely-missing masters. "Nos" pre-exists (seeded items use it) → we
        # must NOT call create_unit('Nos'); only the new group/item are created.
        banner(f"Step 2 — Pre-flight create-new masters (unit {NEW_UOM!r}, "
               f"item {NEW_ITEM!r}) [mirror _write_inventory_voucher existence check]")
        existing = await list_stock_items(client)
        existing_item_names = {s.name.casefold() for s in existing}
        known_units = {s.base_units.casefold() for s in existing if s.base_units}
        known_groups = {s.parent_group.casefold() for s in existing if s.parent_group}
        print(f"  [existence check] {len(existing)} existing stock items; "
              f"{len(known_units)} known units, {len(known_groups)} known groups")
        # Unit — create ONLY if genuinely missing (never CREATE an existing 'Nos').
        if NEW_UOM.casefold() in known_units:
            print(f"  create_unit({NEW_UOM!r}) — SKIPPED (already exists; would block on modal)")
        else:
            try:
                r = await writer.create_unit(NEW_UOM, NEW_UOM)
                known_units.add(NEW_UOM.casefold())
                print(f"  create_unit({NEW_UOM!r}) -> {json.dumps(r)}")
            except Exception as e:  # noqa: BLE001
                print(f"  create_unit({NEW_UOM!r}) — note: {type(e).__name__}: {e}")
        # Stock group — create ONLY if genuinely missing.
        if NEW_GROUP.casefold() in known_groups:
            print(f"  create_stock_group({NEW_GROUP!r}) — SKIPPED (already exists)")
        else:
            try:
                r = await writer.create_stock_group(NEW_GROUP, "")
                known_groups.add(NEW_GROUP.casefold())
                print(f"  create_stock_group({NEW_GROUP!r}) -> {json.dumps(r)}")
            except Exception as e:  # noqa: BLE001
                print(f"  create_stock_group({NEW_GROUP!r}) — note: {type(e).__name__}: {e}")
        # If the item already exists, skip creating it (and skip cleanup of a pre-existing item).
        if NEW_ITEM.casefold() in existing_item_names:
            new_item_created = False
            print(f"  create_stock_item({NEW_ITEM!r}) — SKIPPED (already exists)")
            record("2-create-new-item",
                   f"stock item {NEW_ITEM!r} already exists (no create)",
                   "already exists — skipped", True)
        else:
            # Stock item create-new (group AI Imported Items, uom Nos, opening 0/0, no HSN, gst 18 → GST-NA).
            try:
                r = await writer.create_stock_item(
                    name=NEW_ITEM, group=NEW_GROUP, uom=NEW_UOM,
                    opening_qty=0, opening_rate=0, hsn_code="", gst_rate=NEW_GST,
                )
                new_item_created = True
                print(f"  create_stock_item({NEW_ITEM!r}) -> {json.dumps(r)}")
                record("2-create-new-item",
                       f"stock item {NEW_ITEM!r} created",
                       f"create_stock_item returned {json.dumps(r)}",
                       True)
            except Exception as e:  # noqa: BLE001
                print(f"  create_stock_item({NEW_ITEM!r}) -> FAILED: {e}")
                record("2-create-new-item",
                       f"stock item {NEW_ITEM!r} created",
                       f"EXCEPTION {type(e).__name__}: {e}",
                       False)

        # ── Step 3: Write ONE stock-based Purchase voucher (both lines) ────
        # Item tuple order = (name, qty, rate, ledger, uom, gst_rate) — exactly the
        # tuples _write_inventory_voucher builds and feeds to create_purchase_voucher.
        items = [
            (MATCHED_ITEM, MATCHED_QTY, MATCHED_RATE, PURCHASE_LEDGER, MATCHED_UOM, MATCHED_GST),
            (NEW_ITEM, NEW_QTY, NEW_RATE, PURCHASE_LEDGER, NEW_UOM, NEW_GST),
        ]
        bill_allocations = [{"name": REFERENCE, "type": "New Ref", "amount": GROSS}]
        try:
            banner(f"Step 3 — Write stock-based Purchase {VCH_NUMBER!r} (2 inventory lines) "
                   f"[writer.create_purchase_voucher — same call as _write_inventory_voucher]")
            print(f"  items = {items}")
            print(f"  bill_allocations = {bill_allocations}")
            result = await writer.create_purchase_voucher(
                date=VCH_DATE,
                voucher_number=VCH_NUMBER,
                party=SUPPLIER,
                items=items,
                narration=NARRATION,
                gst_mode="intra",
                bill_allocations=bill_allocations,
                reference=REFERENCE,
                reference_date=VCH_DATE,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            MASTER_ID = result.get("last_vch_id")
            if MASTER_ID and MASTER_ID != "0":
                print(f"  [captured Master ID] Purchase mid={MASTER_ID}")
            else:
                print(f"  [WARN] no Master ID in create response "
                      f"(last_vch_id={MASTER_ID!r}) — read-back/cleanup may degrade")
            record("3-write-purchase",
                   f"Purchase {VCH_NUMBER} created with a Master ID",
                   f"created={result.get('created')}, last_vch_id={MASTER_ID!r}",
                   bool(MASTER_ID and MASTER_ID != "0"))
        except Exception as e:  # noqa: BLE001
            record("3-write-purchase",
                   f"Purchase {VCH_NUMBER} created", f"EXCEPTION {type(e).__name__}: {e}",
                   False)

        # ── Step 4: READ BACK the stock grid — THE PROOF ──────────────────
        try:
            banner("Step 4 — Read back ALLINVENTORYENTRIES (stock grid) — PROOF lines are "
                   "stock-ledger entries, not narration")
            raw = await client.post_xml(
                build_inventory_readback(VCH_DATE_DISPLAY, VCH_DATE_DISPLAY, COMPANY)
            )
            lines, read_mid = _parse_inventory_entries(raw, NARRATION)
            if read_mid and (not MASTER_ID or MASTER_ID == "0"):
                # Recover Master ID from read-back if the create response lacked one.
                MASTER_ID = read_mid
                print(f"  [recovered Master ID from read-back] mid={MASTER_ID}")

            print(f"\n  Parsed {len(lines)} ALLINVENTORYENTRIES.LIST block(s) "
                  f"for narration {NARRATION!r} (read-back MasterID={read_mid}):")
            for ln in lines:
                print(f"    - STOCKITEMNAME={ln.stock_item!r}  ACTUALQTY={ln.actual_qty!r} "
                      f"(num {ln.qty_num:g})  RATE={ln.rate!r} (num {ln.rate_num:g})  "
                      f"AMOUNT={ln.amount!r}")

            by_name = {ln.stock_item.strip().lower(): ln for ln in lines}
            m_line = by_name.get(MATCHED_ITEM.lower())
            n_line = by_name.get(NEW_ITEM.lower())

            two_lines = len(lines) == 2
            matched_ok = (
                m_line is not None
                and abs(m_line.qty_num - MATCHED_QTY) <= QTY_TOLERANCE
                and abs(m_line.rate_num - MATCHED_RATE) <= TOLERANCE
            )
            new_ok = (
                n_line is not None
                and abs(n_line.qty_num - NEW_QTY) <= QTY_TOLERANCE
                and abs(n_line.rate_num - NEW_RATE) <= TOLERANCE
            )
            ok = two_lines and matched_ok and new_ok
            observed = (
                f"{len(lines)} stock-grid lines; "
                f"matched[{MATCHED_ITEM}]="
                + (f"qty {m_line.qty_num:g}/rate {m_line.rate_num:g} (ok={matched_ok})"
                   if m_line else "ABSENT")
                + f"; new[{NEW_ITEM}]="
                + (f"qty {n_line.qty_num:g}/rate {n_line.rate_num:g} (ok={new_ok})"
                   if n_line else "ABSENT")
            )
            record("4-stock-grid-readback",
                   f"2 ALLINVENTORYENTRIES.LIST: {MATCHED_ITEM} ({MATCHED_QTY:g} {MATCHED_UOM} "
                   f"@ {MATCHED_RATE:g}) + {NEW_ITEM} ({NEW_QTY:g} {NEW_UOM} @ {NEW_RATE:g})",
                   observed, ok)
        except Exception as e:  # noqa: BLE001
            record("4-stock-grid-readback",
                   "2 stock-grid inventory entries with correct qty/rate",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

    finally:
        # ── Step 5: Cleanup — delete voucher, then delete created stock item ─
        banner("Step 5 — CLEANUP (delete voucher by Master ID, then delete created stock item)")
        await cleanup_voucher(client, "Purchase", MASTER_ID)
        # Delete the create-new item we introduced. Seeded "A4 Paper Ream", unit "Nos"
        # and group "Primary" pre-exist → leave them.
        item_cleaned = True
        if new_item_created:
            item_cleaned = await delete_stock_item(client, NEW_ITEM)
        else:
            print(f"  [skip stock-item cleanup — {NEW_ITEM!r} was not created by this run]")

        # ── Verify books restored to baseline ──
        banner("Step 5 — Verify supplier payable restored to baseline (±1)")
        restored = False
        try:
            final_payable = await read_payable(client, SUPPLIER)
            pay_ok = abs(final_payable - base_payable) <= TOLERANCE
            restored = pay_ok and item_cleaned
            if restored:
                print("\n  books restored — supplier payable back to baseline and "
                      "test stock item removed.")
            else:
                print("\n  !! RESIDUE REMAINS — books NOT fully restored:")
                if not pay_ok:
                    print(f"     payable: baseline {base_payable:,.2f} vs now "
                          f"{final_payable:,.2f} (residue {final_payable - base_payable:+,.2f})")
                if not item_cleaned:
                    print(f"     stock item {NEW_ITEM!r} was NOT deleted — remove it manually.")
            record("5-cleanup",
                   "payable restored to baseline (±1) and test stock item deleted",
                   f"payable {base_payable:,.2f}->{final_payable:,.2f} (ok={pay_ok}); "
                   f"item_deleted={item_cleaned}; "
                   + ("books restored" if restored else "RESIDUE REMAINS"),
                   restored)
        except Exception as e:  # noqa: BLE001
            print(f"  [restore-check ERROR] {type(e).__name__}: {e}")
            record("5-cleanup", "books restored to baseline",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Results table ──
        banner("RESULTS TABLE")
        print(f"{'STEP':<24} {'PASS/FAIL':<10} {'EXPECTED':<52} OBSERVED")
        print("-" * 150)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.step:<24} {status:<10} {_short(r.expected, 50):<52} "
                  f"{_short(r.observed, 70)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 150)
        print(f"{len(RESULTS)} steps — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        # ── Overall verdict — the stock-grid read-back is the load-bearing check ──
        banner("OVERALL VERDICT — Inventory line items posted to Tally stock grid")
        grid = next((r for r in RESULTS if r.step == "4-stock-grid-readback"), None)
        create = next((r for r in RESULTS if r.step == "2-create-new-item"), None)
        write = next((r for r in RESULTS if r.step == "3-write-purchase"), None)
        grid_ok = grid is not None and grid.passed
        print(f"  Create-new stock item first : {'PASS' if create and create.passed else 'FAIL'}")
        print(f"  Stock-based Purchase written: {'PASS' if write and write.passed else 'FAIL'}")
        print(f"  2 lines in ALLINVENTORYENTRIES (qty/rate correct): "
              f"{'PASS' if grid_ok else 'FAIL'}")
        verdict = "PASS" if grid_ok else "FAIL"
        print(f"\n  Inventory line items posted to Tally stock grid: {verdict}")
        if grid_ok:
            print("  (Proven: line items live in ALLINVENTORYENTRIES.LIST with the right "
                  "qty/rate — the stock grid, not the narration.)")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 2 inventory write-path live test (stock-grid proof, self-cleaning)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
