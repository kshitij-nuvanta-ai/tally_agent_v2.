#!/usr/bin/env python3
"""Tier-3 verification: hit every read query against the seeded company.

Asserts shape + counts of:
  - companies, ledgers, groups, stock items
  - trial balance rows
  - day book / sales register / purchase register voucher counts
  - bills receivable / payable totals

Usage:
    PYTHONPATH=. python scripts/verify_tally_bridge_live.py \\
        --host localhost --port 9000
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tally_bridge.client import TallyClient
from tally_bridge.queries import masters, reports, vouchers


async def run(host: str, port: int, company: str) -> int:
    client = TallyClient(host=host, port=port)
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "OK" if condition else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {name}{suffix}")
        if not condition:
            failures.append(name)

    print(f"Verifying {company} @ {host}:{port}\n")

    # 1. Companies
    companies = await masters.list_companies(client)
    company_names = [c.name for c in companies]
    check("list_companies includes target", company in company_names,
          f"got {company_names}")

    # 2. Ledgers (33 seeded + 2 default = 35)
    ledger_list = await masters.list_ledgers(client)
    check("list_ledgers >= 35", len(ledger_list) >= 35, f"got {len(ledger_list)}")
    names = {l.name for l in ledger_list}
    check("Capital Account present", "Capital Account" in names)
    check("Apex Technologies Pvt Ltd present", "Apex Technologies Pvt Ltd" in names)
    check("CGST Output present", "CGST Output" in names)

    # 3. Groups (4 seeded + Tally defaults; expect >= 30)
    groups = await masters.list_groups(client)
    check("list_groups >= 30", len(groups) >= 30, f"got {len(groups)}")
    group_names = {g.name for g in groups}
    check("North Zone Debtors present", "North Zone Debtors" in group_names)

    # 4. Stock items (15)
    stock_items = await masters.list_stock_items(client)
    check("list_stock_items == 15", len(stock_items) == 15, f"got {len(stock_items)}")

    # 5. Trial balance (FY 2025-26)
    tb = await reports.trial_balance(client, "01-04-2025", "31-03-2026", company=company)
    check("trial_balance non-empty rows", len(tb.rows) > 0, f"got {len(tb.rows)} rows")

    # 6. Day book over the seed window (full FY just to be safe)
    db = await vouchers.day_book(client, "01-04-2025", "31-03-2026", company=company)
    check("day_book >= 50 vouchers", len(db) >= 50, f"got {len(db)}")

    # 7. Sales register (16 invoices) and Purchase register (8 invoices)
    sr = await vouchers.sales_register(client, "01-04-2025", "31-03-2026", company=company)
    check("sales_register >= 16", len(sr) >= 16, f"got {len(sr)}")
    pr = await vouchers.purchase_register(client, "01-04-2025", "31-03-2026", company=company)
    check("purchase_register >= 8", len(pr) >= 8, f"got {len(pr)}")

    # 8. Bills receivable / payable.
    # Seed has 6 outstanding receivables and 5 outstanding payables
    # (see scripts/seed_data/bharat_traders.py:EXPECTED_RECEIVABLES/PAYABLES).
    # Tight counts catch silent voucher drops that don't affect the master tables.
    br = await reports.bills_receivable(client, "31-03-2026", company=company)
    check("bills_receivable >= 6", len(br) >= 6, f"got {len(br)} bills")
    bp = await reports.bills_payable(client, "31-03-2026", company=company)
    check("bills_payable >= 5", len(bp) >= 5, f"got {len(bp)} bills")

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed: {failures}")
        return 1
    print("All verification checks passed.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--company", default="Bharat Traders Private Limited")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.host, args.port, args.company)))


if __name__ == "__main__":
    main()
