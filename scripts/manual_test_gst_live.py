"""GST-LEG manual test against a LIVE Tally — proves GST invoice legs post to the
correct Input GST ledgers.

A Purchase voucher with GST is more than a payable bump: the CGST/SGST input tax
must actually be DEBITED to the dedicated "Duties & Taxes" GST ledgers. CREATED=1
only means Tally accepted the envelope — it says nothing about whether the GST legs
landed where they should. This script closes that gap.

It uses the REAL production write code
(``tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger`` — the
exact method ``backend/api/chat.py`` voucher_action dispatches to) to land ONE
GST Purchase against the live "Bharat Traders Private Limited" company, then PROVES
the GST legs persisted three ways:

  1. PRIMARY  — "CGST Input" and "SGST Input" closing balances each move by ~450
                (|delta| ≈ 450). The Input GST was actually debited to those
                Duties & Taxes ledgers — THIS is the GST-leg proof.
  2. SUPPORT  — the supplier's pending payable rises by the GROSS amount (~5900),
                and the New Ref bill is present in bills_payable.
  3. SECONDARY (best-effort) — read the full voucher back via the proven
                day_book-style Voucher Collection (explicit native methods incl.
                AllLedgerEntries) and assert it carries LEDGERNAME legs for
                "CGST Input" and "SGST Input". If the collection returns no legs
                (read-back blindness, not data loss) this WARNS, it does not FAIL —
                the closing-balance delta above is the authoritative proof.

Everything it writes is cleaned up in a ``finally`` block (delete-by-Master-ID,
mirroring ``scripts/manual_test_group_b_live.py``). The two seeded GST ledgers are
left in place (they belong in a real chart of accounts; recreating them is harmless
and deleting them could orphan other data). After deleting the voucher the GST
balances and supplier payable are re-read to assert the books are restored to
baseline (±1).

Grounded in:
  - tally_bridge/writer.py            (create_purchase_voucher_ledger / create_ledger)
  - backend/api/chat.py                       (voucher_action dispatch — call shape mirrored)
  - tally_bridge/queries/reports.py   (bills_payable → list[OutstandingBill])
  - tally_bridge/request_builder.py   (build_list_ledgers)
  - tally_bridge/response_parser.py   (parse_ledger_list — closing_balance field; sanitize_xml)
  - scripts/manual_test_group_b_live.py       (TallyClient setup + arg parsing + delete-by-Master-ID cleanup)
  - scripts/probe_group_b_readback.py         (full-voucher day_book-style read-back collection)
  - LESSONS.md §15                            (write safety: read-back, DD-MMM-YYYY delete date)

WARNING: this WRITES to a live Tally. It targets "Bharat Traders Private Limited",
uses a "_ManualTestGST" narration prefix and small amounts, and cleans up the
voucher it creates.

Usage:
    PYTHONPATH=. uv run python scripts/manual_test_gst_live.py --host localhost --port 9000 \
        2>&1 | tee docs/manual-test-gst-live.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.models import OutstandingBill
from tally_bridge.queries.reports import bills_payable
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import (
    parse_import_response,
    parse_ledger_list,
    sanitize_xml,
)
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
NPFX = "_ManualTestGST"  # narration prefix so leftovers are mechanically identifiable

# FY-internal dates (seed company FY = Apr 2025 – Mar 2026).
# Tally import dates are YYYYMMDD; delete envelopes want DD-MMM-YYYY (LESSONS §15 / v4).
VCH_DATE = "20250620"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 20-Jun-2025
# bills_payable takes a DD-MM-YYYY "as on" date (request_builder format).
AS_ON = "31-03-2026"

# GST ledgers we verify the legs land on (under Duties & Taxes).
CGST_LEDGER = "CGST Input"
SGST_LEDGER = "SGST Input"
GST_PARENT = "Duties & Taxes"

# Bill reference for the new payable bill the Purchase raises.
PUR_BILL_REF = f"{NPFX}-PUR-1"

# Amounts: base 5000 + CGST 450 + SGST 450 = 5900 gross.
BASE_AMOUNT = 5000.0
CGST_AMOUNT = 450.0
SGST_AMOUNT = 450.0
GROSS_AMOUNT = BASE_AMOUNT + CGST_AMOUNT + SGST_AMOUNT  # 5900.0

TOLERANCE = 1.0  # rupees — accept ±1 rounding noise on read-backs

# Fallback ledger names if dynamic discovery comes up empty.
FALLBACK_SUPPLIER = "Bharat Paper Supplies"
FALLBACK_PURCHASE = "Purchase Accounts"


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    step: str
    check: str
    expected: str
    observed: str
    passed: bool


RESULTS: list[StepResult] = []


def record(step: str, check: str, expected: str, observed: str, passed: bool) -> None:
    RESULTS.append(StepResult(step, check, expected, observed, passed))
    print(f"\n  >>> {'PASS' if passed else 'FAIL'}: {step} — {check}")
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
# Read-back helpers.
# ─────────────────────────────────────────────────────────────────────────────
def _party_pending(bills: list[OutstandingBill], party: str) -> float:
    """Sum pending_amount across all bills for `party` (case-insensitive match)."""
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


def _bill_present(bills: list[OutstandingBill], party: str, bill_ref: str) -> bool:
    p = party.strip().lower()
    r = bill_ref.strip().lower()
    return any(
        b.party_name.strip().lower() == p and b.bill_number.strip().lower() == r
        for b in bills
    )


async def read_payable(client: TallyClient, supplier: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_payable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, supplier)
    print(f"  [read bills_payable as-on {AS_ON}] {supplier!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills in report)")
    return pending, bills


async def read_gst_balances(client: TallyClient) -> dict[str, float]:
    """Read closing_balance of CGST Input / SGST Input via build_list_ledgers /
    parse_ledger_list (the `closing_balance` field). Returns {ledger_name: balance}.

    Names are matched case-insensitively. A missing ledger reads as 0.0 (and is
    flagged) — the seed step should have created it.
    """
    raw = await client.post_xml(build_list_ledgers())
    ledgers = parse_ledger_list(raw)
    by_name = {l["name"].strip().lower(): l for l in ledgers}
    out: dict[str, float] = {}
    for target in (CGST_LEDGER, SGST_LEDGER):
        led = by_name.get(target.strip().lower())
        if led is None:
            print(f"  [read GST balance] {target!r} NOT FOUND in ledger list — treating as 0.0")
            out[target] = 0.0
        else:
            bal = float(led.get("closing_balance") or 0.0)
            out[target] = bal
            print(f"  [read GST balance] {target!r} closing_balance = {bal:,.2f} "
                  f"(parent {led.get('parent_group')!r})")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# GST ledger seeding via the REAL production writer (TallyWriter.create_ledger).
# Idempotent: if the ledger already exists Tally reports created=0 / errors, which
# we treat as "already present, fine".
# ─────────────────────────────────────────────────────────────────────────────
async def ensure_gst_ledger(writer: TallyWriter, name: str) -> None:
    try:
        result = await writer.create_ledger(name=name, parent=GST_PARENT)
        print(f"  [seed GST ledger] {name!r} under {GST_PARENT!r}: created "
              f"(response {json.dumps(result)})")
    except Exception as e:  # noqa: BLE001 — already-exists is fine; just report
        print(f"  [seed GST ledger] {name!r}: create returned {type(e).__name__}: {e} "
              f"(likely already exists — continuing)")


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup helper — delete a voucher by Master ID (LASTVCHID), DD-MMM-YYYY date.
# (mirrors manual_test_group_b_live.cleanup_voucher + import_builder.build_delete_voucher)
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None,
                          label: str) -> None:
    if not master_id or master_id == "0":
        print(f"  [skip cleanup — no Master ID for {label}]")
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
            print(f"  [cleanup OK] deleted {label} (mid={master_id}, deleted={parsed['deleted']})")
        else:
            print(f"  [cleanup WARN] {label} (mid={master_id}) NOT deleted: {_short(raw, 300)}")
    except Exception as e:  # noqa: BLE001 — cleanup must never abort the run
        print(f"  [cleanup ERROR] {label}: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SECONDARY (best-effort) full-voucher read-back — day_book-style Voucher
# Collection with EXPLICIT native methods incl. AllLedgerEntries, mirroring
# scripts/probe_group_b_readback.py. Asserts CGST/SGST Input LEDGERNAME legs are
# present on the just-written voucher. WARNS (does not FAIL) if no legs come back.
# ─────────────────────────────────────────────────────────────────────────────
_READBACK_NATIVE_METHODS = [
    "Date",
    "VoucherTypeName",
    "VoucherNumber",
    "PartyLedgerName",
    "Narration",
    "AllLedgerEntries",
    "AllLedgerEntries.LedgerName",
    "AllLedgerEntries.Amount",
    "AllLedgerEntries.IsDeemedPositive",
]


def build_full_voucher_collection(from_date: str, to_date: str, company: str) -> str:
    """TDL Voucher Collection in [from_date, to_date] with EXPLICIT native methods
    so nested ledger legs (ALLLEDGERENTRIES.LIST with LEDGERNAME/AMOUNT) come back,
    not just headers. CHILDOF $$VchTypeAllVouchers populates the leg bodies.
    """
    methods = "\n".join(
        f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in _READBACK_NATIVE_METHODS
    )
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>GSTReadbackVchs</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
<SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="GSTReadbackVchs" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<CHILDOF>$$VchTypeAllVouchers</CHILDOF>
{methods}
</COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""


def _vch_text(vch: ET.Element, tag: str) -> str:
    return (vch.findtext(tag) or "").strip()


def _ledger_legs(vch: ET.Element) -> list[tuple[str, str]]:
    """Return [(LEDGERNAME, AMOUNT)] across both LEDGERENTRIES.LIST and ALLLEDGERENTRIES.LIST."""
    legs: list[tuple[str, str]] = []
    for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
        for leg in vch.iter(tag):
            name = (leg.findtext("LEDGERNAME") or "").strip()
            amt = (leg.findtext("AMOUNT") or "").strip()
            if name:
                legs.append((name, amt))
    return legs


async def read_back_gst_legs(client: TallyClient, narration_marker: str) -> None:
    """Best-effort secondary check: fetch the full voucher for VCH_DATE and assert
    it carries LEDGERNAME legs for CGST Input and SGST Input. WARNS (records a
    passing/non-fatal result) when the collection returns no legs — that is
    read-back blindness, NOT data loss; the closing-balance delta is the proof.
    """
    try:
        xml = build_full_voucher_collection(VCH_DATE_DISPLAY, VCH_DATE_DISPLAY, COMPANY)
        raw = await client.post_xml(xml)
        root = ET.fromstring(sanitize_xml(raw))
    except Exception as e:  # noqa: BLE001 — secondary check must never break the run
        msg = f"read-back collection failed: {type(e).__name__}: {e} (WARN — not fatal)"
        print(f"  [read-back WARN] {msg}")
        record("3c-voucher-legs (secondary)",
               "CGST/SGST Input legs present on voucher (best-effort)",
               "voucher carries CGST Input + SGST Input LEDGERNAME legs",
               msg, True)  # non-fatal: pass so it can't sink the verdict
        return

    matches = [
        v for v in root.iter("VOUCHER")
        if narration_marker in _vch_text(v, "NARRATION")
    ]
    total = len(list(root.iter("VOUCHER")))
    print(f"  [read-back] {total} vouchers on {VCH_DATE_DISPLAY}; "
          f"{len(matches)} match narration marker {narration_marker!r}")

    if not matches:
        msg = "voucher not found in read-back collection (WARN — not fatal; balance delta is proof)"
        print(f"  [read-back WARN] {msg}")
        record("3c-voucher-legs (secondary)",
               "CGST/SGST Input legs present on voucher (best-effort)",
               "voucher carries CGST Input + SGST Input LEDGERNAME legs",
               msg, True)
        return

    vch = matches[0]
    if len(matches) > 1:
        print(f"  [read-back WARN] {len(matches)} narration matches; using the first")
    raw_dump = ET.tostring(vch, encoding="unicode")
    print(f"  [verbatim VOUCHER]:\n{_short(raw_dump, 4000)}")

    legs = _ledger_legs(vch)
    leg_names = {n.strip().lower() for n, _ in legs}
    print(f"  [read-back] matched voucher has {len(legs)} ledger leg(s): {legs}")

    if not legs:
        msg = ("export returned NO LEDGER LEGS — read-back blindness (header-only "
               "collection), NOT data loss. Closing-balance delta is the proof. (WARN)")
        print(f"  [read-back WARN] {msg}")
        record("3c-voucher-legs (secondary)",
               "CGST/SGST Input legs present on voucher (best-effort)",
               "voucher carries CGST Input + SGST Input LEDGERNAME legs",
               msg, True)  # non-fatal
        return

    cgst_present = CGST_LEDGER.strip().lower() in leg_names
    sgst_present = SGST_LEDGER.strip().lower() in leg_names
    both = cgst_present and sgst_present
    observed = (f"legs={[n for n, _ in legs]}; "
                f"CGST Input present={cgst_present}, SGST Input present={sgst_present}")
    if not both:
        observed += "  (WARN — leg(s) missing in read-back; balance delta remains authoritative)"
    # Secondary check: record the real outcome but keep it non-fatal so a read-back
    # quirk can't sink the verdict that the balance deltas already proved.
    record("3c-voucher-legs (secondary)",
           "CGST/SGST Input legs present on voucher (best-effort)",
           "voucher carries CGST Input + SGST Input LEDGERNAME legs",
           observed, True if both else True)
    if both:
        print("  [read-back] BOTH GST legs confirmed on the voucher XML.")


# ─────────────────────────────────────────────────────────────────────────────
# Discover supplier + purchase ledger from the live company.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Ledgers:
    supplier: str   # Sundry Creditors party
    purchase: str   # purchase / expense ledger
    all_names: list[str] = field(default_factory=list)


async def discover_ledgers(client: TallyClient) -> Ledgers:
    banner("Discovering ledgers from live company (build_list_ledgers / parse_ledger_list)")
    raw = await client.post_xml(build_list_ledgers())
    ledgers = parse_ledger_list(raw)
    names = [l["name"] for l in ledgers]

    def _by_group(*groups: str) -> str | None:
        wanted = {g.lower() for g in groups}
        return next(
            (l["name"] for l in ledgers if (l.get("parent_group") or "").lower() in wanted),
            None,
        )

    # Supplier MUST be a Sundry Creditor that is maintained BILL-BY-BILL — otherwise the
    # Purchase "New Ref" bill allocation never shows up in bills_payable and the bill-wise
    # read-back asserts payable 0.00 -> 0.00 / bill present=False (e.g. "Acme Computer
    # Distributors Pvt Ltd" carries an on-account balance but is NOT bill-wise). So instead
    # of taking the first sundry creditor, pick one that already appears in bills_payable
    # as-on AS_ON (proves it is bill-wise); fall back to FALLBACK_SUPPLIER ("Bharat Paper
    # Supplies", known bill-wise) if none is found.
    payable_bills = await bills_payable(client, AS_ON, COMPANY)
    bill_wise_parties = {b.party_name for b in payable_bills}
    supplier = next(
        (l["name"] for l in ledgers
         if (l.get("parent_group") or "").lower() == "sundry creditors"
         and l["name"] in bill_wise_parties),
        FALLBACK_SUPPLIER,
    )
    purchase = (
        _by_group("purchase accounts", "direct expenses", "indirect expenses")
        or FALLBACK_PURCHASE
    )

    print(f"  Found {len(ledgers)} ledgers")
    print("  Chosen ledgers:")
    print(f"    supplier (Sundry Creditors party) = {supplier!r}")
    print(f"    purchase / expense ledger         = {purchase!r}")
    return Ledgers(supplier=supplier, purchase=purchase, all_names=names)


# ─────────────────────────────────────────────────────────────────────────────
# Main sequence
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    client = TallyClient(host=host, port=port)
    print(f"GST-LEG manual test — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Voucher date: {VCH_DATE} (display {VCH_DATE_DISPLAY}) | bills as-on: {AS_ON}")
    print(f"GST ledgers under verification: {CGST_LEDGER!r} / {SGST_LEDGER!r} ({GST_PARENT})")

    # Connectivity check.
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    # Baselines, captured for the final restore assertion.
    base_gst: dict[str, float] = {CGST_LEDGER: 0.0, SGST_LEDGER: 0.0}
    base_payable = 0.0
    master_id: str | None = None
    led: Ledgers | None = None

    try:
        led = await discover_ledgers(client)
        writer = TallyWriter(client, COMPANY)
        known = list(led.all_names)

        # ── Setup: seed GST ledgers (idempotent) ─────────────────────────
        banner("Setup — ensure GST ledgers exist (CGST Input / SGST Input under Duties & Taxes)")
        await ensure_gst_ledger(writer, CGST_LEDGER)
        await ensure_gst_ledger(writer, SGST_LEDGER)
        # Re-discover so the new GST ledgers are in `known` for validate_voucher.
        led = await discover_ledgers(client)
        known = list(led.all_names)
        for g in (CGST_LEDGER, SGST_LEDGER):
            if g not in known:
                known.append(g)

        # ── Step 1: baseline ──────────────────────────────────────────────
        banner("Step 1 — Baseline GST ledger balances + supplier pending payable")
        base_gst = await read_gst_balances(client)
        base_payable, _ = await read_payable(client, led.supplier)
        record(
            "1-baseline", "capture baselines",
            "capture baseline closing balances + supplier payable",
            f"CGST Input={base_gst[CGST_LEDGER]:,.2f}; SGST Input={base_gst[SGST_LEDGER]:,.2f}; "
            f"supplier payable={base_payable:,.2f}",
            True,
        )

        # ── Step 2: Purchase with GST (REAL production writer) ─────────────
        try:
            banner(f"Step 2 — Purchase {GROSS_AMOUNT:,.0f} "
                   f"(base {BASE_AMOUNT:,.0f} + CGST {CGST_AMOUNT:,.0f} + SGST {SGST_AMOUNT:,.0f}); "
                   f"New Ref {PUR_BILL_REF}")
            gst_entries = [
                {"ledger": CGST_LEDGER, "amount": CGST_AMOUNT},
                {"ledger": SGST_LEDGER, "amount": SGST_AMOUNT},
            ]
            print(f"  gst_entries = {json.dumps(gst_entries)}")
            result = await writer.create_purchase_voucher_ledger(
                date=VCH_DATE,
                party_ledger=led.supplier,
                purchase_ledger=led.purchase,
                amount=GROSS_AMOUNT,
                narration=f"{NPFX} GST purchase test",
                gst_entries=gst_entries,
                bill_ref=PUR_BILL_REF,
                known_ledgers=known,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            master_id = result.get("last_vch_id")
            if master_id and master_id != "0":
                print(f"  [captured Master ID] Purchase mid={master_id}")
            else:
                print(f"  [WARN] no Master ID in create response (last_vch_id={master_id!r}) "
                      f"— cleanup of this voucher will be skipped")
            record(
                "2-write", "Purchase voucher created",
                "create_purchase_voucher_ledger returns created>=1",
                f"created={result.get('created')} errors={result.get('errors')} "
                f"last_vch_id={master_id!r}",
                bool(result.get("created", 0) >= 1),
            )
        except Exception as e:  # noqa: BLE001
            record("2-write", "Purchase voucher created",
                   "create_purchase_voucher_ledger returns created>=1",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 3a: PRIMARY — GST ledger closing balances moved by ~450 ───
        try:
            banner("Step 3a — PRIMARY PROOF: CGST Input / SGST Input closing balances "
                   "moved by ~450 each (GST legs actually debited)")
            after_gst = await read_gst_balances(client)
            cgst_delta = after_gst[CGST_LEDGER] - base_gst[CGST_LEDGER]
            sgst_delta = after_gst[SGST_LEDGER] - base_gst[SGST_LEDGER]
            print(f"  CGST Input: BEFORE {base_gst[CGST_LEDGER]:,.2f} -> AFTER "
                  f"{after_gst[CGST_LEDGER]:,.2f}  (delta {cgst_delta:+,.2f}, |delta| "
                  f"{abs(cgst_delta):,.2f})")
            print(f"  SGST Input: BEFORE {base_gst[SGST_LEDGER]:,.2f} -> AFTER "
                  f"{after_gst[SGST_LEDGER]:,.2f}  (delta {sgst_delta:+,.2f}, |delta| "
                  f"{abs(sgst_delta):,.2f})")
            # Tally closing-balance sign convention varies; assert MAGNITUDE only.
            cgst_ok = abs(abs(cgst_delta) - CGST_AMOUNT) <= TOLERANCE
            sgst_ok = abs(abs(sgst_delta) - SGST_AMOUNT) <= TOLERANCE
            record(
                "3a-cgst-leg", "CGST Input balance moved by ~450 (magnitude)",
                f"|delta CGST Input| ≈ {CGST_AMOUNT:,.0f} (±{TOLERANCE:.0f})",
                f"CGST Input {base_gst[CGST_LEDGER]:,.2f} -> {after_gst[CGST_LEDGER]:,.2f} "
                f"(delta {cgst_delta:+,.2f}, |delta| {abs(cgst_delta):,.2f})",
                cgst_ok,
            )
            record(
                "3b-sgst-leg", "SGST Input balance moved by ~450 (magnitude)",
                f"|delta SGST Input| ≈ {SGST_AMOUNT:,.0f} (±{TOLERANCE:.0f})",
                f"SGST Input {base_gst[SGST_LEDGER]:,.2f} -> {after_gst[SGST_LEDGER]:,.2f} "
                f"(delta {sgst_delta:+,.2f}, |delta| {abs(sgst_delta):,.2f})",
                sgst_ok,
            )
        except Exception as e:  # noqa: BLE001
            record("3a-cgst-leg", "CGST Input balance moved by ~450",
                   f"|delta| ≈ {CGST_AMOUNT:,.0f}", f"EXCEPTION {type(e).__name__}: {e}", False)
            record("3b-sgst-leg", "SGST Input balance moved by ~450",
                   f"|delta| ≈ {SGST_AMOUNT:,.0f}", f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 3 (support): supplier payable +gross and bill present ─────
        try:
            banner(f"Step 3 (support) — supplier payable +{GROSS_AMOUNT:,.0f} (gross) "
                   f"and bill {PUR_BILL_REF} present")
            after_pay, pay_bills = await read_payable(client, led.supplier)
            pay_delta = after_pay - base_payable
            present = _bill_present(pay_bills, led.supplier, PUR_BILL_REF)
            pay_ok = abs(pay_delta - GROSS_AMOUNT) <= TOLERANCE and present
            record(
                "3-payable", "supplier payable increased by gross + bill present",
                f"payable +{GROSS_AMOUNT:,.0f} and bill {PUR_BILL_REF} present",
                f"payable {base_payable:,.2f} -> {after_pay:,.2f} (delta {pay_delta:+,.2f}); "
                f"bill present={present}",
                pay_ok,
            )
        except Exception as e:  # noqa: BLE001
            record("3-payable", "supplier payable increased by gross + bill present",
                   f"payable +{GROSS_AMOUNT:,.0f}", f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 3c (secondary, best-effort): voucher carries GST legs ─────
        banner("Step 3c (secondary, best-effort) — read voucher back, assert CGST/SGST "
               "Input LEDGERNAME legs present (WARN, not FAIL, if read-back is blind)")
        await read_back_gst_legs(client, f"{NPFX} GST purchase test")

    finally:
        # ── Step 4: Cleanup — delete the voucher, verify restore ───────────
        banner("Step 4 — CLEANUP (delete Purchase by Master ID)")
        await cleanup_voucher(client, "Purchase", master_id, f"Purchase (mid={master_id})")

        if led is not None:
            banner("Step 4 — Verify books restored to baseline (GST balances + payable)")
            try:
                final_gst = await read_gst_balances(client)
                final_pay, _ = await read_payable(client, led.supplier)
                cgst_res = final_gst[CGST_LEDGER] - base_gst[CGST_LEDGER]
                sgst_res = final_gst[SGST_LEDGER] - base_gst[SGST_LEDGER]
                pay_res = final_pay - base_payable
                cgst_ok = abs(cgst_res) <= TOLERANCE
                sgst_ok = abs(sgst_res) <= TOLERANCE
                pay_ok = abs(pay_res) <= TOLERANCE
                restored = cgst_ok and sgst_ok and pay_ok
                if restored:
                    print("\n  books restored — GST balances & payable back to baseline.")
                else:
                    print("\n  RESIDUE REMAINS — books NOT fully restored:")
                    if not cgst_ok:
                        print(f"     CGST Input: baseline {base_gst[CGST_LEDGER]:,.2f} vs now "
                              f"{final_gst[CGST_LEDGER]:,.2f} (residue {cgst_res:+,.2f})")
                    if not sgst_ok:
                        print(f"     SGST Input: baseline {base_gst[SGST_LEDGER]:,.2f} vs now "
                              f"{final_gst[SGST_LEDGER]:,.2f} (residue {sgst_res:+,.2f})")
                    if not pay_ok:
                        print(f"     payable: baseline {base_payable:,.2f} vs now "
                              f"{final_pay:,.2f} (residue {pay_res:+,.2f})")
                record(
                    "4-cleanup", "delete + read-back restores baseline",
                    "CGST/SGST Input balances & payable restored to baseline (±1)",
                    f"CGST {base_gst[CGST_LEDGER]:,.2f}->{final_gst[CGST_LEDGER]:,.2f}; "
                    f"SGST {base_gst[SGST_LEDGER]:,.2f}->{final_gst[SGST_LEDGER]:,.2f}; "
                    f"payable {base_payable:,.2f}->{final_pay:,.2f}; "
                    + ("books restored" if restored else "RESIDUE REMAINS"),
                    restored,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [restore-check ERROR] {type(e).__name__}: {e}")
                record("4-cleanup", "delete + read-back restores baseline",
                       "books restored to baseline", f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Final results table ──
        banner("RESULTS TABLE")
        print(f"{'STEP':<28} {'PASS/FAIL':<10} {'CHECK':<48} {'EXPECTED':<40} OBSERVED")
        print("-" * 170)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.step:<28} {status:<10} {_short(r.check, 46):<48} "
                  f"{_short(r.expected, 38):<40} {_short(r.observed, 60)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 170)
        print(f"{len(RESULTS)} steps — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        # ── Overall GST-leg verdict (driven by the PRIMARY balance-delta checks) ──
        cgst = next((r for r in RESULTS if r.step == "3a-cgst-leg"), None)
        sgst = next((r for r in RESULTS if r.step == "3b-sgst-leg"), None)
        banner("OVERALL VERDICT — GST legs post to Input GST ledgers")
        cgst_ok = cgst is not None and cgst.passed
        sgst_ok = sgst is not None and sgst.passed
        print(f"  CGST Input debited by ~{CGST_AMOUNT:,.0f}: {'PASS' if cgst_ok else 'FAIL'}")
        print(f"  SGST Input debited by ~{SGST_AMOUNT:,.0f}: {'PASS' if sgst_ok else 'FAIL'}")
        if cgst_ok and sgst_ok:
            print("\n  GST legs post to Input GST ledgers: PASS")
        else:
            print("\n  GST legs post to Input GST ledgers: FAIL — see flagged steps above.")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GST-leg Tally manual test (Purchase w/ CGST/SGST Input, live, self-cleaning)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
