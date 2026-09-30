"""Plan part 7 Task 3.9: FakeBooks' forex defaults pinned to the LIVE read-back (Task 2, company B, 2026-09-25).

Evidence (committed once, never edited): `forex_shape_2026-09-25_run2/` (outcome `stored_forex`, chosen V1b) and
`forex_shape_2026-09-25_run1_currency_refused/` (the `$` Currency master create over XML: EXCEPTIONS=1). Every
default the live run settled is compared here with the capture itself, not with a hand-typed copy of it."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agent.tally.xml_utils import read_objects
from probes.companies import COMPANIES
from probes.reads import parse_vouchers, primary_lines
from probes.setup.company_b_data import USD_CURRENCY
from probes.setup.writes import (CURRENCY_FIELDS, ForexLine, TallyWriter, WriteFailed, b_day_voucher_request)
from tests.probes.fake_books import USD_CURRENCY_ROW, FakeBooks, sync_client

B = COMPANIES["B"]
_SYNC = Path(__file__).parent.parent / "fixtures" / "sync"
SHAPE = _SYNC / "forex_shape_2026-09-25_run2"                         # live: stored_forex, chosen V1b
REFUSED = _SYNC / "forex_shape_2026-09-25_run1_currency_refused"     # live: the XML currency create refused
CHOSEN = json.loads((SHAPE / "summary.json").read_text(encoding="utf-8"))["chosen"]
LINES = [("ZZ Party", Decimal("-37216.04"), True), ("Export Sales", Decimal("37216.04"), False)]


def _live_voucher(variant: str) -> dict:
    raw = (SHAPE / f"variant_{variant}.xml").read_text(encoding="utf-8")
    return next(v for v in parse_vouchers(raw) if v["header"].get("NARRATION") == f"S0-throwaway forex {variant}")


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    books.edit_state(lambda s: s["currencies"].__setitem__("$", dict(USD_CURRENCY_ROW)))   # created in the UI live
    return books


def _writer(books) -> TallyWriter:
    return TallyWriter(sync_client(books.transport()), say=lambda _m: None)


def _fake_voucher(books, writer, narration: str) -> dict:
    raw = writer.post(b_day_voucher_request(B, "01-09-2022"))
    return next(v for v in parse_vouchers(raw) if v["header"].get("NARRATION") == narration)


def _write_throwaway(books, *, party_currency: str | None, base_symbol: str) -> tuple[TallyWriter, str]:
    writer = _writer(books)
    writer.create_party_ledger(B, "ZZ Party", parent="Sundry Debtors", bill_wise=False, currency=party_currency)
    narration = "S0-throwaway forex fake"
    writer.create_b_voucher(B, vch_type="Sales", date="20220901", narration=narration, party="ZZ Party",
                            lines=LINES, forex=ForexLine("$", Decimal("448.44"), Decimal("82.99"),
                                                         form=CHOSEN["form"], base_symbol=base_symbol))
    return writer, narration


def _amounts(voucher: dict, party: str) -> dict[str, str]:
    return {("party" if l["fields"]["LEDGERNAME"] == party else l["fields"]["LEDGERNAME"]): l["amount_raw"]
            for l in primary_lines(voucher)}


def test_the_live_run_chose_v1b_the_rate_without_a_base_symbol():
    assert CHOSEN == {"variant": "V1b", "party_currency": "$", "form": "full", "base_symbol": ""}


def test_fake_default_export_is_the_live_v1b_text_byte_for_byte():
    """Sent `@ 82.99/$ = -37216.04` (no base symbol); live exported `@ ? 82.99/$ = -? 37216.04` — the base
    currency's NAME "?" plus a space (its HasSpace=Yes), whatever was sent."""
    books = _books()
    writer, narration = _write_throwaway(books, party_currency="$", base_symbol=CHOSEN["base_symbol"])
    live = _amounts(_live_voucher("V1b"), "ZZ Forex Probe USD Debtor")
    assert _amounts(_fake_voucher(books, writer, narration), "ZZ Party") == live
    assert live == {"party": "-$448.44 @ ? 82.99/$ = -? 37216.04", "Export Sales": "$448.44 @ ? 82.99/$ = ? 37216.04"}


