from datetime import date
from pydantic import BaseModel


class Company(BaseModel):
    name: str
    fy_start: date | None = None
    fy_end: date | None = None


class Ledger(BaseModel):
    name: str
    parent_group: str
    closing_balance: float = 0.0
    opening_balance: float = 0.0


class TrialBalanceRow(BaseModel):
    account_name: str
    debit_amount: float = 0.0
    credit_amount: float = 0.0
    closing_balance: float = 0.0


class VoucherEntry(BaseModel):
    date: date
    voucher_type: str
    voucher_number: str
    party_name: str | None = None
    ledger_name: str
    amount: float
    narration: str | None = None


class ReportResponse(BaseModel):
    report_name: str
    company: str
    from_date: date | None = None
    to_date: date | None = None
    rows: list[dict]
    raw_response: dict | None = None


class OutstandingBill(BaseModel):
    party_name: str
    bill_number: str
    bill_date: date
    due_date: date | None = None
    amount: float
    pending_amount: float
    overdue_days: int | None = None


class StockItem(BaseModel):
    name: str
    parent_group: str = ""
    base_units: str = ""
    closing_balance: float = 0.0
    closing_rate: float = 0.0
    closing_value: float = 0.0


class AccountGroup(BaseModel):
    name: str
    parent: str = ""
