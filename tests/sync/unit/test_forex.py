"""Forex ledgers -- C47 (S1 spec §10.5, D3, D4, D30; LESSONS rules 28-29; controller rulings F3, F25, T10a).

Company B real numbers (§10.5): the two USD sales in `p21_B_fy2022_month_09.xml` (= `p22_B_forex_sales.xml`),
the G2 ledger-level TB `s1_B_tb_ledger_asof_2023-03-31.xml` and the group TB `p18_B_tb_asof_2023-03-31.xml`.

`USD_FY2022_MIRROR` below is NOT loaded from `s1_B_usd_ledger.xml` / `p22_B_usd_ledger.xml` (ruling F3: those are
FY 2025-26 masters -- their OpeningBalance is the CURRENT-FY opening, C46 -- and are never paired with a 2023
as-on). It is what a mirror taken while FY 2022-23 was Tally's current period states: opening face 0 (the dataset
opens the USD party at nil) and closing -$1609.71 @ 82.58 = -132929.85 (the sales' face total at the latest rate).
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

from backend.sync.ingest.resolve import NameIndex
from backend.sync.ingest.snapshots import parse_cells
from backend.sync.parity.forex import face_applies, face_check, forex_lines, scope_face, set_rule
from backend.sync.parity.model import LedgerIn, Sums, build_sums
from contract.parse import amount as parse_amount
from tests.sync import parity_realdata as prd
from tests.sync import realdata

TOL = D("1.00")
USD_FY2022_MIRROR = LedgerIn(
    guid="usd", name="Gulf Office Supplies LLC (USD)", group_guid="sd", primary_group="Current Assets",
    nature="assets", is_forex=True, mirrored_closing=D("-132929.85"), closing_fx=D("-1609.71"),
    closing_fx_rate=D("82.58"), opening_fx=D("0"), balance_source="tally", in_capture=True)
USD = USD_FY2022_MIRROR
SUMS = Sums(total={"usd": D("-133113.72")}, face={"usd": D("-1609.71")}, face_complete={"usd": True},
            fy_total={"usd": D("-133113.72")})


def _b_index() -> NameIndex:
    return NameIndex.from_rows([("ledger", m["data"]["guid"], m["data"]["name"], False)
                                for m in realdata.b_masters() if m["kind"] == "ledger"])


def test_sums_constants_are_the_real_fy2022_lines():                    # ties SUMS to the captures
    index = _b_index()
    usd = index.resolve("ledger", "Gulf Office Supplies LLC (USD)")
    real = build_sums(prd.b_fy2022_facts(index), verified_edge=date(2022, 4, 1), as_on=date(2023, 3, 31))
    assert (real.total[usd], real.face[usd], real.face_complete[usd], real.fy_total[usd]) == \
        (SUMS.total["usd"], SUMS.face["usd"], True, SUMS.fy_total["usd"])


def test_face_and_self_consistency_hold_on_real_numbers():
    assert face_check(USD, SUMS) == (True, None)


def test_face_mismatch_when_a_usd_sale_is_missing():                     # §15.5 "forex sale missing"
    missing = Sums(total={"usd": D("-37216.04")}, face={"usd": D("-448.44")}, face_complete={"usd": True},
                   fy_total={"usd": D("-37216.04")})
    assert face_check(USD, missing) == (False, "forex_face_mismatch")


def test_self_check_mismatch_when_face_times_rate_is_not_the_base():
    assert face_check(replace(USD, mirrored_closing=D("-132929.00")), SUMS) == (False, "forex_face_mismatch")


def test_plain_inr_line_on_forex_ledger_face_incomplete():
    partial = Sums(total=SUMS.total, face=SUMS.face, face_complete={"usd": False}, fy_total=SUMS.fy_total)
    assert face_check(USD, partial) == (False, "forex_face_incomplete")


def test_set_rule_accepts_18387_against_unadjusted_row():
    assert set_rule({"usd": D("183.87")}, D("-183.87"), TOL)


def test_set_rule_rejects_without_unadjusted_row():
    assert not set_rule({"usd": D("183.87")}, None, TOL)


def test_forex_lines_real_b_match_revalued_and_total():
    lines, total = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": D("-132929.85")}, D("-183.87"), TOL)
    (line,) = lines
    assert (line.verdict, line.unrealised, total) == ("match_revalued", D("183.87"), D("183.87"))
    assert (line.our, line.tally, line.our_fx, line.tally_fx) == (D("-133113.72"), D("-132929.85"),
                                                                 D("-1609.71"), D("-1609.71"))


def test_forex_lines_unexplained_when_row_removed():                     # §16 "row removed" case
    lines, total = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": D("-132929.85")}, None, TOL)
    assert (lines[0].verdict, lines[0].cause, total) == ("mismatch", "forex_revaluation_unexplained", D("0"))


def test_forex_lines_face_mismatch_wins_over_accepted_set():             # (a) mismatch is never excused by (b)
    missing = Sums(total={"usd": D("-37216.04")}, face={"usd": D("-448.44")}, face_complete={"usd": True},
                   fy_total={"usd": D("-37216.04")})
    lines, total = forex_lines([USD], {"usd": D("0")}, missing, {"usd": D("-37216.04")}, None, TOL)
    assert (lines[0].verdict, lines[0].cause, total) == ("mismatch", "forex_face_mismatch", D("0"))


def test_forex_lines_no_ledger_anchor():
    lines, total = forex_lines([USD], None, SUMS, {"usd": D("-132929.85")}, D("-183.87"), TOL)
    assert (lines[0].verdict, lines[0].cause, total) == ("not_applicable", "no_ledger_anchor", D("0"))


def test_tb_row_expression_gives_stated_base_and_g2_row_is_plain():      # T10a (replaces the no-op S1-R2 test)
    expression_cell = {"dspdispname": USD.name, "dspcldramta": "-$1609.71 @ ? 82.58/$ = -? 132929.85"}
    (expr_row,) = parse_cells("trial_balance_ledgerwise", [expression_cell]).rows
    assert D(expr_row["amount"]) == D("-132929.85")                      # the stated INR base after "=" (D3)
    g2 = {r.name: r.amount for r in prd.tb("s1_B_tb_ledger_asof_2023-03-31.xml")}
    assert g2[USD.name] == D("-132929.85")                               # the real G2 row: a PLAIN number
    for tally in (D(expr_row["amount"]), g2[USD.name]):
        lines, total = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": tally}, D("-183.87"), TOL)
        assert (lines[0].verdict, total) == ("match_revalued", D("183.87"))


def _current_fy_usd() -> LedgerIn:
    """The real FY 2025-26 USD master (`s1_B_usd_ledger.xml`, G4): opening = closing = -$1609.71 @ 82.58."""
    (m,) = realdata.masters("s1_B_usd_ledger.xml", "ledger")
    opening, closing = parse_amount(m["data"]["openingbalance"]), parse_amount(m["data"]["closingbalance"])
    return LedgerIn(guid=m["data"]["guid"], name=m["data"]["name"], group_guid="Sundry Debtors",
                    primary_group="Current Assets", nature="assets", is_forex=True, mirrored_closing=closing.inr,
                    closing_fx=closing.fx_amount, closing_fx_rate=closing.fx_rate, opening_fx=opening.fx_amount,
                    balance_source="tally", in_capture=True)


def test_real_current_fy_face_check_on_s1_usd_ledger():                  # ruling F3: same FY -> (a) runs
    usd = _current_fy_usd()
    assert (usd.opening_fx, usd.closing_fx, usd.closing_fx_rate) == (D("-1609.71"), D("-1609.71"), D("82.58"))
    assert face_applies(prd.TALLY_CURRENT_FY_END, prd.TALLY_CURRENT_FY_END)
    scoped = scope_face(usd, prd.TALLY_CURRENT_FY_END, prd.TALLY_CURRENT_FY_END)
    assert scoped == usd
    # FY 2025-26 carries no USD voucher (the master's current-FY opening equals its closing): face sum 0
    assert face_check(scoped, Sums(total={}, face={}, face_complete={}, fy_total={})) == (True, None)


def test_2023_as_on_never_false_face_mismatch():                         # ruling F3
    usd = _current_fy_usd()
    index = _b_index()
    real = build_sums(prd.b_fy2022_facts(index), verified_edge=date(2022, 4, 1), as_on=date(2023, 3, 31))
    real = Sums(total={"u": real.total[usd.guid]}, face={"u": real.face[usd.guid]},
                face_complete={"u": real.face_complete[usd.guid]}, fy_total={"u": real.fy_total[usd.guid]})
    usd = replace(usd, guid="u")
    # the trap: the current-FY opening face (-1609.71) + FY 2022-23's face lines (-1609.71) != -1609.71
    assert face_check(usd, real) == (False, "forex_face_mismatch")
    # F3: a 2023 as-on is not the mirrored capture's FY (2025-26) -> no face fields -> (b) carries the ledger
    assert not face_applies(date(2023, 3, 31), prd.TALLY_CURRENT_FY_END)
    scoped = scope_face(usd, date(2023, 3, 31), prd.TALLY_CURRENT_FY_END)
    assert (scoped.opening_fx, scoped.closing_fx, scoped.closing_fx_rate) == (None, None, None)
    lines, total = forex_lines([scoped], {"u": D("0")}, real, {"u": D("-132929.85")}, D("-183.87"), TOL)
    assert (lines[0].verdict, lines[0].cause, total) == ("match_revalued", None, D("183.87"))


def test_face_applies_uses_tally_period_not_ist_today():                 # ruling F25
    # Tally's current period ends 31-03-2026 although the IST-current FY is 2026-27
    assert face_applies(date(2025, 10, 31), date(2026, 3, 31))
    assert not face_applies(date(2026, 9, 28), date(2026, 3, 31))


def test_pl_account_with_expression_balance_is_never_a_forex_line():     # company B's real P&L A/c master
    pl = replace(USD, guid="pl", name="Profit & Loss A/c", primary_group=None, nature=None)
    lines, total = forex_lines([pl], {}, SUMS, {}, None, TOL)
    assert (lines, total) == ([], D("0"))


def test_forex_ledger_absent_from_the_tb_is_zero_never_the_mirrored_closing():
    """10c carry (10a must-fix): absent from the ledger-level TB means 0.00 there -- rung 1's order. The mirrored
    closing is the CURRENT period's figure and is wrong for a bisect month-end or a past FY as-on."""
    (line,), _ = forex_lines([USD], {"usd": D("0")}, SUMS, {}, None, TOL)
    assert line.tally == D("0.00")
    assert line.tally != USD.mirrored_closing
    assert (line.diff, line.verdict, line.cause) == (D("133113.72"), "mismatch", "forex_revaluation_unexplained")


def test_zero_diff_under_an_accepted_set_is_a_plain_match():
    """10c: an E−1 anchor row is already Tally's revalued figure, so with no forex line since the anchor the ledger
    agrees exactly -- `match`, not `match_revalued` carrying 0.00 (§15.5 row 1: BS `match`)."""
    sums = Sums(total={}, face={}, face_complete={}, fy_total={})
    usd = replace(USD, opening_fx=D("-1609.71"))
    (line,), total = forex_lines([usd], {"usd": D("-132929.85")}, sums, {"usd": D("-132929.85")}, D("0"), TOL)
    assert (line.verdict, line.unrealised, line.diff, total) == ("match", None, D("0.00"), D("0"))
