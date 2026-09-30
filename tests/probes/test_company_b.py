import html
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from probes.companies import COMPANIES
from probes.reads import exploded_tb_rows, primary_group_rows
from probes.setup.company_b import (
    CompanyBLoadError, LoadReport, _expected_group_balances, _load_masters, _load_vouchers, _verify, load_company_b,
)
from probes.setup.company_b_data import (
    USD_DEBTOR, USD_EXPORT_PARTY, BillSpec, Dataset, GroupSpec, InventorySpec, LineSpec, UnitSpec, VoucherSpec,
    expected_figures, generate,
)
from probes.setup.writes import B_READBACK_FROM, B_READBACK_TO, TallyWriter, WriteRefused
from tests.probes.fake_books import USD_CURRENCY_ROW, FakeBooks, seed_company_b, sync_client
from tests.probes.fakes import ScriptedIO

B = COMPANIES["B"]
_SYNC_FIXTURES = Path(__file__).parent.parent / "fixtures" / "sync"
LIVE_A_TB_FY_END = _SYNC_FIXTURES / "c33_untyped_2026-09-23" / "p16_A_tb_fy_end.xml"                    # live company A, probe 16, 2026-09-23
LIVE_A_TB_EXPLODED = _SYNC_FIXTURES / "c33_untyped_2026-09-23" / "p17_A_tb_exploded_explodealllevels.xml"   # ditto, probe 17

_FLAG_PAUSE_RE = re.compile(r"^\[S0-B:(\d+)\].*?(ISCANCELLED|ISOPTIONAL) did not stick")


def _loader(books, **io_kwargs):
    io_kwargs.setdefault("on_wait", _operator_who_enters_the_opening_bill(books))
    said: list[str] = []
    writer = TallyWriter(sync_client(books.transport()), said.append)
    return writer, ScriptedIO(**io_kwargs), said


def _empty_b(*, usd_currency: bool = True, educational: bool = False):
    """An empty company B shell. Plan part 7: live B has the `$` Currency master, created in the UI (the XML create
    is refused — forex_shape_2026-09-25_run1_currency_refused/), so the shell has it too unless a test says not.
    Review I1 fix round: the fake's licence follows the load's (`load_company_b` defaults to "licensed"): the forex
    read-back reads 102's own day, 05-09-2022 licensed, which an educational Tally would not honour (C43)."""
    books = FakeBooks(name=B, educational=educational)
    books.edit_state(lambda s: s.update(voucherTypes=["Sales", "Purchase", "Receipt", "Payment", "Sales - GST"]))
    if usd_currency:
        books.edit_state(lambda s: s["currencies"].__setitem__("$", dict(USD_CURRENCY_ROW)))
    return books


OPENING_BILL = {"Op/2022-001": {"party": "Pune Digital Solutions", "amount": "-62500.00"}}   # Dr = receivable


def _operator_who_enters_the_opening_bill(books: FakeBooks):
    """C35: receipts now settle the opening bill Op/2022-001, so a fake without it refuses them (an Agst Ref to an
    unknown bill). Live, the operator enters it in the UI at the opening-bill pause; this does the same."""
    def on_wait(instruction: str) -> None:
        if "Opening bill 'Op/2022-001'" in instruction:
            books.edit_state(lambda s: s.setdefault("bills", {}).update(OPENING_BILL))
    return on_wait


def _operator_who_honours_flag_pauses(books: FakeBooks):
    """F10: the fake never simulates the Tally UI, so a flag pause's re-read fails forever by construction —
    that's an artefact of the fake, not the design. In reality the operator honours the pause and sets the flag
    by hand, and the re-read then sees it (LESSONS/spec §4.3). Mirrors test_p08_ledger_rename.py's
    `on_action`-simulates-the-operator pattern, but for `io.wait` (our pauses carry no `Action` — see the
    module docstring's F2/flag-pause ruling) rather than `io.ask`."""
    enters_opening_bill = _operator_who_enters_the_opening_bill(books)

    def on_wait(instruction: str) -> None:
        enters_opening_bill(instruction)
        match = _FLAG_PAUSE_RE.match(instruction)
        if not match:
            return
        tag, xml_tag = match.group(1), match.group(2)
        field = "cancelled" if xml_tag == "ISCANCELLED" else "optional"

        def fix(s):
            for v in s["vouchers"].values():
                if v["narration"].startswith(f"[S0-B:{tag}]"):
                    v[field] = "Yes"

        books.edit_state(fix)
    return on_wait


def test_a_first_load_lists_before_creating_and_reads_back_every_write():
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io)
    ds = generate()
    assert report.created["groups"] == len(ds.groups)
    assert report.created["vouchers"] == len(ds.vouchers)            # plan part 7: 101/102 written with forex (was C36-skipped)
    assert report.problems == []
    # the first request for each type is a read, not an import
    first = books.requests[0]
    assert "<TALLYREQUEST>Import Data</TALLYREQUEST>" not in first


def test_a_full_load_leaves_exactly_the_bills_the_dataset_expects_open():
    """C35 / review #5: `test_an_agst_ref_receipt_knocks_the_receivable_off` proved knock-off only on hand-built XML;
    no dataset voucher reached that branch. This drives the whole dataset through the writer into the fake (which
    refuses an Agst Ref to an unknown/other party's bill and any over-settlement) and compares every open bill."""
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io)
    assert report.problems == []
    ds = generate()
    expected = expected_figures(ds)
    # C42: cancelled (set by the operator in the UI after the create) and optional vouchers post no bills in Tally,
    # and the fake now models that itself — no hand-exclusion of their New Refs here any more.
    actual = {(b["party"], name): abs(Decimal(b["amount"])) for name, b in books.posted_bills().items()
              if Decimal(b["amount"]) != 0}
    assert actual == expected.bills_outstanding
    assert any(name.startswith("Inv/") for _, name in actual) and any(name.startswith("Pur/") for _, name in actual)


