"""FY coverage rows (S1 spec §8.3). Task 5 landed the bind-time piece — ``fy_rows_for_bind``, used to create the
initial ``sync_fy_coverage`` rows when a workspace binds. Task 7 lands the rest of §8.3: the pure coverage state
machine (``COVERAGE_MATRIX`` / ``apply``), the two edges (``edges``) and the backfill copy (``backfill``), plus
the DB-touching glue for ``PATCH /api/sync/{ws}/coverage`` (§7.10) — ``ack_month``, ``add_fy`` and
``persist_edges_and_backfill`` (also used by ``runs.py`` after a whole-company / single-FY resync start, since
those mutate coverage rows too).

§8.3: "Rows created at bind (``pending``) from FY(``books_from``) to the current FY; ``months_total`` counts
months from ``max(fy_start, books_from)`` to ``min(fy_end, the month the first sync started)`` for window FYs,
and to ``fy_end`` for older FYs." The "window FYs" are the current FY and the one immediately before it — the
two FYs a first sync's initial pass covers (Part 1 §4's 2-FY window); every older FY is already fully in the
past, so it always runs to its own ``fy_end`` regardless of ``first_sync_month``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_end_of, fy_start_of, ist_date
from v2.cloud.errors import ApiError
from v2.cloud.models import SyncFyCoverage, SyncWorkspace


def months_between(start: date, end: date) -> int:
    """Inclusive calendar months from ``start``'s month to ``end``'s month (§8.3)."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


@dataclass(frozen=True)
class FyRow:
    fy_start: date
    fy_end: date
    months_total: int


def fy_rows_for_bind(books_from: date, today_ist: date, first_sync_month: date | None) -> list[FyRow]:
    """FY(``books_from``)...FY(``today_ist``) inclusive, oldest first."""
    first_fy = fy_start_of(books_from)
    current_fy = fy_start_of(today_ist)
    previous_fy = date(current_fy.year - 1, 4, 1)
    end_ref = first_sync_month or today_ist

    rows: list[FyRow] = []
    fy = first_fy
    while fy <= current_fy:
        fy_end = fy_end_of(fy)
        lo = max(fy, books_from)
        hi = min(fy_end, end_ref) if fy in (current_fy, previous_fy) else fy_end
        rows.append(FyRow(fy, fy_end, months_between(lo, hi)))
        fy = date(fy.year + 1, 4, 1)
    return rows


# --- Task 7: pure coverage state machine (§8.3, §15.4, ambiguity A10) ------------------------------------------


@dataclass(frozen=True)
class Cov:
    fy_start: date
    state: str
    months_done: list[str]
    months_total: int


# T7 (controller ruling): one flat literal of all 24 cells — no identity dict comprehension.
COVERAGE_MATRIX: dict[tuple[str, str], str] = {
    ("pending", "month_ack"): "running",
    ("pending", "last_month_ack"): "complete",
    ("pending", "company_resync_start"): "pending",
    ("pending", "fy_resync_start"): "pending",
    ("pending", "add_fy"): "pending",
    ("pending", "ack_replay"): "running",
    ("running", "month_ack"): "running",
    ("running", "last_month_ack"): "complete",
    ("running", "company_resync_start"): "pending",
    ("running", "fy_resync_start"): "pending",
    ("running", "add_fy"): "running",
    ("running", "ack_replay"): "running",
    ("resyncing", "month_ack"): "resyncing",
    ("resyncing", "last_month_ack"): "complete",
    ("resyncing", "company_resync_start"): "resyncing",
    ("resyncing", "fy_resync_start"): "resyncing",
    ("resyncing", "add_fy"): "resyncing",
    ("resyncing", "ack_replay"): "resyncing",
    ("complete", "month_ack"): "complete",
    ("complete", "last_month_ack"): "complete",
    ("complete", "company_resync_start"): "resyncing",
    ("complete", "fy_resync_start"): "resyncing",
    ("complete", "add_fy"): "complete",
    ("complete", "ack_replay"): "complete",
}


def apply(cov: Cov, event: str, month: str | None = None) -> Cov:
    """Pure §8.3 transition. ``month_ack`` derives ``last_month_ack`` / ``ack_replay`` from the data (A10);
    every other event is a direct ``COVERAGE_MATRIX`` lookup. ``company_resync_start`` / ``fy_resync_start``
    reset ``months_done`` to ``[]`` (except from ``pending``, which is already empty)."""
    if event == "month_ack":
        replay = month in cov.months_done
        done = sorted(set(cov.months_done) | {month})
        last = not replay and len(done) >= cov.months_total
        key = "ack_replay" if replay and cov.state != "pending" else ("last_month_ack" if last else "month_ack")
        return Cov(cov.fy_start, COVERAGE_MATRIX[(cov.state, key)], done, cov.months_total)
    new_state = COVERAGE_MATRIX[(cov.state, event)]
    done = [] if event in ("company_resync_start", "fy_resync_start") else list(cov.months_done)
    return Cov(cov.fy_start, new_state, done, cov.months_total)


def edges(rows: list[Cov], current_fy: date) -> tuple[date | None, date | None]:
    """§8.3 the two edges: walk from ``current_fy`` backwards over contiguous FY starts (one calendar year at a
    time — FY starts are always 1 April). The available edge accepts ``complete | resyncing``; the verified edge
    accepts ``complete`` only. Each stops (independently) at the first row that fails its own test, or at the
    first missing FY (a gap)."""
    by_start = {r.fy_start: r for r in rows}
    available: date | None = None
    verified: date | None = None
    avail_stopped = False
    verif_stopped = False
    fy = current_fy
    while True:
        row = by_start.get(fy)
        if row is None:
            break
        if not avail_stopped:
            if row.state in ("complete", "resyncing"):
                available = fy
            else:
                avail_stopped = True
        if not verif_stopped:
            if row.state == "complete":
                verified = fy
            else:
                verif_stopped = True
        if avail_stopped and verif_stopped:
            break
        fy = date(fy.year - 1, 4, 1)
    return available, verified


