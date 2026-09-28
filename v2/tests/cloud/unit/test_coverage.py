"""FY coverage rows created at bind (S1 spec §8.3, task-5 brief step 1)."""
from __future__ import annotations

from datetime import date

from v2.cloud.sync.coverage import FyRow, fy_rows_for_bind, months_between


def test_months_between_inclusive():
    assert months_between(date(2022, 4, 1), date(2023, 3, 31)) == 12
    assert months_between(date(2025, 4, 1), date(2025, 9, 25)) == 6


def test_company_b_rows_from_books_from_to_current_fy():
    rows = fy_rows_for_bind(date(2022, 4, 1), date(2026, 9, 25), None)
    assert [r.fy_start for r in rows] == [date(2022, 4, 1), date(2023, 4, 1), date(2024, 4, 1),
                                          date(2025, 4, 1), date(2026, 4, 1)]
    assert rows[-1].months_total == 6 and rows[-2].months_total == 12 and rows[0].months_total == 12


def test_books_from_mid_year_counts_from_books_from():
    rows = fy_rows_for_bind(date(2025, 10, 1), date(2026, 9, 25), None)
    assert rows[0] == FyRow(date(2025, 4, 1), date(2026, 3, 31), 6)


def test_current_fy_uses_ist_date_at_utc_evening_of_31_march():       # Review Focus 4
    from datetime import datetime, timezone
    from v2.cloud.clock import FixedClock, ist_date
    today = ist_date(FixedClock(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)).now())
    rows = fy_rows_for_bind(date(2025, 4, 1), today, None)
    assert rows[-1].fy_start == date(2026, 4, 1) and rows[-1].months_total == 1


def test_first_sync_month_caps_window_fys_only():
    """A first_sync_month earlier than today caps the current AND previous FY's months_total, but never an
    older FY (which always runs to its own fy_end regardless of first_sync_month)."""
    rows = fy_rows_for_bind(date(2022, 4, 1), date(2026, 9, 25), date(2026, 6, 15))
    by_start = {r.fy_start: r for r in rows}
    assert by_start[date(2026, 4, 1)].months_total == 3   # Apr, May, Jun
    assert by_start[date(2025, 4, 1)].months_total == 12  # previous FY still runs to its own fy_end
    assert by_start[date(2022, 4, 1)].months_total == 12  # older FY unaffected


def test_single_fy_when_books_from_is_current_fy():
    rows = fy_rows_for_bind(date(2026, 6, 1), date(2026, 9, 25), None)
    assert len(rows) == 1
    assert rows[0] == FyRow(date(2026, 4, 1), date(2027, 3, 31), 4)
