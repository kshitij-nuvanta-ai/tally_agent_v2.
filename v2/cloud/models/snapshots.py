"""Snapshot table (spec §4.6): ``tally_report_snapshots`` — verbatim + parsed captures of Tally reports
(trial balance, P&L, balance sheet, stock summary, bills receivable/payable)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MONEY = Numeric(18, 2)


class TallyReportSnapshot(Base):
    __tablename__ = "tally_report_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_date: Mapped[date | None] = mapped_column(Date)
    as_on_date: Mapped[date] = mapped_column(Date, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    request_flags: Mapped[dict | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    counters: Mapped[dict | None] = mapped_column(JSONB)
    cells: Mapped[dict | None] = mapped_column(JSONB)
    rows: Mapped[dict | None] = mapped_column(JSONB)
    row_count: Mapped[int | None] = mapped_column(Integer)
    synthetic_rows: Mapped[dict | None] = mapped_column(JSONB)
    imbalance: Mapped[Numeric | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_snapshots_ws_type_ason", "workspace_id", "report_type", "as_on_date", unique=True),
    )