def test_fake_default_keeps_forex_on_a_base_currency_party_like_live_v3():
    books = _books()
    writer, narration = _write_throwaway(books, party_currency=None, base_symbol="")
    assert (_amounts(_fake_voucher(books, writer, narration), "ZZ Party")
            == _amounts(_live_voucher("V3"), "ZZ Forex Probe INR Debtor"))


def test_fake_default_refuses_the_rate_written_with_the_base_symbol_like_live_v1():
    live = {v["id"]: v for v in json.loads((SHAPE / "summary.json").read_text(encoding="utf-8"))["variants"]}["V1"]
    assert live["classification"] == "refused" and live["base_symbol"] == "?"
    with pytest.raises(WriteFailed, match="exceptions=1"):
        _write_throwaway(_books(), party_currency="$", base_symbol="?")


def test_fake_default_refuses_a_currency_create_like_live_run1():
    assert json.loads((REFUSED / "summary.json").read_text(encoding="utf-8"))["outcome"] == "currency_refused"
    books = FakeBooks(name=B, educational=True)
    with pytest.raises(WriteFailed, match="exceptions=1"):
        _writer(books).create_currency(B, USD_CURRENCY)
    assert "$" not in books.state["currencies"]


def test_the_fakes_usd_row_is_the_live_currency_row():
    live = read_objects((SHAPE / "currencies_after.xml").read_text(encoding="utf-8"), "CURRENCY", CURRENCY_FIELDS)
    row = next(r for r in live if r["Name"] == "$")
    fake = _writer(_books()).list_currencies(B)["$"]
    assert {k: fake.get(k, "") for k in CURRENCY_FIELDS} == {k: row.get(k, "").strip() for k in CURRENCY_FIELDS}


def test_fake_forex_line_carries_the_live_extra_fields():
    """The forex-only leaf fields live exported (V1b line vs V0 line) — the fake must export the same names."""
    control = primary_lines(_live_voucher("V0"))[0]["fields"]
    extra_live = set(primary_lines(_live_voucher("V1b"))[0]["fields"]) - set(control)
    books = _books()
    writer, narration = _write_throwaway(books, party_currency="$", base_symbol="")
    assert extra_live <= set(primary_lines(_fake_voucher(books, writer, narration))[0]["fields"])
    assert extra_live == set()                          # live: a forex line carries no extra leaf field at all


def test_seeded_tag_101_exports_the_live_amount_shape():
    """Plan part 7 Task 3.9: seed_company_b writes 101/102 through the same `_forex_line_text` an import uses, so a
    seeded forex sale exports like the live throwaway (same figures as tag 101): same grammar, symbols and values."""
    from probes.reads import parse_forex_amount
    from tests.probes.fake_books import _export_voucher, seed_company_b
    books = FakeBooks(name=B, educational=True)
    seed_company_b(books, "educational", masters=True)
    state = books.state
    mid, v = next((m, v) for m, v in state["vouchers"].items() if v["narration"].startswith("[S0-B:101]"))
    fake = parse_vouchers(f"<ENVELOPE><BODY><DATA><COLLECTION>{_export_voucher(state, mid, v)}"
                          "</COLLECTION></DATA></BODY></ENVELOPE>")[0]
    live = _live_voucher(CHOSEN["variant"])
    assert ([l["amount_raw"] for l in primary_lines(fake)] == [l["amount_raw"] for l in primary_lines(live)])
    fa = parse_forex_amount(primary_lines(fake)[0]["amount_raw"])
    assert (fa.currency, fa.rate_symbol, fa.fx, fa.rate, fa.base) == ("$", "?", Decimal("-448.44"), Decimal("82.99"),
                                                                       Decimal("-37216.04"))
    assert state["currencies"]["$"] == USD_CURRENCY_ROW and state["ledgers"]["Gulf Office Supplies LLC (USD)"][
        "currency"] == "$"


