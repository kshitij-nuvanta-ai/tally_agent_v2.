"""Server -> agent commands (S1 spec §7.15, D16): a ``sync_commands`` row per command, delivered on the next
heartbeat (``pending`` -> ``delivered``) and acknowledged by a later heartbeat (``delivered`` -> ``done``).

S1 review I3 (controller ruling): a heartbeat ack means "received", not "executed". A ``confirm_resync`` therefore
stays ``delivered`` when acked — it is still the user's authorisation for the ``full_resync`` it names (D16) — and is
closed only by that run's completion (``runs._apply_completion_transition`` -> ``done``) or by a superseding
confirm (``supersede_open_resyncs`` -> ``cancelled``). Every other command kind keeps ack -> ``done``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock
from v2.cloud.models import SyncCommand, SyncRun

RESYNC = "confirm_resync"
OPEN = ("pending", "delivered")


async def enqueue(
    session: AsyncSession, ws_id: uuid.UUID, type_: str, params: dict | None, requested_by: str
) -> SyncCommand:
    cmd = SyncCommand(
        workspace_id=ws_id, type=type_, params=params or {}, requested_by=requested_by, status="pending"
    )
    session.add(cmd)
    await session.flush()
    return cmd


async def deliver_pending(session: AsyncSession, ws_id: uuid.UUID, clock: Clock) -> list[dict]:
    """``pending`` -> ``delivered``. Returns the delivered commands as the wire shape the heartbeat response
    carries (§7.6): ``{"id", "type", "params"}``."""
    now = clock.now()
    rows = (
        await session.execute(
            select(SyncCommand).where(SyncCommand.workspace_id == ws_id, SyncCommand.status == "pending")
        )
    ).scalars().all()
    out = []
    for row in rows:
        row.status = "delivered"
        row.delivered_at = now
        out.append({"id": str(row.id), "type": row.type, "params": row.params or {}})
    if rows:
        await session.flush()
    return out


async def ack(session: AsyncSession, ws_id: uuid.UUID, ids: list[str], clock: Clock) -> None:
    """``delivered`` -> ``done`` for the given command ids. Silently ignores ids that are unknown, belong to
    another workspace, or aren't currently ``delivered`` (an agent replaying an old ack list). A malformed
    (non-UUID) id is skipped on its own — it must never cancel acking the OTHER, well-formed ids in the same
    list (controller ruling, fix round 1)."""
    if not ids:
        return
    now = clock.now()
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except (ValueError, AttributeError, TypeError):
            continue
    if not uuids:
        return
    rows = (
        await session.execute(
            select(SyncCommand).where(
                SyncCommand.workspace_id == ws_id,
                SyncCommand.id.in_(uuids),
                SyncCommand.status == "delivered",
                SyncCommand.type != RESYNC,          # I3: ack = received; a resync stays open for its run
            )
        )
    ).scalars().all()
    for row in rows:
        row.status = "done"
        row.done_at = now
    if rows:
        await session.flush()


async def cancel_open_resyncs(session: AsyncSession, ws_id: uuid.UUID, clock: Clock,
                              keep: uuid.UUID | None = None) -> None:
    """Open (``pending``/``delivered``) ``confirm_resync`` commands of ``ws_id`` that no still-``running`` run is
    using -> ``cancelled`` (``done_at`` = now). ``keep`` is never touched. Used when a newer confirm supersedes
    them (I3) and when a whole-company resync completes (I4: nothing left to resync)."""
    in_use = select(SyncRun.command_id).where(SyncRun.workspace_id == ws_id, SyncRun.status == "running",
                                              SyncRun.command_id.is_not(None))
    q = (update(SyncCommand)
         .where(SyncCommand.workspace_id == ws_id, SyncCommand.type == RESYNC, SyncCommand.status.in_(OPEN),
                SyncCommand.id.not_in(in_use))
         .values(status="cancelled", done_at=clock.now())
         .execution_options(synchronize_session="fetch"))
    if keep is not None:
        q = q.where(SyncCommand.id != keep)
    await session.execute(q)
