from collections import Counter
from datetime import date

import pytest

from tally_bridge.client import TallyClient
from tally_bridge.envelopes import COMPANY_PLACEHOLDER
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_FROM_DATE, B_BOOKS_TO, B_CURRENT_PERIOD,
                                      EDUCATIONAL_DATE_VAR_DAYS, compare_tags, dataset, drift_message, expect_window,
                                      fetch_window, kind_label, loaded_licence, month_window, tag_of, voucher_rows)
from probes.context import ProbeContext
from probes.core import Probe, ProbeBlocked
from probes.p21_full_history_reach import voucher_blocks
from probes.reads import FROM_PLACEHOLDER, TO_PLACEHOLDER, parse_vouchers, tally_date, voucher_request
from probes.results import ResultsStore
from probes.safety import GuardError
from tests.probes.fakes import FakeTally, ScriptedIO, ready_store, vch, vouchers_xml

JUNE = (date(2023, 6, 1), date(2023, 6, 30))
B = COMPANIES["B"]


def _rows(tags_and_days):
    return [{"tag": tag, "date": day} for tag, day in tags_and_days]


def test_tag_of_reads_the_loader_tag_and_nothing_else():
    assert tag_of("[S0-B:281] Sale to शर्मा ट्रेडर्स") == 281
    assert tag_of("  [S0-B:7] Receipt") == 7
    assert tag_of("S0-throwaway 1") is None
    assert tag_of("Paid [S0-B:9] later") is None
    assert tag_of("") is None
    assert (B_BOOKS_FROM, B_BOOKS_FROM_DATE, B_BOOKS_TO) == ("01-04-2022", date(2022, 4, 1), "31-03-2026")


def test_windows_follow_the_written_dataset():
    june = expect_window("educational", *JUNE)
    assert len(june.written) == 20 and not june.flagged and not june.skipped
    sep = expect_window("educational", date(2022, 9, 1), date(2022, 9, 30))
    assert len(sep.written) == 20 and sep.skipped == frozenset()         # plan part 7: 101/102 written with forex (was C36-skipped)
    feb = expect_window("educational", date(2023, 2, 1), date(2023, 2, 28))
    assert len(feb.written) == 20 and feb.flagged == {201, 202}         # cancelled: written, flagged
    assert feb.unflagged == frozenset(feb.written) - {201, 202}
    day = expect_window("educational", date(2023, 6, 1), date(2023, 6, 1))
    assert len(day.written) == 10                                        # Educational: the 1st and the 2nd only
    assert len(expect_window("licensed", date(2023, 6, 1), date(2023, 6, 1)).written) == 1


def test_fy2022_kind_mix_is_what_probe_21_weights_storage_by():
    fy = expect_window("educational", date(2022, 4, 1), date(2023, 3, 31))
    assert len(fy.written) == 240                                      # plan part 7: 101/102 written with forex (was C36-skipped)
    assert Counter(kind_label(v) for v in fy.written.values()) == Counter({
        "sales+inventory+bills": 81, "purchase+inventory+bills": 60, "receipt+bills": 40, "payment+bills": 24,
        "sales+inventory": 13, "payment": 12, "receipt": 8,
        "sales": 2})                               # plan part 7: 101/102 written with forex (was C36-skipped)
    assert dataset("educational") is dataset("educational")            # cached, generated once


def test_voucher_rows_read_tag_and_date_and_skip_the_cmpinfo_counter():
    text = vouchers_xml([vch({"DATE": "20230601", "NARRATION": "[S0-B:281] Sale"}),
                         vch({"DATE": "20230602", "NARRATION": "hand entry"})])
    assert voucher_rows(text) == [{"tag": 281, "date": date(2023, 6, 1)}, {"tag": None, "date": date(2023, 6, 2)}]


def test_an_exact_window_matches():
    june = expect_window("educational", *JUNE)
    result = compare_tags(_rows((t, v.date) for t, v in june.written.items()), june, *JUNE)
    assert result["match"] and result["bounded"]
    assert (result["returned"], result["expected_written"], result["expected_unflagged"]) == (20, 20, 20)
    assert result["date_span"] == ["2023-06-01", "2023-06-02"]


