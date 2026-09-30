import io
import json
import sys
from decimal import Decimal

import pytest

from probes.companies import COMPANIES
from probes.reads import parse_forex_amount
from probes.safety import GuardError
from probes.setup import forex_shape
from probes.setup.company_b_data import USD_DEBTOR
from probes.setup.writes import TallyWriter, WriteFailed, WriteRefused
from tests.probes.fake_books import CANDIDATE_FOREX_KNOBS, FakeBooks, seed_company_b, sync_client

B = COMPANIES["B"]


def _books(**knobs) -> FakeBooks:
    # plan part 7 Task 3.9: the fake's defaults are now the live answers; these tests pin Task 1's code against the
    # candidate ones (an XML currency create that works, a "?" rate accepted) unless a test names its own knob.
    books = FakeBooks(name=B, educational=True, **{**CANDIDATE_FOREX_KNOBS, **knobs})
    seed_company_b(books, "educational", masters=True)
    # Pre-flight F2: the part-7 seed has the UI-made `$` (live B does); Task 1's tests start from B without it.
    books.edit_state(lambda s: s["currencies"].pop("$"))
    return books


def _run(books, tmp_path):
    writer = TallyWriter(sync_client(books.transport()), say=lambda _m: None)
    return forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)


def _throwaways_gone(books) -> bool:
    ledgers = set(books.state["ledgers"])
    return (forex_shape.USD_PARTY not in ledgers and forex_shape.INR_PARTY not in ledgers
            and not any(v["narration"].startswith("S0-throwaway forex") for v in books.state["vouchers"].values()))


def _neighbours(books) -> tuple:
    """Gulf and every real (non-throwaway) voucher: the run must leave them byte-identical (Global Constraints)."""
    state = books.state
    return (state["ledgers"][USD_DEBTOR],
            {m: v for m, v in state["vouchers"].items() if not v["narration"].startswith("S0-throwaway forex")})


def _summary(tmp_path) -> dict:
    return json.loads((tmp_path / "shape" / "summary.json").read_text(encoding="utf-8"))


def test_run_stores_forex_on_both_parties_and_cleans_up(tmp_path):
    books = _books()
    before = _neighbours(books)
    report = _run(books, tmp_path)
    assert report.outcome == "stored_forex" and report.base_symbol == "?"
    assert report.chosen == {"variant": "V1", "party_currency": "$", "form": "full", "base_symbol": "?"}
    assert _neighbours(books) == before
    body = next(r for r in books.requests if "S0-throwaway forex V1" in r and "Import" in r)
    assert "<AMOUNT>-$448.44 @ ?82.99/$ = -?37216.04</AMOUNT>" in body         # R-SYM: the discovered symbol
    assert report.numbering["changed"] is False and report.numbering["before"] > 0
    assert [v.id for v in report.variants] == ["V0", "V1", "V3"]                   # V2 only when V1 fails
    assert {v.id: v.classification for v in report.variants} == {"V0": "plain_inr", "V1": "forex_full",
                                                                  "V3": "forex_full"}
    assert _throwaways_gone(books) and "$" in books.state["currencies"]            # the currency is kept
    files = {p.name for p in (tmp_path / "shape").iterdir()}
    assert {"company_features.xml", "currencies_before.xml", "currencies_after.xml", "ledgers_after_create.xml",
            "variant_V0.xml", "variant_V1.xml", "variant_V3.xml", "numbers_before.xml", "numbers_after.xml",
            "summary.json"} <= files
    assert json.loads((tmp_path / "shape" / "summary.json").read_text())["outcome"] == "stored_forex"


def test_forex_dropped_when_tally_stores_plain_inr(tmp_path):
    books = _books(forex_storage="plain")
    report = _run(books, tmp_path)
    assert report.outcome == "forex_dropped" and report.chosen is None
    assert [v.id for v in report.variants] == ["V0", "V1", "V1b", "V2", "V3"]
    assert all(v.classification == "plain_inr" for v in report.variants)
    assert _throwaways_gone(books)


def test_only_the_no_base_form_accepted_picks_v2(tmp_path):
    report = _run(_books(forex_forms_accepted=("no_base",), forex_export_form="no_base"), tmp_path)
    assert report.outcome == "stored_forex"
    assert report.chosen == {"variant": "V2", "party_currency": "$", "form": "no_base", "base_symbol": "?"}
    assert {v.id: v.classification for v in report.variants}["V1"] == "refused"
    assert {v.id: v.classification for v in report.variants}["V1b"] == "refused"


def test_currency_refused_stops_before_any_throwaway(tmp_path):
    books = _books(forex_currency_create="refuse")
    report = _run(books, tmp_path)
    assert report.outcome == "currency_refused" and report.variants == []
    assert not any("ZZ Forex Probe" in r for r in books.requests)


