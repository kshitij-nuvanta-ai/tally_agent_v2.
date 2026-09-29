"""Probe companies (S0 spec §4). One company is open in Tally at a time."""

COMPANIES: dict[str, str] = {
    "A": "Bharat Traders Probe Copy",
    "B": "Sharma & Sons' Probe Traders",
    "C": "Probe Vault Co",
}
SEED_COMPANY = "Bharat Traders Private Limited"
SEED_BACKUP = "seed_data/TDBK1800_100003.001"

# Throwaway objects on company A (S0 spec §4.2, §5.8). Educational mode accepts voucher dates on the 1st, 2nd or 31st
# of a month only (live 2026-09-22), so every throwaway voucher is dated 31-Mar-2026, inside A's books period.
THROWAWAY_DATE = "20260331"
THROWAWAY_DATE_TEXT = "31-Mar-2026"
THROWAWAY_EXPENSE_LEDGER = "Electricity"   # company A has no "Bank Charges"; this is its first Indirect Expenses ledger
POPUP_STOCK_GROUP = "Electronics"    # exists in the seed company; a duplicate create raises the blocking modal (probe 10)

# Company C (S0 spec §4.4; plan part 6). The company shell, its security and its TallyVault are made by hand in the
# UI; `setup-c` writes exactly this one ledger and one voucher. Books begin 1-Apr-2025 so the voucher's date is the
# books-start day: never after F2 (LESSONS §15 rule 14) and a day Educational TallyPrime accepts (1/2/31, C43).
COMPANY_C_BOOKS_FROM = "01-04-2025"
COMPANY_C_BOOKS_TO = "31-03-2026"
COMPANY_C_LEDGER = "S0 Vault Expense"
COMPANY_C_LEDGER_PARENT = "Indirect Expenses"
COMPANY_C_VOUCHER_DATE = "20250401"
COMPANY_C_VOUCHER_NARRATION = "[S0-C:1] Probe Vault voucher"
COMPANY_C_VOUCHER_AMOUNT = "100.00"
