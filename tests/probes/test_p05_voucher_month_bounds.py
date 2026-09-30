import json
from datetime import date

from tally_bridge.client import TallyClient
from probes import p05_voucher_month_bounds as p05
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.company_b_view import expect_window
from probes.registry import ALL_ORDER, FIRST_ORDER
from probes.results import ResultsStore
from probes.runner import EDUCATIONAL_SUFFIX, run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import FakeTally, ScriptedIO, make_harness, objects_xml, ready_store, vch, vouchers_xml

B = COMPANIES["B"]
JUNE = (date(2023, 6, 1), date(2023, 6, 30))
CURRENT_FY = (date(2025, 4, 1), date(2026, 3, 31))
JUNE_1 = (date(2023, 6, 1), date(2023, 6, 1))
LOADED_AT = "2026-09-24T13:02:33+05:30"


def _store(tmp_path, licence="educational", loaded=True) -> ResultsStore:
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence=licence)
    if loaded:
        store.update_environment(company_b_loaded_at=LOADED_AT)
    return store


async def _run_books(tmp_path, books, **store_kwargs):
    store = _store(tmp_path, **store_kwargs)
    await run_probe(p05.PROBE, labels=None, client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())
    return store, store.probe_entry(5)["parts"]["B"]


def _books(licence="educational", *, tally_educational=None) -> FakeBooks:
    """Company B seeded for `licence`, on a fake Tally of the same edition unless `tally_educational` says otherwise
    (C43: only an educational fake ignores period variables off day 1/2/31)."""
    educational = licence == "educational" if tally_educational is None else tally_educational
    books = FakeBooks(name=B, educational=educational)
    seed_company_b(books, licence)
    return books


def _vouchers(window, *, extra=()):
    exp = expect_window("educational", *window)
    return vouchers_xml([vch({"DATE": v.date.strftime("%Y%m%d"), "VOUCHERTYPENAME": v.vch_type,
                              "NARRATION": v.narration}) for v in exp.written.values()] + list(extra))


def store_bodies(tmp_path) -> list[str]:
    return [json.loads(f.read_text())["request_xml"] for f in (tmp_path / "fixtures").glob("*.json")]


def _current_fy_vouchers() -> str:
    exp = expect_window("educational", *CURRENT_FY)
    return vouchers_xml([vch({"DATE": v.date.strftime("%Y%m%d"), "VOUCHERTYPENAME": v.vch_type,
                              "NARRATION": v.narration}) for v in exp.written.values()])


def _live_run_1(body: str) -> str:
    """Live run 1's shape (logs/p05-live-2026-09-24.log): typed, a to-date Tally dropped (June..current FY end);
    untyped, the current FY 2025-26 only."""
    if 'TYPE="Date"' not in body:
        return _current_fy_vouchers()
    return vouchers_xml([vch({"DATE": v.date.strftime("%Y%m%d"), "VOUCHERTYPENAME": v.vch_type,
                              "NARRATION": v.narration})
                         for v in expect_window("educational", date(2023, 6, 1), CURRENT_FY[1]).written.values()])


JULY_STRAY = vch({"DATE": "20230701", "VOUCHERTYPENAME": "Sales", "NARRATION": "[S0-B:301] Sale"})


def _b_tally(month_route, formula_route=None) -> FakeTally:
    fake = FakeTally([B])
    fake.route("S0CompanyCounters", lambda body: objects_xml("COMPANY", [{
        "Name": B, "GUID": "g-b", "AltVchId": "958", "AltMstId": "40", "BooksFrom": "20220401",
        "LastVoucherDate": "20260331", "AlterID": "40"}]))
    fake.route("S0P05MonthFormula", formula_route or (lambda body: vouchers_xml([])))
    fake.route("S0VoucherMonth", month_route)
    return fake


