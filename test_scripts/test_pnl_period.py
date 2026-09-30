#!/usr/bin/env python3
"""Diagnostic script: Test P&L period fetching from live Tally.

Fetches cumulative P&L for each month-end in the FY, captures raw XML,
and analyzes BSMAINAMT vs PLSUBAMT field usage and sign patterns.

Usage:
    PYTHONPATH=. python test_scripts/test_pnl_period.py --host <TALLY_IP> --port 9000

Requires: Backend dependencies (httpx, etc.) — run from project root with PYTHONPATH=.
"""

# NOTE: profit_and_loss_period() now raises TallyResponseError for non-full-FY
# date ranges (Phase 14 fix). Tests 3+ that call it for individual months will
# fail with this error. This is expected — the diagnostic findings are preserved
# in test_scripts/logs/pnl_period_debug.log.

import argparse
import asyncio
import json
import logging
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

# Project imports
from tally_bridge.client import TallyClient
from tally_bridge.request_builder import build_profit_and_loss
from tally_bridge.response_parser import (
    parse_profit_and_loss,
    parse_amount,
    sanitize_xml,
    _get_text,
)
from tally_bridge.queries.reports import (
    profit_and_loss,
    profit_and_loss_period,
)
from backend.utils.date_utils import format_for_tally

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("test_pnl_period")


def setup_logging():
    """Configure logging to both console and file."""
    log_file = LOG_DIR / "pnl_period_debug.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return log_file


