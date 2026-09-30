"""Voucher tables (spec §4.5): ``tally_vouchers``, ``tally_voucher_ledger_lines``,
``tally_voucher_inventory_lines``, ``tally_bill_allocations``.

Lines, inventory lines and bill allocations have no Tally identity: they are deleted and re-inserted with their
voucher in one transaction (D18/D19), so — unlike masters — they carry no ``guid``/``alter_id``/``raw``/
``first_seen_at``; their FK to ``tally_vouchers.id`` is ``ON DELETE CASCADE``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MONEY = Numeric(18, 2)
FACE = Numeric(18, 4)
RATE = Numeric(18, 6)
QTY = Numeric(18, 4)


class TallyVoucher(Base):
    __tablename__ = "tally_vouchers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    master_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    alter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date)
    voucher_type_name: Mapped[str] = mapped_column(Text, nullable=False)
    voucher_type_guid: Mapped[str | None] = mapped_column(Text)
    base_type: Mapped[str | None] = mapped_column(Text)
    voucher_number: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    party_ledger_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    party_ledger_guid: Mapped[str | None] = mapped_column(Text)
    narration: Mapped[str | None] = mapped_column(Text)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_post_dated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_invoice: Mapped[bool | None] = mapped_column(Boolean)
    has_forex: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_runs.id"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("uq_tally_vouchers_ws_guid", "workspace_id", "guid", unique=True),
        Index("ix_tally_vouchers_ws_date", "workspace_id", "date"),
        Index("ix_tally_vouchers_ws_party_date", "workspace_id", "party_ledger_guid", "date"),
        Index("ix_tally_vouchers_ws_alter", "workspace_id", "alter_id"),
    )


class TallyVoucherLedgerLine(Base):
    __tablename__ = "tally_voucher_ledger_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tally_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_name: Mapped[str] = mapped_column(Text, nullable=False)
    ledger_guid: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Numeric] = mapped_column(MONEY, nullable=False)
    is_deemed_positive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fx_currency: Mapped[str | None] = mapped_column(Text)
    fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    fx_rate: Mapped[Numeric | None] = mapped_column(RATE)
    voucher_date: Mapped[date] = mapped_column(Date, nullable=False)
    countable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_tally_voucher_ledger_lines_voucher", "voucher_id"),
        Index("ix_tally_voucher_ledger_lines_ws", "workspace_id"),
        Index(
            "ix_lines_cover",
            "workspace_id",
            "ledger_guid",
            "voucher_date",
            postgresql_include=["amount", "fx_amount"],
            postgresql_where=text("countable"),
        ),
    )


class TallyVoucherInventoryLine(Base):
    __tablename__ = "tally_voucher_inventory_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tally_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_item_name: Mapped[str] = mapped_column(Text, nullable=False)
    stock_item_guid: Mapped[str] = mapped_column(Text, nullable=False)
    actual_qty: Mapped[Numeric | None] = mapped_column(QTY)
    billed_qty: Mapped[Numeric | None] = mapped_column(QTY)
    qty_text: Mapped[str | None] = mapped_column(Text)
    rate: Mapped[Numeric | None] = mapped_column(QTY)
    rate_text: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Numeric] = mapped_column(MONEY, nullable=False)
    fx_currency: Mapped[str | None] = mapped_column(Text)
    fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    fx_rate: Mapped[Numeric | None] = mapped_column(RATE)
    is_deemed_positive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    voucher_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_tally_voucher_inventory_lines_voucher", "voucher_id"),
        Index("ix_tally_voucher_inventory_lines_ws", "workspace_id"),
        Index("ix_inv_lines_cover", "workspace_id", "stock_item_guid", "voucher_date"),
    )


class TallyBillAllocation(Base):
    __tablename__ = "tally_bill_allocations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tally_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    ledger_line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_guid: Mapped[str] = mapped_column(Text, nullable=False)
    bill_name: Mapped[str] = mapped_column(Text, nullable=False)
    bill_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Numeric] = mapped_column(MONEY, nullable=False)
    fx_currency: Mapped[str | None] = mapped_column(Text)
    fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    fx_rate: Mapped[Numeric | None] = mapped_column(RATE)
    credit_period_text: Mapped[str | None] = mapped_column(Text)
    credit_period_days: Mapped[int | None] = mapped_column(Integer)
    bill_date: Mapped[date | None] = mapped_column(Date)
    voucher_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_tally_bill_allocations_voucher", "voucher_id"),
        Index("ix_tally_bill_allocations_ws", "workspace_id"),
        Index("ix_bill_allocations_cover", "workspace_id", "ledger_guid", "bill_name"),
    )
