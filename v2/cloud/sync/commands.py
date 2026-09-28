"""Server -> agent commands (S1 spec §7.15, D16): a ``sync_commands`` row per command, delivered on the next
heartbeat (``pending`` -> ``delivered``) and acknowledged by a later heartbeat (``delivered`` -> ``done``).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock
from v2.cloud.models import SyncCommand


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
    another workspace, or aren't currently ``delivered`` (an agent replaying an old ack list)."""
    if not ids:
        return
    now = clock.now()
    try:
        uuids = [uuid.UUID(i) for i in ids]
    except ValueError:
        return
    rows = (
        await session.execute(
            select(SyncCommand).where(
                SyncCommand.workspace_id == ws_id,
                SyncCommand.id.in_(uuids),
                SyncCommand.status == "delivered",
            )
        )
    ).scalars().all()
    for row in rows:
        row.status = "done"
        row.done_at = now
    if rows:
        await session.flush()
