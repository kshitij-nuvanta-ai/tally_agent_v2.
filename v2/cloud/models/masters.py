"""Master tables (spec §4.3): ``tally_currencies``, ``tally_groups``, ``tally_voucher_types``, ``tally_ledgers``,
``tally_stock_groups``, ``tally_units``, ``tally_stock_items``.

Every master row shares the §4.1 common columns via ``_MasterCommon``: ``id``, ``workspace_id``, ``guid``,
``alter_id``, ``is_deleted``, ``raw``, ``first_seen_at``, ``updated_at``, ``created_at``, ``name`` — plus a unique
``(workspace_id, guid)`` and a non-unique ``(workspace_id, name) WHERE NOT is_deleted`` index (D13) on every table.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MONEY = Numeric(18, 2)
FACE = Numeric(18, 4)
RATE = Numeric(18, 6)
QTY = Numeric(18, 4)


def _master_indexes(prefix: str) -> tuple:
    return (
        Index(f"uq_{prefix}_ws_guid", "workspace_id", "guid", unique=True),
        Index(f"ix_{prefix}_ws_name_live", "workspace_id", "name", postgresql_where=text("NOT is_deleted")),
    )


class _MasterCommon:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No bare index here: v2_001 gives every master table `uq_<t>_ws_guid` and `ix_<t>_ws_name_live`
    # (both workspace_id-leading), so a separate workspace_id-only index would be redundant and would not
    # match the migration (the authority — see `_master_indexes` below and `models/__init__.py`'s note on
    # keeping this module in sync with `v2_001_sync_tables.py`).
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    alter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    raw: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class TallyCurrency(_MasterCommon, Base):
    __tablename__ = "tally_currencies"

    mailing_name: Mapped[str | None] = mapped_column(Text)
    expanded_symbol: Mapped[str | None] = mapped_column(Text)
    decimal_places: Mapped[int | None] = mapped_column(Integer)
    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = _master_indexes("tally_currencies")


class TallyGroup(_MasterCommon, Base):
    __tablename__ = "tally_groups"

    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_guid: Mapped[str | None] = mapped_column(Text)
    primary_group: Mapped[str | None] = mapped_column(Text)
    nature: Mapped[str | None] = mapped_column(Text)
    is_revenue: Mapped[bool | None] = mapped_column(Boolean)
    affects_gross_profit: Mapped[bool | None] = mapped_column(Boolean)
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    reserved_name: Mapped[str | None] = mapped_column(Text)
    derivation_warning: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        *_master_indexes("tally_groups"),
        Index("ix_tally_groups_ws_parent", "workspace_id", "parent_guid"),
    )


class TallyVoucherType(_MasterCommon, Base):
    __tablename__ = "tally_voucher_types"

    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_guid: Mapped[str | None] = mapped_column(Text)
    reserved_name: Mapped[str | None] = mapped_column(Text)
    base_type: Mapped[str | None] = mapped_column(Text)

    __table_args__ = _master_indexes("tally_voucher_types")


class TallyLedger(_MasterCommon, Base):
    __tablename__ = "tally_ledgers"

    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    group_guid: Mapped[str | None] = mapped_column(Text)
    currency_name: Mapped[str | None] = mapped_column(Text)
    is_forex: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_bill_wise: Mapped[bool | None] = mapped_column(Boolean)
    tax_type: Mapped[str | None] = mapped_column(Text)
    gst_duty_head: Mapped[str | None] = mapped_column(Text)
    opening_balance: Mapped[Numeric | None] = mapped_column(MONEY)
    closing_balance: Mapped[Numeric | None] = mapped_column(MONEY)
    opening_fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    opening_fx_rate: Mapped[Numeric | None] = mapped_column(RATE)
    closing_fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    closing_fx_rate: Mapped[Numeric | None] = mapped_column(RATE)
    fx_currency: Mapped[str | None] = mapped_column(Text)
    balance_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'tally'"))
    balance_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_text: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))

    __table_args__ = (
        *_master_indexes("tally_ledgers"),
        Index("ix_tally_ledgers_ws_group", "workspace_id", "group_guid"),
    )


class TallyStockGroup(_MasterCommon, Base):
    __tablename__ = "tally_stock_groups"

    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_guid: Mapped[str | None] = mapped_column(Text)

    __table_args__ = _master_indexes("tally_stock_groups")


class TallyUnit(_MasterCommon, Base):
    __tablename__ = "tally_units"

    is_simple: Mapped[bool | None] = mapped_column(Boolean)
    base_units: Mapped[str | None] = mapped_column(Text)
    additional_units: Mapped[str | None] = mapped_column(Text)
    conversion: Mapped[Numeric | None] = mapped_column(QTY)

    __table_args__ = _master_indexes("tally_units")


class TallyStockItem(_MasterCommon, Base):
    __tablename__ = "tally_stock_items"

    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_guid: Mapped[str | None] = mapped_column(Text)
    base_unit_name: Mapped[str | None] = mapped_column(Text)
    base_unit_guid: Mapped[str | None] = mapped_column(Text)
    closing_qty: Mapped[Numeric | None] = mapped_column(QTY)
    closing_qty_text: Mapped[str | None] = mapped_column(Text)
    closing_value: Mapped[Numeric | None] = mapped_column(MONEY)
    balance_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = _master_indexes("tally_stock_items")
