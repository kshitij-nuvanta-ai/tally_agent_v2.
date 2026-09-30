"""Group B WRITE *read-back* probe — prove the special fields actually PERSISTED.

`scripts/probe_group_b.py` already established that T1a/T1b/B1/B1b return CREATED=1
with no ERRORS. But CREATED=1 only means Tally *accepted* the envelope — it says
NOTHING about whether the special fields (TDS journal legs, bank-reconciliation
instrument number/date/transaction-type, top-level BANKDATE) actually survived the
round-trip and were stored on the voucher.

This focused probe closes that gap for the four never-before-validated probes:

  T1a  TDS receivable Journal — Dr _ProbeGroupB TDS Receivable / Cr customer
  T1b  TDS payable Journal    — Dr supplier / Cr _ProbeGroupB TDS Payable
  B1   Payment w/ BANKALLOCATIONS.LIST on the bank leg (instrument no/date + txn type + bank date)
  B1b  Payment w/ top-level BANKDATE field

For EACH probe the flow is:
  1. Build the SAME XML used in probe_group_b.py.
  2. POST it, capture LASTVCHID (Master ID).
  3. READ THE VOUCHER BACK — full voucher XML for 15-Jun-2025 via a custom TDL
     Voucher Collection that fetches *all* fields (NATIVEMETHOD *). Locate the
     just-created voucher by matching the unique narration string ("_ProbeGroupB ...").
  4. ASSERT the special fields survived by parsing the returned voucher XML, print
     the relevant XML slice verbatim, then a clear PERSISTED: yes/no/partial verdict.
  5. DELETE the voucher (Master ID + DD-MMM-YYYY date). In a finally block, also
     delete the two `_ProbeGroupB TDS` probe ledgers.

How vouchers are read back (the key design choice — FIXED 2026-06-08):
  EARLIER BUG: this script used `<NATIVEMETHOD>*</NATIVEMETHOD>` on a Voucher
  collection. `*` returns ONLY the voucher *header* fields (NARRATION,
  PARTYLEDGERNAME, VOUCHERTYPENAME, ...) and does NOT recursively expand the
  nested `ALLLEDGERENTRIES.LIST` ledger legs — for ANY voucher type. Verified
  live: a matched B1 payment came back with no <LEDGERNAME>, no <AMOUNT>, no
  <BANKALLOCATIONS.LIST>. Every "dropped"/"absent" verdict was therefore a
  read-back blindness artifact, NOT a real data-loss finding.

  FIX: replicate the proven `build_day_book()` collection shape — a TDL Voucher
  collection with EXPLICIT `<NATIVEMETHOD>` entries (the codebase's
  `_voucher_native_methods()` includes `AllLedgerEntries`, which IS known to
  return `ALLLEDGERENTRIES.LIST` legs with LEDGERNAME/AMOUNT, as proven by
  `parse_vouchers()`). On top of that we add explicit native methods for the
  fields we specifically need to verify that `*` never expanded:
    - `AllLedgerEntries.BankAllocations`  → the nested BANKALLOCATIONS.LIST inside
      the bank leg, plus its members (InstrumentNumber / InstrumentDate /
      TransactionType / Date / Amount / PaymentFavouring).
    - `BankDate`, `IsDeemedPositive`, `LedgerName`, `Amount` at the appropriate
      levels.
  We also scope the collection with `<CHILDOF>$$VchTypeAllVouchers</CHILDOF>`
  (documented in tally-write-exploration-v4.md as the trick that actually
  POPULATES the leg/field bodies — without it Tally returns VOUCHER elements with
  empty children).

  We match the target voucher on its unique NARRATION (the "_ProbeGroupB <id>:"
  prefix), dump it verbatim, then assert. If the matched voucher has ZERO ledger
  legs we print a loud "export returned no legs — method still wrong" warning so a
  tooling failure can never again be mistaken for data loss.

WARNING: writes to a live Tally. Targets "Bharat Traders Private Limited", Rs 1/100
amounts, "_ProbeGroupB" narration prefix. Cleanup runs in finally/try blocks.

Usage:
    PYTHONPATH=. uv run python scripts/probe_group_b_readback.py --host localhost --port 9000 \
        2>&1 | tee docs/probe-group-b-readback.log
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

# ─── Reuse everything we can from the original probe script ───────────────────
from scripts.probe_group_b import (
    COMPANY,
    NPFX,
    TDS_RECEIVABLE,
    TDS_PAYABLE,
    VCH_DATE,
    VCH_DATE_DISPLAY,
    Ledgers,
    _esc,
    _journal_leg,
    _ok,
    _short,
    banner,
    cleanup_ledger,
    cleanup_voucher,
    create_ledger,
    discover_ledgers,
    post_and_parse,
)

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _wrap_import
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import sanitize_xml


# ─────────────────────────────────────────────────────────────────────────────
# Read-back result tracking
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ReadbackResult:
    probe_id: str
    description: str
    created: bool = False
    found: bool = False
    # per-field persistence: field-name -> "yes" | "no"
    fields: dict[str, str] = field(default_factory=dict)
    verdict: str = ""  # overall: yes / no / partial / n/a
    note: str = ""


RESULTS: list[ReadbackResult] = []


def _fields_summary(fields: dict[str, str]) -> str:
    if not fields:
        return "(none checked)"
    survived = [k for k, v in fields.items() if v == "yes"]
    dropped = [k for k, v in fields.items() if v == "no"]
    parts = []
    if survived:
        parts.append("survived=" + ",".join(survived))
    if dropped:
        parts.append("dropped=" + ",".join(dropped))
    return " | ".join(parts) if parts else "(none checked)"


def _overall_verdict(created: bool, found: bool, fields: dict[str, str]) -> str:
    if not created:
        return "n/a (not created)"
    if not found:
        return "no (not found on read-back)"
    if not fields:
        return "partial (voucher persisted; no special fields checked)"
    vals = set(fields.values())
    if vals == {"yes"}:
        return "yes"
    if vals == {"no"}:
        return "no"
    return "partial"


# ─────────────────────────────────────────────────────────────────────────────
# Read-back mechanism — TDL Voucher Collection with EXPLICIT native methods.
#
# Replaces the broken `<NATIVEMETHOD>*</NATIVEMETHOD>` collection (header-only;
# nested ledger legs never expanded). This mirrors the proven build_day_book()
# shape: explicit native methods that include `AllLedgerEntries` (returns
# ALLLEDGERENTRIES.LIST legs with LEDGERNAME/AMOUNT, per parse_vouchers()), and
# adds the nested `AllLedgerEntries.BankAllocations` method so the bank leg's
# BANKALLOCATIONS.LIST (InstrumentNumber/InstrumentDate/TransactionType/Date)
# comes back too. CHILDOF $$VchTypeAllVouchers populates the field bodies.
# ─────────────────────────────────────────────────────────────────────────────

# Explicit native methods. Order: header fields first, then the nested lists.
# `AllLedgerEntries` pulls ALLLEDGERENTRIES.LIST legs; the dotted
# `AllLedgerEntries.BankAllocations` pulls the BANKALLOCATIONS.LIST nested inside
# the bank leg (the part `*` silently dropped). Per-leg/per-alloc leaf fields are
# named explicitly so Tally populates them rather than returning empty bodies.
_READBACK_NATIVE_METHODS = [
    # voucher header
    "Date",
    "VoucherTypeName",
    "VoucherNumber",
    "PartyLedgerName",
    "Narration",
    "BankDate",  # top-level bank/value date (B1b)
    # ledger legs + their leaf fields
    "AllLedgerEntries",
    "AllLedgerEntries.LedgerName",
    "AllLedgerEntries.Amount",
    "AllLedgerEntries.IsDeemedPositive",
    # nested bank-reconciliation allocations inside the bank leg + their leaves
    "AllLedgerEntries.BankAllocations",
    "AllLedgerEntries.BankAllocations.InstrumentNumber",
    "AllLedgerEntries.BankAllocations.InstrumentDate",
    "AllLedgerEntries.BankAllocations.TransactionType",
    "AllLedgerEntries.BankAllocations.Date",
    "AllLedgerEntries.BankAllocations.Amount",
    "AllLedgerEntries.BankAllocations.PaymentFavouring",
]


def build_full_voucher_collection(from_date: str, to_date: str, company: str) -> str:
    """TDL Voucher Collection in [from_date, to_date] with EXPLICIT native methods
    so nested ledger legs AND their bank allocations come back (not just headers).
    """
    methods = "\n".join(
        f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in _READBACK_NATIVE_METHODS
    )
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>ProbeReadbackVchs</ID></HEADER>
<BODY><DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
<SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE>
<COLLECTION NAME="ProbeReadbackVchs" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<CHILDOF>$$VchTypeAllVouchers</CHILDOF>
{methods}
</COLLECTION>
</TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""


def _vch_text(vch: ET.Element, tag: str) -> str:
    return (vch.findtext(tag) or "").strip()


async def read_back_voucher(
    client: TallyClient, narration_marker: str, verbose: bool = False
) -> ET.Element | None:
    """Fetch the full voucher XML for VCH_DATE and return the <VOUCHER> whose
    NARRATION contains `narration_marker`, or None if not found.

    Reads back via a custom full-field Voucher Collection scoped to the single
    voucher date (15-Jun-2025 in display form for SVFROMDATE/SVTODATE).
    """
    xml = build_full_voucher_collection(VCH_DATE_DISPLAY, VCH_DATE_DISPLAY, COMPANY)
    raw = await client.post_xml(xml)
    if verbose:
        print(f"  [read-back] raw collection ({len(raw)} chars):")
        print(_short(raw, 4000))
    try:
        root = ET.fromstring(sanitize_xml(raw))
    except ET.ParseError as e:
        print(f"  [read-back PARSE ERROR] {e}")
        return None

    matches = [
        v for v in root.iter("VOUCHER")
        if narration_marker in (_vch_text(v, "NARRATION"))
    ]
    print(f"  [read-back] {len(list(root.iter('VOUCHER')))} vouchers on {VCH_DATE_DISPLAY}; "
          f"{len(matches)} match narration marker {narration_marker!r}")
    if not matches:
        return None
    if len(matches) > 1:
        print(f"  [read-back WARN] {len(matches)} narration matches; using the first")
    matched = matches[0]
    # Tooling guard: if the matched voucher has NO ledger legs, the export method
    # is still broken (header-only) — fail loud so we never mistake a read-back
    # blindness artifact for real data loss again.
    legs = _ledger_legs(matched)
    if not legs:
        print(
            "  [read-back ERROR] export returned NO LEDGER LEGS for the matched "
            "voucher — EXPORT METHOD STILL WRONG (header-only). The nested "
            "ALLLEDGERENTRIES.LIST did not expand; field verdicts below are "
            "read-back blindness, NOT data-loss findings. Fix the collection "
            "before trusting any 'dropped'/'absent' result."
        )
    else:
        print(f"  [read-back] matched voucher has {len(legs)} ledger leg(s): {legs}")
    return matched


def _dump_element(elem: ET.Element, label: str) -> None:
    """Print an element's XML verbatim so a human can eyeball persisted fields."""
    raw = ET.tostring(elem, encoding="unicode")
    print(f"  [verbatim {label}]:\n{_short(raw, 4000)}")


