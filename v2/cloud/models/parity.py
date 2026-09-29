"""Parity tables (spec §4.7, Part 1 §6): ``parity_runs``, ``parity_lines``."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MONEY = Numeric(18, 2)
FACE = Numeric(18, 4)


class ParityRun(Base):
    __tablename__ = "parity_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_runs.id"))
    as_on_date: Mapped[date] = mapped_column(Date, nullable=False)
    rung: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    verified_from: Mapped[date] = mapped_column(Date, nullable=False)
    anchor_as_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    abort_reason: Mapped[str | None] = mapped_column(Text)
    lines_compared: Mapped[int | None] = mapped_column(Integer)
    mismatch_count: Mapped[int | None] = mapped_column(Integer)
    max_abs_diff: Mapped[Numeric | None] = mapped_column(MONEY)
    net_diff: Mapped[Numeric | None] = mapped_column(MONEY)
    forex_unrealised_total: Mapped[Numeric | None] = mapped_column(MONEY)
    tb_imbalance: Mapped[Numeric | None] = mapped_column(MONEY)
    counters_before: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    counters_after: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    remediation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_parity_runs_ws", "workspace_id"),
        Index("ix_parity_runs_ws_started", "workspace_id", "started_at"),
    )


class ParityLine(Base):
    __tablename__ = "parity_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parity_runs.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    guid: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    our_amount: Mapped[Numeric | None] = mapped_column(MONEY)
    tally_amount: Mapped[Numeric | None] = mapped_column(MONEY)
    diff: Mapped[Numeric | None] = mapped_column(MONEY)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    cause: Mapped[str | None] = mapped_column(Text)
    remediation_status: Mapped[str | None] = mapped_column(Text)
    unrealised_diff: Mapped[Numeric | None] = mapped_column(MONEY)
    our_fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    tally_fx_amount: Mapped[Numeric | None] = mapped_column(FACE)
    as_on_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_parity_lines_ws", "workspace_id"),
        Index("ix_parity_lines_ws_run", "workspace_id", "run_id"),
        Index("ix_parity_lines_ws_verdict", "workspace_id", "verdict"),
    )
