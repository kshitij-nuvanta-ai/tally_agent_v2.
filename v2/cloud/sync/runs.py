"""``POST /api/sync/{ws}/runs`` and ``PATCH /api/sync/{ws}/runs/{run_id}`` (S1 spec §7.8, D15, D16). Server-side
cursors move only when a run completes (D15); a ``full_resync`` is refused unless it cites a pending,
user-confirmed ``resync`` command (D16). Opening a whole-company / single-FY ``full_resync`` also drives the
matching coverage event (§8.3's ``company_resync_start`` / ``fy_resync_start``) through ``coverage.apply``.

F12 / F15 (controller rulings, task-7 brief):
  - F12: a failed ``first_sync`` leaves the workspace ``error``; a NEW ``first_sync`` is allowed there while the
    cursors are still NULL (a first sync has never completed) — see ``state.py``'s ``("error",
    "first_sync_opened")`` transition.
  - F15: window ``months_total`` is fixed at bind time to the bind month. If the first sync actually starts in a
    later month, opening it must recompute the window FYs' (current + previous) ``months_total`` using the IST
    month of the run's own start — otherwise the FY could go ``complete`` before its last month is ever acked.
"""
from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_start_of, ist_date
from v2.cloud.errors import ApiError
from v2.cloud.models import AgentDevice, SyncBatch, SyncCommand, SyncFyCoverage, SyncRun, SyncWorkspace
from v2.cloud.sync import coverage, state

# A8 (controller ruling): "fatal code" is undefined in the spec. These are the codes that move a failed
# first_sync run straight to `sync_state = error` (§8.2). Any other error code leaves `first_sync` untouched
# (the agent is expected to retry).
FATAL_RUN_CODES: frozenset[str] = frozenset({"company_mismatch", "unrecoverable"})

_KINDS = ("first_sync", "incremental", "backfill", "full_resync")


class RunCreate(BaseModel):
    kind: str
    scope: dict | None = None
    command_id: uuid.UUID | None = None
    counters_at_start: dict | None = None
    progress_total: int | None = None


class RunPatch(BaseModel):
    status: str
    progress_done: int | None = None
    progress_total: int | None = None
    batches_declared: int | None = None
    cursor_after: dict | None = None
    error_code: str | None = None


def cursor_on_completion(run: SyncRun, body: RunPatch) -> dict | None:
    """§15.3: what the cursor becomes when a run completes. ``incremental`` -> the agent's own ``cursor_after``;
    ``first_sync`` and a whole-company ``full_resync`` -> the counters recorded at the run's own start (D15 —
    never the batch-reported ``cursor_after``, which those kinds don't even send); ``backfill`` and a single-FY
    ``full_resync`` never move the cursor."""
    if run.kind == "incremental":
        return body.cursor_after
    if run.kind == "first_sync" or (run.kind == "full_resync" and (run.scope or {}).get("company")):
        return run.counters_at_start
    return None


# --- helpers ---------------------------------------------------------------------------------------------


async def _existing_open_first_sync(session: AsyncSession, ws_id: uuid.UUID) -> SyncRun | None:
    return (
        await session.execute(
            select(SyncRun).where(
                SyncRun.workspace_id == ws_id, SyncRun.kind == "first_sync", SyncRun.status == "running"
            )
        )
    ).scalars().first()


async def _recompute_window_months_total(session: AsyncSession, sw: SyncWorkspace, clock: Clock) -> None:
    """F15: recompute the window FYs' (current + previous) ``months_total`` to the IST month of THIS run's own
    start, not the month the workspace was bound in. Older FYs are never touched — ``fy_rows_for_bind`` already
    runs them to their own ``fy_end`` regardless of ``first_sync_month``."""
    from v2.cloud.sync.coverage import fy_rows_for_bind  # local import: avoid a module cycle at import time

    today = ist_date(clock.now())
    fresh = {r.fy_start: r for r in fy_rows_for_bind(sw.books_from, today, today)}
    current_fy = fy_start_of(today)
    previous_fy = date(current_fy.year - 1, 4, 1)

    rows = (
        await session.execute(
            select(SyncFyCoverage).where(
                SyncFyCoverage.workspace_id == sw.workspace_id,
                SyncFyCoverage.fy_start.in_([current_fy, previous_fy]),
            )
        )
    ).scalars().all()
    for row in rows:
        fresh_row = fresh.get(row.fy_start)
        if fresh_row is not None:
            row.months_total = fresh_row.months_total
    await session.flush()


