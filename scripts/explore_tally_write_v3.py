"""Tally write exploration v3 — Sales, Purchase, Group, Cancel/Delete.

Tests remaining write paths not covered in v2.
Prerequisite: Tally running on localhost:9000, _Test Ledger B cleaned up.

Usage:
    PYTHONPATH=. python scripts/explore_tally_write_v3.py --host localhost --port 9000
"""
import argparse
import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime

from tally_bridge.client import TallyClient
from tally_bridge.request_builder import build_list_ledgers, build_list_groups, build_day_book
from tally_bridge.response_parser import parse_ledger_list, parse_groups, parse_vouchers, sanitize_xml

COMPANY = "NUVANTA AI TECHNOLOGIES PRIVATE LIMITED"
WRITES_LOG: list[dict] = []


def parse_response(xml_text: str) -> dict:
    try:
        root = ET.fromstring(sanitize_xml(xml_text))
    except ET.ParseError:
        return {"raw": xml_text, "parse_error": True}
    result = {}
    for tag in ["CREATED", "ALTERED", "DELETED", "ERRORS", "LASTVCHID", "LASTMID",
                "COMBINED", "IGNORED", "LINEERROR", "CANCELLED", "EXCEPTIONS"]:
        el = root.find(tag) if root.find(tag) is not None else root.find(f".//{tag}")
        if el is not None and el.text:
            result[tag] = el.text.strip()
    return result


def log_write(action: str, entity_type: str, name: str, success: bool, details: str = ""):
    WRITES_LOG.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action, "type": entity_type, "name": name,
        "success": success, "details": details,
    })


