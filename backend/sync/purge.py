"""``python -m backend.sync purge`` (S1 spec §4.9 Q5): deletes every v2 row of workspaces soft-deleted at least
``settings.purge_grace_days`` ago (or immediately with ``--now``), in FK-safe order (``V2_TABLES``), and logs
counts only — never names, GUIDs or workspace ids in the log line (decision 14 discipline).

``workspaces`` itself (and ``users``) is the CURRENT app's table, read-only from v2 (`models/current.py`); this
module only ever reads it (to find soft-deleted rows and their ``updated_at``) and never writes or deletes from
it. The grace clock is ``workspaces.updated_at`` — the only timestamp the current app's soft-delete sets (see
``backend/api/workspaces.py``: ``ws.is_deleted = True``, and ``Workspace.updated_at`` has ``onupdate=_utcnow``
so it moves on that same flush); there is no dedicated ``deleted_at`` column to read instead (A16).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.sync.clock import Clock
from backend.sync.config import V2Settings
from backend.db.sync_models import V2_TABLES

_OPS_LOGGER = "v2.ops.purge"


async def _eligible_workspace_ids(
    session, *, workspace_id: uuid.UUID | None, now: bool, grace_days: int, clock: Clock
) -> list[uuid.UUID]:
    sql = "SELECT id FROM workspaces WHERE is_deleted = true"
    params: dict = {}
    if workspace_id is not None:
        sql += " AND id = :wid"
        params["wid"] = workspace_id
    if not now:
        sql += " AND updated_at < :cutoff"
        params["cutoff"] = clock.now() - timedelta(days=grace_days)
    return list((await session.execute(text(sql), params)).scalars().all())


async def purge(
    session_factory: async_sessionmaker,
    *,
    workspace_id: uuid.UUID | None = None,
    now: bool = False,
    clock: Clock,
    settings: V2Settings | None = None,
) -> dict[str, int]:
    """Row counts deleted per v2 table (``V2_TABLES`` order — children first, so every FK is satisfied without
    relying on ``ON DELETE CASCADE``). A live workspace, or one still inside the grace period (unless ``now``),
    is never touched — ``_eligible_workspace_ids`` is the only source of truth for which workspaces qualify."""
    settings = settings or V2Settings()
    counts: dict[str, int] = {t: 0 for t in V2_TABLES}
    detached = 0
    async with session_factory() as session:
        ids = await _eligible_workspace_ids(
            session, workspace_id=workspace_id, now=now, grace_days=settings.purge_grace_days, clock=clock
        )
    purged = 0
    for ws_id in ids:
        # One transaction per workspace: a failure in one never rolls back (or wedges) the others.
        try:
            async with session_factory() as session:
                ws_counts, ws_detached = await _purge_workspace(session, ws_id, clock)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(_OPS_LOGGER).error(json.dumps({"event": "purge_workspace_failed",
                                                             "error": type(exc).__name__}))
            continue
        purged += 1
        detached += ws_detached
        for table, n in ws_counts.items():
            counts[table] += n

    logging.getLogger(_OPS_LOGGER).info(
        json.dumps({"event": "purge", "workspaces_purged": purged, "devices_detached": detached, "now": now,
                    "counts": counts})
    )
    return counts


async def _purge_workspace(session, ws_id: uuid.UUID, clock: Clock) -> tuple[dict[str, int], int]:
    """Deletes one workspace's rows, children first. ``agent_devices`` is special: a device that moved to this
    workspace from another (``binding._activate`` re-points ``workspace_id``) is still referenced by the OLD
    workspace's ``sync_runs`` / ``sync_workspaces.active_device_id`` -- deleting it would violate those FKs -- so a
    device still referenced by any surviving row is detached instead (``workspace_id`` NULL, inactive, revoked)."""
    counts: dict[str, int] = {}
    detached = 0
    for table in V2_TABLES:
        if table == "agent_devices":
            referenced = (
                "id IN (SELECT device_id FROM sync_runs WHERE workspace_id <> :w) "
                "OR id IN (SELECT active_device_id FROM sync_workspaces "
                "WHERE active_device_id IS NOT NULL AND workspace_id <> :w)"
            )
            det = await session.execute(
                text("UPDATE agent_devices SET workspace_id = NULL, is_active = false, "
                     "revoked_at = COALESCE(revoked_at, :now), revoke_reason = COALESCE(revoke_reason, "
                     "'workspace_purged') WHERE workspace_id = :w AND (" + referenced + ")"),  # noqa: S608
                {"w": ws_id, "now": clock.now()},
            )
            detached += det.rowcount or 0
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE workspace_id = :w"), {"w": ws_id}  # noqa: S608 (table from V2_TABLES)
        )
        counts[table] = result.rowcount or 0
    return counts, detached
