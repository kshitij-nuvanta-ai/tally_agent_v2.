"""Group B WRITE probe — verify Purchase/DN/CN format variants + new TDS & bank-recon paths.

A single runnable probe that POSTs candidate WRITE XML to a LIVE Tally, parses the
response (CREATED/ALTERED/ERRORS/EXCEPTIONS/LASTVCHID), prints PASS/FAIL per probe with
raw error text on failure, and CLEANS UP every voucher/ledger it creates so the company
books stay pristine.

Grounded entirely in:
  - docs/plans/2026-04-12-group-b-voucher-types-plan.md  (Task 0 / E1–E8 XML)
  - scripts/explore_tally_write_v4.py                    (CREATE → read-back → DELETE style)
  - tally_bridge/import_builder.py               (_wrap_import, _esc, voucher shapes)
  - scripts/probe_bill_allocations_live.py               (Master-ID lookup + delete-by-Master-ID)
  - LESSONS.md §15                                       (write safety: read-back, DD-MMM-YYYY delete)

Probes:
  E1   Purchase with LEDGERENTRIES.LIST (+ Invoice Voucher View + ISPARTYLEDGER)
  E2   Purchase with ALLLEDGERENTRIES.LIST + Accounting Voucher View  (control / known-good)
  E3   Purchase with ALLLEDGERENTRIES.LIST + Invoice Voucher View     (isolate PERSISTEDVIEW)
  E4   Purchase with ISPARTYLEDGER=Yes + BILLALLOCATIONS.LIST (New Ref)
  E5   Debit Note (VCHTYPE="Debit Note") + Agst Ref bill allocation
  E6   Credit Note (VCHTYPE="Credit Note") + Agst Ref bill allocation
  E7   get_company_list() — read-only company-list envelope candidates
  E8   get_party_vouchers() — TDL collection filtering a party's vouchers by type (read-only)
  T1a  TDS receivable Journal — Dr _ProbeTDS Receivable / Cr customer party   (NEW, best-guess)
  T1b  TDS payable Journal    — Dr supplier party / Cr _ProbeTDS Payable      (NEW, best-guess)
  B1   Payment with BANKALLOCATIONS.LIST (cheque date/number/instrument)      (NEW, unvalidated)
  B1b  Payment with top-level BANKDATE field                                  (NEW, unvalidated)

WARNING: this writes to a live Tally. It targets "Bharat Traders Private Limited" and uses
Rs 1.00 / small amounts + a "_ProbeGroupB" narration prefix. Cleanup runs in finally blocks.

Usage:
    PYTHONPATH=. uv run python scripts/probe_group_b.py --host localhost --port 9000 \
        2>&1 | tee docs/probe-group-b.log
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
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import (
    parse_import_response,
    parse_ledger_list,
    sanitize_xml,
)

COMPANY = "Bharat Traders Private Limited"
NPFX = "_ProbeGroupB"  # narration / name prefix so leftovers are mechanically identifiable

# FY-internal date so read-back & delete work (seed company FY = Apr 2025 – Mar 2026).
# Tally import dates are YYYYMMDD; delete envelopes want DD-MMM-YYYY (LESSONS §15 / v4).
VCH_DATE = "20250615"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 15-Jun-2025

# Fallback names (used only if dynamic lookup from the live ledger list comes up empty).
FALLBACK_DEBTOR = "Apex Technologies Pvt Ltd"
FALLBACK_CREDITOR = "Apex Technologies Pvt Ltd"
FALLBACK_BANK = "HDFC Bank"
FALLBACK_EXPENSE = "Bank Charges"

# TDS probe ledger names (created + deleted within this run).
TDS_RECEIVABLE = f"{NPFX} TDS Receivable"
TDS_PAYABLE = f"{NPFX} TDS Payable"


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ProbeResult:
    probe_id: str
    description: str
    passed: bool
    note: str = ""


RESULTS: list[ProbeResult] = []


def record(probe_id: str, description: str, passed: bool, note: str = "") -> None:
    RESULTS.append(ProbeResult(probe_id, description, passed, note))
    print(f"  >>> {'PASS' if passed else 'FAIL'}: {probe_id} — {note}")


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _short(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — extend write timeout (mirrors explore_tally_write_v4.post_write)
# ─────────────────────────────────────────────────────────────────────────────
async def post_write(client: TallyClient, xml: str) -> str:
    saved = client._client.timeout
    client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    try:
        return await client.post_xml(xml)
    finally:
        client._client.timeout = saved


async def post_and_parse(client: TallyClient, xml: str, label: str, verbose: bool = False) -> tuple[dict, str]:
    """POST a write envelope; print XML + raw + parsed; return (parsed_dict, raw_text)."""
    print(f"\n--- {label} ---")
    print(f"  XML: {_short(xml, 1800 if verbose else 1000)}")
    raw = await post_write(client, xml)
    # Verbose probes (T1/B1) dump the FULL raw response — format is unvalidated there.
    print(f"  Raw: {raw.strip() if verbose else _short(raw, 800)}")
    parsed = parse_import_response(raw)
    print(f"  Parsed: {json.dumps(parsed, indent=2)}")
    return parsed, raw


def _ok(parsed: dict) -> bool:
    """A create is good when CREATED>=1 with no ERRORS and no EXCEPTIONS (silent failure)."""
    return parsed.get("created", 0) >= 1 and parsed.get("errors", 0) == 0 and parsed.get("exceptions", 0) == 0


def _err_text(parsed: dict, raw: str) -> str:
    return parsed.get("error_message") or f"created={parsed.get('created')} errors={parsed.get('errors')} exc={parsed.get('exceptions')} | raw={_short(raw, 300)}"


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup helper — delete a voucher by Master ID (LASTVCHID), DD-MMM-YYYY date.
# (mirrors probe_bill_allocations_live.py delete + import_builder.build_delete_voucher)
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None, label: str) -> None:
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
            print(f"  [cleanup WARN] {label} (mid={master_id}) not deleted: {_short(raw, 300)}")
    except Exception as e:  # noqa: BLE001 — cleanup must never abort the run
        print(f"  [cleanup ERROR] {label}: {type(e).__name__}: {e}")


async def cleanup_ledger(client: TallyClient, name: str) -> None:
    """Delete a ledger by name. NAME.LIST is REQUIRED (LESSONS §15 / import_builder)."""
    ledger_xml = (
        f'<LEDGER NAME="{_esc(name)}" ACTION="Delete">\n'
        f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
        f'</LEDGER>'
    )
    xml = _wrap_import("All Masters", COMPANY, ledger_xml)
    try:
        raw = await post_write(client, xml)
        parsed = parse_import_response(raw)
        print(f"  [cleanup ledger] {name!r}: deleted={parsed.get('deleted', 0)} errors={parsed.get('errors', 0)}")
    except Exception as e:  # noqa: BLE001
        print(f"  [cleanup ledger ERROR] {name!r}: {type(e).__name__}: {e}")


async def create_ledger(client: TallyClient, name: str, parent: str) -> bool:
    """Create a simple (bill-wise OFF) ledger under `parent`; return True on success."""
    ledger_xml = (
        f'<LEDGER NAME="{_esc(name)}" ACTION="Create">\n'
        f'<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>\n'
        f'<PARENT>{_esc(parent)}</PARENT>\n'
        f'</LEDGER>'
    )
    xml = _wrap_import("All Masters", COMPANY, ledger_xml)
    parsed, raw = await post_and_parse(client, xml, f"create ledger {name!r} under {parent!r}")
    return _ok(parsed)


# ─────────────────────────────────────────────────────────────────────────────
# Discover real ledgers from the live company (party/bank/expense) for the probes.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Ledgers:
    debtor: str
    creditor: str
    bank: str
    expense: str
    all_names: set[str]


async def discover_ledgers(client: TallyClient) -> Ledgers:
    banner("Discovering ledgers from live company")
    ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    names = {l["name"] for l in ledgers}

    def _by_group(*groups: str) -> str | None:
        wanted = {g.lower() for g in groups}
        return next(
            (l["name"] for l in ledgers if (l.get("parent_group") or "").lower() in wanted),
            None,
        )

    debtor = _by_group("sundry debtors") or (FALLBACK_DEBTOR if FALLBACK_DEBTOR in names else None) or FALLBACK_DEBTOR
    creditor = _by_group("sundry creditors") or (FALLBACK_CREDITOR if FALLBACK_CREDITOR in names else None) or FALLBACK_CREDITOR
    # Bank ledgers live under "Bank Accounts" or "Bank OD A/c" group.
    bank = _by_group("bank accounts", "bank od a/c", "bank occ a/c") or (FALLBACK_BANK if FALLBACK_BANK in names else None) or FALLBACK_BANK
    expense = (
        _by_group("indirect expenses", "direct expenses")
        or (FALLBACK_EXPENSE if FALLBACK_EXPENSE in names else None)
        or FALLBACK_EXPENSE
    )

    print(f"  Found {len(ledgers)} ledgers")
    print(f"  debtor={debtor!r} creditor={creditor!r} bank={bank!r} expense={expense!r}")
    return Ledgers(debtor=debtor, creditor=creditor, bank=bank, expense=expense, all_names=names)


# ─────────────────────────────────────────────────────────────────────────────
# E1–E6 — Purchase / Debit Note / Credit Note format variants (copied from plan Task 0)
# ─────────────────────────────────────────────────────────────────────────────
async def probe_e1(client: TallyClient, led: Ledgers) -> None:
    banner("E1 — Purchase with LEDGERENTRIES.LIST")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<NARRATION>{NPFX} E1: LEDGERENTRIES.LIST</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.creditor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>1.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-1.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E1 Purchase + LEDGERENTRIES.LIST")
    ok = _ok(parsed)
    record("E1", "Purchase w/ LEDGERENTRIES.LIST + Invoice View + ISPARTYLEDGER", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Purchase", parsed.get("last_vch_id"), "E1 Purchase")


async def probe_e2(client: TallyClient, led: Ledgers) -> None:
    banner("E2 — Purchase with ALLLEDGERENTRIES.LIST (control, known-good)")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<NARRATION>{NPFX} E2: ALLLEDGERENTRIES.LIST</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.creditor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>1.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-1.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E2 Purchase + ALLLEDGERENTRIES.LIST")
    ok = _ok(parsed)
    record("E2", "Purchase w/ ALLLEDGERENTRIES.LIST + Accounting View (control)", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Purchase", parsed.get("last_vch_id"), "E2 Purchase")


async def probe_e3(client: TallyClient, led: Ledgers) -> None:
    banner("E3 — Purchase ALLLEDGERENTRIES.LIST + Invoice Voucher View (isolate PERSISTEDVIEW)")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<NARRATION>{NPFX} E3: ALLLEDGER + Invoice View</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.creditor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>1.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-1.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E3 Purchase ALLLEDGER + Invoice View")
    ok = _ok(parsed)
    record("E3", "Purchase ALLLEDGERENTRIES.LIST + Invoice View (isolate PERSISTEDVIEW)", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Purchase", parsed.get("last_vch_id"), "E3 Purchase")


async def probe_e4(client: TallyClient, led: Ledgers) -> None:
    banner("E4 — Purchase with ISPARTYLEDGER=Yes + BILLALLOCATIONS.LIST (New Ref)")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<NARRATION>{NPFX} E4: BILLALLOCATIONS</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.creditor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>1.00</AMOUNT>
<BILLALLOCATIONS.LIST>
<NAME>{NPFX}-INV-E4</NAME>
<BILLTYPE>New Ref</BILLTYPE>
<AMOUNT>1.00</AMOUNT>
</BILLALLOCATIONS.LIST>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-1.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E4 Purchase + BILLALLOCATIONS.LIST")
    ok = _ok(parsed)
    record("E4", "Purchase w/ ISPARTYLEDGER + BILLALLOCATIONS.LIST (New Ref)", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Purchase", parsed.get("last_vch_id"), "E4 Purchase")


async def probe_e5(client: TallyClient, led: Ledgers) -> None:
    banner("E5 — Debit Note with Agst Ref bill allocation")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Debit Note" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Debit Note</VOUCHERTYPENAME>
<NARRATION>{NPFX} E5: Debit Note</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.creditor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>1.00</AMOUNT>
<BILLALLOCATIONS.LIST>
<NAME>{NPFX}-INV-E5</NAME>
<BILLTYPE>Agst Ref</BILLTYPE>
<AMOUNT>1.00</AMOUNT>
</BILLALLOCATIONS.LIST>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-1.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E5 Debit Note")
    ok = _ok(parsed)
    record("E5", "Debit Note w/ LEDGERENTRIES.LIST + Agst Ref BILLALLOCATIONS", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Debit Note", parsed.get("last_vch_id"), "E5 Debit Note")


async def probe_e6(client: TallyClient, led: Ledgers) -> None:
    banner("E6 — Credit Note with Agst Ref bill allocation")
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Credit Note" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>
<NARRATION>{NPFX} E6: Credit Note</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.debtor)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>-1.00</AMOUNT>
<BILLALLOCATIONS.LIST>
<NAME>{NPFX}-SALE-E6</NAME>
<BILLTYPE>Agst Ref</BILLTYPE>
<AMOUNT>-1.00</AMOUNT>
</BILLALLOCATIONS.LIST>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>1.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "E6 Credit Note")
    ok = _ok(parsed)
    record("E6", "Credit Note w/ LEDGERENTRIES.LIST + Agst Ref BILLALLOCATIONS", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Credit Note", parsed.get("last_vch_id"), "E6 Credit Note")


# ─────────────────────────────────────────────────────────────────────────────
# E7 — get_company_list() : read-only, no cleanup. Try several candidate envelopes.
# ─────────────────────────────────────────────────────────────────────────────
async def probe_e7(client: TallyClient) -> None:
    banner("E7 — get_company_list() (read-only)")
    candidates = {
        "Collection:List of Companies": """<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>List of Companies</ID></HEADER>