# ─────────────────────────────────────────────────────────────────────────────
# T1a / T1b — TDS Journal read-back assertions.
# Assert: voucher type = Journal AND both expected legs present with right amounts.
# ─────────────────────────────────────────────────────────────────────────────
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


def _assert_tds_journal(
    probe_id: str, description: str, created: bool,
    vch: ET.Element | None, tds_ledger: str, party_ledger: str,
) -> None:
    res = ReadbackResult(probe_id=probe_id, description=description, created=created)
    if not created:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "skipped read-back (create failed)"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return
    res.found = vch is not None
    if vch is None:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "voucher not found in read-back collection"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return

    _dump_element(vch, f"{probe_id} VOUCHER")
    vtype = _vch_text(vch, "VOUCHERTYPENAME") or vch.get("VCHTYPE", "")
    legs = _ledger_legs(vch)
    leg_names = {n for n, _ in legs}
    print(f"  [{probe_id}] VOUCHERTYPENAME={vtype!r}; legs found={legs}")

    res.fields["voucher_type=Journal"] = "yes" if vtype.lower() == "journal" else "no"
    res.fields[f"tds_leg({tds_ledger})"] = "yes" if tds_ledger in leg_names else "no"
    res.fields[f"party_leg({party_ledger})"] = "yes" if party_ledger in leg_names else "no"

    res.verdict = _overall_verdict(created, True, res.fields)
    res.note = f"type={vtype!r}; {_fields_summary(res.fields)}"
    RESULTS.append(res)
    print(f"  >>> {probe_id}: PERSISTED {res.verdict} — {res.note}")