def test_a_currency_popup_stops_with_the_hint(tmp_path):
    report = _run(_books(forex_currency_create="popup"), tmp_path)
    assert report.outcome == "popup" and "popup" in report.notes[-1].lower()


def test_control_failure_is_reported_first(tmp_path):
    books = _books(refuse_narrations=("S0-throwaway forex V0",))                   # V0 cannot post
    report = _run(books, tmp_path)
    assert report.outcome == "control_failed" and [v.id for v in report.variants] == ["V0"]


def test_base_party_refusal_is_evidence_not_failure(tmp_path):
    report = _run(_books(forex_on_base_party="refuse"), tmp_path)
    assert report.outcome == "stored_forex"
    assert {v.id: v.classification for v in report.variants}["V3"] == "refused"


def test_a_delete_that_does_not_stick_is_caught_in_the_2022_window(tmp_path):
    with pytest.raises(WriteFailed, match="still there"):
        _run(_books(deletes_stick=False), tmp_path)


def test_leftover_throwaway_refuses_to_run(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["ledgers"].__setitem__(forex_shape.USD_PARTY, dict(s["ledgers"]["Cash"])))
    with pytest.raises(WriteFailed, match="leftover"):
        _run(books, tmp_path)


def test_wrong_company_is_refused_before_any_write(tmp_path):
    books = FakeBooks(name=COMPANIES["A"], educational=True)
    with pytest.raises(GuardError):
        _run(books, tmp_path)
    assert not any("Import" in r for r in books.requests)


@pytest.mark.parametrize("amount, fields, expected", [
    ("-$448.44 @ ?82.99/$ = -?37216.04", {}, "forex_full"),
    ("-$448.44 @ 82.99/$ = -37216.04", {}, "forex_full"),                 # R-SYM: the rate without a base symbol
    ("-$448.44 @ ?82.99/$", {}, "forex_no_base"),
    ("-37216.04", {"FOREXAMOUNT": "-$448.44"}, "plain_with_forex_field"),
    ("-37216.04", {}, "plain_inr"),
    ("-37216.05", {}, "other"),
    # I1: values that parse but are not what was sent — never "stored"
    ("-$37216.04 @ ?82.99/$ = -?3088558.16", {}, "forex_mismatch"),      # Tally reinterpreted the amount
    ("-?37216.04 @ ?1.00/? = -?37216.04", {}, "forex_mismatch"),          # a base-currency line in expression form
    ("-$448.44 @ ?83.00/$ = -?37220.52", {}, "forex_mismatch"),           # another rate
    ("$448.44 @ ?82.99/$ = ?37216.04", {}, "forex_mismatch"),             # the sign flipped
    ("-$448.44 @ ?82.99/$ = -?37216.05", {}, "forex_mismatch"),           # a stated base ≠ the INR sent
    ("-$448.44 @ ₹82.99/$ = -₹37216.04", {}, "forex_mismatch"),           # a base symbol that isn't the discovered one
    # I1: the field route is strict — a stray "$" or "@" is not a forex value
    ("-37216.04", {"LEDGERCURRENCY": "$"}, "plain_inr"),
    ("-37216.04", {"NOTE": "@ desk"}, "plain_inr"),
    ("-37216.04", {"FOREXAMOUNT": "448.44"}, "plain_inr"),                 # the face without the currency symbol
])
def test_classify(amount, fields, expected):
    line = {"amount_raw": amount, "fields": {"AMOUNT": amount, **fields}}
    assert forex_shape.classify(line, Decimal("-37216.04"), base_symbol="?") == expected


def test_forex_problems_name_what_differs():
    problems = forex_shape.forex_problems(parse_forex_amount("-$37216.04 @ ?82.99/$ = -?3088558.16"),
                                          Decimal("-37216.04"), base_symbol="?")
    assert any("448.44" in p for p in problems) and any("base" in p for p in problems)


def test_cleanup_failure_does_not_mask_the_first_error(tmp_path):
    # Not in the plan (deviation D3): live Tally refuses to delete a ledger that still has a voucher, so after a
    # delete that did not stick the ledger cleanup fails too — the "still there" error must be the one raised.
    books = _books(deletes_stick=False)
    writer = TallyWriter(sync_client(books.transport()), say=lambda _m: None)

    def refuse(_company, name):
        raise WriteFailed(f"Ledger {name!r} not deleted: in use")
    writer.delete_ledger = refuse
    with pytest.raises(WriteFailed, match="still there"):
        forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)


