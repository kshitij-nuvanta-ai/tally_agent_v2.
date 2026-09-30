import pytest

from probes.companies import COMPANIES, COMPANY_C_LEDGER, COMPANY_C_LEDGER_PARENT, COMPANY_C_VOUCHER_NARRATION
from probes.setup.company_c import CompanyCLoadError, load_company_c
from probes.setup.writes import TallyWriter
from tests.probes.fake_books import FakeBooks, sync_client

C = COMPANIES["C"]


def _writer(books: FakeBooks) -> TallyWriter:
    return TallyWriter(sync_client(books.transport()), say=lambda message: None)


def _imports(books: FakeBooks, since: int = 0) -> list[str]:
    return [r for r in books.requests[since:] if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]


def test_first_load_creates_the_ledger_and_the_voucher():
    books = FakeBooks(name=C, educational=True)
    report = load_company_c(_writer(books))
    assert report.created == ["ledger", "voucher"] and report.skipped == []
    state = books.state
    assert state["ledgers"][COMPANY_C_LEDGER]["parent"] == COMPANY_C_LEDGER_PARENT
    assert [v["narration"] for v in state["vouchers"].values()] == [COMPANY_C_VOUCHER_NARRATION]


def test_a_second_load_writes_nothing():
    books = FakeBooks(name=C, educational=True)
    load_company_c(_writer(books))
    before = len(books.requests)
    report = load_company_c(_writer(books))
    assert report.created == [] and report.skipped == ["ledger", "voucher"] and _imports(books, before) == []


def test_another_open_company_is_refused_before_any_write():
    books = FakeBooks(name=COMPANIES["B"])
    with pytest.raises(CompanyCLoadError, match="open"):
        load_company_c(_writer(books))
    assert _imports(books) == []


def test_the_ledger_under_another_group_is_a_problem_not_a_second_create():
    books = FakeBooks(name=C)
    books.edit_state(lambda s: s["ledgers"].__setitem__(COMPANY_C_LEDGER, {
        "parent": "Sundry Debtors", "email": "", "alter_id": 1, "guid": "g", "opening": "0.00"}))
    with pytest.raises(CompanyCLoadError, match="Sundry Debtors"):
        load_company_c(_writer(books))
    assert _imports(books) == []


def test_a_company_other_than_c_is_refused_even_when_named_probe():
    """Review M1: the `company=` parameter can't point setup-c at another 'Probe' company (A or B)."""
    books = FakeBooks(name=COMPANIES["A"])
    with pytest.raises(CompanyCLoadError, match="only"):
        load_company_c(_writer(books), company=COMPANIES["A"])
    assert books.requests == []
