"""``sync_state`` machine, heartbeat, restore detection, re-link and ``sync-status`` (S1 spec §7.6, §7.7, §7.13,
§7.15, §8.2, §8.4, §8.5, §8.6, D16, D17, D20 hook, D21, Q1, Q25).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock
from v2.cloud.config import V2Settings
from v2.cloud.errors import ApiError
from v2.cloud.models import AgentDevice, SyncCommand, SyncFyCoverage, SyncRun, SyncWorkspace
from v2.cloud.sync import commands, maintenance
from v2.cloud.sync.binding import _bound_elsewhere

# --- §8.2 sync_state machine ---------------------------------------------------------------------------------

STATES = ("awaiting_first_connection", "first_sync", "ready", "error", "restore_detected")

TRANSITIONS: dict[tuple[str, str], str] = {
    ("awaiting_first_connection", "first_sync_opened"): "first_sync",
    ("first_sync", "first_sync_completed_window_complete"): "ready",
    ("first_sync", "fatal"): "error",
    ("error", "run_completed_window_complete"): "ready",
    ("error", "run_completed_window_incomplete"): "first_sync",
    ("ready", "counters_backwards"): "restore_detected",
    ("error", "counters_backwards"): "restore_detected",
    ("restore_detected", "company_resync_completed"): "ready",
    # Controller ruling: completion -> ready must also be defined FROM ready/error (spec §7.8: completing a
    # confirmed whole-company full_resync's 2-FY pass -> ready), so a later run-completion call can never hit an
    # undefined-transition 500 just because the workspace wasn't in restore_detected when the run finished.
    ("ready", "company_resync_completed"): "ready",
    ("error", "company_resync_completed"): "ready",
    **{(s, "relink"): "restore_detected" for s in STATES},
}


def transition(sw: SyncWorkspace, event: str) -> None:
    """Mutates ``sw.sync_state`` in place. Raises ``ValueError`` for an undefined ``(from_state, event)`` pair."""
    key = (sw.sync_state, event)
    if key not in TRANSITIONS:
        raise ValueError(f"no transition from {sw.sync_state!r} on event {event!r}")
    sw.sync_state = TRANSITIONS[key]


# --- §8.6 restore detection (pure) ----------------------------------------------------------------------------


def detect_restore(sw: SyncWorkspace, counters: dict) -> bool:
    """``ours`` counters below the stored cursors on EITHER counter (§8.6). Pure: never mutates ``sw`` or
    touches the DB. ``False`` if either side is missing (nothing to compare against yet)."""
    if sw.cursor_alt_vch_id is None or sw.cursor_alt_mst_id is None:
        return False
    vch = counters.get("alt_vch_id")
    mst = counters.get("alt_mst_id")
    if vch is None or mst is None:
        return False
    return vch < sw.cursor_alt_vch_id or mst < sw.cursor_alt_mst_id


def _resync_offer(reason: str) -> dict:
    return {"scope": "company", "reason": reason}


def _parse_pc_clock(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- §7.6 heartbeat ---------------------------------------------------------------------------------------


async def heartbeat(
    session: AsyncSession,
    sw: SyncWorkspace,
    device: AgentDevice,
    body,  # HeartbeatRequest (v2.cloud.api.sync) — kept untyped here to avoid an api -> sync import cycle
    settings: V2Settings,
    clock: Clock,
) -> dict:
    now = clock.now()
    skew_s = round((_parse_pc_clock(body.pc_clock) - now).total_seconds())

    device.last_seen_at = now
    sw.last_seen_at = now

    seen_company = body.seen_company.model_dump() if body.seen_company else None
    counters = body.counters.model_dump() if body.counters else None

    sw.last_heartbeat = {
        "agent_version": body.agent_version,
        "tally_version": body.tally_version,
        "tally_status": body.tally_status,
        "seen_company": seen_company,
        "counters": counters,
        "last_error_code": body.last_error_code,
        "breaker": body.breaker,
        "outbox_depth": body.outbox_depth,
        "clock_skew_s": skew_s,
    }

    # §8.4/§8.6: restore detection and caught_up_at only ever fire for `ours` — never for another company or a
    # closed/blocked/no-company reading (test_heartbeat_not_ours_never_triggers_restore).
    if body.tally_status == "ours" and counters is not None:
        if detect_restore(sw, counters):
            # Fix round 1 / Critical 1: §8.2 only defines `counters_backwards` FROM `ready`/`error`. Cursors
            # don't move until a resync completes, so every heartbeat while already `restore_detected` (or, in
            # principle, `first_sync`/`awaiting_first_connection`) would otherwise re-detect the same backwards
            # counters and call `transition()` with an undefined pair, raising `ValueError` before the commit —
            # which also silently kills `last_seen_at` updates and command delivery for the rest of THIS
            # request. Only fire the transition when it's actually defined; otherwise the workspace is already
            # in the right state and this heartbeat just continues as a normal heartbeat (records last_seen_at,
            # delivers pending commands — including the `confirm_resync` that's the only way out).
            if (sw.sync_state, "counters_backwards") in TRANSITIONS:
                transition(sw, "counters_backwards")
                sw.restore_reason = "counters_backwards"
                sw.ladder = {**(sw.ladder or {}), "resync_offered": _resync_offer("restore")}
        elif (
            sw.cursor_alt_vch_id is not None
            and sw.cursor_alt_mst_id is not None
            and counters.get("alt_vch_id") == sw.cursor_alt_vch_id
            and counters.get("alt_mst_id") == sw.cursor_alt_mst_id
        ):
            sw.caught_up_at = now
    elif body.tally_status == "other_company_same_name" and seen_company is not None:
        sw.relink_prompt = {"guid": seen_company.get("guid"), "name": seen_company.get("name")}

    # Server -> agent commands (D16): ack what the agent confirmed FIRST, then hand back whatever is newly
    # pending, so a command enqueued and acked within the same request is never double-delivered.
    if body.acked_commands:
        await commands.ack(session, sw.workspace_id, body.acked_commands, clock)
    delivered = await commands.deliver_pending(session, sw.workspace_id, clock)

    # D20: one bounded maintenance slice per heartbeat, no scheduler.
    await maintenance.run_slice(session, sw, settings, clock)

    sw.updated_at = now
    await session.commit()

    return {
        "server_time": now.isoformat(),
        "sync_state": sw.sync_state,
        "cursors": {"alt_vch_id": sw.cursor_alt_vch_id, "alt_mst_id": sw.cursor_alt_mst_id},
        "commands": delivered,
    }


# --- §7.7 state -------------------------------------------------------------------------------------------


async def _coverage_json(session: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            select(SyncFyCoverage)
            .where(SyncFyCoverage.workspace_id == workspace_id)
            .order_by(SyncFyCoverage.fy_start)
        )
    ).scalars().all()
    return [
        {
            "fy_start": r.fy_start.isoformat(),
            "fy_end": r.fy_end.isoformat(),
            "state": r.state,
            "months_total": r.months_total,
            "months_complete": r.months_complete,
        }
        for r in rows
    ]


async def get_state(session: AsyncSession, sw: SyncWorkspace) -> dict:
    """§7.7: cursors, coverage rows, open runs, pending commands, ``books_from``, ``base_currency_name`` — the
    agent's start-up call; its SQLite copy is only a cache."""
    open_runs = (
        await session.execute(
            select(SyncRun).where(SyncRun.workspace_id == sw.workspace_id, SyncRun.status == "running")
        )
    ).scalars().all()
    pending_commands = (
        await session.execute(
            select(SyncCommand).where(
                SyncCommand.workspace_id == sw.workspace_id, SyncCommand.status == "pending"
            )
        )
    ).scalars().all()

    return {
        "sync_state": sw.sync_state,
        "cursors": {"alt_vch_id": sw.cursor_alt_vch_id, "alt_mst_id": sw.cursor_alt_mst_id},
        "coverage": await _coverage_json(session, sw.workspace_id),
        "books_from": sw.books_from.isoformat(),
        "base_currency_name": sw.base_currency_name,
        "open_runs": [
            {"id": str(r.id), "kind": r.kind, "status": r.status, "progress_done": r.progress_done,
             "progress_total": r.progress_total}
            for r in open_runs
        ],
        "commands": [
            {"id": str(c.id), "type": c.type, "params": c.params or {}} for c in pending_commands
        ],
    }