def test_a_second_load_sends_zero_creates():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)
    before = len([r for r in books.requests if "Import Data" in r])
    report = load_company_b(writer, io)
    after = len([r for r in books.requests if "Import Data" in r])
    assert after == before                       # nothing new was sent
    assert sum(report.created.values()) == 0
    assert report.skipped["vouchers"] == 960        # plan part 7: 101/102 written with forex (was C36-skipped)
    assert report.skipped["currencies"] == 1 and not any("<CURRENCY " in r for r in books.requests)


def test_a_master_that_already_exists_is_not_recreated():
    books = _empty_b()
    books.edit_state(lambda s: s["groups"].__setitem__("National Creditors", {"parent": "Sundry Creditors"}))
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert report.skipped["groups"] == 1
    creates = [r for r in books.requests if 'GROUP NAME="National Creditors" ACTION="Create"' in r]
    assert creates == []                         # LESSONS §15 rule 10


def test_a_company_without_probe_in_the_name_is_refused_before_any_request():
    books = FakeBooks(name="Sharma & Sons Traders")
    writer, io, _ = _loader(books)
    with pytest.raises(WriteRefused):
        load_company_b(writer, io, company="Sharma & Sons Traders")
    assert books.requests == []


def test_a_missing_custom_voucher_type_pauses_and_then_fails_if_still_missing():
    books = _empty_b()
    books.edit_state(lambda s: s.update(voucherTypes=["Sales", "Purchase", "Receipt", "Payment"]))  # no "Sales - GST"
    writer, io, _ = _loader(books)
    with pytest.raises(CompanyBLoadError, match="Sales - GST"):
        load_company_b(writer, io)
    assert any("Sales - GST" in w for w in io.waits)         # the operator was asked first


def test_a_flag_that_did_not_stick_becomes_a_pause_naming_the_voucher():
    books = _empty_b()
    books.drop_flags = True                      # the fake accepts the voucher but not ISOPTIONAL/ISCANCELLED
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert any("[S0-B:201]" in p for p in report.pauses)
    assert any("[S0-B:301]" in p for p in report.pauses)


def test_the_run_ends_by_checking_counts_and_balances_against_the_expected_figures():
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io)
    assert report.problems == []


def test_a_wrong_count_in_an_already_complete_fy_is_reported_as_a_problem_not_swallowed():
    """I2: drift must be flagged only when a chronologically EARLIER FY is short while a LATER one is already
    complete — that's the pattern a date-ordered loader would never produce on its own. Removing the very last
    (most recent) voucher would look identical to a legitimately-interrupted first run and must NOT be flagged
    (see test_a_resumed_run_is_not_flagged_as_drift below) — so this test removes one from the FIRST FY, which
    has three later, fully-loaded FYs proving it should already be complete."""
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)

    def remove_one_from_first_fy(s):
        early_mid = next(mid for mid, v in s["vouchers"].items() if v["date"] < "20230401")
        del s["vouchers"][early_mid]

    books.edit_state(remove_one_from_first_fy)
    report = load_company_b(writer, io)
    assert any("2022-23" in p or "count" in p.lower() for p in report.problems)


def test_a_resumed_run_is_not_flagged_as_drift():
    """I2: the reviewer's reproduction — a partial LATEST FY (nothing later exists yet to prove it "should"
    already be complete) is the state of a legitimately-interrupted first run, not evidence of drift."""
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)

    def remove_one_from_last_fy(s):
        # An expense payment (no bills): deleting it straight from the fake's state can't unwind any bill it
        # settled, and the fake (C35) rightly refuses a recreated Agst Ref that would then over-settle.
        latest_mid = max((mid for mid in s["vouchers"] if "Payment for" in s["vouchers"][mid]["narration"]),
                         key=lambda mid: s["vouchers"][mid]["date"])
        del s["vouchers"][latest_mid]

    books.edit_state(remove_one_from_last_fy)
    report = load_company_b(writer, io)
    assert not any("2025-26" in p and "count was" in p for p in report.problems)


def test_educational_mode_only_uses_dates_tally_accepts():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io, licence="educational")
    dates = [v["date"] for v in books.state["vouchers"].values()]
    assert all(int(d[6:8]) in (1, 2, 31) for d in dates)


def test_vouchers_are_imported_in_date_order():                                                              # M3
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)
    dates = [v["date"] for v in books.state["vouchers"].values()]
    assert dates == sorted(dates)


# --- C1: a failed master create pauses and recovers, never crashes -------------------------------------------------
def test_a_failed_unit_create_pauses_and_recovers_when_the_operator_fixes_it_in_the_ui():
    books = _empty_b()
    books.fail_imports = True
    writer, io, _ = _loader(books)
    tiny = Dataset(groups=(), units=(UnitSpec(name="Box of 10 Nos", first_unit="Box", second_unit="Nos", conversion=10),),
                   items=(), ledgers=(), vouchers=(), licence="licensed")

    def operator_fixes_it(instruction: str) -> None:
        # simulate the operator creating the unit by hand in the Tally UI (bypassing the fake's fail_imports,
        # which only gates the XML import path)
        books.edit_state(lambda s: s["units"].__setitem__("Box of 10 Nos", {"base": "Nos", "conversion": "10"}))

    io2 = ScriptedIO(on_wait=operator_fixes_it)
    report = LoadReport()
    _load_masters(writer, io2, B, tiny, report)          # must not raise
    assert any("Box of 10 Nos" in p for p in report.pauses)


def test_a_failed_master_create_raises_if_the_operator_never_fixes_it():
    books = _empty_b()
    books.fail_imports = True
    writer, io, _ = _loader(books)
    tiny = Dataset(groups=(GroupSpec(name="National Creditors", parent="Sundry Creditors"),),
                   units=(), items=(), ledgers=(), vouchers=(), licence="licensed")
    report = LoadReport()
    with pytest.raises(CompanyBLoadError, match="National Creditors"):
        _load_masters(writer, io, B, tiny, report)
    assert any("National Creditors" in p for p in report.pauses)