# --- C47 (live 2026-09-25): a forex ledger is valued, and its closing exported, at the latest voucher rate -------------
LIVE_USD_PARTY_CLOSING = "-$1609.71 @ ? 82.58/$ = -? 132929.85"      # ledger_details after setup-b, live company B


def test_the_usd_party_closing_is_the_live_expression_at_the_latest_rate():
    from tests.probes.fake_books import seed_company_b
    books = FakeBooks(name=B, educational=True)
    seed_company_b(books, "educational", masters=True)
    row = _writer(books).ledger_details(B, "Gulf Office Supplies LLC (USD)")
    assert (row["CurrencyName"], row["ClosingBalance"]) == ("$", LIVE_USD_PARTY_CLOSING)
    assert _writer(books).ledger_details(B, "Export Sales")["ClosingBalance"] != ""   # a plain INR ledger stays plain


def test_the_fakes_trial_balance_is_out_by_the_forex_difference_like_live():
    from probes.reads import exploded_tb_rows, primary_group_rows
    from tests.probes.fake_books import seed_company_b
    books = FakeBooks(name=B, educational=True)
    seed_company_b(books, "educational", masters=True)
    rows = exploded_tb_rows(_writer(books).b_trial_balance(B, "01-04-2022", "31-03-2026"))
    total = sum((r["closing_balance"] for r in primary_group_rows(rows).values() if r["closing_balance"] is not None),
                Decimal("0.00"))
    assert total == Decimal("183.87")                                  # live note: "total observed: 183.87"
    sundry = next(r for r in rows if r["account_name"] == "Sundry Debtors")["closing_balance"]
    assert abs(sundry) == Decimal("2590148.41")                        # live: "Tally has |-2590148.41|"


def _usd_party_row(books) -> dict:
    """Probe 22 B's own USD-ledger request, asked of the fake."""
    import httpx
    from agent.tally.envelopes import formula_string
    from probes import p22_forex as p22
    from probes.reads import master_request
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    xml = master_request("S0P22Ledger", "Ledger", p22.LEDGER_FIELDS, B,
                         filters=[("S0P22IsParty", f"$Name = {formula_string(USD_EXPORT_PARTY)}")])
    with httpx.Client(transport=books.transport(), base_url="http://tally") as client:
        text = client.post("/", content=xml.encode("utf-8")).text
    return read_objects(text, "LEDGER", p22.LEDGER_FIELDS)[0]


def test_the_usd_party_opening_and_closing_match_the_live_capture():
    """C47 review I2: live exports the USD party's OpeningBalance as an expression too (p22_B_usd_ledger.xml:51-52,
    the current FY's opening — probe 11 B live: ledger openings are FY-scoped)."""
    from probes import p22_forex as p22
    from tests.probes.fake_books import seed_company_b
    live = read_objects((_SYNC / "p22_B_usd_ledger.xml").read_text(encoding="utf-8"), "LEDGER", p22.LEDGER_FIELDS)[0]
    books = FakeBooks(name=B, educational=True, ledger_opening_scope="fy")
    seed_company_b(books, "educational", masters=True)
    row = _usd_party_row(books)
    assert (row["OpeningBalance"], row["ClosingBalance"]) == (live["OpeningBalance"], live["ClosingBalance"])
    assert row["OpeningBalance"] == LIVE_USD_PARTY_CLOSING


def test_the_plain_forex_opening_is_a_candidate_knob():
    from agent.tally.amounts import parse_decimal
    from tests.probes.fake_books import seed_company_b
    books = FakeBooks(name=B, educational=True, ledger_opening_scope="fy", forex_ledger_opening="plain")
    seed_company_b(books, "educational", masters=True)
    assert parse_decimal(_usd_party_row(books)["OpeningBalance"]) == Decimal("-132929.85")