def parse_pnl_detailed(raw_xml: str) -> list[dict]:
    """Parse P&L XML and capture BOTH BSMAINAMT and PLSUBAMT for each row."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows = []
    children = list(root)
    current_name = None
    for child in children:
        if child.tag == "DSPACCNAME":
            current_name = _get_text(child, "DSPDISPNAME")
        elif child.tag == "PLAMT" and current_name:
            main_amt = parse_amount(_get_text(child, "BSMAINAMT"))
            sub_amt = parse_amount(_get_text(child, "PLSUBAMT"))
            raw_main = _get_text(child, "BSMAINAMT")
            raw_sub = _get_text(child, "PLSUBAMT")
            # Current parser logic
            amount = main_amt if main_amt != 0.0 else sub_amt
            rows.append({
                "account_name": current_name,
                "bsmainamt_raw": raw_main,
                "plsubamt_raw": raw_sub,
                "bsmainamt": main_amt,
                "plsubamt": sub_amt,
                "chosen_amount": amount,
                "chosen_field": "BSMAINAMT" if main_amt != 0.0 else ("PLSUBAMT" if sub_amt != 0.0 else "ZERO"),
            })
            current_name = None
    return rows


async def fetch_raw_pnl(client: TallyClient, from_date: str, to_date: str) -> str:
    """Fetch raw P&L XML from Tally (bypassing parser)."""
    xml_payload = build_profit_and_loss(from_date, to_date)
    return await client.post_xml(xml_payload)


async def run_diagnostics(host: str, port: int):
    """Main diagnostic routine."""
    client = TallyClient(host=host, port=port)

    fy_start = date(2025, 4, 1)
    fy_end = date(2026, 3, 31)

    # =========================================================================
    # TEST 1: Fetch cumulative P&L for each month-end and inspect raw XML
    # =========================================================================
    logger.info("=" * 80)
    logger.info("TEST 1: Cumulative P&L for each month-end (raw XML inspection)")
    logger.info("=" * 80)

    month_ends = []
    current = fy_start
    while current <= fy_end:
        # Last day of current month
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        if month_end > fy_end:
            month_end = fy_end
        month_ends.append(month_end)
        current = next_month

    cumulative_data = {}  # month_end -> {raw_xml, parsed_detailed, parsed_standard}

    for month_end in month_ends:
        from_str = format_for_tally(fy_start)
        to_str = format_for_tally(month_end)
        label = month_end.strftime("%b %Y")

        logger.info(f"\n--- Cumulative P&L: {from_str} to {to_str} ({label}) ---")

        try:
            raw_xml = await fetch_raw_pnl(client, from_str, to_str)
            parsed_detailed = parse_pnl_detailed(raw_xml)
            parsed_standard = parse_profit_and_loss(raw_xml)

            # Save raw XML
            xml_file = LOG_DIR / f"pnl_cumulative_{month_end.strftime('%Y%m')}.xml"
            xml_file.write_text(raw_xml, encoding="utf-8")

            cumulative_data[month_end] = {
                "raw_xml": raw_xml,
                "detailed": parsed_detailed,
                "standard": parsed_standard,
                "xml_size": len(raw_xml.encode("utf-8")),
            }

            logger.info(f"  XML size: {len(raw_xml.encode('utf-8'))} bytes")
            for row in parsed_detailed:
                logger.info(
                    f"  {row['account_name']:30s} | "
                    f"BSMAINAMT={row['bsmainamt']:>15.2f} (raw='{row['bsmainamt_raw']}') | "
                    f"PLSUBAMT={row['plsubamt']:>15.2f} (raw='{row['plsubamt_raw']}') | "
                    f"chosen={row['chosen_amount']:>15.2f} ({row['chosen_field']})"
                )

        except Exception as e:
            logger.error(f"  FAILED: {e}")

    # =========================================================================
    # TEST 2: Compare cumulative values across months for Sales Accounts
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 2: Sales Accounts cumulative progression")
    logger.info("=" * 80)

    logger.info(f"{'Month':>10s} | {'XML Size':>8s} | {'BSMAINAMT':>15s} | {'PLSUBAMT':>15s} | {'Chosen':>15s} | {'Field':>10s}")
    logger.info("-" * 90)

    for month_end in month_ends:
        data = cumulative_data.get(month_end)
        if not data:
            continue
        label = month_end.strftime("%b %Y")
        sales = next((r for r in data["detailed"] if r["account_name"] == "Sales Accounts"), None)
        if sales:
            logger.info(
                f"{label:>10s} | {data['xml_size']:>8d} | "
                f"{sales['bsmainamt']:>15.2f} | {sales['plsubamt']:>15.2f} | "
                f"{sales['chosen_amount']:>15.2f} | {sales['chosen_field']:>10s}"
            )

    # =========================================================================
    # TEST 3: Subtraction approach — show what profit_and_loss_period() computes
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 3: Period P&L via subtraction (profit_and_loss_period)")
    logger.info("=" * 80)

    logger.info(f"{'Month':>10s} | {'Sales Period':>15s} | {'Expected':>15s}")
    logger.info("-" * 50)

    prev_month_end = None
    for month_end in month_ends:
        month_start = date(month_end.year, month_end.month, 1)
        from_str = format_for_tally(month_start)
        to_str = format_for_tally(month_end)
        label = month_end.strftime("%b %Y")

        try:
            result = await profit_and_loss_period(client, from_str, to_str)
            sales = next((r for r in result.rows if r["account_name"] == "Sales Accounts"), None)
            sales_val = sales["closing_balance"] if sales else 0.0

            # Compute expected from cumulative data
            expected = "N/A"
            curr_data = cumulative_data.get(month_end)
            prev_data = cumulative_data.get(prev_month_end) if prev_month_end else None
            if curr_data:
                curr_sales = next((r for r in curr_data["standard"] if r["account_name"] == "Sales Accounts"), None)
                if prev_data:
                    prev_sales = next((r for r in prev_data["standard"] if r["account_name"] == "Sales Accounts"), None)
                    if curr_sales and prev_sales:
                        expected = f"{curr_sales['closing_balance'] - prev_sales['closing_balance']:>15.2f}"
                elif curr_sales:
                    expected = f"{curr_sales['closing_balance']:>15.2f}"

            logger.info(f"{label:>10s} | {sales_val:>15.2f} | {expected:>15s}")

        except Exception as e:
            logger.error(f"{label:>10s} | ERROR: {e}")

        prev_month_end = month_end

    # =========================================================================
    # TEST 4: Alternative parsing — always use abs() and infer sign from account type
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 4: Alternative parsing — combine BSMAINAMT + PLSUBAMT with abs()")
    logger.info("=" * 80)

    # Income accounts should be positive, expense accounts negative
    INCOME_ACCOUNTS = {"Sales Accounts"}
    EXPENSE_ACCOUNTS = {"Cost of Sales :", "Add: Purchase Accounts", "Indirect Expenses"}

    logger.info(f"{'Month':>10s} | {'Sales (current)':>15s} | {'Sales (abs-fix)':>15s} | {'Expenses (current)':>18s} | {'Expenses (abs-fix)':>18s}")
    logger.info("-" * 90)

    prev_cumulative_abs = {}  # account_name -> abs amount
    prev_month_end = None

    for month_end in month_ends:
        data = cumulative_data.get(month_end)
        if not data:
            continue
        label = month_end.strftime("%b %Y")

        # Method: Take whichever field is non-zero, use abs(), then apply sign based on account type
        current_cumulative_abs = {}
        for row in data["detailed"]:
            name = row["account_name"]
            raw_val = row["bsmainamt"] if row["bsmainamt"] != 0.0 else row["plsubamt"]
            abs_val = abs(raw_val)
            if name in EXPENSE_ACCOUNTS:
                current_cumulative_abs[name] = -abs_val  # expenses negative
            else:
                current_cumulative_abs[name] = abs_val  # income positive

        # Compute period values
        current_period_abs = {}
        current_period_standard = {}
        for row in data["standard"]:
            name = row["account_name"]
            current_period_standard[name] = row["closing_balance"]

        for name, cum_abs in current_cumulative_abs.items():
            prev_abs = prev_cumulative_abs.get(name, 0.0)
            current_period_abs[name] = cum_abs - prev_abs

        sales_std = current_period_standard.get("Sales Accounts", 0.0)
        if prev_month_end and prev_month_end in cumulative_data:
            prev_std = next(
                (r["closing_balance"] for r in cumulative_data[prev_month_end]["standard"]
                 if r["account_name"] == "Sales Accounts"),
                0.0,
            )
            sales_std_period = next(
                (r["closing_balance"] for r in data["standard"]
                 if r["account_name"] == "Sales Accounts"),
                0.0,
            ) - prev_std
        else:
            sales_std_period = next(
                (r["closing_balance"] for r in data["standard"]
                 if r["account_name"] == "Sales Accounts"),
                0.0,
            )

        sales_abs_period = current_period_abs.get("Sales Accounts", 0.0)

        # Sum expenses
        exp_std_period = 0.0
        exp_abs_period = 0.0
        for name in EXPENSE_ACCOUNTS:
            if name in current_period_abs:
                exp_abs_period += current_period_abs[name]
            curr_std = next(
                (r["closing_balance"] for r in data["standard"] if r["account_name"] == name),
                0.0,
            )
            if prev_month_end and prev_month_end in cumulative_data:
                prev_std = next(
                    (r["closing_balance"] for r in cumulative_data[prev_month_end]["standard"]
                     if r["account_name"] == name),
                    0.0,
                )
                exp_std_period += curr_std - prev_std
            else:
                exp_std_period += curr_std

        logger.info(
            f"{label:>10s} | {sales_std_period:>15.2f} | {sales_abs_period:>15.2f} | "
            f"{exp_std_period:>18.2f} | {exp_abs_period:>18.2f}"
        )

        prev_cumulative_abs = current_cumulative_abs
        prev_month_end = month_end

    # =========================================================================
    # TEST 5: Direct XML dump for visual comparison — first 3 months
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 5: Raw XML comparison for Apr, May, Jun cumulative P&L")
    logger.info("=" * 80)

    for month_end in month_ends[:3]:
        data = cumulative_data.get(month_end)
        if not data:
            continue
        label = month_end.strftime("%b %Y")
        logger.info(f"\n--- {label} (size={data['xml_size']} bytes) ---")
        logger.debug(f"Raw XML:\n{data['raw_xml']}")
        # Also log to INFO but truncated
        xml_lines = data["raw_xml"].strip().split("\n")
        for line in xml_lines:
            logger.info(f"  XML: {line.rstrip()}")

    # =========================================================================
    # TEST 6: Verify Tally actually ignores SVFROMDATE for P&L
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 6: Does Tally ignore SVFROMDATE? (same SVTODATE, different SVFROMDATE)")
    logger.info("=" * 80)

    to_str = format_for_tally(date(2025, 12, 31))
    for from_dt in [date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)]:
        from_str = format_for_tally(from_dt)
        try:
            raw_xml = await fetch_raw_pnl(client, from_str, to_str)
            parsed = parse_pnl_detailed(raw_xml)
            sales = next((r for r in parsed if r["account_name"] == "Sales Accounts"), None)
            logger.info(
                f"  SVFROMDATE={from_str}, SVTODATE={to_str} -> "
                f"XML size={len(raw_xml.encode('utf-8'))}, "
                f"Sales BSMAINAMT={sales['bsmainamt'] if sales else 'N/A'}, "
                f"PLSUBAMT={sales['plsubamt'] if sales else 'N/A'}, "
                f"chosen={sales['chosen_amount'] if sales else 'N/A'}"
            )
        except Exception as e:
            logger.error(f"  SVFROMDATE={from_str} FAILED: {e}")

    # =========================================================================
    # TEST 7: Fetch same date range twice — is response deterministic?
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 7: Determinism — fetch same date range 3 times")
    logger.info("=" * 80)

    from_str = format_for_tally(fy_start)
    to_str = format_for_tally(date(2025, 9, 30))
    results = []
    for i in range(3):
        raw_xml = await fetch_raw_pnl(client, from_str, to_str)
        parsed = parse_pnl_detailed(raw_xml)
        sales = next((r for r in parsed if r["account_name"] == "Sales Accounts"), None)
        size = len(raw_xml.encode("utf-8"))
        results.append((size, sales["bsmainamt"] if sales else 0, sales["plsubamt"] if sales else 0))
        logger.info(
            f"  Fetch {i+1}: size={size}, BSMAINAMT={sales['bsmainamt'] if sales else 'N/A'}, "
            f"PLSUBAMT={sales['plsubamt'] if sales else 'N/A'}"
        )

    if len(set(results)) == 1:
        logger.info("  ✓ All 3 fetches returned identical results — response is deterministic")
    else:
        logger.warning("  ✗ Responses differ across fetches — NON-DETERMINISTIC!")

    # =========================================================================
    # TEST 8: TYPE=Collection P&L — Ledger ClosingBalance via CHILDOF
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 8: TYPE=Collection — P&L ledgers via CHILDOF (Revenue + Expense)")
    logger.info("=" * 80)

    async def fetch_collection_pnl(
        client: TallyClient,
        group_name: str,
        from_date: str,
        to_date: str,
        company: str | None = None,
    ) -> str:
        """Fetch P&L ledgers via TYPE=Collection with CHILDOF + BELONGSTO."""
        company_var = f"<SVCurrentCompany>{company}</SVCurrentCompany>" if company else ""
        xml_payload = f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>PLLedgers_{group_name.replace(' ', '')}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
{company_var}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="PLLedgers_{group_name.replace(' ', '')}" ISMODIFY="No">
<TYPE>Ledger</TYPE>
<CHILDOF>{group_name}</CHILDOF>
<BELONGSTO>Yes</BELONGSTO>
<NATIVEMETHOD>Name</NATIVEMETHOD>
<NATIVEMETHOD>Parent</NATIVEMETHOD>
<NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""
        return await client.post_xml(xml_payload)

    def parse_collection_ledgers(raw_xml: str) -> list[dict]:
        """Parse TYPE=Collection ledger response."""
        root = ET.fromstring(sanitize_xml(raw_xml))
        ledgers = []
        for ledger in root.iter("LEDGER"):
            name = _get_text(ledger, "NAME") or ledger.get("NAME", "")
            if not name:
                continue
            ledgers.append({
                "name": name,
                "parent": _get_text(ledger, "PARENT"),
                "opening_balance": parse_amount(_get_text(ledger, "OPENINGBALANCE")),
                "closing_balance": parse_amount(_get_text(ledger, "CLOSINGBALANCE")),
            })
        return ledgers

    # 8a: Fetch full-year P&L ledgers via Collection for both Revenue and Expense
    fy_from = format_for_tally(fy_start)
    fy_to = format_for_tally(fy_end)

    for group in ["Revenue", "Expense"]:
        logger.info(f"\n--- {group} group (full year: {fy_from} to {fy_to}) ---")
        try:
            raw = await fetch_collection_pnl(client, group, fy_from, fy_to)
            xml_file = LOG_DIR / f"pnl_collection_{group.lower()}_full_year.xml"
            xml_file.write_text(raw, encoding="utf-8")
            ledgers = parse_collection_ledgers(raw)
            logger.info(f"  XML size: {len(raw.encode('utf-8'))} bytes, {len(ledgers)} ledgers")
            total = 0.0
            for lg in ledgers:
                logger.info(
                    f"  {lg['name']:40s} | parent={lg['parent']:25s} | "
                    f"opening={lg['opening_balance']:>15.2f} | closing={lg['closing_balance']:>15.2f}"
                )
                total += lg["closing_balance"]
            logger.info(f"  {'TOTAL':40s} | {'':25s} | {'':>15s} | {total:>15.2f}")
        except Exception as e:
            logger.error(f"  FAILED: {e}")

    # Also try primary P&L groups if Revenue/Expense don't work
    for group in ["Sales Accounts", "Purchase Accounts", "Indirect Expenses", "Direct Expenses", "Direct Incomes", "Indirect Incomes"]:
        logger.info(f"\n--- {group} (full year) ---")
        try:
            raw = await fetch_collection_pnl(client, group, fy_from, fy_to)
            ledgers = parse_collection_ledgers(raw)
            total = sum(lg["closing_balance"] for lg in ledgers)
            logger.info(f"  {len(ledgers)} ledgers, total closing_balance={total:,.2f}")
            for lg in ledgers:
                logger.info(f"    {lg['name']:40s} | closing={lg['closing_balance']:>15.2f}")
        except Exception as e:
            logger.error(f"  FAILED ({type(e).__name__}): {e}")

    # =========================================================================
    # TEST 9: Collection P&L — cumulative progression per month-end
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 9: Collection P&L — Sales ledger ClosingBalance per month-end")
    logger.info("=" * 80)

    logger.info(f"{'Month':>10s} | {'Collection CB':>15s} | {'TYPE=Data CB':>15s} | {'Match?':>6s}")
    logger.info("-" * 60)

    collection_cumulative = {}  # month_end -> total sales closing balance

    for month_end in month_ends:
        to_str = format_for_tally(month_end)
        label = month_end.strftime("%b %Y")
        try:
            raw = await fetch_collection_pnl(client, "Sales Accounts", fy_from, to_str)
            ledgers = parse_collection_ledgers(raw)
            total_cb = sum(lg["closing_balance"] for lg in ledgers)
            collection_cumulative[month_end] = total_cb

            # Compare with TYPE=Data
            data_cb = 0.0
            td = cumulative_data.get(month_end)
            if td:
                sales_row = next((r for r in td["standard"] if r["account_name"] == "Sales Accounts"), None)
                data_cb = sales_row["closing_balance"] if sales_row else 0.0

            match = "✓" if abs(total_cb - data_cb) < 0.01 else "✗"
            logger.info(f"{label:>10s} | {total_cb:>15.2f} | {data_cb:>15.2f} | {match:>6s}")
        except Exception as e:
            logger.error(f"{label:>10s} | ERROR: {e}")

    # =========================================================================
    # TEST 10: Collection — does SVFROMDATE matter?
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 10: Collection — does SVFROMDATE affect ClosingBalance?")
    logger.info("=" * 80)

    to_str = format_for_tally(date(2025, 12, 31))
    for from_dt in [date(2025, 4, 1), date(2025, 7, 1), date(2025, 10, 1)]:
        from_str = format_for_tally(from_dt)
        try:
            raw = await fetch_collection_pnl(client, "Sales Accounts", from_str, to_str)
            ledgers = parse_collection_ledgers(raw)
            total_cb = sum(lg["closing_balance"] for lg in ledgers)
            logger.info(f"  SVFROMDATE={from_str}, SVTODATE={to_str} -> total CB={total_cb:,.2f} ({len(ledgers)} ledgers)")
        except Exception as e:
            logger.error(f"  SVFROMDATE={from_str} FAILED: {e}")

    # =========================================================================
    # TEST 11: Collection with OpeningBalance — can we derive period values?
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 11: Collection — OpeningBalance + ClosingBalance for period derivation")
    logger.info("=" * 80)

    logger.info("Fetching Sales Accounts ledgers with different SVFROMDATE/SVTODATE pairs:")
    test_ranges = [
        (date(2025, 4, 1), date(2025, 4, 30), "Apr only"),
        (date(2025, 4, 1), date(2025, 5, 31), "Apr-May"),
        (date(2025, 5, 1), date(2025, 5, 31), "May only"),
        (date(2025, 7, 1), date(2025, 9, 30), "Q2 (Jul-Sep)"),
        (date(2025, 10, 1), date(2025, 12, 31), "Q3 (Oct-Dec)"),
        (date(2025, 4, 1), date(2026, 3, 31), "Full year"),
    ]

    for from_dt, to_dt, label in test_ranges:
        from_str = format_for_tally(from_dt)
        to_str = format_for_tally(to_dt)
        try:
            raw = await fetch_collection_pnl(client, "Sales Accounts", from_str, to_str)
            ledgers = parse_collection_ledgers(raw)
            logger.info(f"\n  {label} ({from_str} to {to_str}):")
            for lg in ledgers:
                net = lg["closing_balance"] - lg["opening_balance"]
                logger.info(
                    f"    {lg['name']:35s} | opening={lg['opening_balance']:>12.2f} | "
                    f"closing={lg['closing_balance']:>12.2f} | net(CB-OB)={net:>12.2f}"
                )
            total_ob = sum(lg["opening_balance"] for lg in ledgers)
            total_cb = sum(lg["closing_balance"] for lg in ledgers)
            total_net = total_cb - total_ob
            logger.info(
                f"    {'TOTAL':35s} | opening={total_ob:>12.2f} | "
                f"closing={total_cb:>12.2f} | net(CB-OB)={total_net:>12.2f}"
            )
        except Exception as e:
            logger.error(f"  {label} FAILED: {e}")

    # =========================================================================
    # TEST 12: Group ClosingBalance — query Group object directly
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 12: Group ClosingBalance via TYPE=Collection on Group objects")
    logger.info("=" * 80)

    async def fetch_group_balance(
        client: TallyClient,
        parent_group: str,
        from_date: str,
        to_date: str,
    ) -> str:
        """Fetch Group objects with ClosingBalance."""
        xml_payload = f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>PLGroups</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="PLGroups" ISMODIFY="No">
<TYPE>Group</TYPE>
<CHILDOF>{parent_group}</CHILDOF>
<BELONGSTO>Yes</BELONGSTO>
<NATIVEMETHOD>Name</NATIVEMETHOD>
<NATIVEMETHOD>Parent</NATIVEMETHOD>
<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
<NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""
        return await client.post_xml(xml_payload)

    def parse_collection_groups(raw_xml: str) -> list[dict]:
        """Parse Group collection response."""
        root = ET.fromstring(sanitize_xml(raw_xml))
        groups = []
        for group in root.iter("GROUP"):
            name = _get_text(group, "NAME") or group.get("NAME", "")
            if not name:
                continue
            groups.append({
                "name": name,
                "parent": _get_text(group, "PARENT"),
                "opening_balance": parse_amount(_get_text(group, "OPENINGBALANCE")),
                "closing_balance": parse_amount(_get_text(group, "CLOSINGBALANCE")),
            })
        return groups

    # Test Group balances for key months (Jul onwards where sales exist)
    test_months = [
        date(2025, 7, 31),   # Jul — first sales month
        date(2025, 9, 30),   # Sep — Q2 end
        date(2025, 12, 31),  # Dec — Q3 end
        date(2026, 3, 31),   # Mar — FY end
    ]

    for parent in ["Primary", "Revenue", "Expense"]:
        logger.info(f"\n--- Group: {parent} ---")
        logger.info(f"{'Month':>10s} | {'Group Name':>30s} | {'OpeningBal':>12s} | {'ClosingBal':>12s}")
        logger.info("-" * 75)
        for month_end in test_months:
            from_str = format_for_tally(fy_start)
            to_str = format_for_tally(month_end)
            label = month_end.strftime("%b %Y")
            try:
                raw = await fetch_group_balance(client, parent, from_str, to_str)
                groups = parse_collection_groups(raw)
                if not groups:
                    logger.info(f"{label:>10s} | (no groups returned)")
                for g in groups:
                    logger.info(
                        f"{label:>10s} | {g['name']:>30s} | {g['opening_balance']:>12.2f} | {g['closing_balance']:>12.2f}"
                    )
            except Exception as e:
                logger.error(f"{label:>10s} | ERROR: {e}")

    # =========================================================================
    # TEST 13: Ledger with LOCALFORMULA — compute balance from transactions
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 13: Ledger with LOCALFORMULA for transaction-based balance")
    logger.info("=" * 80)

    async def fetch_ledger_with_formula(
        client: TallyClient,
        group_name: str,
        from_date: str,
        to_date: str,
    ) -> str:
        """Fetch Ledger objects with LOCALFORMULA for net transaction amount."""
        xml_payload = f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>PLLedgerFormula</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="PLLedgerFormula" ISMODIFY="No">
<TYPE>Ledger</TYPE>
<CHILDOF>{group_name}</CHILDOF>
<BELONGSTO>Yes</BELONGSTO>
<NATIVEMETHOD>Name</NATIVEMETHOD>
<NATIVEMETHOD>Parent</NATIVEMETHOD>
<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>
<NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>
</COLLECTION>
<OBJECT NAME="Ledger" ISINITIALIZE="Yes">
<LOCALFORMULA>TxnBalance : $$AsAmount:$$ClosingBalance:Ledger:$Name</LOCALFORMULA>
</OBJECT>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""
        return await client.post_xml(xml_payload)

    for month_end in test_months:
        from_str = format_for_tally(fy_start)
        to_str = format_for_tally(month_end)
        label = month_end.strftime("%b %Y")
        logger.info(f"\n--- {label} ({from_str} to {to_str}) ---")
        try:
            raw = await fetch_ledger_with_formula(client, "Sales Accounts", from_str, to_str)
            xml_file = LOG_DIR / f"pnl_formula_{month_end.strftime('%Y%m')}.xml"
            xml_file.write_text(raw, encoding="utf-8")
            # Log raw XML for inspection
            logger.info(f"  XML size: {len(raw.encode('utf-8'))} bytes")
            # Parse — check for any non-zero values
            ledgers = parse_collection_ledgers(raw)
            for lg in ledgers:
                logger.info(
                    f"  {lg['name']:35s} | opening={lg['opening_balance']:>12.2f} | closing={lg['closing_balance']:>12.2f}"
                )
            # Also dump raw XML if small
            if len(raw) < 3000:
                for line in raw.strip().split("\n"):
                    logger.info(f"  RAW: {line.rstrip()}")
        except Exception as e:
            logger.error(f"  FAILED: {e}")

    # =========================================================================
    # TEST 14: Voucher-based P&L — sum Sales voucher amounts by month
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 14: Voucher-based P&L — sum Sales invoices per month")
    logger.info("=" * 80)

    from tally_bridge.request_builder import build_sales_register
    from tally_bridge.response_parser import parse_vouchers

    # Fetch full-year sales register
    try:
        raw = await client.post_xml(build_sales_register(fy_from, fy_to))
        xml_file = LOG_DIR / "sales_register_full_year.xml"
        xml_file.write_text(raw, encoding="utf-8")
        vouchers = parse_vouchers(raw, fy_from, fy_to)
        logger.info(f"  Full-year sales register: {len(vouchers)} vouchers, XML size={len(raw.encode('utf-8'))}")

        # Group by month and sum amounts
        from collections import defaultdict
        monthly_sales: dict[str, float] = defaultdict(float)
        for v in vouchers:
            month = v.get("month", "Unknown")
            # Sum ledger entries for sales amounts (look for positive = sales income)
            for entry in v.get("ledger_entries", []):
                ledger = entry.get("ledger_name", "")
                amt = entry.get("amount", 0.0)
                # Sales ledger entries are typically negative in Tally (credit)
                if "SALES" in ledger.upper() or "sales" in ledger.lower():
                    monthly_sales[month] += amt

        logger.info(f"\n  Monthly Sales from Vouchers (ledger entries with 'SALES'):")
        logger.info(f"  {'Month':>10s} | {'Voucher Sales':>15s}")
        logger.info("  " + "-" * 30)
        cumulative = 0.0
        for month_end in month_ends:
            label = month_end.strftime("%b %Y")
            val = monthly_sales.get(label, 0.0)
            cumulative += val
            logger.info(f"  {label:>10s} | {val:>15.2f} (cumulative: {cumulative:>15.2f})")

        # Also show all ledger names in vouchers for reference
        all_ledger_names = set()
        for v in vouchers:
            for entry in v.get("ledger_entries", []):
                all_ledger_names.add(entry.get("ledger_name", ""))
        logger.info(f"\n  Unique ledger names in sales vouchers: {sorted(all_ledger_names)}")

    except Exception as e:
        logger.error(f"  FAILED: {e}")

    # =========================================================================
    # TEST 15: Compare TYPE=Data P&L with Tally desktop expected values
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 15: TYPE=Data P&L — is it REALLY cumulative from FY start?")
    logger.info("=" * 80)

    logger.info("If cumulative, values should be monotonically non-decreasing.")
    logger.info("Apr-Jun have no sales, so cumulative to Jun should be 0.\n")

    prev_val = None
    monotonic = True
    for month_end in month_ends:
        data = cumulative_data.get(month_end)
        if not data:
            continue
        label = month_end.strftime("%b %Y")
        sales = next((r for r in data["standard"] if r["account_name"] == "Sales Accounts"), None)
        val = sales["closing_balance"] if sales else 0.0
        flag = ""
        if prev_val is not None:
            if val < prev_val:
                flag = " ← DECREASED (not cumulative!)"
                monotonic = False
            elif val > prev_val:
                flag = " ← increased"
        prev_val = val
        logger.info(f"  Cumulative to {label:>10s}: Sales = {val:>15.2f}{flag}")

    if monotonic:
        logger.info("\n  ✓ Values are monotonically non-decreasing — consistent with cumulative")
    else:
        logger.warning("\n  ✗ Values DECREASE between months — NOT truly cumulative!")
        logger.warning("  This means TYPE=Data P&L does NOT reliably return cumulative from FY start.")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 80)

    logger.info("\nTYPE=Data P&L cumulative Sales progression:")
    for month_end in month_ends:
        data = cumulative_data.get(month_end)
        if data:
            label = month_end.strftime("%b %Y")
            sales = next((r for r in data["standard"] if r["account_name"] == "Sales Accounts"), None)
            val = sales["closing_balance"] if sales else 0.0
            logger.info(f"  {label:>10s}: {val:>15.2f}")

    logger.info(f"\nDebug logs saved to: {LOG_DIR}")
    logger.info(f"Raw XML files saved to: {LOG_DIR}/pnl_cumulative_*.xml")


def main():
    parser = argparse.ArgumentParser(description="Test P&L period fetching from live Tally")
    parser.add_argument("--host", default="localhost", help="Tally host (default: localhost)")
    parser.add_argument("--port", type=int, default=9000, help="Tally port (default: 9000)")
    args = parser.parse_args()

    log_file = setup_logging()
    logger.info(f"Starting P&L period diagnostics against {args.host}:{args.port}")
    logger.info(f"Logs will be saved to: {log_file}")

    asyncio.run(run_diagnostics(args.host, args.port))


if __name__ == "__main__":
    main()
