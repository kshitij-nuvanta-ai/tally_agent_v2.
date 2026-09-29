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

Task 7 review, fix round 1 (Important 1-4, controller rulings):
  - I1: opening ANY run interrupts every OTHER device's still-``running`` run for this workspace first
    (``_interrupt_other_device_runs``) — a take-over during a ``first_sync`` must not wedge the workspace
    forever. §7.8's "resume re-opens the interrupted one" applies only to the SAME device: by the time
    ``_open_first_sync`` looks for an existing open run, another device's run has already been flipped to
    ``interrupted``, so the new device always gets a fresh run (and the partial unique index on ``(workspace_id)
    WHERE status='running' AND kind='first_sync'`` never conflicts).
  - I2: ``incremental`` is refused (409 ``run_kind_not_allowed``) while the cursors are NULL — a first sync has
    never completed, so there is nothing for an incremental to be relative to. The §8.2 ``error -> next run
    completed -> ready (or first_sync)`` row now applies to ANY run kind that completes while ``sync_state ==
    "error"``, not only ``first_sync``.
  - I3: ``batches_declared`` is now REQUIRED on ``PATCH .../runs/{id} {status: completed}`` (422
    ``batches_declared_required`` otherwise) — D15's "only if every batch the run declared was acked" cannot be
    opted out of by omitting the field.
  - I4: a ``full_resync``'s ``scope`` must equal its command's confirmed scope (whole-company vs. the SAME
    ``fy_start``), and a command already bound to another still-``running`` run cannot open a second one (reusable
    once that run ends ``failed`` or ``interrupted``). Completing ANY confirmed resync — whole-company or
    single-FY — marks its command ``done``, not just a whole-company one.
