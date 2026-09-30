"""Real-data parity, company B (`Sharma & Sons' Probe Traders`) as-on 31-03-2023 -- S1 spec §0 success criterion 2
("including the ₹183.87 forex revaluation"), §10.5, §15.5 rows 2-4, the §15 seeded-fault matrix. Pure: the Task 12
assembler (``realdata.assemble_b_fy2022``) + ``realdata.pure_parity``.

Inputs (all captures except the anchor): the 240 FY 2022-23 vouchers (`p21_B_fy2022_month_01..12.xml`), B's masters,
the group TB `p18_B_tb_asof_2023-03-31.xml` (incl. the `Unadjusted Forex Gain/Loss` row), and G2 -- the ledger-level
TB as-on 31-03-2023 (`s1_B_tb_ledger_asof_2023-03-31.xml`; the brief's `..._tb_ledgerwise_...` name is advisory).
Ruling F3: G2's rows are also rung 1's "Tally figure" for this past as-on (the mirrored masters are FY 2025-26).

Books-start anchor (plan ambiguity A5, rulings F9): when Task 12 was dispatched G6 -- B's ledger-level TB as-on
books_from -- was not captured, so the anchor is ``company_b_data.generate()``'s ledger openings (the values setup-b
wrote and ``_verify`` read back); every such test id carries ``anchor_source=dataset``, and that anchor is never posted
through ``/snapshots``. G6 (`s1_B_tb_ledger_asof_2022-04-01.xml`) was captured during the task (controller commit
3620693): closing columns only, i.e. the balance at the END of 01-04-2022 -- exactly A's G1 shape -- so it is consumed
exactly as G1 is (D9: TB as-on books_from minus our own lines dated books_from). The two headline tests also run on it
(``anchor_source=tally``), and ``test_g6_books_start_anchor_reconciles_with_the_dataset_openings`` pins that the
G6-derived anchor equals the dataset openings ledger by ledger. Tests skip, naming G6, when it is absent.

Seeded faults (each applied to a COPY of the assembled data) -- rulings F4 / F7 / T12:
- ``drop_usd_sale_101``: on B-2023 no face is available (F3), so the USD party is ``mismatch`` /
  ``forex_revaluation_unexplained`` (classifier ``forex_gap``) -- ``forex_face_mismatch`` is proven in
  ``test_forex.py`` (unit) and ``test_parity_api.py`` (FakeBooks current FY).
- ``flip_cancelled_flag_on_201`` is dropped (F7: a cancelled voucher exports no amounts, the run stays ok);
  ``flag_filter_inverted`` is covered by the FakeBooks optional-voucher twin below.
- ``drop_a_ledger_master`` names the ledger it drops: ``Capital Account`` (a TB row with no FY 2022-23 lines).
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal as D

import pytest

from backend.sync.ingest.snapshots import NAME_KEYS, parse_cells
from probes.setup import company_b_data
from tests.sync import realdata as rd

AS_ON, BOOKS_FROM = date(2023, 3, 31), date(2022, 4, 1)
G2 = "s1_B_tb_ledger_asof_2023-03-31.xml"
G6 = "s1_B_tb_ledger_asof_2022-04-01.xml"
USD = "Gulf Office Supplies LLC (USD)"
ANCHOR_ID = "anchor_source=dataset"


def _g2() -> rd.Snapshot:
    if not (rd.SYNC / G2).exists():
        pytest.skip("G2 not captured (company B ledger-level TB as-on 31-03-2023)")
    return rd.Snapshot.capture(G2)


def _g6() -> rd.Snapshot:
    if not (rd.SYNC / G6).exists():
        pytest.skip("G6 not captured (company B ledger-level TB as-on books_from 01-04-2022)")
    return rd.Snapshot.capture(G6)


def dataset_anchor() -> dict[str, D]:
    """A5: B's books-start anchor from the dataset's ledger openings (cross-checked against G6 below)."""
    return {spec.name: spec.opening for spec in company_b_data.generate("educational").ledgers if spec.opening}


def _run(b: rd.Assembled | None = None, *, tb: rd.Snapshot | None = None,
         anchor: dict[str, D] | None = None, anchor_source: str = "dataset") -> rd.ParityResult:
    """``anchor_source="dataset"`` (A5) or ``"tally"`` (G6) -- the real ledger-level TB as-on books_from, if captured (then
    the D9 books-start anchor is that TB minus our own lines dated 01-04-2022, exactly as the engine does it)."""
    b = b or rd.assemble_b_fy2022()
    lw = _g2()
    snapshots = {"trial_balance": tb or rd.Snapshot.capture("p18_B_tb_asof_2023-03-31.xml"),
                 "trial_balance_ledgerwise": lw}
    if anchor_source == "tally":
        snapshots["anchor"] = _g6()
        return rd.pure_parity(b, snapshots=snapshots, mirrored=rd.mirrored_from_tb(lw), as_on=AS_ON,
                              verified_edge=BOOKS_FROM)
    return rd.pure_parity(b, snapshots=snapshots, mirrored=rd.mirrored_from_tb(lw), as_on=AS_ON,
                          verified_edge=BOOKS_FROM, dataset_anchor=dataset_anchor() if anchor is None else anchor)


def _breakdown(result: rd.ParityResult) -> list:
    return [(l.scope, l.name, l.our, l.tally, l.diff, l.verdict, l.cause) for l in result.problems()]


ANCHORS = pytest.mark.parametrize("anchor_source", ["dataset", "tally"], ids=[ANCHOR_ID, "anchor_source=tally"])


@ANCHORS
def test_company_b_2023_rung2_ok_with_forex_18387(anchor_source):
    """§0 criterion 2: status `ok`, the USD party `match_revalued` with unrealised 183.87, `Export Sales` `match`,
    `forex_unrealised_total == 183.87` -- on the dataset anchor (A5) and, where G6 exists, on Tally's own books-start
    ledger-level TB."""
    result = _run(anchor_source=anchor_source)
    assert result.anchor_source == (G6 if anchor_source == "tally" else "dataset")
    assert result.status == "ok", _breakdown(result)
    usd = result.ledger(USD)
    assert (usd.verdict, usd.unrealised, usd.our, usd.tally, usd.cause) == \
        ("match_revalued", D("183.87"), D("-133113.72"), D("-132929.85"), None)
    export = [l for l in result.lines if l.scope == "ledger" and l.name == "Export Sales" and l.verdict != "not_applicable"]
    assert [(l.our, l.tally, l.verdict) for l in export] == [(D("133113.72"), D("133113.72"), "match")]
    assert result.forex_unrealised_total == D("183.87")
    assert result.summary["forex_unrealised_total"] == "183.87" and result.summary["match_revalued"] == 1
    assert result.imbalance == D("0.00")                              # §10.1(6): B nets to 0.00 with the forex row
    ca = result.group("Current Assets")
    assert (ca.our, ca.verdict) == (D("-1954753.74"), "match")


@ANCHORS
def test_company_b_2023_row_removed_forex_revaluation_unexplained(anchor_source):
    """§16: the same inputs with the `Unadjusted Forex Gain/Loss` cell dropped from the group TB -> the set rule
    rejects -> the USD party `mismatch`, cause `forex_revaluation_unexplained` (classified `forex_gap`)."""
    tb = rd.Snapshot.capture("p18_B_tb_asof_2023-03-31.xml").without("Unadjusted Forex Gain/Loss")
    result = _run(tb=tb, anchor_source=anchor_source)
    assert result.status == "suspect"
    raw, classified = result.ledger(USD, raw=True), result.ledger(USD)
    assert (raw.verdict, raw.cause, raw.diff) == ("mismatch", "forex_revaluation_unexplained", D("183.87"))
    assert (classified.verdict, classified.cause) == ("mismatch", "forex_gap")
    assert result.forex_unrealised_total == D("0.00")


def test_g6_books_start_anchor_reconciles_with_the_dataset_openings():
    """G6 (closing at the end of 01-04-2022) minus our own FY 2022-23 lines dated 01-04-2022 -- the engine's D9 anchor
    -- equals ``company_b_data``'s ledger openings for every ledger (A5's dataset anchor is the same figure)."""
    from backend.sync.parity.anchors import anchor_amounts
    from backend.sync.parity.model import build_sums
    from tests.sync import parity_realdata as prd
    g6 = _g6()
    b = rd.assemble_b_fy2022()
    index, ledgers, _, _ = prd._index_and_ledgers(b.masters)
    facts = prd._line_facts(prd._parse_vouchers(b.vouchers), index)
    day_one = build_sums([f for f in facts if f.voucher_date == BOOKS_FROM], verified_edge=BOOKS_FROM,
                         as_on=BOOKS_FROM).total
    assert day_one                                               # G6 does include day-1 activity
    anchors, unresolved = anchor_amounts(g6.rows, index, day_one, ledgerwise_flags=g6.flags)
    assert unresolved == []
    by_name = {l["guid"]: l["name"] for l in ledgers}
    derived = {by_name[g]: a for g, a in anchors.items() if a != 0}
    assert derived == {n: a for n, a in dataset_anchor().items() if a != 0}


def test_company_b_forex_tb_row_form_matches_g2():
    """S1-R2: G2 answers the USD ledger's TB row as a PLAIN INR figure (not the `-$… @ ? …/$ = -? …` expression),
    and ``parse_cells`` takes it as that base."""
    g2 = _g2()
    key = NAME_KEYS["trial_balance"]
    (cell,) = [c for c in g2.cells if c.get(key) == USD]
    amounts = [v for k, v in cell.items() if k != key and v]
    assert amounts and not any("@" in v or "$" in v for v in amounts), cell
    (row,) = [r for r in parse_cells("trial_balance", [cell]).rows]
    assert D(row["amount"]) == D("-132929.85")


# --- the §15 seeded-fault matrix on real B-2023 data ------------------------------------------------------------


def _drop_narrated(b: rd.Assembled, prefix: str) -> str:
    (v,) = [v for v in b.vouchers if (v["data"].get("narration") or "").startswith(prefix)]
    b.vouchers.remove(v)
    return v["data"]["guid"]


def _first(b: rd.Assembled, vch_type: str, with_ledgers: set[str] = frozenset()) -> dict:
    for v in b.vouchers:
        d = v["data"]
        names = {e["ledgername"] for e in d.get("ledger_entries", [])}
        if (d["vouchertypename"] == vch_type and d["iscancelled"] == "No" and d["isoptional"] == "No"
                and with_ledgers <= names and not any("@" in e["amount"] for e in d["ledger_entries"])):
            return v
    raise AssertionError(f"no countable INR {vch_type}")


def fault_drop_usd_sale_101(b: rd.Assembled) -> dict:
    _drop_narrated(b, "[S0-B:101]")
    return {}


def fault_drop_one_inr_sale(b: rd.Assembled) -> dict:
    b.vouchers.remove(_first(b, "Sales", {"Domestic Sales", "Output CGST", "Output SGST"}))
    return {}


def fault_duplicate_one_receipt(b: rd.Assembled) -> dict:
    twin = copy.deepcopy(_first(b, "Receipt"))
    twin["data"]["guid"] += "-dup"                   # the same voucher stored twice under two GUIDs
    b.vouchers.append(twin)
    return {}


DROPPED_LEDGER = "Capital Account"


def fault_drop_a_ledger_master(b: rd.Assembled) -> dict:
    (m,) = [m for m in b.masters if m["kind"] == "ledger" and m["data"]["name"] == DROPPED_LEDGER]
    b.masters.remove(m)
    return {}


SHIFTED = ("Cash", "HDFC Bank Current A/c", "Pune Digital Solutions")


def fault_shift_anchor_by_1000_on_3_ledgers(b: rd.Assembled) -> dict:
    anchor = dataset_anchor()
    for name in SHIFTED:
        anchor[name] = anchor.get(name, D("0")) + D("1000")
    return {"anchor": anchor}


def _only_ledger_problems(result: rd.ParityResult) -> dict[str, tuple]:
    return {l.name: (l.verdict, l.cause) for l in result.problems() if l.scope == "ledger"}


@pytest.mark.parametrize("fault, expect", [
    ("drop_usd_sale_101", "forex_gap"),
    ("drop_one_inr_sale", "voucher_missed_or_duplicated"),
    ("duplicate_one_receipt", "voucher_missed_or_duplicated"),
    ("drop_a_ledger_master", "masters_gap"),
    ("shift_anchor_by_1000_on_3_ledgers", "anchor_wrong"),        # classifier row 5 (a), fix round 1
], ids=lambda x: f"{x}-{ANCHOR_ID}" if isinstance(x, str) and x.startswith(("drop", "dup", "shift")) else x)
def test_company_b_seeded_faults(fault, expect):
    b = rd.assemble_b_fy2022().copy()
    kwargs = globals()[f"fault_{fault}"](b)
    result = _run(b, **kwargs)
    assert result.status == "suspect", _breakdown(result)
    problems = _only_ledger_problems(result)
    if fault == "drop_usd_sale_101":
        # F4: no face on B-2023 -> the set rule rejects; the INR side (Export Sales) shows the full base.
        raw = result.ledger(USD, raw=True)
        assert (raw.verdict, raw.cause) == ("mismatch", "forex_revaluation_unexplained")
        assert problems[USD] == ("mismatch", "forex_gap")
        assert problems["Export Sales"][0] == "mismatch"
        assert result.ledger(USD, raw=True).diff != 0
    elif fault == "drop_a_ledger_master":
        assert problems[DROPPED_LEDGER] == ("missing_in_db", "masters_gap")
        assert {r.action for r in result.remediations} >= {"refetch_masters"}
    elif fault == "shift_anchor_by_1000_on_3_ledgers":
        assert {problems[n] for n in SHIFTED} == {("mismatch", expect)}
    else:
        assert problems and {c for _, c in problems.values()} == {expect}, problems
        assert sum((l.diff for l in result.problems() if l.scope == "ledger"), D("0")) == D("0")
        assert {r.action for r in result.remediations} == {"month_bisect"}


def test_seeded_fault_shift_anchor_is_anchor_wrong_with_its_remediations_anchor_source_dataset():
    """Task 12 fix round 1 (controller ruling on §10.7 row 5): the three shifted ledgers (diff -1000.00 each) are
    `anchor_wrong`, and the run asks for the anchor TB as-on E-1 again plus a masters refetch."""
    b = rd.assemble_b_fy2022().copy()
    result = _run(b, **fault_shift_anchor_by_1000_on_3_ledgers(b))
    problems = {l.name: (l.diff, l.verdict, l.cause) for l in result.problems() if l.scope == "ledger"}
    assert problems == {n: (D("-1000.00"), "mismatch", "anchor_wrong") for n in SHIFTED}
    assert [(r.action, r.params) for r in result.remediations] == [
        ("capture_snapshot", {"report_type": "trial_balance_ledgerwise", "as_on": "2022-03-31"}),
        ("refetch_masters", {})]


# --- F7: flag_filter_inverted on a FakeBooks optional-voucher twin ------------------------------------------------


def test_flag_filter_inverted_on_fakebooks_optional_voucher_twin():
    """Ruling F7: the real B-2023 data can't carry this fault (cancelled vouchers export no amounts; the optional
    vouchers [S0-B:301]/[S0-B:302] are FY 2023-24). FakeBooks company B, FY 2023-24, verified from books_from: our
    side counts optional voucher [S0-B:301] as if the optional filter were inverted -> each of its ledgers differs by
    exactly its amount on that voucher -> `flag_filter_inverted` (§10.7 row 3)."""
    from datetime import datetime, timezone

    from tests.sync.fakeb import fakeb_company_b
    fb = fakeb_company_b()
    as_on = date(2024, 3, 31)
    b = fb.assembled(BOOKS_FROM, as_on, datetime(2026, 9, 25, 6, tzinfo=timezone.utc))
    snaps = {"trial_balance": fb.tb_snapshot(as_on, False), "trial_balance_ledgerwise": fb.tb_snapshot(as_on, True),
             "anchor": fb.tb_snapshot(BOOKS_FROM, True)}
    lw = snaps["trial_balance_ledgerwise"]
    control = rd.pure_parity(b, snapshots=snaps, mirrored=rd.mirrored_from_tb(lw), as_on=as_on,
                             verified_edge=BOOKS_FROM)
    assert control.status == "ok", _breakdown(control)

    (opt,) = [v for v in b.vouchers if v["data"]["narration"].startswith("[S0-B:301]")]
    assert opt["data"]["isoptional"] == "Yes"
    # The stored voucher stays optional (it is the classifier's evidence, as the DB engine reads it); the inverted
    # filter is modelled by our side ALSO counting it -- a countable copy of the same postings.
    flipped = b.copy()
    counted = copy.deepcopy(opt)
    counted["data"]["guid"] += "-counted"
    counted["data"]["isoptional"] = "No"
    flipped.vouchers.append(counted)
    result = rd.pure_parity(flipped, snapshots=snaps, mirrored=rd.mirrored_from_tb(lw), as_on=as_on,
                            verified_edge=BOOKS_FROM)
    problems = _only_ledger_problems(result)
    touched = {e["ledgername"] for e in opt["data"]["ledger_entries"]}
    assert problems and set(problems) <= touched
    assert set(problems.values()) == {("mismatch", "flag_filter_inverted")}
