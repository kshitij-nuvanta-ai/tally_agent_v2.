"""Parity rung 1 -- ledger level, balance-sheet ledgers (S1 spec §10.4, D3, D9, D11, §15.5)."""
from decimal import Decimal as D

import pytest

from v2.cloud.parity.model import LedgerIn, Sums
from v2.cloud.parity.rung1 import rung1
from v2.contract.parse import amount as parse_amount
from v2.tests.cloud import parity_realdata as prd
from v2.tests.cloud import realdata

TOL = D("1.00")


def L(guid, nature="assets", closing=D("-100.00"), **kw):
    base = dict(guid=guid, name=guid, group_guid="g", primary_group="Current Assets", nature=nature, is_forex=False,
                mirrored_closing=closing, closing_fx=None, closing_fx_rate=None, opening_fx=None,
                balance_source="tally", in_capture=True)
    base.update(kw)
    return LedgerIn(**base)


def S(total):
    return Sums(total=total, face={}, face_complete={}, fy_total={})


@pytest.mark.parametrize("ours, verdict", [(D("-99.01"), "match"), (D("-99.00"), "match"), (D("-98.99"), "mismatch")])
def test_tolerance_boundaries(ours, verdict):                           # §15.5 last row: 0.99 / 1.00 / 1.01
    (line,) = rung1([L("a")], {"a": D("0")}, S({"a": ours}), {}, [], TOL)
    assert line.verdict == verdict


def test_debit_is_negative_anchor_plus_lines():
    (line,) = rung1([L("a", closing=D("-150.00"))], {"a": D("-100.00")}, S({"a": D("-50.00")}), {}, [], TOL)
    assert (line.our, line.tally, line.verdict) == (D("-150.00"), D("-150.00"), "match")


def test_diff_is_tally_minus_ours():
    (line,) = rung1([L("a", closing=D("-150.00"))], {"a": D("-100.00")}, S({"a": D("-40.00")}), {}, [], TOL)
    assert (line.diff, line.verdict) == (D("-10.00"), "mismatch")


def test_nominal_ledger_is_not_applicable_never_match():
    (line,) = rung1([L("s", nature="income")], {"s": D("0")}, S({"s": D("-100.00")}), {}, [], TOL)
    assert (line.verdict, line.cause) == ("not_applicable", "nominal")


def test_pl_account_not_applicable():                                   # D11
    (line,) = rung1([L("pl", name="Profit & Loss A/c", nature="liabilities")], {}, S({}), {}, [], TOL)
    assert (line.verdict, line.cause) == ("not_applicable", "pl_account")


def test_pl_account_not_applicable_even_when_its_balance_made_it_forex():  # company B's P&L A/c is an expression
    (line,) = rung1([L("pl", name="Profit & Loss A/c", nature=None, primary_group=None, is_forex=True)],
                    {}, S({}), {}, [], TOL)
    assert (line.verdict, line.cause) == ("not_applicable", "pl_account")


def test_unclassified_group_not_applicable():
    (line,) = rung1([L("u", nature=None)], {}, S({}), {}, [], TOL)
    assert line.cause == "unclassified_group"


def test_no_ledger_anchor_route_makes_bs_ledgers_not_applicable():      # §15.5 row 12
    lines = rung1([L("a"), L("b")], None, S({"a": D("-100"), "b": D("0")}), {}, [], TOL)
    assert {(l.verdict, l.cause) for l in lines} == {("not_applicable", "no_ledger_anchor")}


def test_needs_tb_takes_tally_figure_from_ledgerwise_tb():              # D3 ledger rule
    (line,) = rung1([L("a", mirrored_closing=None, balance_source="needs_tb")], {"a": D("0")},
                    S({"a": D("-100.00")}), {"a": D("-100.00")}, [], TOL)
    assert line.verdict == "match" and line.tally == D("-100.00")


def test_blank_closing_is_none_and_falls_back_to_ledgerwise_tb():       # ruling F14, G5 `Export Sales` shape
    export = next(o["data"] for o in realdata.g5_ledger_balances() if o["data"]["name"] == "Export Sales")
    assert export["closingbalance"] == "" and parse_amount(export["closingbalance"]) is None   # never 0
    blank = parse_amount(export["closingbalance"])
    # a BS ledger with that shape: absent from the ledger-level TB -> Tally figure 0.00, never "no figure"
    (absent,) = rung1([L("a", mirrored_closing=blank)], {}, S({}), {}, [], TOL)
    assert (absent.tally, absent.our, absent.verdict) == (D("0.00"), D("0"), "match")
    (absent_off,) = rung1([L("a", mirrored_closing=blank)], {}, S({"a": D("-5.00")}), {}, [], TOL)
    assert absent_off.verdict == "mismatch"
    # ... and present in the ledger-level TB -> that row is the Tally figure
    (present,) = rung1([L("a", mirrored_closing=blank)], {}, S({"a": D("-7.00")}), {"a": D("-7.00")}, [], TOL)
    assert (present.tally, present.verdict) == (D("-7.00"), "match")


def test_ledger_absent_from_capture_missing_in_tally():
    (line,) = rung1([L("a", in_capture=False)], {"a": D("0")}, S({}), {}, [], TOL)
    assert line.verdict == "missing_in_tally"


def test_tb_row_resolving_to_no_ledger_missing_in_db():
    lines = rung1([], {}, S({}), {}, ["Ghost Ledger"], TOL)
    assert [(l.name, l.verdict) for l in lines] == [("Ghost Ledger", "missing_in_db")]


def test_unresolved_name_reported_once():
    lines = rung1([], {}, S({}), {}, ["Ghost Ledger", "Ghost Ledger"], TOL)
    assert len(lines) == 1


def test_anchor_absent_ledger_counts_zero():
    (line,) = rung1([L("a", closing=D("-50.00"))], {}, S({"a": D("-50.00")}), {}, [], TOL)
    assert line.verdict == "match"


def test_mid_backfill_anchor_correct_all_time_far_off_is_match():        # R30 regression (§15.5)
    """All-time Σ lines would be off by lakhs (older FYs not synced); the anchor at E-1 absorbs it."""
    (line,) = rung1([L("a", closing=D("-900000.00"))], {"a": D("-850000.00")}, S({"a": D("-50000.00")}), {}, [], TOL)
    assert line.verdict == "match"


def test_forex_bs_ledger_is_left_to_forex_lines():                       # §10.4 "not forex (forex -> §10.5)"
    assert rung1([L("usd", is_forex=True)], {"usd": D("0")}, S({}), {}, [], TOL) == []


def test_real_a_every_bs_ledger_matches():                               # §15.5 row 1, company A FY 2025-26
    case = prd.case_a()
    lines = prd.run(case).rung1
    bs = [l for l in lines if l.verdict not in ("not_applicable",)]
    assert bs and all(l.verdict == "match" for l in bs), [(l.name, l.our, l.tally) for l in bs if l.verdict != "match"]
    assert {l.name for l in lines if l.cause == "nominal"} >= {"Sales - Electronics", "Rent", "Salaries"}
    assert [l.name for l in lines if l.cause == "pl_account"] == ["Profit & Loss A/c"]
    # F14 on real data: IGST Input's CLOSINGBALANCE is "" -> None -> ledger-level TB -> absent -> 0.00
    igst = next(l for l in lines if l.name == "IGST Input")
    assert (igst.tally, igst.verdict) == (D("0.00"), "match")
