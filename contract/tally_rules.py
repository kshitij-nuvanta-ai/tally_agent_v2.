# Copied from: v2/probes/reads.py @ 4aed355
# Changes: PRIMARY_NATURE as-is; RESERVED_VOUCHER_TYPES, the synthetic TB row names, PL_ACCOUNT_LEDGER and
# PRIMARY_PARENT are new (S1 spec §4.4, §10.1, D11, D31).
"""Tally facts the cloud and the agent share (S1 spec §4.4, §10)."""
from __future__ import annotations

PRIMARY_NATURE: dict[str, str] = {
    "Capital Account": "liabilities", "Loans (Liability)": "liabilities", "Current Liabilities": "liabilities",
    "Suspense A/c": "liabilities", "Branch / Divisions": "liabilities",
    "Fixed Assets": "assets", "Investments": "assets", "Current Assets": "assets", "Misc. Expenses (ASSET)": "assets",
    "Sales Accounts": "income", "Direct Incomes": "income", "Indirect Incomes": "income",
    "Purchase Accounts": "expenses", "Direct Expenses": "expenses", "Indirect Expenses": "expenses",
}

RESERVED_VOUCHER_TYPES = frozenset({
    "Attendance", "Contra", "Credit Note", "Debit Note", "Delivery Note", "Job Work In Order", "Job Work Out Order",
    "Journal", "Material In", "Material Out", "Memorandum", "Payment", "Payroll", "Physical Stock", "Purchase",
    "Purchase Order", "Receipt", "Receipt Note", "Rejections In", "Rejections Out", "Reversing Journal", "Sales",
    "Sales Order", "Stock Journal"})   # probe 25 observation `base_types` (probes/results/results.json), 24 values
OPENING_STOCK_ROW = "Opening Stock"
UNADJUSTED_FOREX_ROW = "Unadjusted Forex Gain/Loss"
SYNTHETIC_TB_ROWS = frozenset({OPENING_STOCK_ROW, UNADJUSTED_FOREX_ROW})
PL_ACCOUNT_LEDGER = "Profit & Loss A/c"
PRIMARY_PARENT = "Primary"
