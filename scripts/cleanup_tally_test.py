"""Clean up test entities created by explore_tally_write scripts.

Deletes all entities prefixed with '_Test' from Tally.

Usage:
    PYTHONPATH=. python scripts/cleanup_tally_test.py --host localhost --port 9000
"""
import argparse
import asyncio
import xml.etree.ElementTree as ET

from tally_bridge.client import TallyClient
from tally_bridge.request_builder import build_list_ledgers, build_list_groups, build_day_book
from tally_bridge.response_parser import parse_ledger_list, parse_groups, parse_vouchers, sanitize_xml

COMPANY = "NUVANTA AI TECHNOLOGIES PRIVATE LIMITED"


def parse_response(xml_text: str) -> dict:
    try:
        root = ET.fromstring(sanitize_xml(xml_text))
    except ET.ParseError:
        return {"raw": xml_text}
    result = {}
    for tag in ["CREATED", "ALTERED", "DELETED", "ERRORS", "LASTVCHID",
                "CANCELLED", "EXCEPTIONS", "LINEERROR"]:
        el = root.find(tag) if root.find(tag) is not None else root.find(f".//{tag}")
        if el is not None and el.text:
            result[tag] = el.text.strip()
    return result


async def cleanup(host: str, port: int):
    client = TallyClient(host=host, port=port)

    print("=== TALLY TEST CLEANUP ===\n")

    # 1. Find test vouchers (narration starting with _Test)
    print("--- Finding test vouchers ---")
    resp = await client.post_xml(build_day_book("01-04-2025", "05-04-2026"))
    vouchers = parse_vouchers(resp)
    test_vouchers = [v for v in vouchers if "_Test" in (v.get("narration") or "")]
    print(f"Found {len(test_vouchers)} test voucher(s)")
    for v in test_vouchers:
        print(f"  #{v['voucher_number']} | {v['date']} | {v['voucher_type']} | {v['narration'][:60]}")

    # Delete test vouchers using ALTER with VCHTYPE + VOUCHERNUMBER
    for v in test_vouchers:
        print(f"\n  Deleting voucher #{v['voucher_number']}...")
        # Tally delete needs the master ID or the voucher identifiers
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="{v['voucher_type']}" ACTION="Delete">
<VOUCHERNUMBER>{v['voucher_number']}</VOUCHERNUMBER>
<DATE>{v['date']}</DATE>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        resp = await client.post_xml(xml)
        result = parse_response(resp)
        deleted = result.get("DELETED", "0")
        print(f"  Result: DELETED={deleted} | {result}")

    # 2. Find test ledgers
    print("\n--- Finding test ledgers ---")
    ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    test_ledgers = [l for l in ledgers if l["name"].startswith("_Test")]
    print(f"Found {len(test_ledgers)} test ledger(s)")
    for l in test_ledgers:
        print(f"  {l['name']} → {l['parent_group']}")

    # Delete test ledgers
    for l in test_ledgers:
        print(f"\n  Deleting ledger '{l['name']}'...")
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="{l['name']}" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        resp = await client.post_xml(xml)
        result = parse_response(resp)
        deleted = result.get("DELETED", "0")
        print(f"  Result: DELETED={deleted} | {result}")

    # 3. Find test groups
    print("\n--- Finding test groups ---")
    groups = parse_groups(await client.post_xml(build_list_groups()))
    test_groups = [g for g in groups if g["name"].startswith("_Test")]
    print(f"Found {len(test_groups)} test group(s)")

    for g in test_groups:
        print(f"\n  Deleting group '{g['name']}'...")
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP NAME="{g['name']}" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        resp = await client.post_xml(xml)
        result = parse_response(resp)
        deleted = result.get("DELETED", "0")
        print(f"  Result: DELETED={deleted} | {result}")

    # 4. Verify
    print("\n--- Verification ---")
    ledgers_after = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    remaining = [l for l in ledgers_after if l["name"].startswith("_Test")]
    vouchers_after = parse_vouchers(await client.post_xml(build_day_book("01-04-2025", "05-04-2026")))
    test_v_remaining = [v for v in vouchers_after if "_Test" in (v.get("narration") or "")]

    if remaining or test_v_remaining:
        print(f"  WARNING: Still remaining — ledgers: {[l['name'] for l in remaining]}, vouchers: {len(test_v_remaining)}")
    else:
        print(f"  All test entities cleaned up. Ledgers: {len(ledgers_after)}, Vouchers: {len(vouchers_after)}")

    await client.close()
    print("\n=== CLEANUP COMPLETE ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(cleanup(args.host, args.port))