# --- §7.15 re-link (shared by the device and web paths) --------------------------------------------------


async def apply_relink(
    session: AsyncSession, sw: SyncWorkspace, new_guid: str, name: str, clock: Clock, user_id: uuid.UUID
) -> None:
    """Fix round 1 / Important 1: reuses binding's own ``_bound_elsewhere`` check (§7.5/A6) so relink can never
    create two live workspaces bound to the same Tally GUID — 409 ``company_bound_elsewhere`` before any
    mutation, exactly like a fresh bind would refuse it."""
    elsewhere = await _bound_elsewhere(session, user_id, new_guid, sw.workspace_id)
    if elsewhere is not None:
        raise ApiError(409, "company_bound_elsewhere", workspace_id=str(elsewhere))

    now = clock.now()
    previous = list(sw.previous_company_guids or [])
    if sw.tally_company_guid not in previous:
        previous.append(sw.tally_company_guid)
    sw.previous_company_guids = previous
    sw.tally_company_guid = new_guid
    sw.tally_company_name = name
    sw.relink_prompt = None
    transition(sw, "relink")
    sw.restore_reason = "relink"
    sw.ladder = {**(sw.ladder or {}), "resync_offered": _resync_offer("relink")}
    sw.updated_at = now
    await session.flush()


# --- §7.13 sync-status --------------------------------------------------------------------------------------


