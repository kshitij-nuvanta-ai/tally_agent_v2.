"""FY coverage rows created at bind (S1 spec §8.3, task-5 brief step 1)."""
from __future__ import annotations

from datetime import date

from backend.sync.coverage import FyRow, fy_rows_for_bind, months_between


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
    from backend.sync.clock import FixedClock, ist_date
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


# --- Task 7: COVERAGE_MATRIX, apply, edges, backfill (S1 spec §8.3, §15.4, ambiguity A10) --------------------

import pytest
from decimal import Decimal

from backend.sync.coverage import COVERAGE_MATRIX, Cov, apply, backfill, edges

# (from_state, event) -> to_state; 4 states x 6 events = 24 cells (ambiguity A10)
EXPECTED = {
    ("pending", "month_ack"): "running", ("pending", "last_month_ack"): "complete",
    ("pending", "company_resync_start"): "pending", ("pending", "fy_resync_start"): "pending",
    ("pending", "add_fy"): "pending", ("pending", "ack_replay"): "running",
    ("running", "month_ack"): "running", ("running", "last_month_ack"): "complete",
    ("running", "company_resync_start"): "pending", ("running", "fy_resync_start"): "pending",
    ("running", "add_fy"): "running", ("running", "ack_replay"): "running",
    ("resyncing", "month_ack"): "resyncing", ("resyncing", "last_month_ack"): "complete",
    ("resyncing", "company_resync_start"): "resyncing", ("resyncing", "fy_resync_start"): "resyncing",
    ("resyncing", "add_fy"): "resyncing", ("resyncing", "ack_replay"): "resyncing",
    ("complete", "month_ack"): "complete", ("complete", "last_month_ack"): "complete",
    ("complete", "company_resync_start"): "resyncing", ("complete", "fy_resync_start"): "resyncing",
    ("complete", "add_fy"): "complete", ("complete", "ack_replay"): "complete",
}


def test_matrix_is_the_spec_table():
    assert COVERAGE_MATRIX == EXPECTED


def _row(state, done, total=3):
    return Cov(date(2024, 4, 1), state, list(done), total)


@pytest.mark.parametrize("cell", sorted(EXPECTED))
def test_every_cell(cell):
    state, event = cell
    done = {"pending": [], "running": ["2024-04"], "resyncing": ["2024-04"],
            "complete": ["2024-04", "2024-05", "2024-06"]}[state]
    if event == "month_ack":
        # T7: use 2024-08 for (complete, month_ack) so it never hits the replay path (2024-05 is already done).
        month = "2024-08" if state == "complete" else "2024-05"
        out = apply(_row(state, done), "month_ack", month)
    elif event == "last_month_ack":
        out = apply(_row(state, done, total=len(done) + 1), "month_ack", "2024-07")
    elif event == "ack_replay":
        out = apply(_row(state, done), "month_ack", done[0] if done else "2024-04")
    else:
        out = apply(_row(state, done), event)
    assert out.state == EXPECTED[cell]
    if event in ("company_resync_start", "fy_resync_start") and state != "pending":
        assert out.months_done == []


def test_replay_counts_once():                                        # §14 scenario 12
    r = apply(apply(_row("running", ["2024-04"]), "month_ack", "2024-05"), "month_ack", "2024-05")
    assert r.months_done == ["2024-04", "2024-05"]


def test_edges_available_counts_resyncing_verified_does_not():        # §14 scenario 13
    rows = [Cov(date(2023, 4, 1), "complete", [], 12), Cov(date(2024, 4, 1), "resyncing", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert edges(rows, date(2026, 4, 1)) == (date(2023, 4, 1), date(2025, 4, 1))


def test_edges_stop_at_first_gap():
    rows = [Cov(date(2023, 4, 1), "complete", [], 12), Cov(date(2024, 4, 1), "pending", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert edges(rows, date(2026, 4, 1)) == (date(2025, 4, 1), date(2025, 4, 1))


def test_edges_none_when_current_fy_not_complete():
    rows = [Cov(date(2026, 4, 1), "running", ["2026-04"], 6)]
    assert edges(rows, date(2026, 4, 1)) == (None, None)


def test_backfill_young_company_is_100_complete():
    rows = [Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert backfill(rows, date(2025, 4, 1), date(2026, 4, 1)) == ("complete", Decimal("100.00"))


def test_backfill_percent_over_pre_window_fys():
    rows = [Cov(date(2022, 4, 1), "pending", [], 12),
            Cov(date(2023, 4, 1), "running", ["2023-04", "2023-05", "2023-06"], 12),
            Cov(date(2024, 4, 1), "pending", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    state, pct = backfill(rows, date(2022, 4, 1), date(2026, 4, 1))
    assert state == "running" and pct == Decimal("8.33")         # 3 / 36 pre-window months
