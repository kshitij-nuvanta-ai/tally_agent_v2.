"""S1 task 10c's FakeBooks knobs, pinned to the LIVE company-B FY 2025-26 Trial Balances (the parity DB tests run on
FakeBooks B, ruling F9): `tb_fy_scoped` (nominal ledgers restart each FY; earlier FYs' profit on a top-level
`Profit & Loss A/c` row) and `unadjusted_forex_row` (the synthetic `Unadjusted Forex Gain/Loss` row)."""
from datetime import date
from decimal import Decimal as D

from backend.sync.ingest.snapshots import parse_cells
from contract import transcode
from tests.sync import parity_fakebooks as pf
from tests.sync import realdata

AS_ON = date(2026, 3, 31)


def _rows(cells) -> dict[str, D]:
    out: dict[str, D] = {}
    for r in parse_cells("trial_balance", cells).rows:
        out.setdefault(r["name"], D(r["amount"]) if r["amount"] is not None else None)
    return out


def test_knobs_reproduce_the_live_fy2025_group_tb_figures():
    fake = _rows(pf.Capture(pf.books()).tb_cells("trial_balance", AS_ON))
    live = _rows(transcode.report_cells(realdata.read_capture("s1_B_tb_group_asof_2026-03-31.xml"), "trial_balance"))
    # the rows FakeBooks can know (no stock valuation, so the purchase/stock-dependent rows differ by design)
    for name in ("Capital Account", "Sales Accounts", "Indirect Expenses", "Unadjusted Forex Gain/Loss"):
        assert fake[name] == live[name], name
    assert live["Sales Accounts"] == D("1084724.72") and live["Unadjusted Forex Gain/Loss"] == D("-183.87")
    assert "Profit & Loss A/c" in fake and "Profit & Loss A/c" in live


def test_knobs_make_every_tb_net_to_zero_like_live_b():
    cap = pf.Capture(pf.books())
    for report in ("trial_balance", "trial_balance_ledgerwise"):
        for as_on in (date(2022, 4, 1), date(2023, 3, 31), date(2025, 3, 31), AS_ON):
            assert parse_cells(report, cap.tb_cells(report, as_on)).imbalance == D("0.00"), (report, as_on)


def test_knobs_off_keep_the_all_time_nominal_rows_and_no_forex_row():
    cap = pf.Capture(pf.books(tb_fy_scoped=False, unadjusted_forex_row=False))
    rows = _rows(cap.tb_cells("trial_balance", AS_ON))
    assert "Unadjusted Forex Gain/Loss" not in rows and "Profit & Loss A/c" not in rows
    assert rows["Sales Accounts"] != D("1084724.72")