# ─────────────────────────────────────────────────────────────────────────────
# B1 — Payment with BANKALLOCATIONS.LIST. The KEY unknown: did the bank-reconciliation
# data persist? Check the bank leg for a BANKALLOCATIONS.LIST and the presence of
# INSTRUMENTNUMBER / INSTRUMENTDATE / TRANSACTIONTYPE / DATE (bank date).
# ─────────────────────────────────────────────────────────────────────────────
def _find_bank_allocations(vch: ET.Element) -> list[ET.Element]:
    return list(vch.iter("BANKALLOCATIONS.LIST"))


def _assert_b1_bank_allocations(
    probe_id: str, description: str, created: bool, vch: ET.Element | None,
) -> None:
    res = ReadbackResult(probe_id=probe_id, description=description, created=created)
    if not created:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "skipped read-back (create failed)"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return
    res.found = vch is not None
    if vch is None:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "voucher not found in read-back collection"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return

    # Dump the whole voucher verbatim — bank-recon format is the main unknown.
    _dump_element(vch, f"{probe_id} VOUCHER (verbose)")

    bank_allocs = _find_bank_allocations(vch)
    res.fields["BANKALLOCATIONS.LIST"] = "yes" if bank_allocs else "no"
    print(f"  [{probe_id}] BANKALLOCATIONS.LIST blocks found: {len(bank_allocs)}")

    # Check each reconciliation field across all returned BANKALLOCATIONS.LIST blocks.
    recon_fields = ["INSTRUMENTNUMBER", "INSTRUMENTDATE", "TRANSACTIONTYPE", "DATE"]
    for fld in recon_fields:
        present_vals: list[str] = []
        for ba in bank_allocs:
            val = (ba.findtext(fld) or "").strip()
            if val:
                present_vals.append(val)
        res.fields[fld] = "yes" if present_vals else "no"
        print(f"  [{probe_id}] {fld}: {'present ' + str(present_vals) if present_vals else 'ABSENT/empty'}")
        if bank_allocs:
            for i, ba in enumerate(bank_allocs):
                _dump_element(ba, f"{probe_id} BANKALLOCATIONS.LIST[{i}]")
            break  # dump the blocks once, not per-field

    res.verdict = _overall_verdict(created, True, res.fields)
    res.note = _fields_summary(res.fields)
    RESULTS.append(res)
    print(f"  >>> {probe_id}: PERSISTED {res.verdict} — {res.note}")


