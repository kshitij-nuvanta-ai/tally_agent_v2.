"""Lazy maintenance (S1 spec §4.9, D20, §7.6): each heartbeat runs one bounded slice of raw purge (Q22), parity
retention (Q21), batch-log retention and the storage estimate/alert (Q23). No scheduler: nothing here runs unless
a heartbeat arrives (S1-R9) — the `purge` CLI (`v2/cloud/sync/purge.py`, Q5) is the only thing that ever deletes a
whole workspace's rows.

Each sub-step is bounded BOTH by rows (`LIMIT settings.maintenance_slice_rows`) and by elapsed wall time
(`settings.maintenance_slice_seconds`) — a slice that runs long on one sub-step skips the rest rather than
blowing the heartbeat's budget; the next heartbeat picks up where this one left off (nothing here is
transactionally atomic across sub-steps, and that is fine: each sub-step's own WHERE clause makes it safe to
retry or interrupt at any point).

§4.9:
- **Raw purge (Q22):** `raw` is kept only for the newest two FYs of a workspace's coverage (current + previous,
  IST). Ingest (`ingest/store.py`) already stores `raw = NULL` for anything arriving outside that window; this
  slice nulls `raw` for rows that were IN the window when written and then rolled OUT of it (`add_fy` rollover,
  §7.10). Uses the SAME window definition as `store.raw_window_fys` (controller ruling 3 / Task 8c re-review
  finding: there were three copies of "window FYs" — store, `coverage.backfill`, `state.sync_status` — drifting
  apart risks nulling raw for a FY still in the window at an `add_fy` boundary).
- **Parity retention (Q21, 90 / 7 / 90):** `parity_lines` whose verdict is NOT a problem verdict (i.e. a clean
  `match`/`match_revalued`/`not_applicable` compare) are pruned once older than 7 days. `parity_runs` (and,
  via `ON DELETE CASCADE`, whatever lines still remain on them — including 90-day-old problem lines) are pruned
  once older than 90 days — EXCEPT the run `last_parity` points at and the run the ladder's `last_run_id` points
  at, which are never pruned regardless of age (controller ruling 5).
- **Batch-log retention:** `sync_batches` rows older than 90 days are pruned.
- **Storage estimate + alert (Q23):** `storage_estimate_bytes = Σ row counts × AVG_ROW_BYTES[table]`, refreshed
  every slice; `storage_alert = estimate > settings.storage_alert_bytes` — an ops signal (counts only, per
  `parity/opsignal.py`'s decision-14 discipline) fires the first time a workspace crosses the line. No hard stop.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, null, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_end_of, ist_date
from v2.cloud.config import V2Settings
from v2.cloud.ingest.store import raw_window_fys
from v2.cloud.models import ParityLine, ParityRun, SyncBatch, SyncWorkspace, TallyVoucher, V2_TABLES
from v2.cloud.parity.model import PROBLEM_VERDICTS

_OPS_LOGGER = "v2.ops.storage"

# F19 (controller ruling): measured 2026-09-28 on the Task 8c real ingest of company B's FY2022-23 vouchers
# (`p21_B_fy2022_month_01..12.xml`, 240 vouchers / 780 ledger lines / 152 inventory lines / 203 bills) plus its
# full master set (`realdata.b_masters()`), posted through the real `/batches` endpoint against
# TEST_DATABASE_URL — `SELECT round(avg(pg_column_size(t.*)))::int FROM <table> t` per v2 table, one representative
# row for the tables that capture leaves empty (`tally_stock_groups`, `sync_commands`, `sync_quarantine`) shaped
# like the real inserts in `commands.py` / `pipeline.py._record_quarantine`, and one representative
# `parity_runs`/`parity_lines`/`tally_report_snapshots` row shaped like `parity/engine.py`'s / the snapshot
# handler's real writes (a full parity run wasn't re-run for this measurement). See
# `.superpowers/sdd/2026-09-25-bi-s1-cloud-plan/task-11-report.md` for the measurement script.
AVG_ROW_BYTES: dict[str, int] = {
    "parity_lines": 184,
    "tally_bill_allocations": 184,
    "tally_voucher_inventory_lines": 221,
    "tally_voucher_ledger_lines": 175,
    "parity_runs": 256,
    "tally_vouchers": 372,
    "sync_batches": 504,
    "sync_runs": 192,
    "sync_workspaces": 216,
    "agent_devices": 200,
    "sync_commands": 109,
    "sync_quarantine": 224,
    "sync_fy_coverage": 103,
    "tally_currencies": 369,
    "tally_groups": 386,
    "tally_voucher_types": 417,
    "tally_ledgers": 574,
    "tally_stock_groups": 284,
    "tally_units": 343,
    "tally_stock_items": 497,
    "tally_report_snapshots": 408,
}


@dataclass
class SliceReport:
    """Final shape (controller ruling F18) so Task 11 only has to fill in the numbers, never change the type."""

    ran: bool = False
    raw_nulled: int = 0
    parity_runs_pruned: int = 0
    parity_lines_pruned: int = 0
    batches_pruned: int = 0
    storage_estimate_bytes: int = 0
    storage_alert: bool = False
    elapsed_s: float = 0.0


class _Budget:
    """Wall-clock budget shared across a slice's sub-steps (§4.9: bounded by rows AND time)."""

    def __init__(self, seconds: float):
        self._start = time.monotonic()
        self._seconds = seconds

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def has_time(self) -> bool:
        return self.elapsed() < self._seconds