async def _open_first_sync(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: RunCreate, clock: Clock
) -> SyncRun:
    existing = await _existing_open_first_sync(session, sw.workspace_id)
    if existing is not None:
        return existing  # §7.8 resume: return the interrupted run as-is (no re-open, no re-transition)

    cursors_null = sw.cursor_alt_vch_id is None and sw.cursor_alt_mst_id is None
    allowed = sw.sync_state in ("awaiting_first_connection", "first_sync") or (
        sw.sync_state == "error" and cursors_null  # F12
    )
    if not allowed:
        raise ApiError(409, "run_kind_not_allowed")

    now = clock.now()
    run = SyncRun(
        workspace_id=sw.workspace_id,
        device_id=device.id,
        kind="first_sync",
        scope=body.scope,
        command_id=body.command_id,
        status="running",
        progress_done=0,
        progress_total=body.progress_total,
        counters_at_start=body.counters_at_start,
        started_at=now,
    )
    session.add(run)
    await session.flush()

    if (sw.sync_state, "first_sync_opened") in state.TRANSITIONS:
        state.transition(sw, "first_sync_opened")

    await _recompute_window_months_total(session, sw, clock)
    return run


async def _require_confirmed_resync_command(session: AsyncSession, sw: SyncWorkspace, body: RunCreate) -> None:
    """D16: a ``full_resync`` needs a pending, user-confirmed ``resync`` command — enforced server-side, not
    only by agent discipline."""
    if body.command_id is None:
        raise ApiError(409, "resync_not_confirmed")
    cmd = await session.get(SyncCommand, body.command_id)
    if (
        cmd is None
        or cmd.workspace_id != sw.workspace_id
        or cmd.type != "confirm_resync"
        or cmd.status not in ("pending", "delivered")
    ):
        raise ApiError(409, "resync_not_confirmed")