def test_compare_reports_missing_extra_untagged_and_duplicates():
    june = expect_window("educational", *JUNE)
    tags = sorted(june.written)
    rows = _rows([(t, date(2023, 6, 1)) for t in tags[1:]] + [(tags[1], date(2023, 6, 1)), (101, date(2023, 6, 1)),
                                                              (None, date(2023, 6, 2))])
    result = compare_tags(rows, june, *JUNE)
    assert not result["match"]
    assert result["missing"] == [tags[0]]
    assert result["extra"] == [101]                         # a Sep-2022 tag showing up in June is extra, never expected
    assert result["duplicates"] == [tags[1]]
    assert result["untagged"] == 1


def test_a_date_outside_the_window_is_not_bounded():
    june = expect_window("educational", *JUNE)
    rows = _rows([(t, v.date) for t, v in june.written.items()] + [(301, date(2023, 7, 1))])
    result = compare_tags(rows, june, *JUNE)
    assert not result["bounded"] and result["out_of_window"] == 1 and not result["match"]
    assert result["date_span"] == ["2023-06-01", "2023-07-01"]


def test_flagged_tags_are_recorded_but_never_decide_the_match():
    feb_window = (date(2023, 2, 1), date(2023, 2, 28))
    feb = expect_window("educational", *feb_window)
    with_flags = compare_tags(_rows((t, v.date) for t, v in feb.written.items()), feb, *feb_window)
    without_flags = compare_tags(_rows((t, feb.written[t].date) for t in feb.unflagged), feb, *feb_window)
    assert with_flags["match"] and with_flags["flagged_returned"] == [201, 202] and not with_flags["flagged_missing"]
    assert without_flags["match"] and without_flags["flagged_missing"] == [201, 202]


def test_loaded_licence_needs_a_clean_setup_b_and_a_recorded_licence():
    with pytest.raises(ProbeBlocked, match="setup-b"):
        loaded_licence({"licence": "educational"})
    with pytest.raises(ProbeBlocked, match="probe 0"):
        loaded_licence({"company_b_loaded_at": "2026-09-24T13:02:33+05:30"})
    assert loaded_licence({"licence": "educational", "company_b_loaded_at": "x"}) == "educational"


def test_compare_tags_separates_a_request_failure_from_books_drift():
    """I1: reach_ok tracks whether THIS request bounded and returned every expected tag — the real "did the
    request work" question. drifted tracks a fully-bounded, fully-complete window that also carries an
    untagged/extra/duplicate row — company B no longer matches its dataset, never a request failure. `match`
    keeps its original, stricter meaning (reach_ok and not drifted)."""
    june = expect_window("educational", *JUNE)
    exact = compare_tags(_rows((t, v.date) for t, v in june.written.items()), june, *JUNE)
    assert exact["reach_ok"] and not exact["drifted"] and exact["match"]

    tags = sorted(june.written)
    missing_one = compare_tags(_rows((t, june.written[t].date) for t in tags[1:]), june, *JUNE)
    assert not missing_one["reach_ok"] and not missing_one["drifted"] and not missing_one["match"]

    unbounded = compare_tags(_rows([(t, v.date) for t, v in june.written.items()] + [(301, date(2023, 7, 1))]),
                             june, *JUNE)
    assert not unbounded["reach_ok"] and not unbounded["drifted"] and not unbounded["match"]

    with_untagged = compare_tags(_rows([(t, v.date) for t, v in june.written.items()]
                                       + [(None, date(2023, 6, 1))]), june, *JUNE)
    assert with_untagged["reach_ok"] and with_untagged["drifted"] and not with_untagged["match"]

    with_extra = compare_tags(_rows([(t, v.date) for t, v in june.written.items()] + [(101, date(2023, 6, 1))]),
                              june, *JUNE)
    assert with_extra["reach_ok"] and with_extra["drifted"] and not with_extra["match"]

    with_duplicate = compare_tags(_rows([(t, v.date) for t, v in june.written.items()]
                                        + [(tags[0], june.written[tags[0]].date)]), june, *JUNE)
    assert with_duplicate["reach_ok"] and with_duplicate["drifted"] and not with_duplicate["match"]