async def post_and_report(client: TallyClient, xml: str, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        resp = await client.post_xml(xml)
        result = parse_response(resp)
        print(f"  Parsed: {json.dumps(result, indent=4)}")
        print(f"  Raw: {resp.strip()[:300]}")
        return result
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return {"error": str(e)}


async def run(host: str, port: int):
    client = TallyClient(host=host, port=port)
    print(f"Tally Write Exploration v3 — {datetime.now().isoformat()}")
    print(f"Testing: Group, Sales, Purchase, Cancel, Delete")

    # ---- Baseline ----
    print("\n>>> Baseline counts")
    ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    groups = parse_groups(await client.post_xml(build_list_groups()))
    print(f"  Ledgers: {len(ledgers)} | Groups: {len(groups)}")

    # Check for leftover test entities
    test_l = [l["name"] for l in ledgers if l["name"].startswith("_Test")]
    test_g = [g["name"] for g in groups if g["name"].startswith("_Test")]
    if test_l or test_g:
        print(f"  WARNING: Leftover test entities — ledgers: {test_l}, groups: {test_g}")
        print(f"  Please clean up manually before running this script.")
        await client.close()
        return

    # ============================================================
    # TEST 1: Create Group (with NAME.LIST — our working format)
    # ============================================================
    xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP NAME="_Test Explore Group" ACTION="Create">
<NAME.LIST><NAME>_Test Explore Group</NAME></NAME.LIST>
<PARENT>Indirect Expenses</PARENT>
</GROUP>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result = await post_and_report(client, xml, "TEST 1: Create Group (NAME.LIST format)")
    group_ok = result.get("CREATED") == "1"
    log_write("Create", "Group", "_Test Explore Group", group_ok)

    # Also try official doc format (without NAME.LIST) for comparison
    xml_alt = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP Action="Create">
<NAME>_Test Explore Group Alt</NAME>
<PARENT>Indirect Expenses</PARENT>
</GROUP>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result_alt = await post_and_report(client, xml_alt, "TEST 1b: Create Group (official doc format — no NAME.LIST)")
    group_alt_ok = result_alt.get("CREATED") == "1"
    log_write("Create", "Group", "_Test Explore Group Alt", group_alt_ok, "official doc format")

    # ============================================================
    # TEST 2: Create ledger under the test group
    # ============================================================
    parent_group = "_Test Explore Group" if group_ok else "Indirect Expenses"
    xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Explore Ledger" ACTION="Create">
<NAME.LIST><NAME>_Test Explore Ledger</NAME></NAME.LIST>
<PARENT>{parent_group}</PARENT>
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result = await post_and_report(client, xml, "TEST 2: Create Ledger")
    ledger_ok = result.get("CREATED") == "1"
    log_write("Create", "Ledger", "_Test Explore Ledger", ledger_ok)

    if not ledger_ok:
        print("\n  ABORT: Cannot continue without a working ledger.")
        await client.close()
        return

    # ============================================================
    # TEST 3: Create Payment voucher (same as v2, confirming)
    # ============================================================
    xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>20260405</DATE>
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>_Test Payment — exploration v3</NARRATION>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>_Test Explore Ledger</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-150.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>Cash</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>150.00</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result = await post_and_report(client, xml, "TEST 3: Create Payment Voucher")
    payment_ok = result.get("CREATED") == "1"
    payment_vch_id = result.get("LASTVCHID")
    log_write("Create", "Payment", f"_Test Payment (LASTVCHID={payment_vch_id})", payment_ok)

    # ============================================================
    # TEST 4: Create Sales voucher (B1c preview)
    # ============================================================
    # First check for a Sales ledger
    sales_ledgers = [l for l in ledgers if l["parent_group"] == "Sales Accounts"]
    sales_ledger = sales_ledgers[0]["name"] if sales_ledgers else "Sales"

    # Check for Sundry Debtors
    debtor_ledgers = [l for l in ledgers if l["parent_group"] == "Sundry Debtors"]
    debtor_ledger = debtor_ledgers[0]["name"] if debtor_ledgers else None

    if debtor_ledger:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="Sales" ACTION="Create">
<DATE>20260405</DATE>
<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
<NARRATION>_Test Sales — exploration v3</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{debtor_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>-1180.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{sales_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>1000.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>OUTPUT CGST</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>90.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>OUTPUTSGST</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>90.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 4: Create Sales Voucher (party={debtor_ledger})")
        sales_ok = result.get("CREATED") == "1"
        sales_vch_id = result.get("LASTVCHID")
        log_write("Create", "Sales", f"_Test Sales (LASTVCHID={sales_vch_id})", sales_ok)
    else:
        print("\n  SKIPPED: No Sundry Debtor ledger found for Sales test")
        sales_ok = False
        sales_vch_id = None

    # ============================================================
    # TEST 5: Create Purchase voucher (B1b preview)
    # ============================================================
    creditor_ledgers = [l for l in ledgers if l["parent_group"] in ("Sundry Creditors", "Professional Creditors", "FOREIGN  IMPORTS")]
    creditor_ledger = creditor_ledgers[0]["name"] if creditor_ledgers else None
    purchase_ledgers = [l for l in ledgers if l["parent_group"] == "Purchase Accounts"]
    purchase_ledger = purchase_ledgers[0]["name"] if purchase_ledgers else "Purchase"

    if creditor_ledger:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>20260405</DATE>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<NARRATION>_Test Purchase — exploration v3</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{creditor_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>2360.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>{purchase_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-2000.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>INPUT CGST</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-180.00</AMOUNT>
</LEDGERENTRIES.LIST>
<LEDGERENTRIES.LIST>
<LEDGERNAME>INPUT SGST</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-180.00</AMOUNT>
</LEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 5: Create Purchase Voucher (party={creditor_ledger})")
        purchase_ok = result.get("CREATED") == "1"
        purchase_vch_id = result.get("LASTVCHID")
        log_write("Create", "Purchase", f"_Test Purchase (LASTVCHID={purchase_vch_id})", purchase_ok)
    else:
        print("\n  SKIPPED: No creditor ledger found for Purchase test")
        purchase_ok = False
        purchase_vch_id = None

    # ============================================================
    # TEST 6: Cancel Payment voucher (correct TAGNAME/TAGVALUE format)
    # ============================================================
    if payment_ok and payment_vch_id:
        # Try cancelling by Master ID (LASTVCHID)
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER DATE="20260405" TAGNAME="Master ID" TAGVALUE="{payment_vch_id}"
         VCHTYPE="Payment" ACTION="Cancel">
<NARRATION>_Test Cancel — exploration v3</NARRATION>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 6: Cancel Payment (Master ID={payment_vch_id})")
        cancel_ok = result.get("CANCELLED") == "1" or result.get("ALTERED") == "1"
        log_write("Cancel", "Payment", f"LASTVCHID={payment_vch_id}", cancel_ok)

    # ============================================================
    # TEST 7: Delete Sales voucher (correct TAGNAME/TAGVALUE format)
    # ============================================================
    if sales_ok and sales_vch_id:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER DATE="20260405" TAGNAME="Master ID" TAGVALUE="{sales_vch_id}"
         VCHTYPE="Sales" ACTION="Delete">
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 7: Delete Sales (Master ID={sales_vch_id})")
        delete_ok = result.get("DELETED") == "1"
        log_write("Delete", "Sales", f"LASTVCHID={sales_vch_id}", delete_ok)

    # ============================================================
    # TEST 8: Delete Purchase voucher
    # ============================================================
    if purchase_ok and purchase_vch_id:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER DATE="20260405" TAGNAME="Master ID" TAGVALUE="{purchase_vch_id}"
         VCHTYPE="Purchase" ACTION="Delete">
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 8: Delete Purchase (Master ID={purchase_vch_id})")
        delete_purchase_ok = result.get("DELETED") == "1"
        log_write("Delete", "Purchase", f"LASTVCHID={purchase_vch_id}", delete_purchase_ok)

    # ============================================================
    # TEST 9: Delete the cancelled Payment voucher
    # ============================================================
    if payment_ok and payment_vch_id:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER DATE="20260405" TAGNAME="Master ID" TAGVALUE="{payment_vch_id}"
         VCHTYPE="Payment" ACTION="Delete">
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"TEST 9: Delete cancelled Payment (Master ID={payment_vch_id})")
        delete_cancelled_ok = result.get("DELETED") == "1"
        log_write("Delete", "Payment", f"cancelled, LASTVCHID={payment_vch_id}", delete_cancelled_ok)

    # ============================================================
    # CLEANUP: Delete test ledger and groups
    # ============================================================
    print("\n\n>>> CLEANUP")

    # Delete test ledger
    xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Explore Ledger" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result = await post_and_report(client, xml, "CLEANUP: Delete test ledger")
    log_write("Delete", "Ledger", "_Test Explore Ledger", result.get("DELETED") == "1")

    # Delete test groups
    for gname in ["_Test Explore Group", "_Test Explore Group Alt"]:
        xml = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<GROUP NAME="{gname}" ACTION="Delete"/>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result = await post_and_report(client, xml, f"CLEANUP: Delete group '{gname}'")
        log_write("Delete", "Group", gname, result.get("DELETED") == "1")

    # ============================================================
    # Verify
    # ============================================================
    print("\n\n>>> FINAL VERIFICATION")
    ledgers_after = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    groups_after = parse_groups(await client.post_xml(build_list_groups()))
    remaining_l = [l["name"] for l in ledgers_after if l["name"].startswith("_Test")]
    remaining_g = [g["name"] for g in groups_after if g["name"].startswith("_Test")]

    vouchers_today = parse_vouchers(await client.post_xml(build_day_book("05-04-2026", "05-04-2026")))
    remaining_v = [v for v in vouchers_today if "_Test" in (v.get("narration") or "")]

    if remaining_l or remaining_g or remaining_v:
        print(f"  WARNING: Remaining test entities:")
        if remaining_l: print(f"    Ledgers: {remaining_l}")
        if remaining_g: print(f"    Groups: {remaining_g}")
        if remaining_v: print(f"    Vouchers: {[v['narration'][:40] for v in remaining_v]}")
    else:
        print(f"  All clean! Ledgers: {len(ledgers_after)}, Groups: {len(groups_after)}")

    # ============================================================
    # Summary
    # ============================================================
    print("\n\n" + "=" * 60)
    print("WRITES LOG")
    print("=" * 60)
    for w in WRITES_LOG:
        status = "OK" if w["success"] else "FAILED"
        detail = f" ({w['details']})" if w["details"] else ""
        print(f"  {w['time']} | {status:6s} | {w['action']:8s} | {w['type']:10s} | {w['name']}{detail}")

    await client.close()
    print(f"\nDone — {datetime.now().isoformat()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
