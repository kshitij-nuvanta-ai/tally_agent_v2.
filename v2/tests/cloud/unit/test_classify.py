"""Parity cause classifier (S1 spec §10.7, §16 "Classifier": one case per signature)."""
from datetime import date
from decimal import Decimal as D

from v2.cloud.parity.classify import Context, classify
from v2.cloud.parity.model import Line

FY_START = date(2025, 4, 1)
VERIFIED_EDGE = date(2025, 4, 1)


def ctx(**kw):
    base = dict(anchor_rows={}, flagged_amounts={}, forex_guids=set(), fy_start=FY_START,
                verified_edge=VERIFIED_EDGE, month_ends_without_tb=[])
    base.update(kw)
    return Context(**base)


def ledger_line(guid, diff, *, verdict="mismatch", cause=None, our=D("0"), tally=None, scope="ledger"):
    tally = our + diff if tally is None and diff is not None else tally
    return Line(scope, guid, guid, our, tally, diff, verdict, cause)


def test_tb_row_to_no_ledger_is_masters_gap_refetch_masters():
    lines = [Line("ledger", None, "Ghost Ledger", None, None, None, "missing_in_db", None)]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "masters_gap"
    assert [r.action for r in rem] == ["refetch_masters"]
    assert rem[0].params == {}


def test_live_ledger_absent_is_ledger_deleted_reconcile_masters():
    lines = [Line("ledger", "g1", "Old Bank", D("-500.00"), None, None, "missing_in_tally", None)]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "ledger_deleted"
    assert [(r.action, r.params) for r in rem] == [("reconcile_masters", {"master_type": "ledger"})]


def test_diff_equal_to_one_cancelled_voucher_amount_is_flag_filter_inverted_no_remediation():
    # Ruling F7: a cancelled voucher exports no amounts at all (LESSONS rule 23) -- only an OPTIONAL voucher's
    # flag flip changes an exported ledger sum, so ``flagged_amounts`` is populated from optional vouchers only.
    optional_voucher_amount = D("-1500.00")
    lines = [ledger_line("bank", optional_voucher_amount)]
    out, rem = classify(lines, ctx(flagged_amounts={"bank": {optional_voucher_amount}}))
    assert out[0].cause == "flag_filter_inverted"
    assert rem == []


def test_equal_and_opposite_pair_is_voucher_missed_or_duplicated_month_bisect():
    lines = [ledger_line("bank", D("-25000.00")), ledger_line("creditor", D("25000.00"))]
    month_ends = [date(2025, 6, 30), date(2025, 9, 30)]
    out, rem = classify(lines, ctx(month_ends_without_tb=month_ends))
    assert {l.cause for l in out} == {"voucher_missed_or_duplicated"}
    assert len(rem) == 1 and rem[0].action == "month_bisect"
    assert rem[0].params == {"fy_start": FY_START.isoformat(),
                              "month_ends": [d.isoformat() for d in month_ends]}


def test_equal_and_opposite_four_ledger_gst_set_is_voucher_missed_or_duplicated():
    # A dropped GST sale moves FOUR ledgers (ruling F5): party gross, Domestic Sales net, Output CGST, Output SGST.
    # Invoice: goods Rs 1,00,000.00 + 9% CGST (Rs 9,000.00) + 9% SGST (Rs 9,000.00) = party gross Rs 1,18,000.00.
    lines = [
        ledger_line("party", D("-118000.00")),          # Dr Party -- debit is negative
        ledger_line("domestic_sales", D("100000.00")),  # Cr Domestic Sales
        ledger_line("output_cgst", D("9000.00")),        # Cr Output CGST
        ledger_line("output_sgst", D("9000.00")),        # Cr Output SGST
    ]
    out, rem = classify(lines, ctx())
    assert {l.cause for l in out} == {"voucher_missed_or_duplicated"}
    assert [r.action for r in rem] == ["month_bisect"]


def test_mismatches_not_netting_to_zero_do_not_match_the_pair_signature():
    lines = [ledger_line("a", D("5000.00")), ledger_line("b", D("3000.00"))]
    out, rem = classify(lines, ctx())
    assert not any(l.cause == "voucher_missed_or_duplicated" for l in out)
    assert not any(r.action == "month_bisect" for r in rem)


def test_three_bs_ledgers_diff_like_their_anchor_rows_is_anchor_wrong():
    anchor_rows = {"a": D("500.00"), "b": D("500.00"), "c": D("500.00")}
    lines = [ledger_line("a", D("500.00")), ledger_line("b", D("500.00")), ledger_line("c", D("500.00"))]
    out, rem = classify(lines, ctx(anchor_rows=anchor_rows, verified_edge=date(2025, 4, 1)))
    assert {l.cause for l in out} == {"anchor_wrong"}
    actions = [r.action for r in rem]
    assert actions == ["capture_snapshot", "refetch_masters"]
    snap = next(r for r in rem if r.action == "capture_snapshot")
    assert snap.params == {"report_type": "trial_balance_ledgerwise", "as_on": date(2025, 3, 31).isoformat()}


def test_single_ledger_diff_is_ledger_gap_refetch_ledger_vouchers():
    lines = [ledger_line("only_one", D("2500.00"))]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "ledger_gap"
    assert [(r.action, r.params) for r in rem] == \
        [("refetch_ledger_vouchers", {"ledger_guid": "only_one", "fy_start": FY_START.isoformat()})]


def test_forex_face_mismatch_is_forex_gap():
    # Ruling F4: forex_face_mismatch is only reachable when the face check applies (a current-FY as-on).
    lines = [Line("ledger", "usd_party", "USD Party", D("-100000.00"), D("-105000.00"), D("-5000.00"),
                  "mismatch", "forex_face_mismatch")]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "forex_gap"
    assert [(r.action, r.params) for r in rem] == \
        [("refetch_ledger_vouchers", {"ledger_guid": "usd_party", "fy_start": FY_START.isoformat()})]


def test_forex_revaluation_unexplained_is_forex_gap():
    # Ruling F4: forex_revaluation_unexplained is the path reachable when no forex face is available (a past FY).
    lines = [Line("ledger", "usd_party", "USD Party", D("-100000.00"), D("-137216.04"), D("-37216.04"),
                  "mismatch", "forex_revaluation_unexplained")]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "forex_gap"
    assert [r.action for r in rem] == ["refetch_ledger_vouchers"]


def test_group_only_is_group_walk_wrong_no_remediation():
    lines = [Line("group", None, "Sundry Creditors", D("100.00"), D("105.00"), D("5.00"),
                  "mismatch", "group_walk_wrong")]
    out, rem = classify(lines, ctx())
    assert out[0].cause == "group_walk_wrong"
    assert rem == []


def test_order_is_first_match_wins():
    # Two lines that net to zero: the pair rule (row 4) must win over what a naive per-line pass might otherwise
    # treat as "one ledger differs" (row 6) if it processed them one at a time.
    lines = [ledger_line("x", D("2000.00")), ledger_line("y", D("-2000.00"))]
    out, rem = classify(lines, ctx())
    assert {l.cause for l in out} == {"voucher_missed_or_duplicated"}
    assert not any(r.action == "refetch_ledger_vouchers" for r in rem)


def test_remediation_ids_are_stable():
    lines = [ledger_line("only_one", D("2500.00"))]
    _, rem1 = classify(lines, ctx())
    _, rem2 = classify(lines, ctx())
    assert rem1[0].id == rem2[0].id
    assert len(rem1[0].id) == 16

    other_lines = [ledger_line("other_guid", D("2500.00"))]
    _, rem3 = classify(other_lines, ctx())
    assert rem3[0].id != rem1[0].id
