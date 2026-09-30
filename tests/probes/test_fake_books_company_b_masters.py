"""Task 4: FakeBooks as a clean setup-b leaves company B, masters included, for probes 11/14/15/16B/18B."""
from datetime import date
from decimal import Decimal

import httpx

from tally_bridge.envelopes import formula_string, wrap_report
from tally_bridge.sync_reports import parse_ledger_list
from tally_bridge.xml_utils import detect_error, read_objects
from probes.companies import COMPANIES
from probes.company_b_view import ledger_balances_at
from probes.reads import exploded_tb_rows, master_request, primary_group_rows, stock_rows_any_depth
from tests.probes.fake_books import FakeBooks, MALFORMED_ANSWER, seed_company_b

B = COMPANIES["B"]


def _books(**knobs) -> FakeBooks:
    # C47: the USD party's balances export in the live expression form (FakeBooks' default); parse_ledger_list reads
    # them at their stated INR base (test_the_live_forex_closing_reads_at_its_base below).
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


def _ask(books: FakeBooks, xml: str) -> str:
    with httpx.Client(transport=books.transport(), base_url="http://tally") as client:
        return client.post("/", content=xml.encode("utf-8")).text


def _ledgers(books, static_vars=None, name="S0P16BLedgers"):
    xml = master_request(name, "Ledger", ["Name", "Parent", "OpeningBalance", "ClosingBalance"], B,
                         static_vars=static_vars, allow_period_vars=static_vars is not None)
    return {r["name"]: r for r in parse_ledger_list(_ask(books, xml))}


def test_masters_match_the_dataset_and_the_tb_nets_to_zero():
    books = _books(opening_stock_row=True)
    state = books.state
    assert state["ledgers"]["Pune Digital Solutions"]["opening"] == "-62500.00"
    assert state["items"]["A4 Paper Ream"]["opening_qty"] == "15 Box"
    assert state["items"]["USB Cable Type-C"]["opening_value"] == "-10200.00"
    assert state["bills"]["Op/2022-001"] == {"party": "Pune Digital Solutions", "amount": "-62500.00",
                                             "opening": True, "date": "20220331"}
    rows = exploded_tb_rows(_ask(books, wrap_report("Trial Balance", "01-04-2022", "31-03-2023", B,
                                                    extra_vars={"EXPLODEFLAG": "Yes"})))
    primaries = primary_group_rows(rows)
    # C47 (live 2026-09-25): the TB is out by the unrealised forex difference — the USD party at the latest rate,
    # Export Sales at the bases. As on 31-03-2023 both USD sales (Sep 2022) are in: 183.87.
    from probes.setup.company_b_data import expected_figures, generate
    gap = expected_figures(generate("educational")).forex_revaluation[date(2023, 3, 31)]
    assert gap == Decimal("183.87")
    assert sum((r["closing_balance"] or Decimal(0)) for r in primaries.values()) == gap
    assert next(r for r in rows if r["account_name"] == "Opening Stock")["closing_balance"] == Decimal("-24450.00")


def test_ledger_collection_ignores_svtodate_unless_the_knob_says_otherwise():
    now = ledger_balances_at("educational", date(2026, 3, 31))
    back = ledger_balances_at("educational", date(2023, 3, 31))
    ignored = _ledgers(_books(), {"SVTODATE": "31-03-2023"})
    assert ignored["HDFC Bank Current A/c"]["closing_balance"] == now["HDFC Bank Current A/c"]
    honoured = _ledgers(_books(ledger_svtodate_honoured=True), {"SVTODATE": "31-03-2023"})
    assert honoured["HDFC Bank Current A/c"]["closing_balance"] == back["HDFC Bank Current A/c"]


