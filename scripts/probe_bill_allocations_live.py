"""Live probe: verify BILLALLOCATIONS.LIST flows through to Bills Receivable.

Flow:
  1. Fire ONE sales voucher S_TEST_01 with New Ref bill alloc.
  2. Query bills_receivable — assert S_TEST_01 shows up with full outstanding.
  3. Fire ONE receipt RCT_TEST_01 with Agst Ref bill alloc against S_TEST_01.
  4. Re-query bills_receivable — assert S_TEST_01 cleared (or absent).
  5. CLEANUP (always, in finally): delete both test vouchers via MasterId.
  6. Print PASS/FAIL summary + day_book delta.

Tally @ localhost:9000, company "Bharat Traders Private Limited".
Pre-req: 50 seeded vouchers already present; we only add+remove 2.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

from tally_bridge.client import TallyClient
from tally_bridge.queries.reports import bills_receivable
from tally_bridge.queries.vouchers import day_book
from tally_bridge.response_parser import detect_error, sanitize_xml
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
HOST = "localhost"
PORT = 9000

VCH_DATE_YYYYMMDD = "20260302"
VCH_DATE_DISPLAY = "02-Mar-2026"  # for delete envelope (DD-MMM-YYYY)
AS_ON_DATE = "31-03-2026"  # DD-MM-YYYY for bills query
FROM_DATE = "01-04-2025"
TO_DATE = "31-03-2026"

SALES_VCH = "S_TEST_01"
RECEIPT_VCH = "RCT_TEST_01"
PARTY = "Apex Technologies Pvt Ltd"
BANK = "HDFC Bank - Current A/c"

# HP Laptop 15s × 2 @ 45000 = 90000 + 18% GST (intra) = 106200
ITEM_NAME = "HP Laptop 15s"
ITEM_QTY = 2
ITEM_RATE = 45000.0
SALES_LEDGER = "Sales - Electronics"
UOM = "Nos"
GST_RATE = 18
TOTAL_AMOUNT = 106200.00


def build_voucher_master_id_query(from_date: str, to_date: str, company: str) -> str:
    """TDL collection: voucher → MasterId, plus filterable fields."""
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
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
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
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


async def fetch_master_ids(client: TallyClient, vnums: set[str]) -> dict[str, dict]:
    """Return {voucher_number: {master_id, voucher_type, date_display}} for matches."""
    raw = await client.post_xml(
        build_voucher_master_id_query(FROM_DATE, TO_DATE, COMPANY)
    )
    err = detect_error(raw)
    if err:
        raise RuntimeError(f"MasterId query failed: {err}")
    root = ET.fromstring(sanitize_xml(raw))
    out: dict[str, dict] = {}
    for v in root.iter("VOUCHER"):
        vnum = (v.findtext("VOUCHERNUMBER") or "").strip()
        if vnum not in vnums:
            continue
        mid = (v.findtext("MASTERID") or "").strip()
        date_raw = (v.findtext("DATE") or "").strip()
        vtype = (v.findtext("VOUCHERTYPENAME") or "").strip()
        if not mid or not date_raw:
            continue
        try:
            dt = datetime.strptime(date_raw, "%Y%m%d")
        except ValueError:
            continue
        out[vnum] = {
            "master_id": mid,
            "voucher_type": vtype,
            "date_display": dt.strftime("%d-%b-%Y"),
        }
    return out


async def daybook_count(client: TallyClient) -> int:
    rows = await day_book(client, FROM_DATE, TO_DATE, company=COMPANY)
    return len(rows)


async def find_test_bill(client: TallyClient, bill_name: str) -> dict | None:
    bills = await bills_receivable(client, AS_ON_DATE, company=COMPANY)
    for b in bills:
        if b.bill_number.strip() == bill_name:
            return {
                "party": b.party_name,
                "bill": b.bill_number,
                "amount": float(b.amount),
                "pending": float(b.pending_amount),
            }
    return None


async def main() -> int:
    client = TallyClient(host=HOST, port=PORT)
    writer = TallyWriter(client, COMPANY)

    failures: list[str] = []
    sales_created = False
    receipt_created = False
    # Track LASTVCHID returned by Tally directly — voucher numbers may be auto-renumbered
    # by Tally's voucher type config, so VOUCHERNUMBER lookup is unreliable for cleanup.
    sales_master_id: str | None = None
    receipt_master_id: str | None = None

    try:
        print(f"=== BILLALLOCATIONS live probe ===")
        print(f"Tally @ {HOST}:{PORT}, company={COMPANY!r}")
        print()

        initial_count = await daybook_count(client)
        print(f"Initial day_book count: {initial_count}")
        print()

        # ----- Step 1: Sales voucher with New Ref -----
        print(f"[1] Creating Sales {SALES_VCH} (₹{TOTAL_AMOUNT:.2f}) "
              f"with New Ref bill alloc...")
        try:
            result = await writer.create_sales_voucher(
                date=VCH_DATE_YYYYMMDD,
                voucher_number=SALES_VCH,
                party=PARTY,
                items=[(ITEM_NAME, ITEM_QTY, ITEM_RATE, SALES_LEDGER, UOM, GST_RATE)],
                narration=f"Probe sale — bill ref test {SALES_VCH}",
                gst_mode="intra",
                bill_allocations=[
                    {"name": SALES_VCH, "type": "New Ref",
                     "amount": TOTAL_AMOUNT, "credit_period": "30 Days"},
                ],
            )
            sales_created = True
            sales_master_id = str(result.get("last_vch_id") or "").strip() or None
            print(f"    OK created={result.get('created')} "
                  f"lastvchid={sales_master_id}")
        except Exception as e:
            failures.append(f"sales create failed: {e}")
            print(f"    FAILED: {e}")
            return _summary(failures, sales_created, receipt_created)

        # ----- Step 2: Verify bill shows in receivables -----
        print(f"[2] Querying bills_receivable as on {AS_ON_DATE}...")
        bill = await find_test_bill(client, SALES_VCH)
        if bill is None:
            failures.append(
                f"bill {SALES_VCH} NOT found in bills_receivable after sale "
                f"(BILLALLOCATIONS may have been ignored)"
            )
            print(f"    FAIL: {SALES_VCH} missing from receivables")
        else:
            print(f"    Found: party={bill['party']!r} bill={bill['bill']!r} "
                  f"amount={bill['amount']:.2f} pending={bill['pending']:.2f}")
            if abs(bill["pending"] - TOTAL_AMOUNT) > 0.5:
                failures.append(
                    f"bill {SALES_VCH} pending={bill['pending']:.2f} "
                    f"doesn't match expected {TOTAL_AMOUNT:.2f}"
                )
                print(f"    FAIL: pending mismatch")
            else:
                print(f"    PASS: receivable matches")
        print()

        # ----- Step 3: Receipt voucher with Agst Ref -----
        print(f"[3] Creating Receipt {RECEIPT_VCH} (₹{TOTAL_AMOUNT:.2f}) "
              f"with Agst Ref against {SALES_VCH}...")
        try:
            result = await writer.create_receipt_voucher(
                date=VCH_DATE_YYYYMMDD,
                voucher_number=RECEIPT_VCH,
                party=PARTY,
                bank_ledger=BANK,
                amount=TOTAL_AMOUNT,
                narration=f"Probe receipt — clearing {SALES_VCH}",
                bill_allocations=[
                    {"name": SALES_VCH, "type": "Agst Ref",
                     "amount": TOTAL_AMOUNT},
                ],
            )
            receipt_created = True
            receipt_master_id = str(result.get("last_vch_id") or "").strip() or None
            print(f"    OK created={result.get('created')} "
                  f"lastvchid={receipt_master_id}")
        except Exception as e:
            failures.append(f"receipt create failed: {e}")
            print(f"    FAILED: {e}")

        # ----- Step 4: Verify bill cleared -----
        print(f"[4] Re-querying bills_receivable...")
        bill = await find_test_bill(client, SALES_VCH)
        if bill is None:
            print(f"    PASS: {SALES_VCH} no longer in receivables (fully cleared)")
        elif abs(bill["pending"]) < 0.5:
            print(f"    PASS: {SALES_VCH} still listed but pending=0")
        else:
            failures.append(
                f"bill {SALES_VCH} still has pending={bill['pending']:.2f} "
                f"after Agst Ref receipt"
            )
            print(f"    FAIL: pending={bill['pending']:.2f} (expected ~0)")
        print()

        return _summary(failures, sales_created, receipt_created)

    finally:
        # ----- Step 5: Cleanup (ALWAYS) -----
        print()
        print("=== Cleanup ===")
        # Delete receipt first (clears the bill reference), then sales.
        targets = []
        if receipt_master_id:
            targets.append(("Receipt", RECEIPT_VCH, receipt_master_id))
        if sales_master_id:
            targets.append(("Sales", SALES_VCH, sales_master_id))

        if not targets:
            print("Nothing to clean up.")
        for vtype, vnum, mid in targets:
            print(f"  Deleting {vtype} #{vnum} (mid={mid}, {VCH_DATE_DISPLAY})...",
                  end=" ")
            try:
                r = await writer.delete_voucher(
                    voucher_type=vtype,
                    master_id=mid,
                    date=VCH_DATE_DISPLAY,
                )
                deleted = r.get("deleted", 0)
                if deleted >= 1:
                    print(f"OK (deleted={deleted})")
                else:
                    print(f"FAIL: {r}")
            except Exception as e:
                print(f"FAIL: {e}")

        try:
            final_count = await daybook_count(client)
            print(f"Final day_book count: {final_count}")
        except Exception as e:
            print(f"  Final count fetch failed: {e}")

        await client.close()


def _summary(failures: list[str], sales_created: bool, receipt_created: bool) -> int:
    print()
    print("=== Summary ===")
    print(f"sales_created={sales_created} receipt_created={receipt_created}")
    if failures:
        print(f"FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: BILLALLOCATIONS reconciliation works end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