def test_a_failed_voucher_create_pauses_and_recovers_when_the_operator_fixes_it_in_the_ui():
    """C1's crash guard was extended to voucher creates (same failure class as master creates) but had no test
    of its own — this is that test, structured exactly like the master-create one above."""
    books = _empty_b()
    books.fail_imports = True
    writer, io, _ = _loader(books)
    tiny_voucher = VoucherSpec(
        tag=999, kind="payment", vch_type="Payment", date=date(2025, 6, 1), party="Office Rent",
        narration="[S0-B:999] Payment for Office Rent",
        lines=(LineSpec(ledger="Office Rent", amount=Decimal("100.00"), deemed_positive=True),
               LineSpec(ledger="HDFC Bank Current A/c", amount=Decimal("-100.00"), deemed_positive=False)),
        inventory=(), bills=())
    tiny = Dataset(groups=(), units=(), items=(), ledgers=(), vouchers=(tiny_voucher,), licence="licensed")

    def operator_creates_it(instruction: str) -> None:
        # simulate the operator creating the voucher by hand in the Tally UI (bypassing the fake's
        # fail_imports, which only gates the XML import path)
        books.edit_state(lambda s: s["vouchers"].__setitem__(
            "9001", {"narration": tiny_voucher.narration, "date": "20250601", "post_dated": "No",
                     "cancelled": "No", "optional": "No", "lines": []}))

    io2 = ScriptedIO(on_wait=operator_creates_it)
    report = LoadReport()
    _load_vouchers(writer, io2, B, tiny, report)          # must not raise
    assert any("[S0-B:999]" in p for p in report.pauses)
    assert report.created["vouchers"] == 0
    assert report.skipped["vouchers"] == 1


def test_a_failed_voucher_create_raises_if_the_operator_never_fixes_it():
    books = _empty_b()
    books.fail_imports = True
    writer, io, _ = _loader(books)
    tiny_voucher = VoucherSpec(
        tag=999, kind="payment", vch_type="Payment", date=date(2025, 6, 1), party="Office Rent",
        narration="[S0-B:999] Payment for Office Rent",
        lines=(LineSpec(ledger="Office Rent", amount=Decimal("100.00"), deemed_positive=True),
               LineSpec(ledger="HDFC Bank Current A/c", amount=Decimal("-100.00"), deemed_positive=False)),
        inventory=(), bills=())
    tiny = Dataset(groups=(), units=(), items=(), ledgers=(), vouchers=(tiny_voucher,), licence="licensed")
    report = LoadReport()
    with pytest.raises(CompanyBLoadError, match=r"S0-B:999"):
        _load_vouchers(writer, io, B, tiny, report)
    assert any("[S0-B:999]" in p for p in report.pauses)


# --- C2: `_verify` has real, direct coverage -------------------------------------------------------------------
def test_verify_reports_a_voucher_count_mismatch_directly():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)
    ds = generate()
    books.edit_state(lambda s: s["vouchers"].popitem())      # doctor the books; no second load, so no healing
    report = LoadReport()
    _verify(writer, B, ds, report)
    assert any("count" in p.lower() for p in report.problems)


def test_verify_reports_a_whole_missing_fy():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)
    ds = generate()

    def wipe_2022_23(s):
        s["vouchers"] = {mid: v for mid, v in s["vouchers"].items() if not ("20220401" <= v["date"] <= "20230331")}

    books.edit_state(wipe_2022_23)
    report = LoadReport()
    _verify(writer, B, ds, report)
    assert any("2022-23" in p for p in report.problems)


def test_verify_passes_when_nothing_was_doctored():
    """The negative case C2 asked for: `_verify` doesn't touch flags at all (that's `_settle_flags`'s job), so
    with F9's opening-balance fix this is a genuine `problems == []` — not a default, not a whitelist. The other
    two `_verify`-direct tests above prove it can find something real; this proves it doesn't cry wolf.
    C42: "nothing doctored" includes the operator honouring the flag pauses — until 201/202 are cancelled by hand
    they post, in Tally and in the fake, and the balances rightly disagree with the expected figures."""
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    load_company_b(writer, io)
    ds = generate()
    report = LoadReport()
    _verify(writer, B, ds, report)
    assert report.problems == []


def test_verify_reports_a_duplicated_voucher():
    """I4: `_read_vouchers` used to key rows by tag, last-wins, and `_verify` counted the dict's values — so 960
    vouchers plus a duplicate read as exactly 960 and the run came back clean. Ruling C17's whole rationale is
    that a duplicate is worse than a gap, so the verifier must be able to see one: the raw row count now drives
    the per-FY comparison, and any tag seen more than once gets its own `problems` line."""
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)

    def duplicate_one_voucher(s):
        mid, v = next((mid, v) for mid, v in s["vouchers"].items() if v["narration"].startswith("[S0-B:5]"))
        s["vouchers"]["90001"] = dict(v)          # the same tag, a second Master ID — what a re-run would land

    books.edit_state(duplicate_one_voucher)
    report = LoadReport()
    _verify(writer, B, generate(), report)
    assert any("[S0-B:5]" in p and "duplicate" in p.lower() for p in report.problems)
    assert any("count mismatch" in p for p in report.problems)     # and the FY count sees the extra copy too


# --- C3: group-level balance verification --------------------------------------------------------------------
def test_verify_reports_a_group_balance_mismatch():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)
    ds = generate()

    def corrupt_one_line(s):
        _mid, v = next(iter(s["vouchers"].items()))
        line = v["lines"][0]
        line["amount"] = str(Decimal(line["amount"]) + Decimal("500.00"))

    books.edit_state(corrupt_one_line)
    report = LoadReport()
    _verify(writer, B, ds, report)
    assert any("balance" in p.lower() for p in report.problems)


