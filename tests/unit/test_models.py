from datetime import date
from tally_bridge.models import (Company, Ledger, TrialBalanceRow, ReportResponse, OutstandingBill, StockItem, AccountGroup)

def test_company_minimal():
    c = Company(name="Bharat Traders Pvt Ltd")
    assert c.name == "Bharat Traders Pvt Ltd"
    assert c.fy_start is None

def test_company_with_fy():
    c = Company(name="Test Co", fy_start=date(2025, 4, 1), fy_end=date(2026, 3, 31))
    assert c.fy_start == date(2025, 4, 1)

def test_ledger_defaults():
    l = Ledger(name="Cash", parent_group="Cash-in-Hand")
    assert l.closing_balance == 0.0
    assert l.opening_balance == 0.0

def test_ledger_with_balances():
    l = Ledger(name="HDFC Bank", parent_group="Bank Accounts", closing_balance=-500000.0, opening_balance=-200000.0)
    assert l.closing_balance == -500000.0

def test_trial_balance_row_defaults():
    r = TrialBalanceRow(account_name="Sales Accounts")
    assert r.debit_amount == 0.0
    assert r.credit_amount == 0.0
    assert r.closing_balance == 0.0

def test_report_response():
    r = ReportResponse(report_name="Trial Balance", company="Bharat Traders", rows=[{"account_name": "Cash", "closing_balance": 50000}])
    assert len(r.rows) == 1
    assert r.from_date is None

def test_outstanding_bill():
    b = OutstandingBill(party_name="Apex Technologies Pvt Ltd", bill_number="S001", bill_date=date(2025, 10, 1), amount=94000.0, pending_amount=40000.0)
    assert b.due_date is None
    assert b.pending_amount == 40000.0

def test_outstanding_bill_has_overdue_days():
    bill = OutstandingBill(
        party_name="Test", bill_number="B001", bill_date=date(2025, 10, 1),
        amount=10000, pending_amount=10000, overdue_days=45,
    )
    assert bill.overdue_days == 45

def test_outstanding_bill_overdue_days_default_none():
    bill = OutstandingBill(
        party_name="Test", bill_number="B001", bill_date=date(2025, 10, 1),
        amount=10000, pending_amount=10000,
    )
    assert bill.overdue_days is None


def test_stock_item_model():
    item = StockItem(
        name="HP Laptop 15s", parent_group="Electronics", base_units="Nos",
        closing_balance=10.0, closing_rate=38000.0, closing_value=380000.0,
    )
    assert item.name == "HP Laptop 15s"
    assert item.parent_group == "Electronics"


def test_account_group_model():
    group = AccountGroup(name="Sales Accounts", parent="Revenue")
    assert group.name == "Sales Accounts"
    assert group.parent == "Revenue"