async def sync_status(session: AsyncSession, sw: SyncWorkspace) -> dict:
    first_sync = None
    if sw.sync_state == "first_sync":
        run = (
            await session.execute(
                select(SyncRun).where(
                    SyncRun.workspace_id == sw.workspace_id,
                    SyncRun.kind == "first_sync",
                    SyncRun.status == "running",
                )
            )
        ).scalars().first()
        done = (run.progress_done or 0) if run else 0
        total = (run.progress_total or 0) if run else 0
        percent = round(done / total * 100, 1) if total else 0.0
        first_sync = {"percent": percent, "done": done, "total": total}

    hb = sw.last_heartbeat or {}
    active_device = await session.get(AgentDevice, sw.active_device_id) if sw.active_device_id else None

    last_parity = None
    if sw.last_parity:
        last_parity = dict(sw.last_parity)
        if last_parity.get("state") == "suspect":
            # Part 1 §6 "suspect is invisible": the web-facing status never shows `suspect`.
            last_parity["state"] = "ok"

    return {
        "sync_state": sw.sync_state,
        "restore_reason": sw.restore_reason,
        "first_sync": first_sync,
        "last_synced_at": sw.last_synced_at.isoformat() if sw.last_synced_at else None,
        "caught_up_at": sw.caught_up_at.isoformat() if sw.caught_up_at else None,
        "agent": {
            "device_name": active_device.device_name if active_device else None,
            "last_seen_at": sw.last_seen_at.isoformat() if sw.last_seen_at else None,
            "tally_status": hb.get("tally_status"),
            "agent_version": hb.get("agent_version"),
            "clock_skew_s": hb.get("clock_skew_s"),
        },
        "backfill": {
            "oldest_available_fy": sw.oldest_available_fy.isoformat() if sw.oldest_available_fy else None,
            "oldest_complete_fy": sw.oldest_complete_fy.isoformat() if sw.oldest_complete_fy else None,
            "books_from": sw.books_from.isoformat() if sw.books_from else None,
            "percent": float(sw.backfill_percent) if sw.backfill_percent is not None else 0.0,
            "state": sw.backfill_state,
        },
        "last_parity": last_parity,
        "relink_prompt": sw.relink_prompt,
        "resync_offered": (sw.ladder or {}).get("resync_offered"),
        "quarantine_count": sw.quarantine_count,
        "storage_alert": sw.storage_alert,
    }