def _second_level(rows: list[dict]) -> dict[str, Decimal]:
    """First row per account name, the same way `_verify_balances` picks its comparison rows."""
    out: dict[str, Decimal] = {}
    for row in rows:
        if row["account_name"] not in out and row["closing_balance"] is not None:
            out[row["account_name"]] = row["closing_balance"]
    return out


def test_the_fakes_trial_balance_matches_the_live_fixtures_sign_pattern():
    """F15: `_verify_balances` had been an identity check — the fake replays the exact line amounts the
    expectation is built from, so it could never disagree with itself. That proves nothing about whether either
    side matches REALITY. This test pins polarity against a real Tally export instead: if the fake's TB — built
    from a completely independent code path (FakeBooks' own group-nature inference, not `company_b_data.py`'s
    dataset) — landed on the OPPOSITE polarity, this is what would catch a "corrected" F12 that over-corrected
    into the mirror image. It does not prove the live run will show this (only the live run can).

    I3 (final review) — RE-ANCHORED, and two assertions deliberately changed:

    * The anchor was `fixtures/tally_samples/trial_balance_live.xml`, whose own SOURCE.md says verbatim "parser
      tests only, never seed-parity anchors" (it is the NUVANTA company's data, not the seed company). It now
      anchors to this project's OWN live company-A captures, `p16_A_tb_fy_end.xml` and
      `p17_A_tb_exploded_explodealllevels.xml` (probe 16/17, live 2026-09-23), and asserts they agree.
    * `Current Assets < 0` is GONE, on both halves. That assertion came from the forbidden fixture and is
      contradicted by company A's own live capture, which shows Current Assets **+2,605,093.00** — because A's
      `Bank Accounts` (+1,696,830) and `Cash-in-Hand` (+23,000) carry the wrong sign for debit-natured groups,
      the known seed defect the tracker records. Those two buckets are therefore deliberately NOT asserted
      here either. In their place this pins the second-level buckets `_verify_balances` actually compares and
      whose live signs are trustworthy: Sundry Debtors (debit, negative), Sundry Creditors (credit, positive),
      Duties & Taxes (net input GST, debit, negative).
    """
    live_rows = exploded_tb_rows(LIVE_A_TB_FY_END.read_text(encoding="utf-8"))
    exploded_rows = exploded_tb_rows(LIVE_A_TB_EXPLODED.read_text(encoding="utf-8"))
    live, live_second = primary_group_rows(live_rows), _second_level(live_rows)
    exploded_second = _second_level(exploded_rows)                          # the two live exports agree on
    assert {k: v for k, v in exploded_second.items() if k in live_second} == live_second   # every shared bucket
    assert live["Capital Account"]["closing_balance"] > 0
    assert live["Sales Accounts"]["closing_balance"] > 0
    assert live["Purchase Accounts"]["closing_balance"] < 0
    assert live["Indirect Expenses"]["closing_balance"] < 0
    assert live_second["Sundry Debtors"] < 0
    assert live_second["Sundry Creditors"] > 0
    assert live_second["Duties & Taxes"] < 0

    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    load_company_b(writer, io)
    fake_rows = exploded_tb_rows(writer.b_trial_balance(B, B_READBACK_FROM, B_READBACK_TO))
    fake, fake_second = primary_group_rows(fake_rows), _second_level(fake_rows)
    assert fake["Capital Account"]["closing_balance"] > 0
    assert fake["Sales Accounts"]["closing_balance"] > 0
    assert fake["Purchase Accounts"]["closing_balance"] < 0
    assert fake["Indirect Expenses"]["closing_balance"] < 0
    assert fake_second["Sundry Debtors"] < 0
    assert fake_second["Sundry Creditors"] > 0
    # C41: company B's purchases now buy the stock its sales sell, at cost below the sale rate, so its Output GST
    # outweighs its Input GST — Duties & Taxes nets to a CREDIT (a liability), positive by the same live convention
    # as Sundry Creditors. Before C41 the purchases were random amounts ~2.7x the sales, a net input-GST debit.
    # Company A's live capture (a net input-GST debit, asserted above) still pins the negative half of that rule.
    ds = generate()
    gst = {name: sum((line.amount for v in ds.vouchers if not (v.cancelled or v.skip_reason)
                      for line in v.lines if line.ledger == name), Decimal("0.00"))
           for name in ("Input CGST", "Input SGST", "Output CGST", "Output SGST")}
    assert gst["Output CGST"] + gst["Output SGST"] > -(gst["Input CGST"] + gst["Input SGST"]) > 0   # not vacuous
    assert fake_second["Duties & Taxes"] > 0


# --- I1: the flag pause re-reads and reports if it still didn't take -----------------------------------------------
def test_a_flag_that_never_sticks_is_reported_as_a_problem_after_the_reread():
    books = _empty_b()
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    # ISCANCELLED is never written at all (create_b_voucher's own docstring) and nothing in this fake simulates
    # an operator fixing it, so the re-read after the pause must still find it wrong.
    assert any("[S0-B:201]" in p and "ISCANCELLED" in p for p in report.problems)


# --- I3: the four flag-tagged vouchers are never auto-recreated on a resumed run --------------------------------
def test_a_missing_flagged_voucher_on_a_resumed_run_is_not_recreated():
    books = _empty_b()
    writer, io, _ = _loader(books)
    load_company_b(writer, io)

    def remove_tag_201(s):
        mid = next(mid for mid, v in s["vouchers"].items() if v["narration"].startswith("[S0-B:201]"))
        del s["vouchers"][mid]

    books.edit_state(remove_tag_201)
    report = load_company_b(writer, io)
    assert report.created["vouchers"] == 0
    assert report.skipped["vouchers"] == 959        # 960 written (plan part 7: 101/102 with forex, was C36) less 201
    assert any("[S0-B:201]" in p for p in report.problems)
    assert any("[S0-B:201]" in p for p in report.pauses)


