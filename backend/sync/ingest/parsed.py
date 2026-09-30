"""Ingest-time parsed object shapes (S1 spec §5.3, §12 step 5). Frozen dataclasses only -- no DB, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from contract.parse import Amount


@dataclass(frozen=True)
class PBill:
    name: str
    bill_type: str | None
    amount: Amount | None
    credit_days: int | None
    credit_text: str
    bill_date: date | None


@dataclass(frozen=True)
class PLine:
    line_no: int
    ledger_name: str
    amount: Amount
    is_deemed_positive: bool
    ledger_guid_hint: str | None
    bills: list[PBill]


@dataclass(frozen=True)
class PInv:
    line_no: int
    stock_item_name: str
    amount: Amount
    actual_qty: Decimal | None
    billed_qty: Decimal | None
    qty_text: str
    rate: Decimal | None
    rate_text: str
    is_deemed_positive: bool | None


@dataclass(frozen=True)
class PVoucher:
    index: int
    guid: str
    master_id: str
    alter_id: int
    date: date
    effective_date: date | None
    voucher_type_name: str
    voucher_number: str
    reference: str
    party_ledger_name: str
    narration: str
    is_cancelled: bool
    is_optional: bool
    is_post_dated: bool
    is_invoice: bool | None
    lines: list[PLine]
    inventory: list[PInv]
    has_forex: bool
    raw: dict


@dataclass(frozen=True)
class PMaster:
    index: int
    kind: str
    guid: str
    alter_id: int
    name: str
    parent: str | None
    fields: dict[str, Any]
    raw: dict


@dataclass(frozen=True)
class PBalance:
    index: int
    kind: str
    guid: str
    name: str
    captured_at: datetime
    closing: Amount | None
    opening: Amount | None
    qty: Decimal | None
    qty_text: str
    raw: dict


@dataclass(frozen=True)
class ObjectError:
    index: int
    kind: str
    guid: str | None
    code: str
    detail: str


@dataclass(frozen=True)
class ObjectWarning:
    index: int
    code: str