async def _run_fake(tmp_path, fake):
    client, _, capture = make_harness(tmp_path, fake)
    store = _store(tmp_path)
    await run_probe(p05.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return store, store.probe_entry(5)["parts"]["B"]


async def test_typed_svdates_bound_june_2023_and_the_request_is_confirmed(tmp_path):
    store, part = await _run_books(tmp_path, _books())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["month_typed"]["match"] and obs["month_typed"]["returned"] == 20
    assert obs["month_untyped"]["returned"] == 240 and not obs["month_untyped"]["bounded"]
    assert obs["month_untyped"]["date_span"] == ["2025-04-01", "2026-03-31"]
    assert obs["c33_reproduced"] is True
    assert obs["day"]["match"] and obs["day"]["returned"] == 10
    assert obs["report_period_vars"]["identical_bytes"] is False
    assert "C33 reproduced" in part["summary"] and part["summary"].endswith(EDUCATIONAL_SUFFIX)
    assert 'TYPE="Date"' in part["spec_impact"]
    assert part["fixtures"] == ["p05_B_month_svdates.xml", "p05_B_month_svdates_untyped.xml",
                                "p05_B_day_svdates.xml", "p05_B_report_tb_typed.xml", "p05_B_report_tb_untyped.xml"]
    confirmed = store.confirmed("voucher_month")
    assert confirmed["probe"] == 5 and confirmed["form"] == "svdates_typed" and confirmed["day_window_exact"]
    # C43: the educational June window ends on the 2nd, is recorded, and 30-06-2023 is never sent.
    assert obs["window"] == {"licence": "educational", "from": "01-06-2023", "to": "02-06-2023", "clamped": True,
                             "reason": p05.C43_REASON}
    assert ("educational: to-date clamped to 02-06-2023 because Tally ignores non-1/2/31 date variables — C43"
            in part["summary"])
    assert obs["report_period_vars"]["as_on"] == "02-06-2023"
    assert not any("30-06-2023" in body for body in store_bodies(tmp_path))
    assert '<SVFROMDATE TYPE="Date">__FROM__</SVFROMDATE>' in confirmed["xml_template"]
    assert "<SVCurrentCompany>__COMPANY__</SVCurrentCompany>" in confirmed["xml_template"]


async def test_licensed_books_put_one_voucher_on_the_first(tmp_path):
    _, part = await _run_books(tmp_path, _books("licensed"), licence="licensed")
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["day"]["returned"] == 1
    assert part["observations"]["window"] == {"licence": "licensed", "from": "01-06-2023", "to": "30-06-2023",
                                              "clamped": False, "reason": None}
    assert "clamped" not in part["summary"] and "C43" not in part["summary"]
    assert part["observations"]["report_period_vars"]["as_on"] == "30-06-2023"


async def test_a_licence_that_does_not_match_the_books_shows_in_the_day_window(tmp_path):
    _, part = await _run_books(tmp_path, _books("educational", tally_educational=False), licence="licensed")
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["month_typed"]["match"]                  # same 20 tags, all inside June either way
    assert not part["observations"]["day"]["match"] and "half-month" in part["spec_impact"]


async def test_company_b_not_loaded_blocks_before_any_voucher_request(tmp_path):
    books = _books()
    _, part = await _run_books(tmp_path, books, loaded=False)
    assert part["outcome"] == "BLOCKED" and "setup-b" in part["summary"]
    assert not any("<TYPE>Voucher</TYPE>" in r for r in books.requests)


async def test_the_formula_is_confirmed_when_typed_svdates_do_not_bound(tmp_path):
    fake = _b_tally(lambda body: _vouchers(JUNE, extra=[JULY_STRAY]),
                    lambda body: _vouchers(JUNE if '"02-06-2023"' in body else JUNE_1))
    store, part = await _run_fake(tmp_path, fake)
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["month_formula"]["match"] and "$Date formula" in part["spec_impact"]
    confirmed = store.confirmed("voucher_month")
    assert confirmed["form"] == "formula" and "S0P05InWindow" in confirmed["xml_template"]
    assert "__FROM__" in confirmed["xml_template"]
    assert "p05_B_month_formula.xml" in part["fixtures"]


async def test_neither_form_bounding_fails_and_confirms_nothing(tmp_path):
    stray = lambda body: _vouchers(JUNE, extra=[JULY_STRAY])       # noqa: E731
    store, part = await _run_fake(tmp_path, _b_tally(stray, stray))
    assert part["outcome"] == "FAILED"
    assert "filters by date in Python" in part["spec_impact"]
    assert store.confirmed("voucher_month") is None
    assert "p05_B_day_svdates.xml" not in part["fixtures"] and "p05_B_report_tb_typed.xml" in part["fixtures"]


async def test_an_untyped_answer_that_is_also_bounded_is_flagged_not_trusted(tmp_path):
    fake = _b_tally(lambda body: _vouchers(JUNE_1 if "01-06-2023</SVTODATE>" in body else JUNE))
    _, part = await _run_fake(tmp_path, fake)
    assert part["outcome"] == "CONFIRMED"
    assert part["observations"]["c33_reproduced"] is False
    assert "C33 NOT reproduced" in part["summary"]
    # M4: untyped evidence must never change the CONFIRMED verdict, but the spec_impact text must not assert the
    # C33 silent-fallback claim when it wasn't reproduced this run.
    assert 'TYPE="Date"' in part["spec_impact"]
    assert "silently answers for the company's current period" not in part["spec_impact"]


def test_ordered_runs_put_probe_5_right_before_probe_21():
    for order in (ALL_ORDER, FIRST_ORDER):
        assert order.index((5, "B")) == order.index((21, "B")) - 1


# Day 2: under C43 the educational June window is 01-06..02-06, and an educational company holds nothing on the 15th.
UNTAGGED_JUNE_ROW = vch({"DATE": "20230602", "VOUCHERTYPENAME": "Journal", "NARRATION": "typed by hand"})


async def test_a_bounded_window_with_an_untagged_row_is_books_drift_blocked(tmp_path):
    """I1: the window bounds and every expected tag comes back, but an extra untagged row shows company B has
    drifted from the dataset — never a request failure, and the formula fallback must never be tried for it."""
    fake = _b_tally(lambda body: _vouchers(JUNE, extra=[UNTAGGED_JUNE_ROW]))
    store, part = await _run_fake(tmp_path, fake)
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert part["observations"]["month_typed"]["bounded"] is True
    assert part["observations"]["month_typed"]["drifted"] is True
    assert "month_svdates" in part["summary"] and "untagged 1" in part["summary"]
    assert store.confirmed("voucher_month") is None
    assert not any("S0P05MonthFormula" in body for body in fake.requests)


async def test_a_bounded_window_with_a_duplicate_tag_is_books_drift_blocked(tmp_path):
    june = expect_window("educational", *JUNE)
    dup = next(iter(june.written.values()))
    duplicate_row = vch({"DATE": dup.date.strftime("%Y%m%d"), "VOUCHERTYPENAME": dup.vch_type,
                         "NARRATION": dup.narration})
    fake = _b_tally(lambda body: _vouchers(JUNE, extra=[duplicate_row]))
    store, part = await _run_fake(tmp_path, fake)
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert part["observations"]["month_typed"]["bounded"] is True
    assert part["observations"]["month_typed"]["drifted"] is True
    assert f"duplicates [{dup.tag}]" in part["summary"]
    assert store.confirmed("voucher_month") is None
    assert not any("S0P05MonthFormula" in body for body in fake.requests)


async def test_c33_is_reproduced_when_the_untyped_answer_is_the_current_period(tmp_path):
    """Live run 1: untyped → 240 vouchers 2025-04-01..2026-03-31 for a June-2023 request. That IS C33, even though
    the typed form failed to bound too — the old check (typed match and untyped mismatch) called it NOT reproduced."""
    _, part = await _run_fake(tmp_path, _b_tally(_live_run_1, _live_run_1))
    assert part["outcome"] == "FAILED"
    obs = part["observations"]
    assert obs["month_untyped"]["returned"] == 240
    assert obs["month_untyped"]["date_span"] == ["2025-04-01", "2026-03-31"]
    assert obs["c33_reproduced"] is True
    assert "C33 reproduced: untyped, Tally answered 240 voucher(s) dated 2025-04-01..2026-03-31" in part["summary"]
    assert "NOT reproduced" not in part["summary"]
    assert "to-date clamped to 02-06-2023" in part["summary"]


async def test_c33_is_not_reproduced_when_the_untyped_strays_fall_outside_the_current_period(tmp_path):
    def month(body: str) -> str:
        return _vouchers(JUNE_1) if 'TYPE="Date"' in body else _vouchers(JUNE, extra=[JULY_STRAY])
    _, part = await _run_fake(tmp_path, _b_tally(month))
    assert part["observations"]["month_untyped"]["out_of_window"] == 1
    assert part["observations"]["c33_reproduced"] is False
    assert "C33 NOT reproduced" in part["summary"]


async def test_c33_is_not_reproduced_when_the_untyped_answer_is_empty(tmp_path):
    def month(body: str) -> str:
        return _vouchers(JUNE_1) if 'TYPE="Date"' in body else vouchers_xml([])
    _, part = await _run_fake(tmp_path, _b_tally(month))
    assert part["observations"]["c33_reproduced"] is False


async def test_the_educational_fake_reproduces_live_run_1_with_the_old_30th_window(tmp_path, monkeypatch):
    """Item 4 end to end: with the pre-C43 window (to 30-06-2023) the educational fake gives live run 1's FAILED
    shape — the typed month runs to the current FY's end. The C43 rule is switched off here on purpose, once, for
    both call sites (fetch_window and the request-level guard share one implementation — Ruling Q4)."""
    from probes import safety
    monkeypatch.setattr(p05, "month_window", lambda y, m, licence: (date(2023, 6, 1), date(2023, 6, 30)))
    monkeypatch.setattr(safety, "EDUCATIONAL_DATE_VAR_DAYS", tuple(range(1, 32)))
    _, part = await _run_books(tmp_path, _books())
    obs = part["observations"]
    assert part["outcome"] == "FAILED"
    assert obs["month_typed"]["returned"] == 680 and obs["month_typed"]["date_span"] == ["2023-06-01", "2026-03-31"]
    assert obs["c33_reproduced"] is True