# --- I4: an existing master under the wrong parent/base unit is flagged --------------------------------------------
def test_an_existing_ledger_under_the_wrong_parent_is_flagged():
    books = _empty_b()
    books.edit_state(lambda s: s["ledgers"].__setitem__(
        "Delhi Metal Traders", {"parent": "Sundry Creditors", "email": "", "alter_id": 999, "guid": "x"}))
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert any("Delhi Metal Traders" in p and "Sundry Creditors" in p for p in report.problems)


def test_an_existing_item_under_the_wrong_base_unit_is_flagged():
    books = _empty_b()
    books.edit_state(lambda s: s["items"].__setitem__("USB Cable Type-C", {"parent": "", "base_units": "Box of 10 Nos"}))
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert any("USB Cable Type-C" in p for p in report.problems)


# --- I5: the opening-bill pause is read-verified, not unconditional ------------------------------------------------
def test_the_opening_bill_pauses_when_absent():
    books = _empty_b()
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert any("Op/2022-001" in p for p in report.pauses)


def test_the_opening_bill_pause_is_skipped_when_the_bill_is_already_there():
    books = _empty_b()
    books.edit_state(lambda s: s["bills"].update(OPENING_BILL))
    writer, io, _ = _loader(books)
    report = load_company_b(writer, io)
    assert not any("Op/2022-001" in p for p in report.pauses)


# --- M2: the line-ordering contract is a real stop, not a bare `assert` --------------------------------------------
def test_an_inventory_voucher_whose_party_is_not_the_first_line_stops_the_load():
    """M2: `create_b_voucher` can only check `len(lines) >= 2` (ledger names carry no semantic tag), so the
    party-first contract lives here. It used to be a bare `assert`: gone under `python -O`, and an
    AssertionError is not caught by `_load_vouchers`' `except WriteFailed`, so it crashed the load mid-run
    rather than stopping it readably."""
    books = _empty_b()
    writer, io, _ = _loader(books)
    misordered = VoucherSpec(
        tag=998, kind="sales", vch_type="Sales", date=date(2025, 6, 1), party="Pune Digital Solutions",
        narration="[S0-B:998] Sale to Pune Digital Solutions",
        lines=(LineSpec(ledger="Domestic Sales", amount=Decimal("1000.00"), deemed_positive=False),
               LineSpec(ledger="Pune Digital Solutions", amount=Decimal("-1000.00"), deemed_positive=True)),
        inventory=(InventorySpec(item="USB Cable Type-C", qty=Decimal("1"), rate=Decimal("1000.00"),
                                 amount=Decimal("1000.00")),),
        bills=())
    tiny = Dataset(groups=(), units=(), items=generate().items, ledgers=(), vouchers=(misordered,),
                   licence="licensed")
    report = LoadReport()
    with pytest.raises(CompanyBLoadError, match="must list the party"):
        _load_vouchers(writer, io, B, tiny, report)
    assert [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r] == []


# --- C37 (review #7): every voucher is validated before the first one is sent --------------------------------------
def test_bad_vouchers_stop_the_load_before_any_voucher_is_written_and_are_all_named():
    """create_b_voucher's ValueErrors (balance, C32 allocation, C34 bills) used to escape `_load_vouchers`
    (it catches WriteFailed only) and crash the load mid-run, after earlier vouchers were already in Tally. Now the
    whole dataset is validated first; every bad tag is named in one CompanyBLoadError, and nothing is imported —
    not even the good voucher dated before them."""
    books = _empty_b()
    writer, io, _ = _loader(books)
    good = VoucherSpec(
        tag=990, kind="payment", vch_type="Payment", date=date(2022, 4, 1), party="Office Rent",
        narration="[S0-B:990] Payment for Office Rent",
        lines=(LineSpec(ledger="Office Rent", amount=Decimal("-100.00"), deemed_positive=True),
               LineSpec(ledger="HDFC Bank Current A/c", amount=Decimal("100.00"), deemed_positive=False)),
        inventory=(), bills=())
    unbalanced = replace(good, tag=991, date=date(2022, 5, 1), narration="[S0-B:991] unbalanced",
                         lines=(good.lines[0], replace(good.lines[1], amount=Decimal("99.00"))))
    bad_bill = replace(good, tag=992, date=date(2022, 6, 1), narration="[S0-B:992] bad bill", party="Office Rent",
                       bills=(BillSpec(name="Pur/1", bill_type="Agst Ref", amount=Decimal("50.00"), credit_period=None),))
    tiny = Dataset(groups=(), units=(), items=(), ledgers=(), vouchers=(good, unbalanced, bad_bill),
                   licence="licensed")
    with pytest.raises(CompanyBLoadError) as info:
        _load_vouchers(writer, io, B, tiny, LoadReport())
    assert "[S0-B:991]" in str(info.value) and "[S0-B:992]" in str(info.value)
    assert "[S0-B:990]" not in str(info.value)
    assert [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r] == []


def test_the_whole_company_b_dataset_passes_pre_validation():
    from probes.setup.company_b import validate_dataset
    for licence in ("licensed", "educational"):
        assert validate_dataset(generate(licence)) == []


# --- C39: opening stock goes out as a debit, so the opening trial balance closes ---------------------------------------
def test_after_a_full_load_the_trial_balance_nets_to_zero_with_opening_stock_on_the_debit_side():
    """Live 2026-09-24: with USB Cable's OPENINGVALUE sent +10200 the TB put it on the Cr side and did not close;
    re-sent −10200 (and A4 Paper −14250) it balanced exactly. The dataset's openings net to zero only with the
    24,450 of opening stock as a debit (test_opening_balances_net_to_zero), and every voucher balances."""
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io)
    # C47: the USD party is valued at the latest voucher rate, Export Sales at the bases — the TB is out by the
    # unrealised forex difference, exactly as live (183.87 on 2026-09-25), and the note says it's expected.
    assert any("Trial Balance Dr/Cr total observed: 183.87" in n and "expected 183.87" in n and "C47" in n
               for n in report.notes), report.notes
    assert not report.problems