def test_drift_message_names_the_offending_rows():
    june = expect_window("educational", *JUNE)
    tags = sorted(june.written)
    drifted = compare_tags(_rows([(t, v.date) for t, v in june.written.items()]
                                 + [(101, date(2023, 6, 1)), (None, date(2023, 6, 2)),
                                    (tags[0], june.written[tags[0]].date)]), june, *JUNE)
    message = drift_message("month_svdates", drifted)
    assert "month_svdates" in message and "extra [101]" in message and "untagged 1" in message
    assert f"duplicates [{tags[0]}]" in message
    assert "setup-b" in message


async def test_fetch_window_gives_both_consumption_paths_the_same_tag_set(tmp_path):
    """P8b (review M2): probe 5's path (compare_tags via voucher_rows) and probe 21's path (voucher_blocks + tag_of,
    for size measurement) used to decode the response two different ways. Feeding the same raw month response
    through the one shared `fetch_window` and then independently re-parsing its returned raw text through BOTH
    consumption paths must give identical tag sets and dates."""
    june = expect_window("educational", *JUNE)
    response = vouchers_xml([vch({"DATE": v.date.strftime("%Y%m%d"), "VOUCHERTYPENAME": v.vch_type,
                                  "NARRATION": v.narration}) for v in june.written.values()])
    fake = FakeTally([B])
    fake.route("S0Test", lambda body: response)
    template = voucher_request("S0Test", ["Date"], COMPANY_PLACEHOLDER, from_date=FROM_PLACEHOLDER,
                               to_date=TO_PLACEHOLDER)
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    probe = Probe(id=999, name="test", question="", feeds=(), parts={})
    ctx = ProbeContext(probe=probe, part="B", company_name=B, client=TallyClient(transport=fake.transport(), trust_env=False),
                       store=store, capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())

    result, raw_text = await fetch_window(ctx, "window", template, "educational", "01-06-2023", "02-06-2023")

    # Probe 5's path: compare_tags built from voucher_rows(raw_text).
    p05_rows = voucher_rows(raw_text)
    p05_tags = {row["tag"] for row in p05_rows}
    p05_dates = {row["date"] for row in p05_rows}
    # Probe 21's path: voucher_blocks(raw_text) + tag_of/parse_vouchers, independently, on the SAME raw_text.
    p21_blocks = [parse_vouchers(block)[0] for block in voucher_blocks(raw_text)]
    p21_tags = {tag_of(v["header"].get("NARRATION", "")) for v in p21_blocks}
    p21_dates = {tally_date(v["header"].get("DATE", "")) for v in p21_blocks}

    expected_tags, expected_dates = set(june.written), {v.date for v in june.written.values()}
    assert p05_tags == p21_tags == expected_tags
    assert p05_dates == p21_dates == expected_dates
    assert result["match"] and result["returned"] == len(june.written)


# --- C43: the month window a licence can ask Tally for -----------------------------------------------------------
@pytest.mark.parametrize("year, month, licence, window", [
    (2023, 6, "licensed", (date(2023, 6, 1), date(2023, 6, 30))),
    (2023, 6, "educational", (date(2023, 6, 1), date(2023, 6, 2))),
    (2023, 7, "educational", (date(2023, 7, 1), date(2023, 7, 31))),
    (2024, 2, "educational", (date(2024, 2, 1), date(2024, 2, 2))),
    (2024, 2, "licensed", (date(2024, 2, 1), date(2024, 2, 29))),
    (2026, 3, "educational", (date(2026, 3, 1), date(2026, 3, 31))),
])
def test_month_window_clamps_the_to_date_to_an_allowed_day_only_when_educational(year, month, licence, window):
    assert month_window(year, month, licence) == window


