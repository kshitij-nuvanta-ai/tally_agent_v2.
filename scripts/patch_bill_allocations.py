#!/usr/bin/env python3
"""In-place patch: add Agst Ref BILLALLOCATIONS to existing seeded receipts/payments.

Path A of the bill-allocation rework. Targets a company that was seeded under the
PRE-CHANGE seeder (RCT/PMT vouchers without bill_allocations + receipt amounts at
base rather than gross). Steps per voucher:

  1. Look up MasterId via TDL collection (filter by VOUCHERNUMBER).
  2. Delete the voucher (TAGNAME="Master ID").
  3. Recreate via TallyWriter with the NEW amount (RCT: gross) and Agst Ref
     bill_allocations as defined in scripts/seed_data/bharat_traders.py.

After patch: queries bills_receivable + bills_payable and asserts the residuals
match EXPECTED_BILLS_RECEIVABLE / EXPECTED_BILLS_PAYABLE.

Usage:
    PYTHONPATH=. python scripts/patch_bill_allocations.py \\
        --host localhost --port 9000 \\
        --company "Bharat Traders Private Limited"
    # add --dry-run to print what would happen without touching Tally
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

from tally_bridge.client import TallyClient
from tally_bridge.queries.reports import bills_payable, bills_receivable
from tally_bridge.response_parser import detect_error, sanitize_xml
from tally_bridge.writer import TallyWriter
from scripts.seed_data import bharat_traders as bt

FROM_DATE = "01-04-2025"
TO_DATE = "31-03-2026"
AS_ON_DATE = "31-03-2026"


def _build_master_id_query(company: str) -> str:
    """TDL collection: MasterId + Date + VoucherTypeName + PartyLedgerName.
    Tally auto-renumbers receipts/payments to plain integers (1, 2, ...) on import,
    so VOUCHERNUMBER lookup fails. Narration text was changed in the new fixture,
    so that fails too. Composite key (voucher_type, date_yyyymmdd, party) is unique
    per fixture row and stable across narration/amount changes — use it.
    """
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>VchAllWithMasterId</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{FROM_DATE}</SVFROMDATE>
<SVTODATE>{TO_DATE}</SVTODATE>
<SVCurrentCompany>{company}</SVCurrentCompany>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="VchAllWithMasterId" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<CHILDOF>$$VchTypeAllVouchers</CHILDOF>
<NATIVEMETHOD>MasterId</NATIVEMETHOD>
<NATIVEMETHOD>Date</NATIVEMETHOD>
<NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>
<NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>
<NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>
<NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


# Composite-key tuple type: (voucher_type_lower, date_yyyymmdd, party_name)
_Key = tuple[str, str, str]


async def _fetch_master_ids_by_key(
    client: TallyClient, company: str, target_keys: set[_Key]
) -> dict[_Key, dict]:
    """Returns {(vtype_lower, yyyymmdd, party): info}.
    `party` is matched by walking ALLLEDGERENTRIES.LIST/LEDGERNAME — Tally's
    PARTYLEDGERNAME points to bank for Receipt vouchers (asymmetric from Payment),
    so we look for any ledger entry whose name appears in target_keys."""
    raw = await client.post_xml(_build_master_id_query(company))
    err = detect_error(raw)
    if err:
        raise RuntimeError(f"MasterId query failed: {err}")
    root = ET.fromstring(sanitize_xml(raw))
    out: dict[_Key, dict] = {}
    duplicates: list[_Key] = []
    # Pre-index targets by (vtype, date) → set of parties we care about.
    targets_by_td: dict[tuple[str, str], set[str]] = {}
    for vtype, date_, party in target_keys:
        targets_by_td.setdefault((vtype, date_), set()).add(party)

    for v in root.iter("VOUCHER"):
        mid = (v.findtext("MASTERID") or "").strip()
        date_raw = (v.findtext("DATE") or "").strip()
        vtype = (v.findtext("VOUCHERTYPENAME") or "").strip()
        vnum = (v.findtext("VOUCHERNUMBER") or "").strip()
        if not (mid and date_raw and vtype):
            continue
        td = (vtype.lower(), date_raw)
        candidate_parties = targets_by_td.get(td)
        if not candidate_parties:
            continue
        # Walk ledger entries; pick the first one matching a target party.
        matched_party: str | None = None
        for le in v.iter("ALLLEDGERENTRIES.LIST"):
            ln = (le.findtext("LEDGERNAME") or "").strip()
            if ln in candidate_parties:
                matched_party = ln
                break
        if not matched_party:
            continue
        key: _Key = (vtype.lower(), date_raw, matched_party)
        try:
            dt = datetime.strptime(date_raw, "%Y%m%d")
        except ValueError:
            continue
        if key in out:
            duplicates.append(key)
            continue
        out[key] = {
            "master_id": mid,
            "voucher_type": vtype,
            "date_display": dt.strftime("%d-%b-%Y"),
            "voucher_number": vnum,
        }
    if duplicates:
        print(f"  WARN: duplicate (type,date,party) ignored after first match: {duplicates}")
    return out


def _format_inr(n: float) -> str:
    # Indian comma format (lakh/crore).
    s = f"{int(round(n)):,}"
    # Convert 970,537 → 9,70,537 — replace first ',XXX' with last 3, regroup left in 2s.
    if "," not in s:
        return s
    head, _, last3 = s.rpartition(",")
    head = head.replace(",", "")
    grouped = []
    while len(head) > 2:
        grouped.append(head[-2:])
        head = head[:-2]
    if head:
        grouped.append(head)
    return ",".join(reversed(grouped)) + "," + last3


async def _patch(
    client: TallyClient,
    writer: TallyWriter,
    company: str,
    dry_run: bool,
) -> int:
    # Targets: all 10 receipts + 4 party payments (the ones with `against` set).
    pmt_targets = [v for v in bt.PAYMENTS if v[6] is not None]
    rct_targets = list(bt.RECEIPTS)

    # Composite key (vtype_lower, yyyymmdd, party) → fixture vnum, for log readability.
    key_to_vnum: dict[_Key, str] = {}
    for v in pmt_targets:
        key_to_vnum[("payment", v[1], v[2])] = v[0]
    for v in rct_targets:
        key_to_vnum[("receipt", v[1], v[2])] = v[0]
    target_keys = set(key_to_vnum.keys())

    print(f"=== Patch in-place: bill allocations on {company!r} ===")
    print(f"Targets: {len(pmt_targets)} party payments + {len(rct_targets)} receipts "
          f"= {len(target_keys)} vouchers")
    print()

    print("[1] Looking up MasterIds (matching by type+date+party)...")
    mid_map = await _fetch_master_ids_by_key(client, company, target_keys)
    missing = sorted(target_keys - set(mid_map.keys()))
    if missing:
        print(f"  WARN: {len(missing)} target vouchers not found:")
        for k in missing:
            print(f"    - {key_to_vnum[k]}: {k}")
        print(f"        (already patched? wrong company? proceeding with what we have)")
    found_keys = sorted(mid_map.keys(), key=lambda k: (k[1], k[0]))
    print(f"  found {len(found_keys)} of {len(target_keys)}")
    print()

    # ---- Phase A: delete existing target vouchers ----
    print(f"[2] Deleting {len(found_keys)} existing vouchers...")
    deleted_keys: set[_Key] = set()
    for key in found_keys:
        info = mid_map[key]
        vnum = key_to_vnum[key]
        msg = (f"  - {info['voucher_type']} fixture={vnum} tally#{info['voucher_number']} "
               f"(mid={info['master_id']}, {info['date_display']})")
        if dry_run:
            print(f"{msg} [DRY-RUN]")
            deleted_keys.add(key)
            continue
        try:
            r = await writer.delete_voucher(
                voucher_type=info["voucher_type"],
                master_id=info["master_id"],
                date=info["date_display"],
            )
            n = r.get("deleted", 0)
            if n >= 1:
                print(f"{msg} OK")
                deleted_keys.add(key)
            else:
                print(f"{msg} FAIL: {r}")
        except Exception as e:
            print(f"{msg} FAIL: {e}")
    print()

    # ---- Phase B: recreate with new amounts + Agst Ref ----
    print(f"[3] Recreating with Agst Ref allocations...")
    create_failures: list[str] = []

    # Recreate in chronological order so day_book ordering remains sane.
    combined = (
        [("PMT", v) for v in pmt_targets if ("payment", v[1], v[2]) in deleted_keys]
        + [("R", v) for v in rct_targets if ("receipt", v[1], v[2]) in deleted_keys]
    )
    combined.sort(key=lambda x: x[1][1])

    for kind, v in combined:
        if kind == "PMT":
            vnum, date, payee, bank, amount, narration, against = v
            bill_alloc = [
                {"name": against, "type": "Agst Ref", "amount": amount}
            ]
            print(f"  + Payment {vnum} {date} {payee} ₹{amount} → Agst Ref {against}")
            if dry_run:
                continue
            try:
                await writer.create_payment_voucher(
                    date=date, debit_ledger=payee, credit_ledger=bank,
                    amount=amount, narration=narration,
                    bill_allocations=bill_alloc,
                )
            except Exception as e:
                create_failures.append(f"{vnum}: {e}")
                print(f"      FAIL: {e}")
        else:  # "R"
            vnum, date, party, bank, amount, narration, against = v
            bill_alloc = [
                {"name": against, "type": "Agst Ref", "amount": amount}
            ]
            print(f"  + Receipt {vnum} {date} {party} ₹{amount} → Agst Ref {against}")
            if dry_run:
                continue
            try:
                await writer.create_receipt_voucher(
                    date=date, voucher_number=vnum, party=party,
                    bank_ledger=bank, amount=amount, narration=narration,
                    bill_allocations=bill_alloc,
                )
            except Exception as e:
                create_failures.append(f"{vnum}: {e}")
                print(f"      FAIL: {e}")
    print()

    if create_failures:
        print(f"FAIL: {len(create_failures)} create errors:")
        for f in create_failures:
            print(f"  - {f}")
        return 1

    if dry_run:
        print("DRY-RUN complete — no changes made.")
        return 0

    # ---- Phase C: verify residuals ----
    return await _verify_residuals(client, company)


async def _verify_residuals(client: TallyClient, company: str) -> int:
    print(f"[4] Verifying bills_receivable / bills_payable as on {AS_ON_DATE}...")
    failures: list[str] = []

    br = await bills_receivable(client, AS_ON_DATE, company=company)
    bp = await bills_payable(client, AS_ON_DATE, company=company)

    actual_br = {(b.party_name.strip(), b.bill_number.strip()): float(b.pending_amount)
                 for b in br}
    actual_bp = {(b.party_name.strip(), b.bill_number.strip()): float(b.pending_amount)
                 for b in bp}

    expected_br = {(p, n): float(a) for p, n, a in bt.EXPECTED_BILLS_RECEIVABLE}
    expected_bp = {(p, n): float(a) for p, n, a in bt.EXPECTED_BILLS_PAYABLE}

    print()
    print("  --- Bills Receivable ---")
    print(f"  Expected {len(expected_br)} bills, got {len(actual_br)} bills.")
    for key, exp in expected_br.items():
        got = actual_br.get(key)
        ok = got is not None and abs(got - exp) < 0.5
        marker = "OK" if ok else "FAIL"
        got_s = f"₹{_format_inr(got)}" if got is not None else "MISSING"
        print(f"    [{marker}] {key[0]} | {key[1]} | "
              f"expected ₹{_format_inr(exp)} got {got_s}")
        if not ok:
            failures.append(f"BR mismatch {key}: expected {exp} got {got}")
    extra_br = set(actual_br) - set(expected_br)
    for key in sorted(extra_br):
        print(f"    [EXTRA] {key[0]} | {key[1]} | actual ₹{_format_inr(actual_br[key])}")
        failures.append(f"BR unexpected bill {key}")

    print()
    print("  --- Bills Payable ---")
    print(f"  Expected {len(expected_bp)} bills, got {len(actual_bp)} bills.")
    for key, exp in expected_bp.items():
        got = actual_bp.get(key)
        ok = got is not None and abs(got - exp) < 0.5
        marker = "OK" if ok else "FAIL"
        got_s = f"₹{_format_inr(got)}" if got is not None else "MISSING"
        print(f"    [{marker}] {key[0]} | {key[1]} | "
              f"expected ₹{_format_inr(exp)} got {got_s}")
        if not ok:
            failures.append(f"BP mismatch {key}: expected {exp} got {got}")
    extra_bp = set(actual_bp) - set(expected_bp)
    for key in sorted(extra_bp):
        print(f"    [EXTRA] {key[0]} | {key[1]} | actual ₹{_format_inr(actual_bp[key])}")
        failures.append(f"BP unexpected bill {key}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} discrepancies.")
        return 1
    print("PASS: all bill residuals match expected values.")
    return 0


async def main_async(host: str, port: int, company: str, dry_run: bool) -> int:
    client = TallyClient(host=host, port=port)
    writer = TallyWriter(client=client, company=company)
    try:
        return await _patch(client, writer, company, dry_run)
    finally:
        await client.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--company", default="Bharat Traders Private Limited")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args.host, args.port, args.company, args.dry_run)))


if __name__ == "__main__":
    main()
