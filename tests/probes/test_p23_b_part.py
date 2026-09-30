from datetime import date
from pathlib import Path

from tally_bridge.client import TallyClient
from probes import p05_voucher_month_bounds as p05
from probes import p23_gst_due_dates as p23
from probes.capture import Capture
from probes.company_b_view import bill_terms
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True, bills=True)
    return books


async def _run(tmp_path, books, *, with_probe_5=True):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    client, capture = TallyClient(transport=books.transport(), trust_env=False), Capture(tmp_path / "fixtures")
    if with_probe_5:
        await run_probe(p05.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    await run_probe(p23.PROBE, labels=["B"], client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(23)["parts"]["B"]


async def test_credit_periods_and_due_dates_agree_with_the_dataset(tmp_path):
    part = await _run(tmp_path, _books())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["sub_verdicts"] == {"voucher_credit_period": "CONFIRMED", "report_due_date": "CONFIRMED"}
    assert obs["credit_period"]["compared"] >= 2 and obs["credit_period"]["values"] == ["30 Days", "45 Days"]
    assert obs["due_dates"]["compared"] >= 1
    # Ruling S4: the live run measures the rule -- (due - bill date) in days per bill, recorded, here = credit days.
    terms = bill_terms("educational")
    offsets = obs["due_dates"]["offsets"]
    assert offsets and all(days == terms[name].credit_days for name, days in offsets.items())
    assert part["fixtures"] == ["p23_B_bills_credit_period.xml", "p23_B_bills_receivable_due.xml"]


async def test_a_due_column_that_ignores_the_credit_period_is_different(tmp_path):
    part = await _run(tmp_path, _books(bill_due_from_credit_period=False))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["due_dates"]["as_bill_date"] and "BILLCREDITPERIOD" in part["spec_impact"]


async def test_a_due_column_that_follows_another_rule_gets_its_own_impact(tmp_path):
    """Ruling S4: due dates present but not bill date + credit days (here one day short) is "due column present,
    rule differs" -- never "the due column doesn't carry the credit period"."""
    part = await _run(tmp_path, _books(bill_due_offset_days=-1))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    due = part["observations"]["due_dates"]
    assert due["verdict"] == "OTHER_RULE" and due["wrong"] and not due["as_bill_date"]
    terms = bill_terms("educational")
    assert all(days == terms[name].credit_days - 1 for name, days in due["offsets"].items())
    assert "rule differs" in part["spec_impact"] and "doesn't carry" not in part["spec_impact"]
    assert "rule differs" in part["summary"]


async def test_vouchers_without_credit_periods_are_different(tmp_path):
    part = await _run(tmp_path, _books(bill_credit_period_exported=False))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["credit_period"]["missing"] and "Bills Receivable snapshot" in part["spec_impact"]


async def test_neither_source_fails_and_drops_the_overdue_split(tmp_path):
    part = await _run(tmp_path, _books(bill_credit_period_exported=False, bill_due_from_credit_period=False))
    assert part["outcome"] == "FAILED" and "dropped from v1" in part["spec_impact"]


async def test_a_bill_the_dataset_never_opened_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["bills"].__setitem__("Hand/1", {"party": "Nagpur Wholesale Traders",
                                                                  "amount": "-10.00"}))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "Hand/1" in part["summary"]


async def test_without_probe_5_the_b_part_blocks(tmp_path):
    part = await _run(tmp_path, _books(), with_probe_5=False)
    assert part["outcome"] == "BLOCKED" and "probe 5" in part["summary"]


def test_the_live_month_export_carries_credit_periods_that_match_the_dataset():
    """Live shape (p21_B_fy2022_month_02.xml, probe 5's request, window 01..02-02-2023)."""
    found = p23.voucher_credit_periods((SYNC / "p21_B_fy2022_month_02.xml").read_text(encoding="utf-8"))
    assert found["Inv/203"]["credit_period"] == "30 Days" and found["Pur/209"]["credit_period"] == "45 Days"
    expected = {n: t for n, t in bill_terms("educational").items()
                if t.credit_period and not t.flagged and date(2023, 2, 1) <= t.bill_date <= date(2023, 2, 2)}
    assert p23.credit_period_check(found, expected)["verdict"] == "CONFIRMED"


def test_a_report_bill_date_that_differs_from_the_voucher_date_is_recorded():
    """Review M3: offsets are measured against the dataset's bill date AND the report row's own BILLDATE, so a Tally
    bill date that differs from the voucher date shows up as such (recorded; the verdict is unchanged)."""
    from probes.company_b_view import BillTerm
    term = BillTerm("S-1", "Party", date(2023, 6, 10), "30 Days", False)
    rows = [{"bill_number": "S-1", "bill_date": "12-Jun-2023", "due_date": "10-Jul-2023"}]
    due = p23.due_date_check(rows, {"S-1": term})
    assert due["verdict"] == "CONFIRMED"
    assert due["offsets"] == {"S-1": 30}
    assert due["report_offsets"] == {"S-1": 28}
    assert due["bill_date_differs"] == {"S-1": {"tally": "12-Jun-2023", "setup": "2023-06-10"}}


def test_matching_report_bill_dates_record_no_difference():
    from probes.company_b_view import BillTerm
    term = BillTerm("S-1", "Party", date(2023, 6, 10), "30 Days", False)
    rows = [{"bill_number": "S-1", "bill_date": "10-Jun-2023", "due_date": "10-Jul-2023"}]
    due = p23.due_date_check(rows, {"S-1": term})
    assert due["report_offsets"] == {"S-1": 30} and due["bill_date_differs"] == {}
