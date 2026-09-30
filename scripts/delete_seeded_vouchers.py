"""One-shot: delete the 9 partial-seed vouchers from "Bharat Traders Private Limited".

Stage 1 seed (Run D) silently dropped 41/50 vouchers due to license-date-clamping;
9 made it into Tally with auto-assigned numeric voucher numbers. This script:

1. Fetches all day_book entries with their MasterId via a TDL Collection using
   CHILDOF=$$VchTypeAllVouchers (verified envelope from Stage 0 — see
   docs/tally-write-exploration-v4.md §"Reading Master IDs").
2. Prints a confirmation table.
3. Deletes each voucher via TallyWriter.delete_voucher (TAGNAME="Master ID").
4. Re-queries day_book and asserts count == 0.

Tally @ localhost:9000, company "Bharat Traders Private Limited".
User has set Tally F2 current date to >= 31-Mar-2026 so writes won't clamp.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

from tally_bridge.client import TallyClient
from tally_bridge.response_parser import detect_error, sanitize_xml
from tally_bridge.queries.vouchers import day_book
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
HOST = "localhost"
PORT = 9000

# Seed FY range — covers Apr 2025 to Mar 2026.
FROM_DATE = "01-04-2025"  # DD-MM-YYYY for SVFROMDATE
TO_DATE = "31-03-2026"


def build_voucher_master_id_query(from_date: str, to_date: str, company: str) -> str:
    """Build TDL Collection that returns vouchers WITH their MasterId.

    The standard day_book query returns voucher field bodies but no MasterId.
    Using CHILDOF=$$VchTypeAllVouchers + NATIVEMETHOD MasterId gets it.
    See docs/tally-write-exploration-v4.md §"Reading Master IDs".
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
<NATIVEMETHOD>Narration</NATIVEMETHOD>
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


def parse_master_id_vouchers(raw_xml: str) -> list[dict]:
    """Parse the MasterId-augmented voucher collection."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    out = []
    for v in root.iter("VOUCHER"):
        date_raw = (v.findtext("DATE") or "").strip()
        master_id = (v.findtext("MASTERID") or "").strip()
        vtype = (v.findtext("VOUCHERTYPENAME") or "").strip()
        vnum = (v.findtext("VOUCHERNUMBER") or "").strip()
        if not master_id or not date_raw:
            continue
        # Tally returns YYYYMMDD here; convert to DD-MMM-YYYY for delete envelope.
        try:
            dt = datetime.strptime(date_raw, "%Y%m%d")
        except ValueError:
            continue
        out.append({
            "master_id": master_id,
            "voucher_type": vtype,
            "voucher_number": vnum,
            "date_yyyymmdd": date_raw,
            "date_display": dt.strftime("%d-%b-%Y"),
            "date_iso": dt.strftime("%Y-%m-%d"),
        })
    return out


async def main() -> int:
    client = TallyClient(host=HOST, port=PORT)
    writer = TallyWriter(client, COMPANY)

    print(f"Connecting to Tally @ {HOST}:{PORT}, company={COMPANY!r}")
    print()

    try:
        # 1. Fetch vouchers with MasterId
        print(f"Fetching vouchers with MasterId from {FROM_DATE} to {TO_DATE}...")
        raw = await client.post_xml(
            build_voucher_master_id_query(FROM_DATE, TO_DATE, COMPANY)
        )
        err = detect_error(raw)
        if err:
            print(f"ERROR: Tally returned: {err}", file=sys.stderr)
            return 1

        vouchers = parse_master_id_vouchers(raw)
        if not vouchers:
            print("No vouchers found. Day book already empty.")
            return 0

        # 2. Print confirmation table
        print(f"Found {len(vouchers)} voucher(s):")
        print()
        print(f"  {'#':>2}  {'Type':<10} {'Num':>4}  {'Date':<12}  MasterID")
        print(f"  {'-'*2}  {'-'*10} {'-'*4}  {'-'*12}  {'-'*10}")
        for i, v in enumerate(vouchers, 1):
            print(f"  {i:>2}  {v['voucher_type']:<10} {v['voucher_number']:>4}  "
                  f"{v['date_display']:<12}  {v['master_id']}")
        print()

        # 3. Delete each
        print("Deleting...")
        for i, v in enumerate(vouchers, 1):
            print(f"  [{i}/{len(vouchers)}] {v['voucher_type']} #{v['voucher_number']} "
                  f"({v['date_display']}, mid={v['master_id']})...", end=" ")
            try:
                result = await writer.delete_voucher(
                    voucher_type=v["voucher_type"],
                    master_id=v["master_id"],
                    date=v["date_display"],  # DD-MMM-YYYY required for delete
                )
            except Exception as e:
                print(f"FAILED: {e}")
                print(f"\nHalting on first failure. parsed={getattr(e, 'args', e)}",
                      file=sys.stderr)
                return 2

            deleted = result.get("deleted", 0)
            errors = result.get("errors", 0)
            exceptions = result.get("exceptions", 0)
            err_msg = result.get("error_message")
            if deleted >= 1 and errors == 0 and exceptions == 0:
                print(f"OK (deleted={deleted})")
            else:
                print(f"FAILED: deleted={deleted}, errors={errors}, "
                      f"exceptions={exceptions}, msg={err_msg}")
                print(f"  full response: {result}", file=sys.stderr)
                return 2

        print()
        # 4. Verify day_book empty
        print("Verifying day_book is empty...")
        remaining = await day_book(
            client, from_date=FROM_DATE, to_date=TO_DATE, company=COMPANY,
        )
        print(f"Final day_book count: {len(remaining)}")
        if remaining:
            print("WARNING: vouchers still present:", file=sys.stderr)
            for v in remaining:
                print(f"  - {v}", file=sys.stderr)
            return 3

        print()
        print("SUCCESS: all 9 vouchers deleted; day_book is empty.")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
