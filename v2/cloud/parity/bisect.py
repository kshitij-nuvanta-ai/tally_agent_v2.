"""Month-bisect evaluation (S1 spec §10.9). Pure: the caller has already evaluated rungs 1-2 at every stored
month-end ledger-level TB in the FY, oldest first; this just finds the first one that diverged."""
from __future__ import annotations

from datetime import date


def first_diverging_month(evaluations: list[tuple[date, bool]]) -> date | None:
    """``evaluations`` is ``(month_end, had_mismatch)`` ordered oldest first. Returns the first month-end with a
    mismatch, or ``None`` if every stored month-end that FY was clean."""
    for month_end, had_mismatch in evaluations:
        if had_mismatch:
            return month_end
    return None