# --- C40: compound-unit quantities go out in the compound's first unit, masters and vouchers alike ----------------------
def test_a4_paper_is_created_and_sold_in_boxes_never_in_the_compound_units_full_name():
    """Masters live-verified 2026-09-24; the voucher-row form ("<n> Box" on ACTUALQTY/BILLEDQTY, "/Box" on RATE) is
    INFERRED from the master behaviour and not yet live-verified for vouchers (tag 2 will show it)."""
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    load_company_b(writer, io)
    imports = [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]
    item = next(r for r in imports if 'STOCKITEM NAME="A4 Paper Ream"' in r)
    assert "<OPENINGBALANCE>15 Box</OPENINGBALANCE>" in item and "<OPENINGRATE>950.00/Box</OPENINGRATE>" in item
    a4_rows = re.findall(r"<STOCKITEMNAME>A4 Paper Ream</STOCKITEMNAME>.*?</ALLINVENTORYENTRIES.LIST>",
                         "".join(imports), re.S)
    assert a4_rows
    for row in a4_rows:
        assert re.search(r"<ACTUALQTY>\d+ Box</ACTUALQTY>", row) and re.search(r"<BILLEDQTY>\d+ Box</BILLEDQTY>", row)
        assert re.search(r"<RATE>[\d.]+/Box</RATE>", row)
    assert books.state["items"]["A4 Paper Ream"]["opening_value"] == "-14250.00"


# --- C41: purchases carry stock, so a full load never takes an item negative -------------------------------------------
@pytest.mark.parametrize("licence", ["licensed", "educational"])
def test_a_full_load_never_takes_an_item_negative_and_the_trial_balance_still_closes(licence):
    """Before C41 every purchase was accounting-only, so the sales alone drove each item negative (Wireless Mouse
    −983 by the end; live Tally showed −12 after tag 1). The fake now tracks stock per item from the inventory rows
    it is actually sent (ACTUALQTY + ISDEEMEDPOSITIVE) on top of each item's opening quantity."""
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence=licence)
    assert report.problems == []
    # C47: the TB is out by exactly the unrealised forex difference (183.87 in both licences), and nothing else.
    assert any("Trial Balance Dr/Cr total observed: 183.87; expected 183.87" in n for n in report.notes), report.notes
    imports = [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]
    purchases = [r for r in imports if 'VCHTYPE="Purchase"' in r]
    assert len(purchases) == 240 and all("<ALLINVENTORYENTRIES.LIST>" in r for r in purchases)
    for r in purchases:          # Op 7: goods in (Yes/−), the allocation to Local Purchases likewise
        assert re.search(r"<ALLINVENTORYENTRIES.LIST>.*?<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>\s*"
                         r"<RATE>[\d.]+/(Nos|Box)</RATE>\s*<AMOUNT>-[\d.]+</AMOUNT>", r, re.S), r
        assert re.search(r"<LEDGERNAME>Local Purchases</LEDGERNAME>\s*<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>"
                         r"\s*<AMOUNT>-[\d.]+</AMOUNT>\s*</ACCOUNTINGALLOCATIONS.LIST>", r), r
    closes = books.stock_day_closes()
    assert len(closes) > 100
    negative = [(day, item, qty) for day, levels in closes for item, qty in levels.items() if qty < 0]
    assert negative == [], negative[:5]
    expected = expected_figures(generate(licence))
    assert closes[-1][1] == {item: qty for (item, day), qty in expected.stock_month_end.items()
                             if day == date(2026, 3, 31)}


# --- C42 (live 2026-09-24, logs/setup-b-live-2026-09-24-run4.log): the live figures are ground truth -----------------
# Run 4 landed all 958 vouchers of the EDUCATIONAL dataset, the operator set ISCANCELLED on 201/202 by hand, 301/302
# went in optional — and Tally's Trial Balance showed these three buckets (magnitudes; the other buckets matched).
LIVE_RUN4_EDUCATIONAL = {"Sundry Debtors": Decimal("2457218.56"), "Sales Accounts": Decimal("4185668.83"),
                         "Duties & Taxes": Decimal("353626.24")}


# Plan part 7: run 4 had 101/102 skipped (C36). Written now, both USD sales' INR base (₹37,216.04 + ₹95,897.68) adds
# to Sales Accounts (Export Sales). Sundry Debtors gains the USD party valued at the LATEST voucher rate instead
# (C47, live 2026-09-25: $1,609.71 × 82.58 = ₹1,32,929.85), and nothing else moves.
USD_SALES_BASE = Decimal("37216.04") + Decimal("95897.68")
USD_PARTY_REVALUED = Decimal("132929.85")
EDUCATIONAL_WITH_FOREX = {b: v + {"Sundry Debtors": USD_PARTY_REVALUED, "Sales Accounts": USD_SALES_BASE}.get(b, 0)
                          for b, v in LIVE_RUN4_EDUCATIONAL.items()}
# C47: the live forex load's Sundry Debtors (logs/setup-b-forex-live-2026-09-25.log, "Tally has |-2590148.41|").
LIVE_FOREX_SUNDRY_DEBTORS = Decimal("2590148.41")


def test_expected_sundry_debtors_is_the_live_forex_load_figure_c47():
    assert EDUCATIONAL_WITH_FOREX["Sundry Debtors"] == LIVE_FOREX_SUNDRY_DEBTORS


def test_expected_group_balances_for_educational_equal_the_live_run4_figures():
    expected = _expected_group_balances(generate("educational"))
    assert {b: abs(expected[b]) for b in LIVE_RUN4_EDUCATIONAL} == EDUCATIONAL_WITH_FOREX       # plan part 7


