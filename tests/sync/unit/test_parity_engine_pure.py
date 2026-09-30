"""The parity engine's pure helpers (task 10c): the D10 comparison, month-ends, the §7.14 summary."""
from datetime import date
from decimal import Decimal as D

from backend.sync.parity.anchors import plan
from backend.sync.parity.engine import effective_edge, imbalance_moved, month_ends, quantize, summary
from backend.sync.parity.model import Line

TOL = D("1.00")


def test_imbalance_compares_as_decimals_at_paise():
    """10c carry (Task 9): "0" and "0.00" are the same imbalance -- never a string compare."""
    assert quantize(D("0")) == quantize(D("0.00")) == D("0.00")
    assert imbalance_moved({"alt_mst_id": 412, "imbalance": "0"}, 412, D("0.00"), TOL) == "same"
    assert imbalance_moved({"alt_mst_id": 412, "imbalance": "0.00"}, 412, D("0"), TOL) == "same"
    assert imbalance_moved({"alt_mst_id": 412, "imbalance": "-183.87"}, 412, D("-183.870"), TOL) == "same"


def test_imbalance_guard_d10():
    base = {"alt_mst_id": 412, "imbalance": "0.00"}
    assert imbalance_moved(base, 412, D("1.00"), TOL) == "same"            # inclusive tolerance
    assert imbalance_moved(base, 412, D("1.01"), TOL) == "stale"
    assert imbalance_moved(base, 412, D("-5.00"), TOL) == "stale"
    assert imbalance_moved(base, 413, D("5.00"), TOL) == "rebaseline"      # the masters changed
    assert imbalance_moved(None, 412, D("5.00"), TOL) == "rebaseline"
    assert imbalance_moved({}, 412, D("5.00"), TOL) == "rebaseline"


def test_month_ends_inclusive_and_leap_aware():
    assert month_ends(date(2023, 4, 1), date(2024, 3, 31))[-2:] == [date(2024, 2, 29), date(2024, 3, 31)]
    assert month_ends(date(2025, 4, 1), date(2025, 6, 29)) == [date(2025, 4, 30), date(2025, 5, 31)]
    assert month_ends(date(2025, 4, 1), date(2025, 4, 29)) == []


def test_summary_counts_compared_lines_only():
    lines = [Line("ledger", "a", "A", D("1"), D("1"), D("0"), "match", None),
             Line("ledger", "b", "B", None, None, None, "not_applicable", "nominal"),
             Line("ledger", "u", "U", D("-2"), D("-1"), D("1"), "match_revalued", None, unrealised=D("1")),
             Line("ledger", None, "Ghost", None, None, None, "missing_in_db", "masters_gap"),
             Line("group", None, "Current Assets", D("5"), D("9"), D("4"), "mismatch", None)]
    assert summary(lines, D("1")) == {"ledgers_compared": 3, "groups_compared": 1, "mismatches": 2,
                                      "match_revalued": 1, "forex_unrealised_total": "1.00"}


def test_effective_edge_never_precedes_books_from():
    """Books beginning mid-FY: the verified FY start precedes books_from -- D9's `plan` would refuse it."""
    mid = date(2022, 6, 15)
    assert effective_edge(date(2022, 4, 1), mid) == mid
    assert plan(effective_edge(date(2022, 4, 1), mid), mid).as_on == mid            # the books-start anchor
    assert effective_edge(date(2023, 4, 1), mid) == date(2023, 4, 1)