def test_a_dropped_voucher_leaves_an_f2_note(tmp_path):
    # Not in the plan (deviation D4): created=1 but absent from the day (F2 before the date, LESSONS §15 rule 14).
    books = _books()
    writer = TallyWriter(sync_client(books.transport()), say=lambda _m: None)
    real_create = writer.create_b_voucher

    def create_then_vanish(company, **kw):
        mid = real_create(company, **kw)
        if kw["narration"].endswith("V1"):
            books.edit_state(lambda s: s["vouchers"].pop(mid))
        return mid
    writer.create_b_voucher = create_then_vanish
    report = forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)
    assert {v.id: v.classification for v in report.variants}["V1"] == "dropped"
    assert any("F2" in note for note in report.notes)


# --- review 2026-09-25 fix round -------------------------------------------------------------------------------------
def _writer(books) -> TallyWriter:
    return TallyWriter(sync_client(books.transport()), say=lambda _m: None)


def test_a_mismatched_v1_is_not_stored_and_the_bare_rate_is_tried(tmp_path):
    # I1 at run level: Tally keeps "forex" but not the sent values (the amount reinterpreted as dollars).
    books = _books()

    def reinterpret(body):
        if "S0FxDay" not in body:
            return

        def mutate(state):
            for v in state["vouchers"].values():
                if v["narration"] == "S0-throwaway forex V1":
                    for line in v["lines"]:
                        sign = "-" if line["amount"].startswith("-") else ""
                        line["amount_text"] = f"{sign}$37216.04 @ ?82.99/$ = {sign}?3088558.16"
        books.edit_state(mutate)
    books.before_request = reinterpret
    report = _run(books, tmp_path)
    classes = {v.id: v.classification for v in report.variants}
    assert classes["V1"] == "forex_mismatch" and classes["V1b"] == "forex_full"
    assert report.chosen == {"variant": "V1b", "party_currency": "$", "form": "full", "base_symbol": ""}
    assert any(n.startswith("V1:") and "448.44" in n for n in report.notes)


def test_every_variant_mismatched_is_its_own_outcome(tmp_path):
    books = _books()

    def reinterpret(body):
        if "S0FxDay" in body:
            def mutate(state):
                for v in state["vouchers"].values():
                    if v["narration"].startswith("S0-throwaway forex V") and v["narration"] != "S0-throwaway forex V0":
                        for line in v["lines"]:
                            if line.get("amount_text"):
                                line["amount_text"] = line["amount_text"].replace("82.99", "83.99")
            books.edit_state(mutate)
    books.before_request = reinterpret
    report = _run(books, tmp_path)
    assert report.outcome == "forex_mismatch" and report.chosen is None


def test_a_refused_base_symbol_falls_back_to_the_bare_rate(tmp_path):
    # R-SYM: `@ ?82.99/$` refused → `@ 82.99/$` tried next, judged on its read-back.
    report = _run(_books(forex_rate_symbols_refused=("?",)), tmp_path)
    assert {v.id: v.classification for v in report.variants}["V1"] == "refused"
    assert report.outcome == "stored_forex"
    assert report.chosen == {"variant": "V1b", "party_currency": "$", "form": "full", "base_symbol": ""}
    assert [v.base_symbol for v in report.variants if v.id == "V3"] == [""]       # V3 reuses the chosen form


def test_unknown_base_currency_stops_before_any_write(tmp_path):
    books = _books()
    books.edit_state(lambda s: s.__setitem__("currencies", {}))
    report = _run(books, tmp_path)
    assert report.outcome == "base_currency_unknown"
    assert not any("Import" in r for r in books.requests) and _summary(tmp_path)["outcome"] == "base_currency_unknown"


def test_v0_dropped_by_f2_is_named_as_f2_not_a_broken_shape(tmp_path):
    # I2: F2 reset by a restart drops every voucher; V0 must not send anyone to debug the V0 shape.
    books = _books()
    writer = _writer(books)
    real_create = writer.create_b_voucher

    def create_then_vanish(company, **kw):
        mid = real_create(company, **kw)
        books.edit_state(lambda s: s["vouchers"].pop(mid))
        return mid
    writer.create_b_voucher = create_then_vanish
    report = forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)
    assert report.outcome == "f2_or_date_dropped" and [v.id for v in report.variants] == ["V0"]
    assert any("F2" in n for n in report.notes) and not any("shape is wrong" in n for n in report.notes)
    assert forex_shape.USD_PARTY not in books.state["ledgers"]


def test_a_ledger_currency_that_does_not_stick_is_cleaned_up_and_reported(tmp_path):
    # I3: created=1 but CURRENCYNAME dropped → the read-back raises; the ledger must still be deleted.
    books = _books(ledger_currency_sticks=False)
    report = _run(books, tmp_path)
    assert report.outcome == "ledger_currency_refused" and report.variants == []
    assert _throwaways_gone(books)
    assert not any("S0-throwaway forex" in r and "Import" in r for r in books.requests)
    assert (tmp_path / "shape" / "ledgers_after_create.xml").exists()
    assert _summary(tmp_path)["outcome"] == "ledger_currency_refused"