def test_an_educational_load_through_the_fake_lands_the_live_run4_figures_and_verifies_clean():
    books = _empty_b()
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence="educational")
    assert report.problems == []
    rows = exploded_tb_rows(writer.b_trial_balance(B, B_READBACK_FROM, B_READBACK_TO))
    actual = {r["account_name"]: abs(r["closing_balance"]) for r in rows if r["account_name"] in LIVE_RUN4_EDUCATIONAL}
    assert actual == EDUCATIONAL_WITH_FOREX                                                    # plan part 7


# --- plan part 7: the USD export sales written with the live forex shape (C36 lifted) -------------------------------
def _full_load(licence: str = "educational"):
    books = _empty_b(educational=licence == "educational")
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    return books, load_company_b(writer, io, licence=licence)


def _loaded_b():
    """Company B as a clean part-7 `setup-b` leaves it (seed_company_b: masters, the `$` currency, all 960)."""
    books = FakeBooks(name=B, educational=True)
    seed_company_b(books, "educational", masters=True)
    return books


def _drop_tags(*tags):
    def mutate(state):
        for mid in [m for m, v in state["vouchers"].items()
                    if any(v["narration"].startswith(f"[S0-B:{t}]") for t in tags)]:
            del state["vouchers"][mid]
    return mutate


def _load(books):
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    return load_company_b(writer, io, licence="educational")


def _imports(books) -> list[str]:
    return [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]


def test_the_loader_never_creates_the_currency_and_stops_before_any_write_without_it():
    """Live 2026-09-25 (forex_shape_2026-09-25_run1_currency_refused/): the XML `$` create is refused and leaves an
    import-exception master behind that then blocks the UI create. So setup-b never sends one: no `$` → stop, name
    the UI steps, and write nothing at all."""
    books = _empty_b(usd_currency=False)
    writer, io, _ = _loader(books)
    with pytest.raises(CompanyBLoadError, match=r"(?s)'\$'.*Formal name USD.*Create → Currency"):
        load_company_b(writer, io, licence="educational")
    assert _imports(books) == [] and not any("<CURRENCY " in r for r in books.requests)


def test_a_currency_listed_under_its_formal_name_satisfies_the_currency_check():
    from probes.setup.company_b import _require_currencies
    books = _empty_b(usd_currency=False)
    books.edit_state(lambda s: s["currencies"].__setitem__("USD", {"MailingName": "USD", "ExpandedSymbol": "USD"}))
    writer, _, _ = _loader(books)
    report = LoadReport()
    _require_currencies(writer, B, generate("educational"), report)       # review I5: symbol OR formal name
    assert (report.created["currencies"], report.skipped["currencies"]) == (0, 1)
    assert _imports(books) == []


def test_load_uses_the_ui_created_currency_for_the_usd_ledger():
    books, report = _full_load()
    assert (report.created["currencies"], report.skipped["currencies"]) == (0, 1) and not report.problems
    assert not any("<CURRENCY " in r for r in books.requests)
    ledger = next(r for r in _imports(books) if f'LEDGER NAME="{USD_EXPORT_PARTY}"' in r)
    assert "<CURRENCYNAME>$</CURRENCYNAME>" in ledger and "<ISBILLWISEON>No</ISBILLWISEON>" in ledger
    assert books.state["ledgers"][USD_EXPORT_PARTY]["currency"] == "$"
    assert "currency" not in books.state["ledgers"][USD_DEBTOR]          # fact 1: Gulf stays an INR ledger


def test_usd_sales_go_out_with_the_live_v1b_amounts():
    """V1b (forex_shape_2026-09-25_run2): the full form with the rate written WITHOUT a base symbol — V1's `@ ?82.99`
    was refused."""
    books, _ = _full_load()
    for tag, fx, rate, inr in ((101, "448.44", "82.99", "37216.04"), (102, "1161.27", "82.58", "95897.68")):
        body = next(r for r in _imports(books) if f"[S0-B:{tag}]" in r)
        amounts = [html.unescape(a) for a in re.findall(r"<AMOUNT>([^<]*)</AMOUNT>", body)]
        assert amounts == [f"-${fx} @ {rate}/$ = -{inr}", f"${fx} @ {rate}/$ = {inr}"]
        assert "BILLALLOCATIONS" not in body and "INVENTORY" not in body


def test_second_load_sends_no_currency_create_and_no_voucher():
    books, _ = _full_load()
    before = len(_imports(books))
    report = _load(books)
    assert report.created == {k: 0 for k in report.created} and report.skipped["currencies"] == 1
    assert len(_imports(books)) == before and not report.problems


def test_a_foreign_voucher_without_fx_fields_stops_the_load(monkeypatch):
    from probes.setup import company_b
    ds = generate("educational")
    broken = replace(ds, vouchers=tuple(replace(v, fx_rate=None) if v.tag == 101 else v for v in ds.vouchers))
    monkeypatch.setattr(company_b, "generate", lambda licence="licensed": broken)
    books = _empty_b()
    writer, io, _ = _loader(books)
    with pytest.raises(CompanyBLoadError, match=r"\[S0-B:101\].*fx"):
        load_company_b(writer, io, licence="educational")
    assert not any("[S0-B:" in r for r in _imports(books))                     # before any voucher is sent


def test_formerly_skipped_gap_is_a_note_not_a_problem():
    books = _loaded_b()
    books.edit_state(_drop_tags(101, 102))                  # = live B after C36: 958 vouchers, `$` made in the UI
    books.edit_state(lambda s: s["ledgers"].pop(USD_EXPORT_PARTY))
    report = _load(books)
    assert report.created["vouchers"] == 2 and report.created["ledgers"] == 1
    assert (report.created["currencies"], report.skipped["currencies"]) == (0, 1)
    assert not report.problems, report.problems
    assert any("[S0-B:101, 102]" in n and "plan part 7" in n for n in report.notes)