"""
from __future__ import annotations

import uuid
from datetime import date

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_start_of, ist_date
from v2.cloud.errors import ApiError
from v2.cloud.models import AgentDevice, SyncBatch, SyncCommand, SyncFyCoverage, SyncRun, SyncWorkspace
from v2.cloud.sync import commands, coverage, state
from v2.contract.models import Counters

# A8 (controller ruling): "fatal code" is undefined in the spec. These are the codes that move a failed
# first_sync run straight to `sync_state = error` (§8.2). Any other error code leaves `first_sync` untouched
# (the agent is expected to retry).
FATAL_RUN_CODES: frozenset[str] = frozenset({"company_mismatch", "unrecoverable"})

_KINDS = ("first_sync", "incremental", "backfill", "full_resync")


class RunCreate(BaseModel):
    """S1 review I2: ``counters_at_start`` is REQUIRED for every kind and typed as the contract's ``Counters`` —
    a ``first_sync`` opened without it would complete with NULL cursors and wedge the workspace (no incremental,
    no new first_sync); malformed counters are a 422, never a DB ``DataError`` 500."""
    kind: Literal["first_sync", "incremental", "backfill", "full_resync"]
    scope: dict | None = None
    command_id: uuid.UUID | None = None
    counters_at_start: Counters
    progress_total: int | None = None


class RunPatch(BaseModel):
    status: Literal["completed", "failed"]
    progress_done: int | None = None
    progress_total: int | None = None
    batches_declared: int | None = None
    cursor_after: Counters | None = None
    error_code: str | None = None


def cursor_on_completion(run: SyncRun, body: RunPatch) -> dict | None:
    """§15.3: what the cursor becomes when a run completes. ``incremental`` -> the agent's own ``cursor_after``;
    ``first_sync`` and a whole-company ``full_resync`` -> the counters recorded at the run's own start (D15 —
    never the batch-reported ``cursor_after``, which those kinds don't even send); ``backfill`` and a single-FY
    ``full_resync`` never move the cursor."""
    if run.kind == "incremental":
        return body.cursor_after.model_dump() if body.cursor_after is not None else None
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


async def _interrupt_other_device_runs(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, clock: Clock
) -> None:
    """I1 (task-7 review): whenever the workspace's active device opens ANY run, every OTHER device's still-
    ``running`` run for this workspace is marked ``interrupted`` first. A take-over (§9.3) revokes the old
    device but never touches its in-flight run — without this, a first_sync interrupted by a take-over would
    wedge the workspace forever (resume always returns the old device's run; the new device's every PATCH on it
    403s; the partial unique index blocks a second open ``first_sync`` row). This runs for every ``kind``, not
    only ``first_sync`` — a take-over can interrupt an incremental/backfill/full_resync just as well."""
    rows = (
        await session.execute(
            select(SyncRun).where(
                SyncRun.workspace_id == sw.workspace_id,
                SyncRun.status == "running",
                SyncRun.device_id != device.id,
            )
        )
    ).scalars().all()
    if not rows:
        return
    now = clock.now()
    for row in rows:
        row.status = "interrupted"
        row.finished_at = now
    await session.flush()


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
    now = clock.now()
    for row in rows:
        fresh_row = fresh.get(row.fy_start)
        if fresh_row is not None:
            row.months_total = fresh_row.months_total
            # S1 review I6 (F15, Task 7 M5): a row that went `complete` at the OLD, smaller total is no longer
            # complete — it drops back to `running` (so the agent re-syncs the missing month and the verified edge
            # stops claiming it). `completed_at` is cleared; `write_cov` sets it again on the real last ack.
            if row.state == "complete" and len(row.months_done or []) < row.months_total:
                row.state = "running"
                row.completed_at = None
    await session.flush()
    await coverage.persist_edges_and_backfill(session, sw, clock)
    sw.updated_at = now


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
        counters_at_start=body.counters_at_start.model_dump(),
        started_at=now,
    )
    session.add(run)
    await session.flush()

    if (sw.sync_state, "first_sync_opened") in state.TRANSITIONS:
        state.transition(sw, "first_sync_opened")

    await _recompute_window_months_total(session, sw, clock)
    return run


def _scope_matches_command(scope: dict | None, cmd_params: dict) -> bool:
    """I4 (task-7 review): a ``full_resync``'s ``scope`` must equal its command's CONFIRMED scope — whole-
    company, or the single FY with the SAME ``fy_start`` — never a broader or different one. ``cmd_params`` is
    the ``sync_commands.params`` shape ``web_sync.py`` stores: ``{"scope": "company"}`` or ``{"scope": "fy",
    "fy_start": "..."}``; ``scope`` is the run's own §7.8 shape: ``{"company": true}`` or ``{"fy_start":
    "..."}``."""
    scope = scope or {}
    cmd_scope = cmd_params.get("scope")
    if cmd_scope == "company":
        return bool(scope.get("company")) and "fy_start" not in scope
    if cmd_scope == "fy":
        return scope.get("fy_start") == cmd_params.get("fy_start") and not scope.get("company")
    return False


async def _require_confirmed_resync_command(session: AsyncSession, sw: SyncWorkspace, body: RunCreate) -> None:
    """D16: a ``full_resync`` needs a pending, user-confirmed ``resync`` command whose CONFIRMED scope matches
    this run's own scope (I4), and that is not already bound to another still-``running`` run — "opening a run
    binds the command to that run"; a command becomes reusable again only once its earlier run ends ``failed``
    or ``interrupted``. Enforced server-side, not only by agent discipline."""
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
    if not _scope_matches_command(body.scope, cmd.params or {}):
        raise ApiError(409, "resync_not_confirmed")
    in_use = (
        await session.execute(
            select(SyncRun).where(SyncRun.command_id == cmd.id, SyncRun.status == "running")
        )
    ).scalars().first()
    if in_use is not None:
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
        counters_at_start=body.counters_at_start.model_dump(),
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

    # I1: interrupt every OTHER device's still-running run for this workspace before anything else — a
    # take-over must never leave a run permanently wedged in `running`. Kind-agnostic (runs before the
    # kind-specific branches below), and a no-op when there is nothing to interrupt.
    await _interrupt_other_device_runs(session, sw, device, clock)

    if body.kind == "incremental":
        # §8.6 (carried from Task 6 review): while restore_detected, `incremental` is refused outright.
        # `full_resync` is NOT gated by restore_detected itself — it is gated only by the confirmed-command
        # check below, which is exactly the allowed recovery path.
        if sw.sync_state == "restore_detected":
            raise ApiError(409, "restore_detected")
        # I2: an incremental is relative to the cursors a completed first sync set — refuse it outright while
        # they're still NULL, rather than silently letting it complete and leave the workspace stuck (the F12
        # NULL-cursor guard would then refuse every future `first_sync` too).
        if sw.cursor_alt_vch_id is None and sw.cursor_alt_mst_id is None:
            raise ApiError(409, "run_kind_not_allowed")

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
    is_company_resync = run.kind == "full_resync" and (run.scope or {}).get("company")

    # I4: completing ANY confirmed resync — whole-company OR single-FY — marks its command `done`. Previously
    # only the whole-company branch did this, so a single-FY command was never consumed.
    if run.kind == "full_resync" and run.command_id is not None:
        cmd = await session.get(SyncCommand, run.command_id)
        if cmd is not None and cmd.status != "done":
            cmd.status = "done"
            cmd.done_at = clock.now()

    if is_company_resync:
        # §7.8: "Completing a confirmed whole-company full_resync 2-FY pass -> ready" — this specific event
        # already covers every state it can fire from (restore_detected / ready / error), so it takes priority
        # over the generic §8.2 `error` row below.
        if (sw.sync_state, "company_resync_completed") in state.TRANSITIONS:
            state.transition(sw, "company_resync_completed")
        # S1 review I4: the confirmed whole-company resync IS the resolution of a restore / relink — clear the
        # reason and the restore/relink offer so sync-status stops offering a resync that has just been done, and
        # cancel any other still-open confirm (it would trigger a second full re-read). A parity offer (FY-scoped)
        # is the ladder's own and is left to the engine.
        sw.restore_reason = None
        offer = (sw.ladder or {}).get("resync_offered")
        if offer and offer.get("reason") in ("restore", "relink"):
            sw.ladder = {k: v for k, v in sw.ladder.items() if k != "resync_offered"}
        await commands.cancel_open_resyncs(session, sw.workspace_id, clock, keep=run.command_id)
        return

    # §8.2 window-driven transitions: `first_sync`'s own event, and (I2) the SAME `error -> ready / first_sync`
    # row for ANY OTHER run kind (incremental, backfill, single-FY full_resync) that completes while the
    # workspace is still `error` — not only `first_sync` runs.
    #
    # Fix round 2 (controller ruling, re-review of I2): the `error -> ready/first_sync` row applies ONLY when
    # the cursors are non-NULL AFTER this completion's own cursor update above (`incremental`/`first_sync`/a
    # whole-company `full_resync` may have just set them here; an `incremental` opened after F12/I2 already
    # had them set before this call). A `backfill` or single-FY `full_resync` never sets a cursor
    # (`cursor_on_completion` returns `None` for both) — completing one in `error` with cursors STILL NULL (a
    # first_sync that failed before ever completing) must leave the workspace `error`, not `ready` with NULL
    # cursors, which would then refuse BOTH a new `first_sync` (F12's NULL-cursor guard, inverted — it only
    # allows first_sync FROM `error`) and every `incremental` (I2's own NULL-cursor gate) forever.
    cursors_set = sw.cursor_alt_vch_id is not None and sw.cursor_alt_mst_id is not None
    if run.kind == "first_sync" or (sw.sync_state == "error" and cursors_set):
        complete = await _window_complete(session, sw, clock)
        if sw.sync_state == "first_sync":
            if complete and (sw.sync_state, "first_sync_completed_window_complete") in state.TRANSITIONS:
                state.transition(sw, "first_sync_completed_window_complete")
        elif sw.sync_state == "error":
            event = "run_completed_window_complete" if complete else "run_completed_window_incomplete"
            if (sw.sync_state, event) in state.TRANSITIONS:
                state.transition(sw, event)


async def patch_run(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, run_id: uuid.UUID, body: RunPatch, clock: Clock
) -> SyncRun:
    if body.status not in ("completed", "failed"):
        raise ApiError(422, "invalid_status")

    run = await _load_run_for_device(session, sw, device, run_id)
    now = clock.now()

    if body.status == "completed":
        # I3: D15's "only if every batch the run declared was acked" cannot be opted out of by omitting the
        # field — `batches_declared` (>= 0) is now required, checked before any mutation.
        if body.batches_declared is None:
            raise ApiError(422, "batches_declared_required")
        # S1 review I2: an incremental's cursor IS the agent's `cursor_after` — completing one without it would
        # leave the cursor where it was (or NULL), so it is refused before any mutation.
        if run.kind == "incremental" and body.cursor_after is None:
            raise ApiError(422, "cursor_after_required")
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