def test_a_lastvchid_that_is_not_the_read_back_voucher_is_never_deleted(tmp_path):
    # I4: the delete is by Master ID; a LASTVCHID naming a REAL voucher must stop the run, not delete it.
    books = _books()
    writer = _writer(books)
    real_create = writer.create_b_voucher
    real_mid = next(m for m, v in books.state["vouchers"].items() if v["date"] == "20220901")
    writer.create_b_voucher = lambda company, **kw: (real_create(company, **kw), real_mid)[1]
    with pytest.raises(WriteFailed, match="not deleting"):
        forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)
    assert real_mid in books.state["vouchers"]
    assert not any(f'TAGVALUE="{real_mid}"' in r for r in books.requests)
    assert _summary(tmp_path)["outcome"] == "aborted"


def test_an_unverified_currency_create_stops_with_do_not_rerun(tmp_path):
    # I5: created=1 but not listed → never "refused", evidence saved, nothing else written.
    books = _books(forex_currency_listed=False)
    report = _run(books, tmp_path)
    assert report.outcome == "currency_unverified" and "do NOT re-run" in report.notes[-1]
    assert (tmp_path / "shape" / "currencies_after.xml").exists()
    assert not any("ZZ Forex Probe" in r for r in books.requests)


def test_a_refused_currency_still_saves_currencies_after(tmp_path):
    _run(_books(forex_currency_create="refuse"), tmp_path)
    assert (tmp_path / "shape" / "currencies_after.xml").exists()


def test_summary_is_written_when_a_completed_run_fails_its_cleanup(tmp_path):
    # M1: a fully measured run whose ledger delete fails keeps its summary.
    books = _books()
    writer = _writer(books)

    def refuse(_company, name):
        raise WriteFailed(f"Ledger {name!r} not deleted: in use")
    writer.delete_ledger = refuse
    with pytest.raises(WriteFailed, match="in use"):
        forex_shape.run(writer, B, tmp_path / "shape", f2_confirm=lambda: None)
    summary = _summary(tmp_path)
    assert summary["outcome"] == "stored_forex" and any("cleanup" in n for n in summary["notes"])


def test_a_renumbered_voucher_is_flagged(tmp_path):
    # M6: the Sep-2022..FY-end voucher numbers are read before and after; any change is flagged.
    books = _books()
    victim = next(m for m, v in books.state["vouchers"].items() if v["date"] == "20221001")

    def renumber(body):
        if "S0-throwaway forex V1" in body and "Import" in body:
            books.edit_state(lambda s: s["vouchers"][victim].__setitem__("number", "999"))
    books.before_request = renumber
    report = _run(books, tmp_path)
    assert report.numbering["changed"] is True and report.numbering["number_changed"] == [victim]
    assert any("restore" in n and "numbers" in n for n in report.notes)
    assert _summary(tmp_path)["numbering"]["changed"] is True


def test_the_default_f2_confirm_refuses_without_a_terminal(tmp_path, monkeypatch):
    # R-F2: no tty (a heredoc, an agent shell) → refused before the first request, with the way out named.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    books = _books()
    with pytest.raises(WriteRefused, match="f2_confirm"):
        forex_shape.run(_writer(books), B, tmp_path / "shape")
    assert books.requests == []


def test_f2_confirm_is_called_once_after_the_currency_and_before_the_ledgers(tmp_path):
    books = _books()
    seen = []
    forex_shape.run(_writer(books), B, tmp_path / "shape",
                    f2_confirm=lambda: seen.append((any("<CURRENCY " in r for r in books.requests),
                                                    any("ZZ Forex Probe" in r for r in books.requests))))
    assert seen == [(True, False)]


def test_an_existing_evidence_folder_is_never_overwritten(tmp_path):
    (tmp_path / "shape").mkdir()
    (tmp_path / "shape" / "summary.json").write_text("{}")
    books = _books()
    with pytest.raises(FileExistsError):
        _run(books, tmp_path)
    assert books.requests == []


@pytest.mark.parametrize("leftover", ["inr_ledger", "voucher"])
def test_other_leftovers_refuse_too(tmp_path, leftover):
    books = _books()
    if leftover == "inr_ledger":
        books.edit_state(lambda s: s["ledgers"].__setitem__(forex_shape.INR_PARTY, dict(s["ledgers"]["Cash"])))
    else:
        books.edit_state(lambda s: s["vouchers"].__setitem__("99999", {
            **next(iter(s["vouchers"].values())), "narration": "S0-throwaway forex V1", "date": "20220901"}))
    with pytest.raises(WriteFailed, match="leftover"):
        _run(books, tmp_path)
    assert not any("Import" in r for r in books.requests)
