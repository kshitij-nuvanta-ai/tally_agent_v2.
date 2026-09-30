"""Tally write exploration v2 — one operation at a time, careful and safe.

Runs each test independently with verification. No REMOTEID tricks.

Usage:
    PYTHONPATH=. python scripts/explore_tally_write_v2.py --host localhost --port 9000
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


def log_write(action: str, entity_type: str, name: str, success: bool):
    WRITES_LOG.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action, "type": entity_type, "name": name, "success": success,
    })


async def post_and_report(client: TallyClient, xml: str, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        resp = await client.post_xml(xml)
        result = parse_response(resp)
        print(f"  Parsed: {json.dumps(result, indent=4)}")
        print(f"  Raw XML: {resp.strip()}")
        return result
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return {"error": str(e)}


async def run(host: str, port: int):
    client = TallyClient(host=host, port=port)
    print(f"Tally Write Exploration v2 — {datetime.now().isoformat()}")
    print(f"Host: {host}:{port} | Company: {COMPANY}")

    # ---- STEP 1: Baseline counts ----
    print("\n\n>>> STEP 1: Baseline counts")
    ledgers = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    groups = parse_groups(await client.post_xml(build_list_groups()))
    payments = parse_vouchers(await client.post_xml(build_day_book("01-04-2025", "05-04-2026", voucher_type="Payment")))
    print(f"  Ledgers: {len(ledgers)} | Groups: {len(groups)} | Payment vouchers: {len(payments)}")
    test_entities = [l["name"] for l in ledgers if l["name"].startswith("_Test")]
    if test_entities:
        print(f"  WARNING: Found leftover test entities: {test_entities}")

    # ---- STEP 2: Create ledger (try different XML formats) ----
    print("\n\n>>> STEP 2: Create ledger — testing XML formats")

    # Format A: ACTION="Create" in attr, simple PARENT child
    xml_a = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Ledger A" ACTION="Create">
<PARENT>Indirect Expenses</PARENT>
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
    result_a = await post_and_report(client, xml_a, "Format A: ACTION=Create, simple PARENT")
    success_a = result_a.get("CREATED") == "1"
    log_write("Create", "Ledger", "_Test Ledger A", success_a)

    if not success_a:
        # Format B: With NAME.LIST + ACTION=Create
        xml_b = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Ledger B" ACTION="Create">
<NAME.LIST><NAME>_Test Ledger B</NAME></NAME.LIST>
<PARENT>Indirect Expenses</PARENT>
<ISBILLWISEON>No</ISBILLWISEON>
<ISCOSTCENTRESON>No</ISCOSTCENTRESON>
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result_b = await post_and_report(client, xml_b, "Format B: ACTION=Create + NAME.LIST")
        success_b = result_b.get("CREATED") == "1"
        log_write("Create", "Ledger", "_Test Ledger B", success_b)

    if not success_a and not success_b:
        # Format C: No ACTION attribute at all
        xml_c = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Ledger C">
<NAME>_Test Ledger C</NAME>
<PARENT>Indirect Expenses</PARENT>
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result_c = await post_and_report(client, xml_c, "Format C: No ACTION, NAME child")
        success_c = result_c.get("CREATED") == "1"
        log_write("Create", "Ledger", "_Test Ledger C", success_c)

    if not success_a and not (not success_a and not success_b):
        pass  # skip D
    elif not success_a and not success_b and not success_c:
        # Format D: REPORTNAME=Ledgers instead of All Masters
        xml_d = f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Ledgers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<LEDGER NAME="_Test Ledger D" ACTION="Create">
<PARENT>Indirect Expenses</PARENT>
</LEDGER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result_d = await post_and_report(client, xml_d, "Format D: REPORTNAME=Ledgers")
        success_d = result_d.get("CREATED") == "1"
        log_write("Create", "Ledger", "_Test Ledger D", success_d)

    # ---- STEP 3: Verify — check if any test ledger was created ----
    print("\n\n>>> STEP 3: Verify ledger creation")
    ledgers_after = parse_ledger_list(await client.post_xml(build_list_ledgers()))
    new_test_ledgers = [l for l in ledgers_after if l["name"].startswith("_Test")]
    if new_test_ledgers:
        print(f"  SUCCESS! Created test ledgers: {[l['name'] for l in new_test_ledgers]}")
        for l in new_test_ledgers:
            print(f"    {l['name']} → parent: {l['parent_group']}")
    else:
        print(f"  FAILED — no test ledgers found. Ledger count: {len(ledgers)} → {len(ledgers_after)}")
        # Check if count changed even without _Test prefix
        if len(ledgers_after) != len(ledgers):
            new_names = set(l["name"] for l in ledgers_after) - set(l["name"] for l in ledgers)
            print(f"  New ledgers (non-test): {new_names}")

    # ---- STEP 4: If ledger creation worked, test voucher creation ----
    working_ledger = new_test_ledgers[0]["name"] if new_test_ledgers else None
    if working_ledger:
        print(f"\n\n>>> STEP 4: Create Payment voucher using '{working_ledger}'")
        credit_ledger = "Cash"
        xml_voucher = f"""<ENVELOPE>
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
<NARRATION>_Test exploration expense — safe to delete</NARRATION>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{working_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-99.99</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{credit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>99.99</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
        result_v = await post_and_report(client, xml_voucher, "Create Payment Voucher")
        success_v = result_v.get("CREATED") == "1"
        log_write("Create", "Voucher", "_Test exploration expense", success_v)

        if success_v:
            vch_id = result_v.get("LASTVCHID", "?")
            print(f"\n  Voucher created! LASTVCHID={vch_id}")

            # Verify in day book
            print("\n>>> STEP 4b: Verify voucher in day book")
            resp = await client.post_xml(build_day_book("05-04-2026", "05-04-2026"))
            vouchers = parse_vouchers(resp)
            test_v = [v for v in vouchers if "_Test" in (v.get("narration") or "")]
            for v in test_v:
                print(f"  Found: #{v['voucher_number']} | {v['date']} | {v['narration']}")
                for le in v.get("ledger_entries", []):
                    print(f"    {le['ledger_name']}: {le['amount']}")
    else:
        print("\n\n>>> STEP 4: SKIPPED — no working ledger to test with")
        print("  Trying voucher with an existing ledger instead...")

        # Use an existing expense ledger
        expense_ledgers = [l for l in ledgers if l["parent_group"] == "Indirect Expenses"]
        if expense_ledgers:
            test_expense = expense_ledgers[0]["name"]
            credit_ledger = "Cash"
            print(f"  Using existing ledger: {test_expense}")
            xml_voucher = f"""<ENVELOPE>
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
<NARRATION>_Test exploration expense — safe to delete</NARRATION>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{test_expense}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-99.99</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{credit_ledger}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>99.99</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""
            result_v = await post_and_report(client, xml_voucher, f"Create Payment Voucher (existing ledger: {test_expense})")
            success_v = result_v.get("CREATED") == "1"
            log_write("Create", "Voucher", f"_Test with {test_expense}", success_v)

            if success_v:
                vch_id = result_v.get("LASTVCHID", "?")
                print(f"\n  Voucher created! LASTVCHID={vch_id}")
                # Verify
                resp = await client.post_xml(build_day_book("05-04-2026", "05-04-2026"))
                vouchers = parse_vouchers(resp)
                test_v = [v for v in vouchers if "_Test" in (v.get("narration") or "")]
                for v in test_v:
                    print(f"  Found: #{v['voucher_number']} | {v['date']} | {v['narration']}")

    # ---- CLEANUP SUMMARY ----
    print("\n\n" + "=" * 60)
    print("WRITES LOG")
    print("=" * 60)
    for w in WRITES_LOG:
        status = "OK" if w["success"] else "FAILED"
        print(f"  {w['time']} | {status:6s} | {w['action']:8s} | {w['type']:8s} | {w['name']}")

    # List anything that needs manual cleanup
    successful_creates = [w for w in WRITES_LOG if w["action"] == "Create" and w["success"]]
    if successful_creates:
        print("\n>>> ENTITIES TO CLEAN UP:")
        for w in successful_creates:
            print(f"  - {w['type']}: {w['name']}")
        print("  Run: PYTHONPATH=. python scripts/cleanup_tally_test.py")
    else:
        print("\n  No successful creates — nothing to clean up.")

    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