def backfill(rows: list[Cov], books_from: date, current_fy: date) -> tuple[str, Decimal]:
    """§8.3 the backfill copy. Pre-window = FYs older than the previous FY (i.e. excluding the current FY and
    the one immediately before it — the two FYs a first sync's window covers). ``resyncing`` if any pre-window
    row is ``resyncing`` (a whole-company resync); ``complete`` when the verified edge reaches FY(``books_from``);
    else ``running``. ``percent`` = Σ months acked / Σ months_total over the pre-window rows, quantised to 0.01
    (100.00 when there are no pre-window rows — a company younger than the window)."""
    previous_fy = date(current_fy.year - 1, 4, 1)
    pre_window = [r for r in rows if r.fy_start < previous_fy]

    _, verified = edges(rows, current_fy)
    books_fy = fy_start_of(books_from)

    if any(r.state == "resyncing" for r in pre_window):
        state = "resyncing"
    elif verified is not None and verified == books_fy:
        state = "complete"
    else:
        state = "running"

    if not pre_window:
        percent = Decimal("100.00")
    else:
        total = sum(r.months_total for r in pre_window)
        if total == 0:
            percent = Decimal("100.00")
        else:
            done = sum(len(r.months_done) for r in pre_window)
            percent = (Decimal(done) / Decimal(total) * 100).quantize(Decimal("0.01"))

    return state, percent


# --- Task 7: DB glue for PATCH /api/sync/{ws}/coverage (§7.10) --------------------------------------------


def to_cov(row: SyncFyCoverage) -> Cov:
    return Cov(row.fy_start, row.state, list(row.months_done), row.months_total)


def write_cov(row: SyncFyCoverage, cov: Cov, now) -> None:
    row.state = cov.state
    row.months_done = cov.months_done
    row.months_complete = len(cov.months_done)
    if cov.state == "complete" and row.completed_at is None:
        row.completed_at = now


async def persist_edges_and_backfill(session: AsyncSession, sw: SyncWorkspace, clock: Clock) -> tuple:
    """Recomputes both edges and the backfill copy from every ``sync_fy_coverage`` row and writes them onto
    ``sync_workspaces`` (``oldest_available_fy``, ``oldest_complete_fy``, ``backfill_state``,
    ``backfill_percent``) — the denormalised copy ``sync_status`` (§7.13) reads without recomputing live.
    Returns ``(available, verified)``. Caller flushes/commits."""
    rows = (
        await session.execute(
            select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == sw.workspace_id)
            .order_by(SyncFyCoverage.fy_start)
        )
    ).scalars().all()
    cov_rows = [to_cov(r) for r in rows]
    current_fy = fy_start_of(ist_date(clock.now()))

    available, verified = edges(cov_rows, current_fy)
    bstate, bpercent = backfill(cov_rows, sw.books_from, current_fy)

    sw.oldest_available_fy = available
    sw.oldest_complete_fy = verified
    sw.backfill_state = bstate
    sw.backfill_percent = bpercent
    return available, verified


def _row_json(row: SyncFyCoverage, edges_pair: tuple) -> dict:
    available, verified = edges_pair
    return {
        "fy_start": row.fy_start.isoformat(),
        "fy_end": row.fy_end.isoformat(),
        "state": row.state,
        "months_total": row.months_total,
        "months_done": row.months_done,
        "months_complete": row.months_complete,
        "edges": {
            "available": available.isoformat() if available else None,
            "verified": verified.isoformat() if verified else None,
        },
    }


async def ack_month(session: AsyncSession, sw: SyncWorkspace, fy_start: date, month: str, clock: Clock) -> dict:
    """§7.10 ``{"fy_start", "month", "run_id"}``: adds the month to ``months_done`` (idempotent via ``apply``'s
    own replay handling), recomputes state, both edges and the backfill copy. ``run_id`` is accepted by the API
    layer but not otherwise checked here (Task 8 owns run-scoped batch/coverage cross-checks)."""
    row = (
        await session.execute(
            select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == sw.workspace_id, SyncFyCoverage.fy_start == fy_start)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "fy_not_found")

    now = clock.now()
    new_cov = apply(to_cov(row), "month_ack", month)
    write_cov(row, new_cov, now)
    await session.flush()

    edges_pair = await persist_edges_and_backfill(session, sw, clock)
    sw.updated_at = now
    await session.flush()
    return _row_json(row, edges_pair)


async def add_fy(session: AsyncSession, sw: SyncWorkspace, fy_start: date, clock: Clock) -> dict:
    """§7.10 rollover ``{"fy_start", "action": "add_fy"}``: a new FY enters the window as a ``complete`` row
    (A11: ``months_total = 0``, ``months_done = []`` — nothing to ack for a brand-new FY). Idempotent: a repeat
    call for an FY that already has a row is a no-op on that row (still recomputes edges/backfill)."""
    now = clock.now()
    row = (
        await session.execute(
            select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == sw.workspace_id, SyncFyCoverage.fy_start == fy_start)
        )
    ).scalar_one_or_none()
    if row is None:
        row = SyncFyCoverage(
            workspace_id=sw.workspace_id,
            fy_start=fy_start,
            fy_end=fy_end_of(fy_start),
            state="complete",
            months_done=[],
            months_complete=0,
            months_total=0,
            completed_at=now,
        )
        session.add(row)
        await session.flush()

    edges_pair = await persist_edges_and_backfill(session, sw, clock)
    sw.updated_at = now
    await session.flush()
    return _row_json(row, edges_pair)