async def _apply_resync_start(session: AsyncSession, sw: SyncWorkspace, scope: dict | None, clock: Clock) -> None:
    """§8.3: opening a confirmed ``full_resync`` immediately fires the matching coverage event — whole-company
    on every FY row, or single-FY on just that one."""
    scope = scope or {}
    if scope.get("company"):
        rows = (
            await session.execute(select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == sw.workspace_id))
        ).scalars().all()
        now = clock.now()
        for row in rows:
            new_cov = coverage.apply(coverage.to_cov(row), "company_resync_start")
            coverage.write_cov(row, new_cov, now)
        await session.flush()
        await coverage.persist_edges_and_backfill(session, sw, clock)
    elif scope.get("fy_start"):
        fy_start = date.fromisoformat(scope["fy_start"])
        row = (
            await session.execute(
                select(SyncFyCoverage).where(
                    SyncFyCoverage.workspace_id == sw.workspace_id, SyncFyCoverage.fy_start == fy_start
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            now = clock.now()
            new_cov = coverage.apply(coverage.to_cov(row), "fy_resync_start")
            coverage.write_cov(row, new_cov, now)
            await session.flush()
            await coverage.persist_edges_and_backfill(session, sw, clock)


async def _create_run(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: RunCreate, now
) -> SyncRun:
    run = SyncRun(
        workspace_id=sw.workspace_id,
        device_id=device.id,
        kind=body.kind,
        scope=body.scope,
        command_id=body.command_id,
        status="running",
        progress_done=0,
        progress_total=body.progress_total,
        counters_at_start=body.counters_at_start,
        started_at=now,
    )
    session.add(run)
    await session.flush()
    return run


# --- §7.8 POST /runs --------------------------------------------------------------------------------------


async def open_run(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: RunCreate, clock: Clock
) -> SyncRun:
    if body.kind not in _KINDS:
        raise ApiError(422, "invalid_run_kind")

    # §8.6 (carried from Task 6 review): while restore_detected, `incremental` is refused outright. `full_resync`
    # is NOT gated by restore_detected itself — it is gated only by the confirmed-command check below, which is
    # exactly the allowed recovery path.
    if body.kind == "incremental" and sw.sync_state == "restore_detected":
        raise ApiError(409, "restore_detected")

    if body.kind == "first_sync":
        return await _open_first_sync(session, sw, device, body, clock)

    if body.kind == "full_resync":
        await _require_confirmed_resync_command(session, sw, body)
        now = clock.now()
        run = await _create_run(session, sw, device, body, now)
        await _apply_resync_start(session, sw, body.scope, clock)
        return run

    # incremental (not restore_detected) / backfill
    now = clock.now()
    return await _create_run(session, sw, device, body, now)


# --- §7.8 PATCH /runs/{run_id} ------------------------------------------------------------------------------


async def _load_run_for_device(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, run_id: uuid.UUID
) -> SyncRun:
    run = await session.get(SyncRun, run_id)
    if run is None or run.workspace_id != sw.workspace_id:
        raise ApiError(404, "run_not_found")
    if run.device_id != device.id:
        raise ApiError(403, "wrong_workspace")
    if run.status != "running":
        raise ApiError(409, "run_closed")
    return run


async def _count_accepted_batches(session: AsyncSession, run_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(SyncBatch).where(SyncBatch.run_id == run_id, SyncBatch.status == "accepted")
        )
    ).scalar_one()


async def _window_complete(session: AsyncSession, sw: SyncWorkspace, clock: Clock) -> bool:
    today = ist_date(clock.now())
    current_fy = fy_start_of(today)
    previous_fy = date(current_fy.year - 1, 4, 1)
    books_fy = fy_start_of(sw.books_from)
    window_fys = [fy for fy in (previous_fy, current_fy) if fy >= books_fy]
    if not window_fys:
        return True
    rows = (
        await session.execute(
            select(SyncFyCoverage).where(
                SyncFyCoverage.workspace_id == sw.workspace_id, SyncFyCoverage.fy_start.in_(window_fys)
            )
        )
    ).scalars().all()
    if len(rows) != len(window_fys):
        return False
    return all(r.state == "complete" for r in rows)


async def _apply_completion_transition(session: AsyncSession, sw: SyncWorkspace, run: SyncRun, clock: Clock) -> None:
    if run.kind == "first_sync":
        complete = await _window_complete(session, sw, clock)
        if complete:
            event = (
                "first_sync_completed_window_complete" if sw.sync_state == "first_sync"
                else "run_completed_window_complete"
            )
        else:
            event = None if sw.sync_state == "first_sync" else "run_completed_window_incomplete"
        if event is not None and (sw.sync_state, event) in state.TRANSITIONS:
            state.transition(sw, event)
    elif run.kind == "full_resync" and (run.scope or {}).get("company"):
        # §7.8: "Completing a confirmed whole-company full_resync 2-FY pass -> ready, the command done."
        if (sw.sync_state, "company_resync_completed") in state.TRANSITIONS:
            state.transition(sw, "company_resync_completed")
        if run.command_id is not None:
            cmd = await session.get(SyncCommand, run.command_id)
            if cmd is not None and cmd.status != "done":
                cmd.status = "done"
                cmd.done_at = clock.now()


async def patch_run(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, run_id: uuid.UUID, body: RunPatch, clock: Clock
) -> SyncRun:
    if body.status not in ("completed", "failed"):
        raise ApiError(422, "invalid_status")

    run = await _load_run_for_device(session, sw, device, run_id)
    now = clock.now()

    if body.status == "completed":
        if body.batches_declared is not None:
            accepted = await _count_accepted_batches(session, run.id)
            if accepted < body.batches_declared:
                raise ApiError(409, "batches_missing", missing=body.batches_declared - accepted)

        cursor = cursor_on_completion(run, body)
        if cursor is not None:
            sw.cursor_alt_vch_id = cursor.get("alt_vch_id")
            sw.cursor_alt_mst_id = cursor.get("alt_mst_id")
            sw.cursor_set_at = now

        run.status = "completed"
        run.progress_done = body.progress_done
        run.progress_total = body.progress_total
        run.batches_declared = body.batches_declared
        run.cursor_after = cursor
        run.finished_at = now

        await _apply_completion_transition(session, sw, run, clock)
    else:  # failed
        run.status = "failed"
        run.error_code = body.error_code
        run.finished_at = now
        if run.kind == "first_sync" and body.error_code in FATAL_RUN_CODES:
            if (sw.sync_state, "fatal") in state.TRANSITIONS:
                state.transition(sw, "fatal")

    sw.updated_at = now
    await session.flush()
    return run


async def require_open_run(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, run_id: uuid.UUID
) -> SyncRun:
    """Consumed by Task 8 (batches): 409 ``run_closed`` if the run isn't ``running``, 403 ``wrong_workspace`` if
    it belongs to another device."""
    return await _load_run_for_device(session, sw, device, run_id)