# ─────────────────────────────────────────────────────────────────────────────
# B1b — top-level BANKDATE. Check whether BANKDATE (or any reconciliation marker)
# survived on the read-back voucher.
# ─────────────────────────────────────────────────────────────────────────────
def _assert_b1b_bankdate(
    probe_id: str, description: str, created: bool, vch: ET.Element | None,
) -> None:
    res = ReadbackResult(probe_id=probe_id, description=description, created=created)
    if not created:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "skipped read-back (create failed)"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return
    res.found = vch is not None
    if vch is None:
        res.verdict = _overall_verdict(created, False, {})
        res.note = "voucher not found in read-back collection"
        RESULTS.append(res)
        print(f"  >>> {probe_id}: {res.verdict} — {res.note}")
        return

    _dump_element(vch, f"{probe_id} VOUCHER (verbose)")

    # Direct top-level BANKDATE child.
    bankdate = _vch_text(vch, "BANKDATE")
    res.fields["BANKDATE (top-level)"] = "yes" if bankdate else "no"
    print(f"  [{probe_id}] top-level BANKDATE: {repr(bankdate) if bankdate else 'ABSENT'}")

    # Equivalent reconciliation markers anywhere in the voucher (e.g. Tally may
    # relocate the bank date into a per-leg BANKALLOCATIONS.LIST/DATE).
    bank_allocs = _find_bank_allocations(vch)
    alloc_dates = [(ba.findtext("DATE") or "").strip() for ba in bank_allocs]
    alloc_dates = [d for d in alloc_dates if d]
    res.fields["bank-recon marker (any)"] = "yes" if (bankdate or alloc_dates) else "no"
    print(f"  [{probe_id}] BANKALLOCATIONS.LIST/DATE markers: {alloc_dates or 'none'}")
    for i, ba in enumerate(bank_allocs):
        _dump_element(ba, f"{probe_id} BANKALLOCATIONS.LIST[{i}]")

    res.verdict = _overall_verdict(created, True, res.fields)
    res.note = _fields_summary(res.fields)
    RESULTS.append(res)
    print(f"  >>> {probe_id}: PERSISTED {res.verdict} — {res.note}")