def test_month_window_rejects_an_unknown_licence():
    with pytest.raises(ValueError):
        month_window(2023, 6, "trial")


def test_an_educational_month_window_still_bounds_every_voucher_of_that_month():
    """C43's rationale, pinned against the dataset: an educational company holds vouchers only on days 1/2/31, so the
    clamped window returns exactly what the whole calendar month holds — for every month company B has."""
    months = sorted({(v.date.year, v.date.month) for v in dataset("educational").vouchers})
    assert len(months) == 48
    for year, month in months:
        start, end = month_window(year, month, "educational")
        assert start.day == 1 and end.day in EDUCATIONAL_DATE_VAR_DAYS
        whole_month = {v.tag for v in dataset("educational").vouchers
                       if (v.date.year, v.date.month) == (year, month) and not v.skip_reason}
        assert set(expect_window("educational", start, end).written) == whole_month, (year, month)


def test_b_current_period_is_the_last_fy_of_the_dataset():
    assert B_CURRENT_PERIOD == (date(2025, 4, 1), date(2026, 3, 31))


@pytest.mark.parametrize("start, end", [("01-06-2023", "30-06-2023"), ("15-06-2023", "31-07-2023")])
async def test_fetch_window_refuses_an_educational_date_tally_would_silently_ignore(tmp_path, start, end):
    """C43: sending it would read as a healthy answer for the wrong period, so fetch_window refuses before sending."""
    fake = FakeTally([B])
    fake.route("S0Test", lambda body: vouchers_xml([]))
    template = voucher_request("S0Test", ["Date"], COMPANY_PLACEHOLDER, from_date=FROM_PLACEHOLDER,
                               to_date=TO_PLACEHOLDER)
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    ctx = ProbeContext(probe=Probe(id=999, name="test", question="", feeds=(), parts={}), part="B", company_name=B,
                       client=TallyClient(transport=fake.transport(), trust_env=False), store=store,
                       capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())
    with pytest.raises(GuardError, match="C43"):            # Ruling Q4: the one C43 check raises GuardError
        await fetch_window(ctx, "window", template, "educational", start, end)
    assert not any("S0Test" in body for body in fake.requests)
    store.update_environment(licence="licensed")      # the request-level C43 guard reads the store's licence (Task 2)
    result, _ = await fetch_window(ctx, "window", template, "licensed", start, end)   # licensed: any date is fine
    assert result["returned"] == 0


def test_dataset_balances_and_openings_come_from_the_dataset_alone():
    from datetime import date
    from decimal import Decimal
    import pytest
    from probes.company_b_view import ledger_balances_at, ledger_openings_at, ledger_specs
    books = ledger_openings_at("educational", date(2022, 4, 1))
    assert books["Pune Digital Solutions"] == Decimal("-62500.00")
    assert books["Capital Account"] == Decimal("1000000.00")
    assert books == {n: (s.opening or Decimal("0.00")) for n, s in ledger_specs("educational").items()}
    assert set(ledger_balances_at("educational", date(2023, 3, 31))) == set(books)
    with pytest.raises(ValueError):
        ledger_balances_at("educational", date(2023, 3, 30))
    with pytest.raises(ValueError):
        ledger_openings_at("educational", date(2023, 5, 1))


def test_first_voucher_units_and_stock_for_probe_15():
    from datetime import date
    from decimal import Decimal
    from probes.company_b_view import COMPOUND_UNIT, HINDI_DEBTOR, first_voucher, item_specs, qty_unit, stock_qty_at
    hindi = first_voucher("educational", lambda v: not v.narration.isascii())
    # F5: the EDUCATIONAL dataset's first non-ASCII voucher is tag 7 (01-04-2022); tag 2 is the licensed dataset's.
    assert hindi.tag == 7 and HINDI_DEBTOR in hindi.narration
    compound = next(n for n, i in item_specs("educational").items() if i.unit == COMPOUND_UNIT)
    assert compound == "A4 Paper Ream" and qty_unit("educational", compound) == "Box"
    assert stock_qty_at("educational", compound, date(2026, 3, 31)) == Decimal("20")
