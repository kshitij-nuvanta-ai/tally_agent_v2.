"""FY coverage rows (S1 spec §8.3). This task lands only the bind-time piece — ``fy_rows_for_bind``, used to
create the initial ``sync_fy_coverage`` rows when a workspace binds. The rest of §8.3 (``months_done``,
running/complete transitions, the two edges, backfill denormalisation) is Task 7.

§8.3: "Rows created at bind (``pending``) from FY(``books_from``) to the current FY; ``months_total`` counts
months from ``max(fy_start, books_from)`` to ``min(fy_end, the month the first sync started)`` for window FYs,
and to ``fy_end`` for older FYs." The "window FYs" are the current FY and the one immediately before it — the
two FYs a first sync's initial pass covers (Part 1 §4's 2-FY window); every older FY is already fully in the
past, so it always runs to its own ``fy_end`` regardless of ``first_sync_month``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from v2.cloud.clock import fy_end_of, fy_start_of


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