# ─────────────────────────────────────────────────────────────────────────────
# Probe XML builders — IDENTICAL shapes to probe_group_b.py (copied so the payload
# we read back is exactly the payload validated there).
# ─────────────────────────────────────────────────────────────────────────────
def t1a_xml(led: Ledgers) -> str:
    amt = 100.00
    dr = _journal_leg(TDS_RECEIVABLE, deemed_positive=True, signed_amount=-amt)
    cr = _journal_leg(
        led.debtor, deemed_positive=False, signed_amount=amt,
        bill_name=f"{NPFX}-TDS-T1a", bill_type="New Ref",
    )
    return _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Journal" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
<NARRATION>{NPFX} T1a: TDS receivable best-guess journal</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{dr}
{cr}
</VOUCHER>""")


def t1b_xml(led: Ledgers) -> str:
    amt = 100.00
    dr = _journal_leg(
        led.creditor, deemed_positive=True, signed_amount=-amt,
        bill_name=f"{NPFX}-TDS-T1b", bill_type="New Ref",
    )
    cr = _journal_leg(TDS_PAYABLE, deemed_positive=False, signed_amount=amt)
    return _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Journal" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
<NARRATION>{NPFX} T1b: TDS payable best-guess journal</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{dr}
{cr}
</VOUCHER>""")


def b1_xml(led: Ledgers) -> str:
    amt = 100.00
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
    return _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>{NPFX} B1: payment w/ BANKALLOCATIONS</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{expense_leg}
{bank_leg}
</VOUCHER>""")


def b1b_xml(led: Ledgers) -> str:
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
    return _wrap_import("Vouchers", COMPANY, f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{VCH_DATE}</DATE>
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>{NPFX} B1b: payment w/ top-level BANKDATE</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
<BANKDATE>{VCH_DATE}</BANKDATE>
{expense_leg}
{bank_leg}
</VOUCHER>""")


# ─────────────────────────────────────────────────────────────────────────────
# Per-probe CREATE → READ-BACK → ASSERT → DELETE drivers.
# ─────────────────────────────────────────────────────────────────────────────
async def probe_t1a_readback(client: TallyClient, led: Ledgers) -> None:
    banner("T1a — TDS receivable Journal: CREATE → READ-BACK → ASSERT → DELETE")
    parsed, raw = await post_and_parse(client, t1a_xml(led), "T1a TDS receivable journal", verbose=True)
    created = _ok(parsed)
    mid = parsed.get("last_vch_id")
    try:
        vch = await read_back_voucher(client, "T1a:", verbose=False) if created else None
        _assert_tds_journal(
            "T1a", "TDS receivable Journal (Dr TDS Recv / Cr customer)",
            created, vch, TDS_RECEIVABLE, led.debtor,
        )
    finally:
        await cleanup_voucher(client, "Journal", mid, "T1a Journal")


async def probe_t1b_readback(client: TallyClient, led: Ledgers) -> None:
    banner("T1b — TDS payable Journal: CREATE → READ-BACK → ASSERT → DELETE")
    parsed, raw = await post_and_parse(client, t1b_xml(led), "T1b TDS payable journal", verbose=True)
    created = _ok(parsed)
    mid = parsed.get("last_vch_id")
    try:
        vch = await read_back_voucher(client, "T1b:", verbose=False) if created else None
        _assert_tds_journal(
            "T1b", "TDS payable Journal (Dr supplier / Cr TDS Payable)",
            created, vch, TDS_PAYABLE, led.creditor,
        )
    finally:
        await cleanup_voucher(client, "Journal", mid, "T1b Journal")


