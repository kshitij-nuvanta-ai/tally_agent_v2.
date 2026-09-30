"""sign_check.py — the one-request-pair live check that settles C30 (signed OPENINGBALANCE), run offline here."""
import httpx
import pytest

from probes.companies import COMPANIES
from probes.setup.sign_check import LEDGER, run
from probes.setup.writes import TallyWriter, WriteFailed
from tests.probes.fake_books import FakeBooks, sync_client

B = COMPANIES["B"]


def _writer(books):
    return TallyWriter(sync_client(books.transport()), lambda _: None)


def _imports(books):
    return [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]


def test_it_creates_a_debit_opening_reads_it_back_raw_and_deletes_the_ledger():
    books = FakeBooks(name=B)
    raw = run(_writer(books), B)
    assert raw == "-1.00"                                    # the fake stores the wire value as sent
    create, delete = _imports(books)
    assert "<PARENT>Sundry Debtors</PARENT>" in create
    assert "<OPENINGBALANCE>-1.00</OPENINGBALANCE>" in create
    assert 'ACTION="Delete"' in delete and LEDGER in delete
    assert LEDGER not in books.state["ledgers"]


def test_an_existing_check_ledger_is_refused_before_anything_is_written():
    """A leftover from an aborted run would be silently skipped by create_party_ledger and its OLD opening read
    back — so refuse, and let the operator delete it."""
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["ledgers"].__setitem__(LEDGER, {"parent": "Sundry Debtors", "email": "",
                                                                 "alter_id": 1, "guid": "g", "opening": "5.00"}))
    with pytest.raises(WriteFailed, match="already exists"):
        run(_writer(books), B)
    assert _imports(books) == []


def test_the_ledger_is_deleted_even_when_the_read_back_fails():
    books = FakeBooks(name=B)

    def time_out_the_opening_read(body: str) -> None:
        if "S0SignCheckOpening" in body:
            raise httpx.ReadTimeout("no answer")
    books.before_request = time_out_the_opening_read
    with pytest.raises(WriteFailed):
        run(_writer(books), B)
    assert LEDGER not in books.state["ledgers"]
    assert 'ACTION="Delete"' in _imports(books)[-1]


# --- C38 (review #6): the discriminating form — a CREDIT opening under a debit-natured group ------------------------
def test_run_positive_sends_a_credit_opening_under_sundry_debtors_and_deletes_it():
    """−1.00 under Sundry Debtors reads back −1.00 whether Tally reads the sign (C30) or infers the side from the
    group (C21) — non-discriminating. +1.00 is: C30 predicts 1.00 (Cr), C21 predicts −1.00 (Dr)."""
    from probes.setup.sign_check import run_positive
    books = FakeBooks(name=B)                          # the fake models C30: it stores the wire value as sent
    raw = run_positive(_writer(books), B)
    assert raw == "1.00"
    create, delete = _imports(books)
    assert "<PARENT>Sundry Debtors</PARENT>" in create and "<OPENINGBALANCE>1.00</OPENINGBALANCE>" in create
    assert 'ACTION="Delete"' in delete and LEDGER not in books.state["ledgers"]


@pytest.mark.parametrize("raw,expected", [("1.00", "C30"), ("-1.00", "C21"), (" 1.00", "C30"), ("0.00", "neither"),
                                          ("", "neither")])
def test_the_positive_read_back_names_the_ruling_it_supports(raw, expected):
    from probes.setup.sign_check import positive_verdict
    assert positive_verdict(raw).startswith(expected)


def test_a_contra_natural_opening_is_still_refused_unless_asked_for():
    """create_party_ledger's dataset sanity check (M1) must keep refusing +1.00 under Sundry Debtors by default —
    only the sign check opts out, explicitly."""
    from decimal import Decimal
    books = FakeBooks(name=B)
    with pytest.raises(ValueError, match="contra-natural"):
        _writer(books).create_party_ledger(B, LEDGER, parent="Sundry Debtors", bill_wise=False,
                                           opening=Decimal("1.00"))
    assert _imports(books) == []