<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
<TDL><TDLMESSAGE><COLLECTION NAME="List of Companies" ISMODIFY="No"><TYPE>Company</TYPE><NATIVEMETHOD>Name</NATIVEMETHOD></COLLECTION></TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>""",
        "Collection:Company (custom)": """<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>ProbeCompanies</ID></HEADER>
<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
<TDL><TDLMESSAGE><COLLECTION NAME="ProbeCompanies" ISMODIFY="No"><TYPE>Company</TYPE><NATIVEMETHOD>Name</NATIVEMETHOD><NATIVEMETHOD>StartingFrom</NATIVEMETHOD></COLLECTION></TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>""",
        "Function:$$CmpName": """<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Function</TYPE><ID>$$CmpName</ID></HEADER>
<BODY><DESC></DESC></BODY></ENVELOPE>""",
    }
    found_any = False
    detail_parts: list[str] = []
    for label, xml in candidates.items():
        print(f"\n--- E7 candidate: {label} ---")
        try:
            raw = await client.post_xml(xml)
            print(f"  Raw: {_short(raw, 1200)}")
            try:
                root = ET.fromstring(sanitize_xml(raw))
                names = [
                    (c.findtext("NAME") or c.get("NAME") or "").strip()
                    for c in root.iter("COMPANY")
                ]
                names = [n for n in names if n]
                if names:
                    found_any = True
                    detail_parts.append(f"{label}->{len(names)} companies: {names}")
                    print(f"  Companies parsed: {names}")
            except ET.ParseError:
                print("  (unparseable XML)")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {type(e).__name__}: {e}")
            detail_parts.append(f"{label}: error {e}")
    record("E7", "get_company_list() — read-only company-list envelope", found_any,
           "; ".join(detail_parts) if detail_parts else "no candidate returned company names")


# ─────────────────────────────────────────────────────────────────────────────
# E8 — get_party_vouchers(party, voucher_types) : TDL collection filtering by party + type.
# Read-only, no cleanup. Picks a real party dynamically (debtor first, else fallback).
# ─────────────────────────────────────────────────────────────────────────────
def build_party_vouchers_query(party: str, vch_type: str, from_date: str, to_date: str) -> str:
    """TDL Collection of `vch_type` vouchers filtered to `party` via $PartyLedgerName."""
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>ProbePartyVch</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
<SVCURRENTCOMPANY>{_esc(COMPANY)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="ProbePartyVch" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<CHILDOF>$$VchType{vch_type}</CHILDOF>
<NATIVEMETHOD>Date</NATIVEMETHOD>
<NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>
<NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>
<NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>
<NATIVEMETHOD>Reference</NATIVEMETHOD>
<NATIVEMETHOD>Amount</NATIVEMETHOD>
<FILTER>ProbePartyFilter</FILTER>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="ProbePartyFilter">$PartyLedgerName = "{_esc(party)}"</SYSTEM>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""


async def probe_e8(client: TallyClient, led: Ledgers) -> None:
    banner("E8 — get_party_vouchers(party, voucher_types) (read-only)")
    party = led.debtor or FALLBACK_DEBTOR
    detail_parts: list[str] = []
    matched = False
    for vch_type in ("Sales", "Purchase"):
        xml = build_party_vouchers_query(party, vch_type, "01-04-2025", "31-03-2026")
        print(f"\n--- E8 {vch_type} vouchers for party {party!r} ---")
        try:
            raw = await client.post_xml(xml)
            print(f"  Raw: {_short(raw, 1000)}")
            root = ET.fromstring(sanitize_xml(raw))
            vchs = list(root.iter("VOUCHER"))
            parties = {(v.findtext("PARTYLEDGERNAME") or "").strip() for v in vchs}
            parties.discard("")
            # Pass-for-this-type = collection returned only the requested party (filter honored).
            type_ok = bool(vchs) and parties <= {party}
            if type_ok:
                matched = True
            detail_parts.append(f"{vch_type}: {len(vchs)} vch, parties={parties or '∅'}")
            print(f"  {len(vchs)} vouchers; distinct parties={parties or '∅'}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {type(e).__name__}: {e}")
            detail_parts.append(f"{vch_type}: error {e}")
    record("E8", f"get_party_vouchers filter by party={party!r} + type (TDL FILTER)", matched,
           "; ".join(detail_parts))


# ─────────────────────────────────────────────────────────────────────────────
# T1 — TDS Journal (NEW, never tested). Best-guess plain balanced Journal legs.
#
# NOTE: This is a BEST-GUESS structure. We are NOT wiring Tally's native TDS module
# (no TDS nature-of-payment, no TDS duty ledger config). The only question here is:
# does Tally accept a plain balanced Journal voucher with a "TDS" ledger leg against a
# party ledger leg (CREATED=1, ERRORS=0)? Journal sign convention (v4 Op 9 / import_builder):
#   debit  leg = ISDEEMEDPOSITIVE=Yes, AMOUNT negative
#   credit leg = ISDEEMEDPOSITIVE=No,  AMOUNT positive
# Party ledgers are bill-wise, so the party leg carries a New Ref bill allocation to satisfy
# Tally's bill-wise requirement (otherwise it can throw "no bill allocation").
# ─────────────────────────────────────────────────────────────────────────────
def _journal_leg(ledger: str, deemed_positive: bool, signed_amount: float,
                 bill_name: str | None = None, bill_type: str = "New Ref") -> str:
    bill_xml = ""
    if bill_name:
        bill_xml = (
            f"\n<BILLALLOCATIONS.LIST>\n"
            f"<NAME>{_esc(bill_name)}</NAME>\n"
            f"<BILLTYPE>{_esc(bill_type)}</BILLTYPE>\n"
            f"<AMOUNT>{signed_amount:.2f}</AMOUNT>\n"
            f"</BILLALLOCATIONS.LIST>"
        )
    return (
        f"<ALLLEDGERENTRIES.LIST>\n"
        f"<LEDGERNAME>{_esc(ledger)}</LEDGERNAME>\n"
        f"<ISDEEMEDPOSITIVE>{'Yes' if deemed_positive else 'No'}</ISDEEMEDPOSITIVE>\n"
        f"<AMOUNT>{signed_amount:.2f}</AMOUNT>{bill_xml}\n"
        f"</ALLLEDGERENTRIES.LIST>"
    )


async def probe_t1a(client: TallyClient, led: Ledgers) -> None:
    banner("T1a — TDS receivable Journal (NEW, best-guess): Dr TDS Receivable / Cr customer")
    amt = 100.00
    # Dr _ProbeTDS Receivable (asset), Cr customer (reduces what they owe).
    dr = _journal_leg(TDS_RECEIVABLE, deemed_positive=True, signed_amount=-amt)
    cr = _journal_leg(
        led.debtor, deemed_positive=False, signed_amount=amt,
        bill_name=f"{NPFX}-TDS-T1a", bill_type="New Ref",
    )
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Journal" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
<NARRATION>{NPFX} T1a: TDS receivable best-guess journal</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{dr}
{cr}
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "T1a TDS receivable journal", verbose=True)
    ok = _ok(parsed)
    record("T1a", "TDS receivable Journal (Dr TDS Recv / Cr customer) — best-guess", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Journal", parsed.get("last_vch_id"), "T1a Journal")


async def probe_t1b(client: TallyClient, led: Ledgers) -> None:
    banner("T1b — TDS payable Journal (NEW, best-guess): Dr supplier / Cr TDS Payable")
    amt = 100.00
    # Dr supplier (reduces what we owe), Cr _ProbeTDS Payable (liability).
    dr = _journal_leg(
        led.creditor, deemed_positive=True, signed_amount=-amt,
        bill_name=f"{NPFX}-TDS-T1b", bill_type="New Ref",
    )
    cr = _journal_leg(TDS_PAYABLE, deemed_positive=False, signed_amount=amt)
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Journal" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
<NARRATION>{NPFX} T1b: TDS payable best-guess journal</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{dr}
{cr}
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "T1b TDS payable journal", verbose=True)
    ok = _ok(parsed)
    record("T1b", "TDS payable Journal (Dr supplier / Cr TDS Payable) — best-guess", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Journal", parsed.get("last_vch_id"), "T1b Journal")


# ─────────────────────────────────────────────────────────────────────────────
# B1 — Bank reconciliation (NEW, never tested). Payment with BANKALLOCATIONS.LIST on the
# bank leg carrying instrument number/date + transaction type + value (bank) date.
# Format is UNVALIDATED — we dump the full raw Tally response for both variants.
#
# Sign convention for a Payment (import_builder.build_create_payment_voucher):
#   expense (debit) leg = ISDEEMEDPOSITIVE=Yes, AMOUNT negative
#   bank (credit)  leg  = ISDEEMEDPOSITIVE=No,  AMOUNT positive
# ─────────────────────────────────────────────────────────────────────────────
async def probe_b1(client: TallyClient, led: Ledgers) -> None:
    banner("B1 — Payment with BANKALLOCATIONS.LIST on bank leg (NEW, unvalidated)")
    amt = 100.00
    # BANKALLOCATIONS.LIST best-guess fields: DATE (value/bank date, YYYYMMDD),
    # INSTRUMENTNUMBER, INSTRUMENTDATE, TRANSACTIONTYPE (e.g. "Cheque").
    bank_leg = (
        f"<ALLLEDGERENTRIES.LIST>\n"
        f"<LEDGERNAME>{_esc(led.bank)}</LEDGERNAME>\n"
        f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
        f"<AMOUNT>{amt:.2f}</AMOUNT>\n"
        f"<BANKALLOCATIONS.LIST>\n"
        f"<DATE>{VCH_DATE}</DATE>\n"
        f"<INSTRUMENTNUMBER>{NPFX}-CHQ-001</INSTRUMENTNUMBER>\n"
        f"<INSTRUMENTDATE>{VCH_DATE}</INSTRUMENTDATE>\n"
        f"<TRANSACTIONTYPE>Cheque</TRANSACTIONTYPE>\n"
        f"<PAYMENTFAVOURING>{_esc(led.expense)}</PAYMENTFAVOURING>\n"
        f"<AMOUNT>{amt:.2f}</AMOUNT>\n"
        f"</BANKALLOCATIONS.LIST>\n"
        f"</ALLLEDGERENTRIES.LIST>"
    )
    expense_leg = (
        f"<ALLLEDGERENTRIES.LIST>\n"
        f"<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>\n"
        f"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        f"<AMOUNT>-{amt:.2f}</AMOUNT>\n"
        f"</ALLLEDGERENTRIES.LIST>"
    )
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>{NPFX} B1: payment w/ BANKALLOCATIONS</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{expense_leg}
{bank_leg}
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "B1 Payment + BANKALLOCATIONS.LIST", verbose=True)
    ok = _ok(parsed)
    record("B1", "Payment w/ BANKALLOCATIONS.LIST (instrument no/date + txn type)", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Payment", parsed.get("last_vch_id"), "B1 Payment")


async def probe_b1b(client: TallyClient, led: Ledgers) -> None:
    banner("B1b — Payment with top-level BANKDATE field (NEW, unvalidated)")
    amt = 100.00
    bank_leg = (
        f"<ALLLEDGERENTRIES.LIST>\n"
        f"<LEDGERNAME>{_esc(led.bank)}</LEDGERNAME>\n"
        f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>\n"
        f"<AMOUNT>{amt:.2f}</AMOUNT>\n"
        f"</ALLLEDGERENTRIES.LIST>"
    )
    expense_leg = (
        f"<ALLLEDGERENTRIES.LIST>\n"
        f"<LEDGERNAME>{_esc(led.expense)}</LEDGERNAME>\n"
        f"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\n"
        f"<AMOUNT>-{amt:.2f}</AMOUNT>\n"
        f"</ALLLEDGERENTRIES.LIST>"
    )
    # BANKDATE as a top-level voucher field (alternative to per-leg BANKALLOCATIONS).
    xml = _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>{NPFX} B1b: payment w/ top-level BANKDATE</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
<BANKDATE>{VCH_DATE}</BANKDATE>
{expense_leg}
{bank_leg}
</VOUCHER>""")
    parsed, raw = await post_and_parse(client, xml, "B1b Payment + top-level BANKDATE", verbose=True)
    ok = _ok(parsed)
    record("B1b", "Payment w/ top-level BANKDATE field", ok,
           "created" if ok else _err_text(parsed, raw))
    await cleanup_voucher(client, "Payment", parsed.get("last_vch_id"), "B1b Payment")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    client = TallyClient(host=host, port=port)
    print(f"Group B Write Probe — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Voucher date: {VCH_DATE} (display {VCH_DATE_DISPLAY})")

    # Connectivity check.
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    tds_recv_created = False
    tds_pay_created = False
    try:
        led = await discover_ledgers(client)

        # Create the two TDS probe ledgers up-front (deleted in finally).
        banner("Setup — create TDS probe ledgers")
        tds_recv_created = await create_ledger(client, TDS_RECEIVABLE, "Current Assets")
        tds_pay_created = await create_ledger(client, TDS_PAYABLE, "Duties & Taxes")
        # Each probe is wrapped so one failure never aborts the rest.
        probes = [
            ("E1", probe_e1), ("E2", probe_e2), ("E3", probe_e3), ("E4", probe_e4),
            ("E5", probe_e5), ("E6", probe_e6),
            ("E7", lambda c, _l: probe_e7(c)),
            ("E8", probe_e8),
            ("T1a", probe_t1a), ("T1b", probe_t1b),
            ("B1", probe_b1), ("B1b", probe_b1b),
        ]
        for pid, fn in probes:
            try:
                await fn(client, led)
            except Exception as e:  # noqa: BLE001
                record(pid, f"{pid} (uncaught)", False, f"{type(e).__name__}: {e}")
                print(f"  !! {pid} raised: {type(e).__name__}: {e}")
    finally:
        # ── Cleanup TDS probe ledgers (vouchers were cleaned inside each probe) ──
        banner("Cleanup — TDS probe ledgers")
        if tds_recv_created:
            await cleanup_ledger(client, TDS_RECEIVABLE)
        if tds_pay_created:
            await cleanup_ledger(client, TDS_PAYABLE)

        # ── Final results table ──
        banner("RESULTS TABLE")
        print(f"{'PROBE':<6} {'PASS/FAIL':<10} {'DESCRIPTION':<58} NOTE")
        print("-" * 120)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.probe_id:<6} {status:<10} {r.description[:56]:<58} {_short(r.note, 200)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 120)
        print(f"{len(RESULTS)} probes — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Group B Tally WRITE probe (E1–E8 + TDS + bank recon)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