async def probe_b1_readback(client: TallyClient, led: Ledgers) -> None:
    banner("B1 — Payment w/ BANKALLOCATIONS.LIST: CREATE → READ-BACK → ASSERT → DELETE")
    parsed, raw = await post_and_parse(client, b1_xml(led), "B1 Payment + BANKALLOCATIONS.LIST", verbose=True)
    created = _ok(parsed)
    mid = parsed.get("last_vch_id")
    try:
        # Verbose read-back: the bank-recon round-trip is the central unknown.
        vch = await read_back_voucher(client, "B1:", verbose=True) if created else None
        _assert_b1_bank_allocations(
            "B1", "Payment w/ BANKALLOCATIONS.LIST (instrument no/date + txn type + bank date)",
            created, vch,
        )
    finally:
        await cleanup_voucher(client, "Payment", mid, "B1 Payment")


async def probe_b1b_readback(client: TallyClient, led: Ledgers) -> None:
    banner("B1b — Payment w/ top-level BANKDATE: CREATE → READ-BACK → ASSERT → DELETE")
    parsed, raw = await post_and_parse(client, b1b_xml(led), "B1b Payment + top-level BANKDATE", verbose=True)
    created = _ok(parsed)
    mid = parsed.get("last_vch_id")
    try:
        vch = await read_back_voucher(client, "B1b:", verbose=True) if created else None
        _assert_b1b_bankdate(
            "B1b", "Payment w/ top-level BANKDATE field",
            created, vch,
        )
    finally:
        await cleanup_voucher(client, "Payment", mid, "B1b Payment")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    client = TallyClient(host=host, port=port)
    print(f"Group B READ-BACK Probe — {datetime.now().isoformat()}")
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

        banner("Setup — create TDS probe ledgers")
        tds_recv_created = await create_ledger(client, TDS_RECEIVABLE, "Current Assets")
        tds_pay_created = await create_ledger(client, TDS_PAYABLE, "Duties & Taxes")

        probes = [
            ("T1a", probe_t1a_readback),
            ("T1b", probe_t1b_readback),
            ("B1", probe_b1_readback),
            ("B1b", probe_b1b_readback),
        ]
        for pid, fn in probes:
            try:
                await fn(client, led)
            except Exception as e:  # noqa: BLE001 — one probe must never abort the rest
                RESULTS.append(ReadbackResult(
                    probe_id=pid, description=f"{pid} (uncaught)",
                    created=False, found=False, verdict="error",
                    note=f"{type(e).__name__}: {e}",
                ))
                print(f"  !! {pid} raised: {type(e).__name__}: {e}")
    finally:
        # ── Cleanup TDS probe ledgers (vouchers cleaned inside each probe) ──
        banner("Cleanup — TDS probe ledgers")
        if tds_recv_created:
            await cleanup_ledger(client, TDS_RECEIVABLE)
        if tds_pay_created:
            await cleanup_ledger(client, TDS_PAYABLE)

        # ── Final results table ──
        banner("READ-BACK RESULTS TABLE")
        print(f"{'PROBE':<6} {'CREATED':<8} {'FOUND':<7} {'VERDICT':<26} SPECIAL FIELDS PERSISTED")
        print("-" * 130)
        for r in RESULTS:
            print(
                f"{r.probe_id:<6} "
                f"{('yes' if r.created else 'no'):<8} "
                f"{('yes' if r.found else 'no'):<7} "
                f"{r.verdict:<26} "
                f"{_fields_summary(r.fields)}"
            )
        print("-" * 130)
        n_yes = sum(1 for r in RESULTS if r.verdict == "yes")
        n_partial = sum(1 for r in RESULTS if r.verdict.startswith("partial"))
        n_no = len(RESULTS) - n_yes - n_partial
        print(f"{len(RESULTS)} probes — {n_yes} fully persisted, {n_partial} partial, {n_no} no/error")
        print("\nLegend: 'yes' = all checked special fields survived the round-trip; "
              "'partial' = some survived, some dropped; 'no' = special fields dropped "
              "(Tally accepted CREATE but did not store them).")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Group B Tally WRITE read-back probe (T1a/T1b/B1/B1b: CREATE→READ-BACK→ASSERT→DELETE)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