def test_any_other_gap_in_that_fy_is_still_a_problem():
    books = _loaded_b()
    books.edit_state(_drop_tags(101, 102, 103))
    report = _load(books)
    assert any("FY 2022-23" in p and "missing" in p for p in report.problems)
    assert not any("skipped under C36" in n for n in report.notes)


def test_existing_usd_party_without_its_currency_is_a_problem_and_never_altered():
    books = _loaded_b()
    books.edit_state(lambda s: s["ledgers"][USD_EXPORT_PARTY].__setitem__("currency", ""))
    report = _load(books)
    assert any(USD_EXPORT_PARTY in p and "currency" in p and "never altered" in p for p in report.problems)
    assert not any(USD_EXPORT_PARTY in r for r in _imports(books))


def test_full_load_with_forex_verifies_clean():
    books, report = _full_load()
    assert report.created["vouchers"] == 960 and not report.problems            # TB magnitudes incl. the base


def test_forex_sale_round_trips_through_the_extractors_request():
    """CLAUDE.md "Test reality" rule 7, S0 edition: load → read back through probe 5's confirmed request (the typed
    `svdates_typed` form live confirmed) → the forex text survives and parses to the dataset's INR base."""
    from probes import p05_voucher_month_bounds as p05
    from probes.company_b_view import tag_of
    from probes.reads import fill_month_request, forex_base, parse_forex_amount, parse_vouchers, primary_lines
    books, _ = _full_load()
    writer, _, _ = _loader(books)
    raw = writer.post(fill_month_request(p05.svdates_template(), B, "01-09-2022", "02-09-2022"))
    by_tag = {tag_of(v["header"]["NARRATION"]): v for v in parse_vouchers(raw)}
    for tag, inr in ((101, Decimal("37216.04")), (102, Decimal("95897.68"))):
        lines = primary_lines(by_tag[tag])
        assert all(l["amount_raw"].count("? ") == 2 for l in lines)            # the live export layout
        bases = [forex_base(parse_forex_amount(l["amount_raw"]))[0] for l in lines]
        assert sorted(bases) == [-inr, inr] and sum(bases) == 0


# --- plan part 7 review I1: setup-b reads every forex voucher back — created=1 is not proof ---------------------------
def _corrupt_on_readback(books, tag: int, ledger: str, text: str):
    """Tally keeps something else than what was sent: rewrite the stored line just before the first day read-back."""
    def before(body: str) -> None:
        if "S0FxDay" not in body:
            return

        def mutate(state):
            v = next(v for v in state["vouchers"].values() if v["narration"].startswith(f"[S0-B:{tag}]"))
            next(l for l in v["lines"] if l["ledger"] == ledger)["amount_text"] = text
        books.edit_state(mutate)
    books.before_request = before


def test_forex_readback_passes_and_leaves_a_note_on_a_clean_load():
    books, report = _full_load()
    assert not report.problems
    days = [r for r in books.requests if "S0FxDay" in r]
    assert len(days) == 2 and '<SVFROMDATE TYPE="Date">01-09-2022' in days[0]    # each voucher's own C43-safe day
    assert any("[S0-B:101, 102]" in n and "read back as forex" in n for n in report.notes)


def test_a_forex_sale_stored_as_plain_inr_is_a_problem():
    """Review I1 / Review Focus 1: created=1, the count and every group magnitude match — only a read-back sees C36."""
    books = _empty_b(educational=True)
    books.forex_storage = "plain"
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence="educational")
    bad = [p for p in report.problems if "[S0-B:101]" in p or "[S0-B:102]" in p]
    assert len(bad) == 2 and all("plain INR" in p and "pre-forex-with-usd" in p for p in bad), report.problems


def test_a_forex_sale_stored_with_another_base_is_a_problem():
    books = _empty_b(educational=True)
    _corrupt_on_readback(books, 101, USD_EXPORT_PARTY, "-$448.44 @ ? 82.99/$ = -? 37216.05")
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence="educational")
    bad = [p for p in report.problems if "[S0-B:101]" in p]
    assert len(bad) == 1 and "base" in bad[0] and "37216.05" in bad[0], report.problems
    assert not any("[S0-B:102]" in p for p in report.problems)


@pytest.mark.parametrize("text, what", [
    ("-$448.44 @ ? 83.00/$ = -? 37216.04", "rate"),
    ("-$448.45 @ ? 82.99/$ = -? 37216.04", "face"),
    ("-€448.44 @ ? 82.99/€ = -? 37216.04", "currency"),
])
def test_a_forex_sale_with_another_face_rate_or_currency_is_a_problem(text, what):
    books = _empty_b(educational=True)
    _corrupt_on_readback(books, 101, USD_EXPORT_PARTY, text)
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence="educational")
    assert any("[S0-B:101]" in p and what in p for p in report.problems), report.problems


def test_an_existing_plain_usd_sale_is_caught_on_a_rerun_too():
    """A re-run after a bad load must not exit 0 and re-stamp: the check covers every forex voucher in Tally,
    not just the ones this run created."""
    books = _loaded_b()
    books.edit_state(lambda s: [l.pop("amount_text", None) for v in s["vouchers"].values()
                                if v["narration"].startswith("[S0-B:102]") for l in v["lines"]])
    report = _load(books)
    assert report.created["vouchers"] == 0
    assert any("[S0-B:102]" in p and "plain INR" in p for p in report.problems)


def test_a_forex_readback_that_times_out_is_a_problem_not_a_crash():
    books = _empty_b(educational=True)

    def before(body: str) -> None:
        if "S0FxDay" in body:
            books.popup = True                              # a modal: every request from here times out
    books.before_request = before
    writer, io, _ = _loader(books, on_wait=_operator_who_honours_flag_pauses(books))
    report = load_company_b(writer, io, licence="educational")
    assert [p for p in report.problems if "forex read-back" in p and "failed" in p]
    assert not any("read back as forex" in n for n in report.notes)
