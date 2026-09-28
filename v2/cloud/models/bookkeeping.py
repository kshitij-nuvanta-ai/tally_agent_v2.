"""Bookkeeping tables (spec §4.2): ``sync_workspaces``, ``agent_devices``, ``sync_runs``, ``sync_batches``,
``sync_quarantine``, ``sync_commands``, ``sync_fy_coverage``.

Conventions (§4.1): UUID pk ``id`` (``gen_random_uuid()`` server default) except ``sync_workspaces`` whose pk is
``workspace_id`` (one row per bound workspace); ``workspace_id`` FK + index; ``created_at timestamptz
server_default now()``; bare FKs, no ``relationship()``.
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
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SyncWorkspace(Base):
    """One row per bound workspace — Part 1's ``workspace.config.*`` (§4.2).

    No ``created_at``: ``bound_at`` / ``updated_at`` cover row lifecycle (task-3 controller ruling — sync_workspaces
    intentionally has no ``created_at``, unlike every other v2 table).
    """

    __tablename__ = "sync_workspaces"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), primary_key=True
    )
    tally_company_guid: Mapped[str] = mapped_column(Text, nullable=False)
    tally_company_name: Mapped[str] = mapped_column(Text, nullable=False)
    previous_company_guids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    books_from: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency_name: Mapped[str | None] = mapped_column(Text)
    sync_state: Mapped[str] = mapped_column(Text, nullable=False)
    restore_reason: Mapped[str | None] = mapped_column(Text)
    active_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_devices.id"))
    cursor_alt_vch_id: Mapped[int | None] = mapped_column(BigInteger)
    cursor_alt_mst_id: Mapped[int | None] = mapped_column(BigInteger)
    cursor_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    caught_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat: Mapped[dict | None] = mapped_column(JSONB)
    relink_prompt: Mapped[dict | None] = mapped_column(JSONB)
    oldest_available_fy: Mapped[date | None] = mapped_column(Date)
    oldest_complete_fy: Mapped[date | None] = mapped_column(Date)
    backfill_state: Mapped[str | None] = mapped_column(Text)
    backfill_percent: Mapped[Numeric | None] = mapped_column(Numeric(5, 2))
    last_parity: Mapped[dict | None] = mapped_column(JSONB)
    ladder: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    tb_imbalance_baseline: Mapped[dict | None] = mapped_column(JSONB)
    quarantine_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    storage_estimate_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    storage_alert: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (Index("ix_sync_workspaces_company_guid", "tally_company_guid"),)


class AgentDevice(Base):
    __tablename__ = "agent_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"))
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(Text)
    refresh_hash: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_prev_hash: Mapped[str | None] = mapped_column(Text)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_agent_devices_user", "user_id"),
        Index("ix_agent_devices_ws", "workspace_id"),
        Index("uq_agent_devices_refresh_hash", "refresh_hash", unique=True),
        Index(
            "uq_agent_devices_one_active",
            "workspace_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class SyncCommand(Base):
    __tablename__ = "sync_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONB)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_sync_commands_ws_status", "workspace_id", "status"),)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_devices.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict | None] = mapped_column(JSONB)
    command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_commands.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    progress_done: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    batches_declared: Mapped[int | None] = mapped_column(Integer)
    counters_at_start: Mapped[dict | None] = mapped_column(JSONB)
    cursor_after: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_sync_runs_ws", "workspace_id"),
        Index("ix_sync_runs_ws_started", "workspace_id", desc("started_at")),
        Index(
            "uq_sync_runs_one_open_first_sync",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'running' AND kind = 'first_sync'"),
        ),
    )


class SyncBatch(Base):
    __tablename__ = "sync_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_runs.id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    object_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_sync_batches_ws", "workspace_id"),
        Index("uq_sync_batches_ws_batch", "workspace_id", "batch_id", unique=True),
    )


class SyncQuarantine(Base):
    __tablename__ = "sync_quarantine"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    voucher_date: Mapped[date | None] = mapped_column(Date)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_sync_quarantine_ws", "workspace_id"),
        Index(
            "uq_sync_quarantine_open",
            "workspace_id",
            "kind",
            "guid",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )


class SyncFyCoverage(Base):
    __tablename__ = "sync_fy_coverage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    fy_start: Mapped[date] = mapped_column(Date, nullable=False)
    fy_end: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    months_done: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    months_complete: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    months_total: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        Index("ix_sync_fy_coverage_ws", "workspace_id"),
        Index("uq_sync_fy_coverage_ws_fystart", "workspace_id", "fy_start", unique=True),
    )