async def _purge_raw_slice(session: AsyncSession, ws_id: uuid.UUID, clock: Clock, limit: int) -> int:
    """Q22: null `raw` for vouchers whose FY has rolled out of the window (`raw_window_fys` — the single shared
    definition, controller ruling 3), bounded by `limit`. Vouchers ingest already wrote with `raw = NULL` are
    untouched (the `raw IS NOT NULL` filter)."""
    today_ist = ist_date(clock.now())
    window = await raw_window_fys(session, ws_id, today_ist)
    low, high_end = min(window), fy_end_of(max(window))
    t = TallyVoucher.__table__
    ids = (
        await session.execute(
            select(t.c.id)
            .where(
                t.c.workspace_id == ws_id,
                t.c.raw.is_not(None),
                (t.c.date < low) | (t.c.date > high_end),
            )
            .limit(limit)
        )
    ).scalars().all()
    if not ids:
        return 0
    # `null()` -- NOT Python `None` -- because SQLAlchemy's JSON(B) type by default treats a bound `None` as the
    # JSON literal `null` (`none_as_null=False`), not SQL NULL; `raw IS NOT NULL` (this function's own filter,
    # and Q22's whole point) only excludes an actual SQL NULL. Confirmed the hard way: `.values(raw=None)` left
    # `pg_typeof/IS NOT NULL` unchanged despite `rowcount` reporting the update — see task-11-report.md.
    await session.execute(update(t).where(t.c.id.in_(ids)).values(raw=null()))
    return len(ids)


def _protected_run_ids(sw: SyncWorkspace) -> set[uuid.UUID]:
    """The two run ids maintenance must never prune (controller ruling 5): the run `last_parity` points at, and
    the ladder's `last_run_id`."""
    protected: set[uuid.UUID] = set()
    for source in (sw.last_parity, sw.ladder):
        run_id = (source or {}).get("run_id") or (source or {}).get("last_run_id")
        if run_id:
            protected.add(uuid.UUID(run_id))
    return protected


