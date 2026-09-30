from decimal import Decimal
from pathlib import Path

import pytest

from agent.tally.amounts import AmountParseError
from agent.tally.reports import parse_bills, parse_ledger_list, parse_stock_summary, parse_trial_balance

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "tally_samples"
SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _read(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_trial_balance_live():
    rows = parse_trial_balance(_read("trial_balance_live.xml"))
    assert [r["account_name"] for r in rows] == [
        "Capital Account", "Current Liabilities", "Fixed Assets", "Current Assets",
        "Sales Accounts", "Purchase Accounts", "Indirect Expenses",
    ]
    capital, liabilities = rows[0], rows[1]
    assert capital["debit_amount"] is None
    assert capital["credit_amount"] == Decimal("100000.00")
    assert capital["closing_balance"] == Decimal("100000.00")
    assert liabilities["closing_balance"] == Decimal("-223528.66")
    assert rows[3]["debit_amount"] == Decimal("-1048846.53")  # debit is negative
    assert sum(r["closing_balance"] for r in rows) == Decimal("0.00")
    assert all(isinstance(r["closing_balance"], Decimal) for r in rows)


def test_ledger_list_missing_opening_is_none():
    ledgers = parse_ledger_list(_read("ledger_list.xml"))
    assert len(ledgers) == 34
    assert ledgers[0] == {
        "name": "Apex Technologies Pvt Ltd",
        "parent_group": "North Zone Debtors",
        "closing_balance": Decimal("-55000.00"),
        "opening_balance": None,
        "closing_forex": None,
        "opening_forex": None,
    }
    assert any(l["name"] == "Sharma & Sons Traders" for l in ledgers)


def test_bills_receivable_live():
    bills = parse_bills(_read("bills_receivable_live.xml"))
    assert len(bills) == 12
    assert bills[0]["bill_number"] == "#1"
    assert bills[0]["party_name"] == "HCODE TECHNOLOGIES PRIVATE LIMITED"
    assert bills[0]["amount"] == Decimal("200000.00")
    assert bills[0]["overdue_days"] == 272
    assert sum(b["amount"] for b in bills) == Decimal("2360000.00")


def test_bills_payable_live():
    bills = parse_bills(_read("bills_payable_live.xml"))
    assert [(b["bill_number"], b["party_name"], b["amount"]) for b in bills] == [
        ("6ZBO7JXW-0003", "Anthropic, PBC", Decimal("2096.86")),
    ]


def test_stock_summary_live():
    items = parse_stock_summary(_read("stock_summary_live.xml"))
    assert len(items) == 5
    first = items[0]
    assert first["name"] == "Data Cleaning and Matching Application Software"
    assert first["closing_quantity"] == Decimal("-1.0000")
    assert first["base_units"] == "NOS"
    assert first["closing_rate"] is None
    assert first["closing_value"] is None


def test_stock_rate_with_unit_suffix():
    xml = (
        "<ENVELOPE><DSPACCNAME><DSPDISPNAME>Box</DSPDISPNAME></DSPACCNAME><DSPSTKINFO><DSPSTKCL>"
        "<DSPCLQTY>1,200.0000 Box of 10 Nos</DSPCLQTY><DSPCLRATE>1,250.00/Box</DSPCLRATE>"
        "<DSPCLAMTA>-1500000.00</DSPCLAMTA></DSPSTKCL></DSPSTKINFO></ENVELOPE>"
    )
    item = parse_stock_summary(xml)[0]
    assert item["closing_quantity"] == Decimal("1200.0000")
    assert item["base_units"] == "Box of 10 Nos"
    assert item["closing_rate"] == Decimal("1250.00")
    assert item["closing_value"] == Decimal("-1500000.00")


def test_ledger_list_reads_the_live_forex_ledger_at_its_stated_base():
    """C47 (live 2026-09-25, p22_B_usd_ledger.xml): the USD party's Closing/OpeningBalance export as
    `-$1609.71 @ ? 82.58/$ = -? 132929.85`. The balance is the stated INR base (Dr negative, as a plain export) —
    Tally's revalued figure at the latest voucher rate — and the forex parts ride alongside."""
    [row] = parse_ledger_list((SYNC / "p22_B_usd_ledger.xml").read_text(encoding="utf-8"))
    assert row["name"] == "Gulf Office Supplies LLC (USD)" and row["parent_group"] == "Sundry Debtors"
    assert row["closing_balance"] == Decimal("-132929.85") and row["opening_balance"] == Decimal("-132929.85")
    for key in ("closing_forex", "opening_forex"):
        fx = row[key]
        assert (fx.face, fx.currency, fx.rate, fx.base, fx.base_derived) == (
            Decimal("-1609.71"), "$", Decimal("82.58"), Decimal("-132929.85"), False)


def _one_ledger(closing: str, opening: str = "") -> str:
    return (f"<ENVELOPE><LEDGER NAME=\"X\"><NAME>X</NAME><PARENT>Sundry Debtors</PARENT>"
            f"<CLOSINGBALANCE>{closing}</CLOSINGBALANCE><OPENINGBALANCE>{opening}</OPENINGBALANCE></LEDGER></ENVELOPE>")


def test_ledger_list_mixes_plain_and_forex_halves():
    [row] = parse_ledger_list(_one_ledger("-$448.44 @ 82.99/$", "-1,000.00"))
    assert row["closing_balance"] == Decimal("-37216.04") and row["closing_forex"].base_derived is True
    assert row["opening_balance"] == Decimal("-1000.00") and row["opening_forex"] is None


def test_ledger_list_still_raises_on_an_unreadable_balance():
    with pytest.raises(AmountParseError, match="37216.04"):
        parse_ledger_list(_one_ledger("-$448.44 @ ? 82.99/€ = -? 37216.04"))
