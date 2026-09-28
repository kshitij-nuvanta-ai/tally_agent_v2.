"""Parity rung 2 -- group level with ledger resolution (S1 spec §10.6, D4, D29, LESSONS rule 19), plus the
real-data end-to-end parity of company A FY 2025-26 and company B as-on 31-03-2023 (§15.5 rows 1-2)."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from v2.cloud.parity.forex import forex_lines
from v2.cloud.parity.model import LedgerIn, Line, Sums, TbRow, is_clean
from v2.cloud.parity.rung1 import rung1
from v2.cloud.parity.rung2 import primary_group_rows, rung2
from v2.tests.cloud import parity_realdata as prd

TOL = D("1.00")
LW = {"ISLEDGERWISE": "Yes"}


def L(guid, primary="Current Assets", nature="assets", closing=D("0"), **kw):
    base = dict(guid=guid, name=guid, group_guid="g", primary_group=primary, nature=nature, is_forex=False,
                mirrored_closing=closing, closing_fx=None, closing_fx_rate=None, opening_fx=None,
                balance_source="tally", in_capture=True)
    base.update(kw)
    return LedgerIn(**base)


def S(total=None, fy_total=None):
    return Sums(total=total or {}, face={}, face_complete={}, fy_total=fy_total or {})


def _groups(lines):
    return {l.name: l for l in lines if l.scope == "group"}


def test_primary_group_first_occurrence_only():                          # p16_A: group row, then a LEDGER row
    rows = prd.tb("p16_A_tb_fy_end.xml")
    assert [r.name for r in rows[:2]] == ["Capital Account", "Capital Account"]
    groups = primary_group_rows(rows)
    assert groups["Capital Account"] == D("750000.00")
    assert groups == {"Capital Account": D("750000.00"), "Current Liabilities": D("1757357.00"),
                      "Current Assets": D("2605093.00"), "Sales Accounts": D("2057650.00"),
                      "Purchase Accounts": D("-2521300.00"), "Indirect Expenses": D("-1343000.00")}
    # the first row wins even when the same-named ledger row carries a different figure
    assert primary_group_rows([TbRow("Capital Account", D("-5")), TbRow("Capital Account", D("-9"))]) == \
        {"Capital Account": D("-5")}
    # sub-group and synthetic rows are never primary-group rows
    assert "Sundry Debtors" not in groups and "Opening Stock" not in groups


def test_opening_stock_added_only_to_stock_bearing_group():
    ledgers = [L("cash", closing=D("-10")), L("fa", primary="Fixed Assets", closing=D("-20"))]
    r1 = rung1(ledgers, {}, S({"cash": D("-10"), "fa": D("-20")}), {}, [], TOL, ledgerwise_flags=LW)
    lines = rung2(ledgers, r1, [], {"Current Assets": D("-35"), "Fixed Assets": D("-20")}, D("-25"),
                  "Current Assets", {}, S(), [], TOL, ledgerwise_flags=LW)
    g = _groups(lines)
    assert (g["Current Assets"].our, g["Current Assets"].verdict) == (D("-35"), "match")
    assert (g["Fixed Assets"].our, g["Fixed Assets"].verdict) == (D("-20"), "match")


def test_bs_group_includes_accepted_forex_unrealised():                  # §10.5 B numbers under Current Assets
    usd = L("usd", is_forex=True, mirrored_closing=D("-132929.85"), closing_fx=D("-1609.71"),
            closing_fx_rate=D("82.58"), opening_fx=D("0"))
    sums = Sums(total={"usd": D("-133113.72")}, face={"usd": D("-1609.71")}, face_complete={"usd": True},
                fy_total={"usd": D("-133113.72")})
    r1 = rung1([usd], {"usd": D("0")}, sums, {"usd": D("-132929.85")}, [], TOL, ledgerwise_flags=LW)
    fl, total = forex_lines([usd], {"usd": D("0")}, sums, {"usd": D("-132929.85")}, D("-183.87"), TOL)
    lines = rung2([usd], r1, fl, {"Current Assets": D("-132929.85")}, None, "Current Assets", {}, sums, [], TOL,
                  ledgerwise_flags=LW)
    g = _groups(lines)["Current Assets"]
    assert (g.our, g.tally, g.verdict, total) == (D("-132929.85"), D("-132929.85"), "match", D("183.87"))


def test_bs_group_gets_no_allowance_when_forex_set_rejected():           # non-forex never get an allowance
    usd = L("usd", is_forex=True, mirrored_closing=D("-132929.85"))
    sums = S({"usd": D("-133113.72")})
    fl, _ = forex_lines([usd], {"usd": D("0")}, sums, {"usd": D("-132929.85")}, None, TOL)
    lines = rung2([usd], [], fl, {"Current Assets": D("-132929.85")}, None, "Current Assets", {}, sums, [], TOL,
                  ledgerwise_flags=LW)
    g = _groups(lines)["Current Assets"]
    assert (g.our, g.verdict) == (D("-133113.72"), "mismatch")


@pytest.mark.parametrize("tally, verdict", [(D("100.00"), "match"), (D("101.00"), "match"),
                                            (D("101.01"), "mismatch"), (D("98.99"), "mismatch")])
def test_nominal_ledgers_compared_per_ledger_from_fy_start(tally, verdict):
    sales = L("sales", primary="Sales Accounts", nature="income")
    # `total` covers [E, as_on] across FYs -- rung 2 must use `fy_total` (nominal ledgers restart each FY)
    lines = rung2([sales], [], [], {"Sales Accounts": tally}, None, "Current Assets", {"sales": tally},
                  S(total={"sales": D("999999")}, fy_total={"sales": D("100.00")}), [], TOL, ledgerwise_flags=LW)
    (ledger,) = [l for l in lines if l.scope == "ledger"]
    assert (ledger.our, ledger.tally, ledger.verdict) == (D("100.00"), tally, verdict)


def test_nominal_ledger_absent_from_tb_is_zero():
    sales = L("sales", primary="Sales Accounts", nature="income")
    lines = rung2([sales], [], [], {}, None, "Current Assets", {}, S(fy_total={"sales": D("0")}), [], TOL,
                  ledgerwise_flags=LW)
    assert [(l.scope, l.name, l.tally, l.verdict) for l in lines] == [
        ("ledger", "sales", D("0"), "match"), ("group", "Sales Accounts", D("0.00"), "match")]


def test_nominal_tb_row_unresolved_missing_in_db():
    lines = rung2([], [], [], {}, None, "Current Assets", {}, S(), ["Ghost Sales"], TOL, ledgerwise_flags=LW)
    assert [(l.name, l.verdict, l.guid) for l in lines] == [("Ghost Sales", "missing_in_db", None)]


def test_group_mismatch_with_all_ledgers_matching_is_group_walk_wrong():
    a, b = L("a", closing=D("-10")), L("b", closing=D("-20"))
    r1 = rung1([a, b], {}, S({"a": D("-10"), "b": D("-20")}), {}, [], TOL, ledgerwise_flags=LW)
    assert all(l.verdict == "match" for l in r1)
    lines = rung2([a, b], r1, [], {"Current Assets": D("-50")}, None, "Current Assets", {}, S(), [], TOL,
                  ledgerwise_flags=LW)
    g = _groups(lines)["Current Assets"]
    assert (g.verdict, g.cause) == ("mismatch", "group_walk_wrong")


def test_group_mismatch_with_a_ledger_mismatching_has_no_group_cause():
    a = L("a", closing=D("-10"))
    r1 = rung1([a], {}, S({"a": D("-15")}), {}, [], TOL, ledgerwise_flags=LW)
    lines = rung2([a], r1, [], {"Current Assets": D("-10")}, None, "Current Assets", {}, S(), [], TOL,
                  ledgerwise_flags=LW)
    g = _groups(lines)["Current Assets"]
    assert (g.verdict, g.cause) == ("mismatch", None)


def test_group_absent_from_tb_with_ledgers_is_compared_against_zero():
    fa = L("fa", primary="Fixed Assets", closing=D("0"))
    r1 = rung1([fa], {}, S({"fa": D("-5")}), {}, [], TOL, ledgerwise_flags=LW)
    g = _groups(rung2([fa], r1, [], {}, None, "Current Assets", {}, S(), [], TOL, ledgerwise_flags=LW))
    assert (g["Fixed Assets"].tally, g["Fixed Assets"].verdict) == (D("0"), "mismatch")


def test_bs_group_with_no_ledger_anchor_is_not_applicable():             # never `match` without an anchor
    a = L("a", closing=D("-10"))
    r1 = rung1([a], None, S({"a": D("-10")}), {}, [], TOL, ledgerwise_flags=LW)
    g = _groups(rung2([a], r1, [], {"Current Assets": D("-10")}, None, "Current Assets", {}, S(), [], TOL,
                      ledgerwise_flags=LW))["Current Assets"]
    assert (g.verdict, g.cause) == ("not_applicable", "no_ledger_anchor")


def test_explodeflag_tb_never_used_as_ledger_level():                    # D29, probe 17 caveat 2
    rows = prd.tb("p17_A_tb_exploded_explodeflag.xml")
    flags = prd.request_flags("p17_A_tb_exploded_explodeflag.xml")
    assert flags == {"EXPLODEFLAG": "Yes"}
    as_if_ledgerwise = {r.name: r.amount for r in rows}
    with pytest.raises(ValueError, match="not a ledger-level TB"):
        rung2([], [], [], primary_group_rows(rows), None, "Current Assets", as_if_ledgerwise, S(), [], TOL,
              ledgerwise_flags=flags)


# --- real data, end to end (controller ruling 5: success criteria, not numbers to tune toward) -------------------

def _breakdown(lines: list[Line]) -> list:
    return [(l.scope, l.name, l.our, l.tally, l.diff, l.verdict, l.cause) for l in lines
            if l.verdict not in ("match", "match_revalued", "not_applicable")]


def test_real_a_fy2025_parity_ok():                                      # §15.5 row 1
    case = prd.case_a()
    result = prd.run(case)
    assert is_clean(result.all_lines), _breakdown(result.all_lines)
    assert result.forex == [] and result.forex_unrealised_total == D("0")
    groups = _groups(result.rung2)
    assert set(groups) == {"Capital Account", "Current Liabilities", "Current Assets", "Sales Accounts",
                           "Purchase Accounts", "Indirect Expenses", "Direct Expenses"}
    # Direct Expenses: not a TB row (Tally omits zero rows) but its ledgers exist -> compared against 0.00
    assert (groups["Direct Expenses"].tally, groups["Direct Expenses"].our) == (D("0.00"), D("0"))
    ca = groups["Current Assets"]                                        # LESSONS rule 19: 7,49,293 + 18,55,800
    assert (ca.our, ca.tally, ca.verdict) == (D("2605093.00"), D("2605093.00"), "match")
    assert case.opening_stock == D("1855800.00")
    nominal = [l for l in result.rung2 if l.scope == "ledger"]
    assert nominal and all(l.verdict == "match" for l in nominal)
    assert {l.verdict for l in result.rung1 if l.cause == "nominal"} == {"not_applicable"}


def test_real_b_2023_parity_ok_anchor_source_dataset():                   # §15.5 row 2, A5
    case = prd.case_b_2023_dataset_anchor()
    result = prd.run(case)
    assert is_clean(result.all_lines), _breakdown(result.all_lines)
    (usd,) = result.forex
    assert (usd.name, usd.verdict, usd.unrealised, usd.our, usd.tally) == (
        "Gulf Office Supplies LLC (USD)", "match_revalued", D("183.87"), D("-133113.72"), D("-132929.85"))
    assert usd.cause is None                                              # F3: no false forex_face_mismatch
    assert result.forex_unrealised_total == D("183.87")
    assert case.unadjusted == D("-183.87")
    export = next(l for l in result.rung2 if l.scope == "ledger" and l.name == "Export Sales")
    assert (export.our, export.verdict) == (D("133113.72"), "match")    # bases, never revalued
    groups = _groups(result.rung2)
    assert groups["Current Assets"].our == D("-1954753.74") and groups["Current Assets"].verdict == "match"
    # the P&L A/c master carries an expression balance (so D30 makes it forex) -- still D11 not_applicable
    pl = [l for l in result.all_lines if l.name == "Profit & Loss A/c"]
    assert [(l.verdict, l.cause) for l in pl] == [("not_applicable", "pl_account")]


def test_real_b_2023_forex_sale_missing_is_caught():                      # §15.5 row 3, on the real case
    case = prd.case_b_2023_dataset_anchor()
    usd = next(l.guid for l in case.ledgers if l.name == "Gulf Office Supplies LLC (USD)")
    export = next(l.guid for l in case.ledgers if l.name == "Export Sales")
    # drop sale 74 (-95897.68 / $-1161.27) from our side
    total, fy_total = dict(case.sums.total), dict(case.sums.fy_total)
    total[usd] += D("95897.68")
    total[export] -= D("95897.68")
    fy_total[usd] += D("95897.68")
    fy_total[export] -= D("95897.68")
    sums = Sums(total=total, face={**case.sums.face, usd: case.sums.face[usd] + D("1161.27")},
                face_complete=case.sums.face_complete, fy_total=fy_total)
    result = prd.run(replace(case, sums=sums))
    assert not is_clean(result.all_lines)
    (usd_line,) = result.forex
    assert (usd_line.verdict, usd_line.cause) == ("mismatch", "forex_revaluation_unexplained")
    export_line = next(l for l in result.rung2 if l.scope == "ledger" and l.name == "Export Sales")
    assert export_line.verdict == "mismatch"                              # full base on the INR side


# --- 10c carry: the group-anchor route (§10.4 "rung 2 carries the check at group level", §15.5 row 12) -------------

def _no_anchor_r1(ledgers, sums):
    return rung1(ledgers, None, sums, {}, [], TOL, ledgerwise_flags=LW)


def test_group_anchor_route_compares_the_bs_group_when_no_ledger_anchor_exists():
    a, b = L("a"), L("b")
    fa = L("fa", primary="Fixed Assets")
    sums = S({"a": D("-10"), "b": D("-20"), "fa": D("-5")})
    r1 = _no_anchor_r1([a, b, fa], sums)
    assert {l.verdict for l in r1} == {"not_applicable"} and {l.cause for l in r1} == {"no_ledger_anchor"}
    g = _groups(rung2([a, b, fa], r1, [], {"Current Assets": D("-155"), "Fixed Assets": D("-5")}, D("-25"),
                      "Current Assets", {}, sums, [], TOL, ledgerwise_flags=LW,
                      group_anchors={"Current Assets": D("-100")}))
    # anchor −100 + lines −30 + Opening Stock −25 = −155; Fixed Assets is absent from the anchor TB -> 0 + −5
    assert (g["Current Assets"].our, g["Current Assets"].verdict, g["Current Assets"].cause) == \
        (D("-155"), "match", None)
    assert (g["Fixed Assets"].our, g["Fixed Assets"].verdict) == (D("-5"), "match")
    assert is_clean(r1 + list(g.values()))


def test_group_anchor_route_catches_a_missing_line_at_group_level():
    a = L("a")
    sums = S({"a": D("-10")})
    g = _groups(rung2([a], _no_anchor_r1([a], sums), [], {"Current Assets": D("-160")}, None, "Current Assets",
                      {}, sums, [], TOL, ledgerwise_flags=LW, group_anchors={"Current Assets": D("-100")}))
    assert (g["Current Assets"].diff, g["Current Assets"].verdict) == (D("-50"), "mismatch")


def test_group_anchor_route_forex_member_without_a_split_is_not_applicable():
    usd = L("usd", is_forex=True)
    sums = S({"usd": D("-100")})
    fl, _ = forex_lines([usd], None, sums, {}, None, TOL)
    kwargs = dict(ledgerwise_flags=LW, group_anchors={"Current Assets": D("0")})
    g = _groups(rung2([usd], [], fl, {"Current Assets": D("-120")}, None, "Current Assets", {}, sums, [], TOL,
                      **kwargs))["Current Assets"]
    assert (g.verdict, g.cause) == ("not_applicable", "forex_unsplit")
    g = _groups(rung2([usd], [], fl, {"Current Assets": D("-120")}, None, "Current Assets", {}, sums, [], TOL,
                      group_unrealised={"Current Assets": D("-20")}, **kwargs))["Current Assets"]
    assert (g.our, g.verdict) == (D("-120"), "match")


def test_never_clean_when_no_balance_sheet_figure_was_verified():
    """10c carry: `is_clean` (and so status `ok`) needs a verified balance-sheet figure at either rung -- every
    BS ledger and group `not_applicable` (no ledger anchor, no group anchor) verified nothing."""
    a = L("a")
    sums = S({"a": D("-10")})
    r1 = _no_anchor_r1([a], sums)
    r2 = rung2([a], r1, [], {"Current Assets": D("-10")}, None, "Current Assets", {}, sums, [], TOL,
               ledgerwise_flags=LW)
    lines = r1 + r2
    assert not any(l.verdict in ("mismatch", "missing_in_db", "missing_in_tally") for l in lines)
    assert not is_clean(lines)
