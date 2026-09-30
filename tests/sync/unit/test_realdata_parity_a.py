"""Real-data parity, company A (`Bharat Traders Probe Copy`) FY 2025-26 -- S1 spec §0 success criterion 2, §15.5
row 1, §16 "Real-data parity". Pure: the Task 12 assembler (``realdata.assemble_a``) + ``realdata.pure_parity``.

Every figure comes from a capture (Review Focus 1). The books-start anchor is G1
(`s1_A_tb_ledger_asof_2025-04-01.xml`, the Task 0 capture; the brief's `..._tb_ledgerwise_...` name is advisory).

Controller ruling T12: the brief's anchor-derived fallback test (anchor_L = ledgerwise TB FY-end row minus our FY
lines) is dropped -- it is tautological; and the Bills Receivable/Payable residual test is not repeated here (Task 9's
``test_snapshot_rows.py`` owns it).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from backend.sync.parity import engine
from tests.sync import realdata as rd

AS_ON, BOOKS_FROM = date(2026, 3, 31), date(2025, 4, 1)
G1 = "s1_A_tb_ledger_asof_2025-04-01.xml"


def _g1() -> rd.Snapshot:
    if not (rd.SYNC / G1).exists():
        pytest.skip("G1 not captured (company A ledger-level TB as-on books_from)")
    return rd.Snapshot.capture(G1)


def _snapshots() -> dict[str, rd.Snapshot]:
    return {"trial_balance": rd.Snapshot.capture("p16_A_tb_fy_end.xml"),
            "trial_balance_ledgerwise": rd.Snapshot.capture("p17_A_tb_exploded_isledgerwise.xml"),
            "anchor": _g1()}


def _run(assembled: rd.Assembled | None = None, *, mirrored: dict | None = None) -> rd.ParityResult:
    a = assembled or rd.assemble_a()
    return rd.pure_parity(a, snapshots=_snapshots(), mirrored=rd.mirrored_from_masters(a) if mirrored is None
                          else mirrored, as_on=AS_ON, verified_edge=BOOKS_FROM)


def _breakdown(result: rd.ParityResult) -> list:
    return [(l.scope, l.name, l.our, l.tally, l.diff, l.verdict, l.cause) for l in result.problems()]


def test_company_a_fy2025_ok():
    """§0 criterion 2 / §15.5 row 1: every BS ledger `match`, nominal rung-1 `not_applicable`, rung-2 nominal
    `match`, `Profit & Loss A/c` `not_applicable`, status `ok`."""
    a = rd.assemble_a()
    result = _run(a)
    assert result.status == "ok", _breakdown(result)
    assert result.anchor_as_on == BOOKS_FROM and result.anchor_source == G1
    assert result.problems() == [] and result.remediations == []
    rung1_bs = [l for l in result.raw_lines if l.scope == "ledger" and l.cause is None and l.our is not None
                and l.tally is not None and l.verdict != "not_applicable"]
    nominal_r1 = [l for l in result.lines if l.scope == "ledger" and l.cause == "nominal"]
    assert nominal_r1 and {l.verdict for l in nominal_r1} == {"not_applicable"}
    nominal_names = {l.name for l in nominal_r1}
    bs = [l for l in rung1_bs if l.name not in nominal_names]
    nominal_r2 = [l for l in rung1_bs if l.name in nominal_names]
    assert bs and {l.verdict for l in bs} == {"match"}
    assert nominal_r2 and {l.verdict for l in nominal_r2} == {"match"}
    assert {l.name for l in nominal_r2} == nominal_names                 # every nominal ledger compared at rung 2
    pl = [l for l in result.lines if l.name == "Profit & Loss A/c"]
    assert [(l.verdict, l.cause) for l in pl] == [("not_applicable", "pl_account")]
    # every live ledger is accounted for exactly once at rung 1
    ledger_names = {m["data"]["name"] for m in a.masters if m["kind"] == "ledger"}
    assert {l.name for l in result.lines if l.scope == "ledger"} == ledger_names
    assert result.forex_unrealised_total == D("0.00") and result.summary["mismatches"] == 0
    ca = result.group("Current Assets")                                  # LESSONS rule 19: 7,49,293 + 18,55,800
    assert (ca.our, ca.tally, ca.verdict) == (D("2605093.00"), D("2605093.00"), "match")


def test_company_a_imbalance_non_zero_accepted_as_baseline():
    """D10 / §10.1(6): company A's TB does not net to zero (the seed opening defect) -- with no baseline that is
    recorded as the baseline, never a stale-Tally discard, and the run still computes `ok`."""
    result = _run()
    assert result.imbalance is not None and result.imbalance != D("0.00")
    assert engine.imbalance_moved(None, 412, result.imbalance, D("1.00")) == "rebaseline"
    baseline = {"alt_mst_id": 412, "imbalance": str(result.imbalance)}
    assert engine.imbalance_moved(baseline, 412, result.imbalance, D("1.00")) == "same"
    assert result.status == "ok"


@pytest.mark.parametrize("which, ledgers_file", [
    ("post_dated", "p16_A_ledgers_with_post_dated.xml"),
    ("future", "p16_A_ledgers_with_future_voucher.xml"),
])
def test_company_a_post_dated_and_future_vouchers_counted(which, ledgers_file):
    """§10.2 (probe 16): Tally's closing counts both throwaways (Cash 23,000.00 -> 23,001.00 in each capture), and
    so does rung 1 -- the voucher dated 31-03-2026 (post-dated or not) is inside [E, as_on]. Without it our Cash is
    exactly 1.00 short (still inside the inclusive ₹1.00 tolerance, so the proof is the figures, not the verdict)."""
    a = rd.assemble_a()
    after = {o["data"]["name"]: o["data"].get("closingbalance") for o in rd.masters(ledgers_file, "ledger")}
    with_it = a.copy()
    extra = with_it.add_probe16(which)                  # its inferred keys are marked (Review Focus 1)
    assert extra["data"]["date"] == "20260331"
    assert with_it.synthetic_ids[("voucher", extra["data"]["guid"])] == "inferred"
    cash = _run(with_it, mirrored=after).ledger("Cash")
    assert (cash.our, cash.tally, cash.diff, cash.verdict) == (D("23001.00"), D("23001.00"), D("0.00"), "match")
    without = _run(a, mirrored=after).ledger("Cash")
    assert (without.our, without.tally, without.diff) == (D("23000.00"), D("23001.00"), D("1.00"))