async def _prune_parity_slice(session: AsyncSession, sw: SyncWorkspace, clock: Clock, limit: int) -> tuple[int, int]:
    """Q21 (90 / 7 / 90): clean (non-problem-verdict) lines older than 7 days are pruned directly; runs older
    than 90 days are pruned (cascading their remaining lines, including any 90-day-old problem lines) unless
    protected (`_protected_run_ids`). Returns `(runs_pruned, lines_pruned)` — lines pruned counts both the direct
    7-day prune and whatever cascaded off a pruned run."""
    now = clock.now()
    t_lines = ParityLine.__table__
    t_runs = ParityRun.__table__

    line_ids = (
        await session.execute(
            select(t_lines.c.id)
            .where(
                t_lines.c.workspace_id == sw.workspace_id,
                t_lines.c.created_at < now - timedelta(days=7),
                t_lines.c.verdict.notin_(PROBLEM_VERDICTS),
            )
            .limit(limit)
        )
    ).scalars().all()
    lines_pruned = 0
    if line_ids:
        await session.execute(delete(t_lines).where(t_lines.c.id.in_(line_ids)))
        lines_pruned += len(line_ids)

    protected = _protected_run_ids(sw)
    run_q = select(t_runs.c.id).where(
        t_runs.c.workspace_id == sw.workspace_id, t_runs.c.created_at < now - timedelta(days=90)
    )
    if protected:
        run_q = run_q.where(t_runs.c.id.notin_(protected))
    run_ids = (await session.execute(run_q.limit(limit))).scalars().all()
    runs_pruned = 0
    if run_ids:
        cascaded = (
            await session.execute(
                select(func.count()).select_from(t_lines).where(t_lines.c.run_id.in_(run_ids))
            )
        ).scalar_one()
        await session.execute(delete(t_runs).where(t_runs.c.id.in_(run_ids)))
        runs_pruned = len(run_ids)
        lines_pruned += cascaded

    return runs_pruned, lines_pruned


async def _prune_batches_slice(session: AsyncSession, ws_id: uuid.UUID, clock: Clock, limit: int) -> int:
    """Batch-log retention: 90 days."""
    t = SyncBatch.__table__
    ids = (
        await session.execute(
            select(t.c.id)
            .where(t.c.workspace_id == ws_id, t.c.created_at < clock.now() - timedelta(days=90))
            .limit(limit)
        )
    ).scalars().all()
    if not ids:
        return 0
    await session.execute(delete(t).where(t.c.id.in_(ids)))
    return len(ids)


async def _storage_estimate(session: AsyncSession, ws_id: uuid.UUID) -> int:
    """Q23: Σ row counts × `AVG_ROW_BYTES[table]` across this workspace's own rows in every v2 table."""
    total = 0
    for table in V2_TABLES:
        n = (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :w"), {"w": ws_id}  # noqa: S608 (table from V2_TABLES, not user input)
            )
        ).scalar_one()
        total += n * AVG_ROW_BYTES.get(table, 0)
    return total


def _storage_alert_event(ws_id: uuid.UUID, estimate_bytes: int, threshold_bytes: int) -> dict:
    """Q23 ops signal: counts only (decision 14 discipline, `parity/opsignal.py`) — no names, no GUIDs, just the
    workspace id (already opaque) and the two byte counts that crossed."""
    return {"workspace_id": str(ws_id), "event": "storage_alert", "estimate_bytes": estimate_bytes,
            "threshold_bytes": threshold_bytes}


async def run_slice(
    session: AsyncSession, sw: SyncWorkspace, settings: V2Settings, clock: Clock
) -> SliceReport:
    """D20: one bounded maintenance slice, called from every heartbeat (`state.heartbeat`). Each sub-step is
    skipped once the slice's time budget (`settings.maintenance_slice_seconds`) is spent; the storage estimate
    always runs last (cheap, index-only counts) so a heartbeat that timed out on retention still refreshes the
    alert flag."""
    budget = _Budget(settings.maintenance_slice_seconds)
    limit = settings.maintenance_slice_rows
    report = SliceReport(ran=True)

    if budget.has_time():
        report.raw_nulled = await _purge_raw_slice(session, sw.workspace_id, clock, limit)

    if budget.has_time():
        report.parity_runs_pruned, report.parity_lines_pruned = await _prune_parity_slice(
            session, sw, clock, limit
        )

    if budget.has_time():
        report.batches_pruned = await _prune_batches_slice(session, sw.workspace_id, clock, limit)

    estimate = await _storage_estimate(session, sw.workspace_id)
    alert = estimate > settings.storage_alert_bytes
    sw.storage_estimate_bytes = estimate
    sw.storage_alert = alert
    report.storage_estimate_bytes = estimate
    report.storage_alert = alert
    if alert:
        logging.getLogger(_OPS_LOGGER).info(
            json.dumps(_storage_alert_event(sw.workspace_id, estimate, settings.storage_alert_bytes))
        )

    report.elapsed_s = budget.elapsed()
    return report