def test_opening_scope_books_or_fy():
    fy = ledger_balances_at("educational", date(2025, 3, 31))
    assert _ledgers(_books())["HDFC Bank Current A/c"]["opening_balance"] == Decimal("-868050.00")
    assert _ledgers(_books(ledger_opening_scope="fy"))["HDFC Bank Current A/c"]["opening_balance"] == \
        fy["HDFC Bank Current A/c"]
    assert _ledgers(_books(ledger_opening_scope="fy"))["Domestic Sales"]["opening_balance"] is None   # nominal: 0


def test_svfromdate_on_a_b_ledger_collection_wedges_by_default():
    books = _books()
    try:
        _ledgers(books, {"SVFROMDATE": "01-04-2024", "SVTODATE": "31-03-2025"})
    except httpx.ReadTimeout:
        pass
    else:
        raise AssertionError("expected the wedge")
    assert books.popup is True


def test_filtered_items_opening_bills_and_stock_summary():
    books = _books()
    xml = master_request("S0P11PartyBills", "Ledger", ["Name", "OpeningBalance", "BillAllocations"], B,
                         filters=[("S0P11IsParty", f"$Name = {formula_string('Pune Digital Solutions')}")])
    text = _ask(books, xml)
    assert text.count("<LEDGER ") == 1 and "<NAME>Op/2022-001</NAME>" in text and "-62500.00" in text
    items = read_objects(_ask(books, master_request("S0P15Item", "StockItem", ["Name", "BaseUnits"], B,
                         filters=[("S0P15IsItem", '$Name = "A4 Paper Ream"')])), "STOCKITEM", ["Name", "BaseUnits"])
    assert items == [{"Name": "A4 Paper Ream", "BaseUnits": "Box of 10 Nos"}]
    rows = stock_rows_any_depth(_ask(books, wrap_report("Stock Summary", "01-04-2022", "31-03-2026", B)))
    assert next(r for r in rows if r["name"] == "A4 Paper Ream")["qty"] == Decimal("20")


def test_malformed_and_unknown_company_answers():
    bad = master_request("S0P14Ledgers", "Ledger", ["Name"], B).replace(
        "Sharma &amp; Sons&apos; Probe Traders", "Sharma & Sons' Probe Traders")
    assert _ask(_books(), bad) == MALFORMED_ANSWER and detect_error(MALFORMED_ANSWER)
    assert "<LEDGER " in _ask(_books(tolerate_raw_ampersand=True), bad)
    unknown = master_request("S0P14Ledgers", "Ledger", ["Name"], B + " X")
    assert "<LEDGER " in _ask(_books(), unknown)                                  # default: SVCurrentCompany ignored
    assert detect_error(_ask(_books(honour_company_var=True), unknown))


def test_the_live_forex_closing_reads_at_its_base():
    """C47 / plan part 7 Review Focus 4: the USD party's live ClosingBalance `-$1609.71 @ ? 82.58/$ = -? 132929.85`
    (formerly an AmountParseError) reads at its stated INR base — the latest-rate revalued figure — with the parts."""
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    row = _ledgers(_books(forex_ledger_closing="expression"))[USD_EXPORT_PARTY]
    assert row["closing_balance"] == Decimal("-132929.85")
    fx = row["closing_forex"]
    assert (fx.face, fx.currency, fx.rate, fx.base_derived) == (Decimal("-1609.71"), "$", Decimal("82.58"), False)
    assert row["closing_balance"] == _ledgers(_books(forex_ledger_closing="plain"))[USD_EXPORT_PARTY]["closing_balance"]


def test_the_live_forex_opening_reads_at_its_base_too():
    """C47 review I2: live, the USD party's (FY-scoped) OpeningBalance is the same expression as its closing
    (p22_B_usd_ledger.xml:52); it reads at its base as well."""
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    row = _ledgers(_books(ledger_opening_scope="fy", forex_ledger_opening="expression"))[USD_EXPORT_PARTY]
    assert row["opening_balance"] == Decimal("-132929.85") and row["opening_forex"].face == Decimal("-1609.71")
