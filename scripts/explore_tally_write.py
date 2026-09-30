"""Explore Tally write operations against live instance.

Runs a series of write/read/delete tests to verify XML formats and capture
Tally's response structure. All test entities are prefixed with '_Test_' for
easy identification and cleanup.

Usage:
    PYTHONPATH=. python scripts/explore_tally_write.py --host localhost --port 9000

Outputs:
    - Console: step-by-step results
    - docs/tally-write-exploration.log: tee'd output
    - docs/tally-write-exploration.md: documented findings (manual)
"""
import argparse
import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime

from tally_bridge.client import TallyClient
from tally_bridge.request_builder import (
    build_day_book,
    build_list_groups,
    build_list_ledgers,
)
from tally_bridge.response_parser import (
    parse_groups,
    parse_ledger_list,
    parse_vouchers,
    sanitize_xml,
)

# Track all writes for undo
WRITES_LOG: list[dict] = []


def log_write(action: str, entity_type: str, name: str, details: dict):
    """Track a write operation for potential undo."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "entity_type": entity_type,
        "name": name,
        "details": details,
    }
    WRITES_LOG.append(entry)
    print(f"  [TRACKED] {action} {entity_type}: {name}")


def build_create_group_xml(name: str, parent: str, company: str) -> str:
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP NAME="{name}" ACTION="Create">
<PARENT>{parent}</PARENT>
</GROUP>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_create_ledger_xml(name: str, parent: str, company: str, gstin: str | None = None) -> str:
    gstin_xml = f"\n<PARTYGSTIN>{gstin}</PARTYGSTIN>" if gstin else ""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="{name}" ACTION="Create">
<PARENT>{parent}</PARENT>{gstin_xml}
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_create_payment_xml(
    date: str, debit_ledger: str, credit_ledger: str,
    amount: float, narration: str, company: str,
) -> str:
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{date}</DATE>
<NARRATION>{narration}</NARRATION>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{debit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{credit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_delete_ledger_xml(name: str, company: str) -> str:
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="{name}" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_delete_group_xml(name: str, company: str) -> str:
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP NAME="{name}" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_delete_voucher_xml(voucher_type: str, date: str, narration: str, company: str) -> str:
    """Delete a voucher by type + master ID. Tally needs REMOTEID or specific identification."""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="{voucher_type}" ACTION="Delete" REMOTEID="_test_explore_voucher">
<DATE>{date}</DATE>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_create_payment_with_remoteid_xml(
    date: str, debit_ledger: str, credit_ledger: str,
    amount: float, narration: str, company: str, remote_id: str,
) -> str:
    """Create payment with REMOTEID for easy deletion later."""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="Payment" ACTION="Create" REMOTEID="{remote_id}">
<DATE>{date}</DATE>
<NARRATION>{narration}</NARRATION>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{debit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{credit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_delete_voucher_by_remoteid_xml(voucher_type: str, remote_id: str, company: str) -> str:
    """Delete a voucher using its REMOTEID."""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="{voucher_type}" ACTION="Delete" REMOTEID="{remote_id}">
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def parse_response(xml_text: str) -> dict:
    """Parse Tally import response into a summary dict."""
    try:
        root = ET.fromstring(sanitize_xml(xml_text))
    except ET.ParseError:
        return {"raw": xml_text, "parse_error": True}

    result = {}
    for tag in ["CREATED", "ALTERED", "DELETED", "ERRORS", "LASTVCHID", "LASTMID",
                "COMBINED", "IGNORED", "LINEERROR"]:
        el = root.find(tag) or root.find(f".//{tag}")
        if el is not None and el.text:
            result[tag] = el.text.strip()
    result["raw"] = xml_text
    return result


async def run_exploration(host: str, port: int):
    client = TallyClient(host=host, port=port)
    company = "NUVANTA AI TECHNOLOGIES PRIVATE LIMITED"

    print("=" * 70)
    print("TALLY WRITE EXPLORATION")
    print(f"Host: {host}:{port} | Company: {company}")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 70)

    # ----------------------------------------------------------------
    # TEST 1: Inspect existing ledger list
    # ----------------------------------------------------------------
    print("\n--- Test 1: Existing Ledger List ---")
    xml = build_list_ledgers()
    resp = await client.post_xml(xml)
    ledgers = parse_ledger_list(resp)
    print(f"Total ledgers: {len(ledgers)}")
    for l in ledgers[:10]:
        print(f"  {l['name']:40s} | {l['parent_group']}")
    print(f"  ... ({len(ledgers)} total)")

    # Check for cash/bank ledgers (we'll need one as credit ledger)
    cash_ledgers = [l for l in ledgers if l["parent_group"].lower() in ("cash-in-hand", "bank accounts", "bank occ a/c")]
    print(f"\nCash/Bank ledgers: {[l['name'] for l in cash_ledgers]}")

    # ----------------------------------------------------------------
    # TEST 2: Inspect GST ledger structure
    # ----------------------------------------------------------------
    print("\n--- Test 2: GST Ledger Detection ---")
    gst_keywords = ["GST", "CGST", "SGST", "IGST", "TAX", "INPUT", "OUTPUT"]
    gst_ledgers = [l for l in ledgers if any(k in l["name"].upper() for k in gst_keywords)]
    print(f"GST-related ledgers ({len(gst_ledgers)}):")
    for l in gst_ledgers:
        print(f"  {l['name']:40s} | under: {l['parent_group']}")
    if not gst_ledgers:
        print("  (none found — company may not have GST ledgers set up)")

    # ----------------------------------------------------------------
    # TEST 3: Inspect group hierarchy
    # ----------------------------------------------------------------
    print("\n--- Test 3: Account Group Hierarchy ---")
    xml = build_list_groups()
    resp = await client.post_xml(xml)
    groups = parse_groups(resp)
    print(f"Total groups: {len(groups)}")
    for g in groups:
        indent = "  " if g["parent"] else ""
        print(f"  {indent}{g['name']:40s} → parent: {g['parent'] or '(root)'}")

    # ----------------------------------------------------------------
    # TEST 4: Sample existing Payment vouchers
    # ----------------------------------------------------------------
    print("\n--- Test 4: Sample Existing Payment Vouchers ---")
    xml = build_day_book("01-04-2025", "05-04-2026", voucher_type="Payment", company=company)
    resp = await client.post_xml(xml)
    vouchers = parse_vouchers(resp)
    print(f"Total Payment vouchers: {len(vouchers)}")
    for v in vouchers[:5]:
        print(f"  {v['date']} | {v['voucher_number']:10s} | {v['narration'][:50]}")
        for le in v.get("ledger_entries", []):
            print(f"    {le['ledger_name']:30s}: {le['amount']}")

    # ----------------------------------------------------------------
    # TEST 5: Create a test group
    # ----------------------------------------------------------------
    print("\n--- Test 5: Create Test Group ---")
    xml = build_create_group_xml("_Test Exploration Group", "Indirect Expenses", company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")
    log_write("Create", "Group", "_Test Exploration Group", result)

    # ----------------------------------------------------------------
    # TEST 6: Create a test ledger under the new group
    # ----------------------------------------------------------------
    print("\n--- Test 6: Create Test Ledger ---")
    xml = build_create_ledger_xml("_Test Expense Ledger", "_Test Exploration Group", company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")
    log_write("Create", "Ledger", "_Test Expense Ledger", result)

    # ----------------------------------------------------------------
    # TEST 7: Create a test Payment voucher (with REMOTEID for easy delete)
    # ----------------------------------------------------------------
    print("\n--- Test 7: Create Test Payment Voucher ---")
    # Use an existing cash/bank ledger as credit
    credit_ledger = cash_ledgers[0]["name"] if cash_ledgers else "Cash"
    print(f"Using credit ledger: {credit_ledger}")
    remote_id = "_test_explore_payment_001"
    xml = build_create_payment_with_remoteid_xml(
        date="20260405",
        debit_ledger="_Test Expense Ledger",
        credit_ledger=credit_ledger,
        amount=123.45,
        narration="Test exploration expense - safe to delete",
        company=company,
        remote_id=remote_id,
    )
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")
    log_write("Create", "Voucher", remote_id, {**result, "credit_ledger": credit_ledger})

    # ----------------------------------------------------------------
    # TEST 8: Verify the voucher appears in day book
    # ----------------------------------------------------------------
    print("\n--- Test 8: Verify Voucher in Day Book ---")
    xml = build_day_book("05-04-2026", "05-04-2026", company=company)
    resp = await client.post_xml(xml)
    vouchers = parse_vouchers(resp)
    test_vouchers = [v for v in vouchers if "Test exploration" in (v.get("narration") or "")]
    print(f"Found {len(test_vouchers)} test voucher(s) in day book:")
    for v in test_vouchers:
        print(f"  {v['date']} | #{v['voucher_number']} | {v['narration']}")
        for le in v.get("ledger_entries", []):
            print(f"    {le['ledger_name']}: {le['amount']}")

    # ----------------------------------------------------------------
    # TEST 9: Test error case — create voucher with non-existent ledger
    # ----------------------------------------------------------------
    print("\n--- Test 9: Error Case — Non-existent Ledger ---")
    xml = build_create_payment_xml(
        date="20260405",
        debit_ledger="_Nonexistent Ledger 12345",
        credit_ledger=credit_ledger,
        amount=100.00,
        narration="This should fail",
        company=company,
    )
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")

    # ----------------------------------------------------------------
    # TEST 10: Test duplicate creation — create same group again
    # ----------------------------------------------------------------
    print("\n--- Test 10: Duplicate Creation — Same Group ---")
    xml = build_create_group_xml("_Test Exploration Group", "Indirect Expenses", company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")

    # ================================================================
    # CLEANUP — Delete everything we created (reverse order)
    # ================================================================
    print("\n" + "=" * 70)
    print("CLEANUP — Deleting test entities")
    print("=" * 70)

    # Delete voucher by REMOTEID
    print("\n--- Cleanup 1: Delete Test Voucher ---")
    xml = build_delete_voucher_by_remoteid_xml("Payment", remote_id, company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")

    # Delete test ledger
    print("\n--- Cleanup 2: Delete Test Ledger ---")
    xml = build_delete_ledger_xml("_Test Expense Ledger", company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")

    # Delete test group
    print("\n--- Cleanup 3: Delete Test Group ---")
    xml = build_delete_group_xml("_Test Exploration Group", company)
    resp = await client.post_xml(xml)
    result = parse_response(resp)
    print(f"Response: {json.dumps({k:v for k,v in result.items() if k != 'raw'}, indent=2)}")
    print(f"Raw: {resp}")

    # Verify cleanup
    print("\n--- Cleanup Verification ---")
    xml = build_list_ledgers()
    resp = await client.post_xml(xml)
    ledgers_after = parse_ledger_list(resp)
    test_remaining = [l for l in ledgers_after if l["name"].startswith("_Test")]
    if test_remaining:
        print(f"WARNING: {len(test_remaining)} test entities remain: {[l['name'] for l in test_remaining]}")
    else:
        print("All test entities cleaned up successfully.")

    await client.close()

    # ================================================================
    # WRITES LOG
    # ================================================================
    print("\n" + "=" * 70)
    print("WRITES LOG (for manual undo if needed)")
    print("=" * 70)
    for w in WRITES_LOG:
        print(f"  {w['timestamp']} | {w['action']:8s} | {w['entity_type']:8s} | {w['name']}")

    print("\n" + "=" * 70)
    print(f"EXPLORATION COMPLETE — {datetime.now().isoformat()}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explore Tally write operations")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run_exploration(args.host, args.port))
