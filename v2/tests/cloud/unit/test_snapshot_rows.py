"""§7.12 report-cell parsing (S1 spec §4.6, §7.12, D3, D8) -- ``parse_cells`` on real S0/S1 captures, no DB.

Controller ruling F2 (task 9 brief): the TB imbalance is Σ first-occurrence primary-group rows + the top-level
``Profit & Loss A/c`` ledger row (if present) + the top-level ``Unadjusted Forex Gain/Loss`` row (if present).
A18's formula omitted the ``Profit & Loss A/c`` row, which gives a false non-zero on a multi-FY company (see
``test_group_tb_with_pl_row_nets_to_zero`` below, company B as-on 31-03-2026: primary rows alone are out by
-14,40,883.90, matching the value A18 warned about; the P&L row brings it back to 0.00).
"""
from decimal import Decimal
from pathlib import Path

import pytest

from v2.cloud.ingest.snapshots import parse_cells
from v2.contract import transcode
from v2.contract.tally_rules import PL_ACCOUNT_LEDGER, PRIMARY_NATURE, UNADJUSTED_FOREX_ROW

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _cells(name, rt):
    return transcode.report_cells((SYNC / name).read_text(encoding="utf-8"), rt)


def test_b_tb_2023_nets_to_zero_with_forex_row():                     # §10.1 item 6 real value
    snap = parse_cells("trial_balance", _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance"))
    assert snap.imbalance == Decimal("0.00")
    assert set(snap.synthetic) == {"Opening Stock", "Unadjusted Forex Gain/Loss"}


def test_b_tb_without_forex_row_is_out_by_18387():                     # LESSONS rule 29(b) + the S1 note
    cells = [c for c in _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance")
             if c["dspdispname"] != "Unadjusted Forex Gain/Loss"]
    assert parse_cells("trial_balance", cells).imbalance == Decimal("183.87")


def test_a_tb_fy_end_imbalance_non_zero():                             # company A seed opening defect (D10)
    # Controller ruling F2, pinned value: primary rows only (no P&L A/c row, no forex row in this capture) --
    # Capital Account 750000.00 + Current Liabilities 1757357.00 + Current Assets 2605093.00 + Sales Accounts
    # 2057650.00 - Purchase Accounts 2521300.00 - Indirect Expenses 1343000.00 = 33,05,800.00 exactly.
    assert parse_cells("trial_balance", _cells("p16_A_tb_fy_end.xml", "trial_balance")).imbalance == \
        Decimal("3305800.00")


def test_group_tb_with_pl_row_nets_to_zero():                          # controller ruling F2 (supersedes A18)
    """Company B as-on 31-03-2026 (`s1_B_tb_group_asof_2026-03-31.xml`) carries a top-level `Profit & Loss A/c`
    ledger row (1440883.90 Cr) alongside the forex row. Primary rows alone sum to -14,40,883.90 (A18's formula,
    which omitted this row, would report that as the imbalance -- a false `discarded_stale` on every multi-FY
    company); the P&L row + the forex row (-183.87) bring the true imbalance to exactly 0.00."""
    cells = _cells("s1_B_tb_group_asof_2026-03-31.xml", "trial_balance")
    snap = parse_cells("trial_balance", cells)
    assert snap.imbalance == Decimal("0.00")
    assert set(snap.synthetic) == {"Opening Stock", "Unadjusted Forex Gain/Loss"}

    seen: set[str] = set()
    primary_total = Decimal("0")
    for c in cells:
        name = c["dspdispname"]
        if name in PRIMARY_NATURE and name not in seen:
            seen.add(name)
            primary_total += (Decimal(c["dspcldramta"]) if c["dspcldramta"] else Decimal("0")) + \
                (Decimal(c["dspclcramta"]) if c["dspclcramta"] else Decimal("0"))
    assert primary_total == Decimal("-1440700.03")                     # primary rows alone: NOT zero
    pl_row = next(c for c in cells if c["dspdispname"] == PL_ACCOUNT_LEDGER)
    assert Decimal(pl_row["dspclcramta"]) == Decimal("1440883.90")
    forex_row = next(c for c in cells if c["dspdispname"] == UNADJUSTED_FOREX_ROW)
    assert Decimal(forex_row["dspcldramta"]) == Decimal("-183.87")


def test_opening_stock_nested_not_added_again():                       # T9: replaces the verbatim-duplicate test
    """A18: `Opening Stock` is nested inside `Current Assets`'s own row, never summed on top of it. Proof: summing
    the first-occurrence PRIMARY rows PLUS both synthetic rows (i.e. double-counting Opening Stock as if it were
    its own primary group, and adding the forex row) gives -24,450.00 on company B as-on 2023-03-31 -- NOT the
    correct 0.00 -- which is exactly why the real formula (§ above) leaves Opening Stock out of the primary sum."""
    cells = _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance")
    seen: set[str] = set()
    total = Decimal("0")
    for c in cells:
        name = c["dspdispname"]
        if (name in PRIMARY_NATURE or name in ("Opening Stock", "Unadjusted Forex Gain/Loss")) and name not in seen:
            seen.add(name)
            total += (Decimal(c["dspcldramta"]) if c["dspcldramta"] else Decimal("0")) + \
                (Decimal(c["dspclcramta"]) if c["dspclcramta"] else Decimal("0"))
    assert total == Decimal("-24450.00")

    snap = parse_cells("trial_balance", cells)
    assert snap.imbalance == Decimal("0.00") and "Opening Stock" in snap.synthetic


def test_both_capital_account_rows_kept_in_order():                    # T9: renamed (only asserts both are kept)
    rows = parse_cells("trial_balance", _cells("p16_A_tb_fy_end.xml", "trial_balance")).rows
    assert [r["name"] for r in rows].count("Capital Account") == 2
    assert rows[0]["name"] == rows[1]["name"] == "Capital Account"
    assert rows[0]["amount"] == rows[1]["amount"] == "750000.00"


@pytest.mark.parametrize("fixture, rt", [
    ("p12_A_stock_summary_today.xml", "stock_summary"), ("p23_B_bills_receivable_due.xml", "bills_receivable"),
    ("p12_A_bs_today.xml", "balance_sheet"), ("p12_A_pl_fy2025.xml", "profit_and_loss"),
    ("p17_A_tb_exploded_isledgerwise.xml", "trial_balance_ledgerwise"),
])
def test_every_report_type_parses(fixture, rt):
    assert parse_cells(rt, _cells(fixture, rt)).rows


def test_a_bills_residuals_match_seed_anchors():                        # Part 1 §8: ₹9,70,537 / ₹18,34,142
    rec = parse_cells("bills_receivable", _cells("p12_A_bills_receivable_today.xml", "bills_receivable")).rows
    pay = parse_cells("bills_payable", _cells("p12_A_bills_payable_today.xml", "bills_payable")).rows
    # The capture's own total is exact to the paise -- both happen to equal the rupee-rounded seed anchors exactly.
    assert abs(sum(Decimal(r["amount"]) for r in rec)) == Decimal("970537.00")
    assert abs(sum(Decimal(r["amount"]) for r in pay)) == Decimal("1834142.00")


def test_unknown_report_type_raises():
    with pytest.raises(ValueError):
        parse_cells("not_a_report", [])


def test_unstated_forex_expression_amount_is_unparseable():
    """D3 for snapshots: an expression cell without a stated `= base` is `unparseable_amount` -- S1 never
    revalues a forex figure server-side."""
    from v2.contract.parse import WireParseError

    cells = [{"dspdispname": "USD Debtor", "dspcldramta": "$1609.71 @ ?82.58/$", "dspclcramta": ""}]
    with pytest.raises(WireParseError):
        parse_cells("trial_balance", cells)
