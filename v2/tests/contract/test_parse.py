from datetime import date
from decimal import Decimal

import pytest

from v2.contract import parse
from v2.contract.parse import WireParseError


@pytest.mark.parametrize("text, inr", [
    ("-62800.00", Decimal("-62800.00")),      # p16_A_ledgers.xml
    ("1261.42", Decimal("1261.42")),          # p22_B_forex_sales.xml, Output CGST
    ("0.00", Decimal("0.00")),
    ("-1,048,846.53", Decimal("-1048846.53")),
])
def test_amount_plain_numbers(text, inr):
    a = parse.amount(text)
    assert a.inr == inr and a.stated and a.fx_amount is None and a.text == text


def test_amount_forex_voucher_line_takes_the_stated_base():            # p22_B_forex_sales.xml [S0-B:101]
    a = parse.amount("-$448.44 @ ? 82.99/$ = -? 37216.04")
    assert (a.inr, a.fx_currency, a.fx_amount, a.fx_rate, a.stated) == (
        Decimal("-37216.04"), "$", Decimal("-448.44"), Decimal("82.99"), True)


def test_amount_forex_ledger_balance_takes_the_stated_base():          # p22_B_usd_ledger.xml
    a = parse.amount("-$1609.71 @ ? 82.58/$ = -? 132929.85")
    assert a.inr == Decimal("-132929.85") and a.fx_amount == Decimal("-1609.71") and a.fx_rate == Decimal("82.58")


def test_amount_forex_credit_side_positive():
    a = parse.amount("$448.44 @ ? 82.99/$ = ? 37216.04")
    assert a.inr == Decimal("37216.04") and a.fx_amount == Decimal("448.44")


def test_amount_expression_without_base_is_unstated_never_computed():   # D3
    a = parse.amount("-$448.44 @ ? 82.99/$")
    assert a.inr is None and a.stated is False and a.fx_amount == Decimal("-448.44")


@pytest.mark.parametrize("text", ["", "   "])
def test_amount_blank_is_none_never_zero(text):
    assert parse.amount(text) is None
    assert parse.amount(None) is None


@pytest.mark.parametrize("text", ["abc", "12.3.4", "$ @ ? /$", "-$1 @ ? 2/€ = -? 2"])
def test_amount_junk_raises(text):
    with pytest.raises(WireParseError) as err:
        parse.amount(text)
    assert err.value.code == "unparseable_amount"


@pytest.mark.parametrize("text, expected", [
    ("20220901", date(2022, 9, 1)),       # p22_B_forex_sales.xml DATE
    ("1-Oct-25", date(2025, 10, 1)),      # report date form
    ("15-Dec-25", date(2025, 12, 15)),
    ("01-10-2025", date(2025, 10, 1)),
    ("01-04-2022", date(2022, 4, 1)),     # snapshot from_date
])
def test_tally_date_forms(text, expected):
    assert parse.tally_date(text) == expected


@pytest.mark.parametrize("text", ["", "2022-13-01", "31-02-2023", "yesterday"])
def test_tally_date_invalid_raises(text):
    with pytest.raises(WireParseError) as err:
        parse.tally_date(text)
    assert err.value.code == "invalid_date"


def test_logical_yes_no_only():
    assert parse.logical("Yes") is True and parse.logical("No") is False
    for bad in ("", "yes ", "Y", "True"):
        with pytest.raises(WireParseError):
            parse.logical(bad)


@pytest.mark.parametrize("text, number", [
    (" 17 Nos", Decimal("17")),               # p22_B_forex_sales.xml
    ("10 Box 0 Nos", Decimal("10")),          # p15_B_compound_unit_voucher.xml (C40)
    ("-2.0000 NOS", Decimal("-2.0000")),
    ("", None),
])
def test_quantity_leading_number(text, number):
    assert parse.quantity(text) == (number, text)


def test_rate_plain_and_currency():
    assert parse.rate("824.46/Nos") == (Decimal("824.46"), "Nos", "824.46/Nos")
    assert parse.rate("$12.50/Nos") == (None, "Nos", "$12.50/Nos")
    assert parse.rate("") == (None, None, "")


def test_credit_period_days_only():                                     # p23_B_bills_credit_period.xml
    assert parse.credit_period("30 Days") == (30, "30 Days")
    assert parse.credit_period("45 Days") == (45, "45 Days")
    assert parse.credit_period("15-Oct-25") == (None, "15-Oct-25")
    assert parse.credit_period(None) == (None, "")


def test_name_strips_reserved_prefix_and_keeps_unicode():               # p25_A_groups.xml, p23_B_bills_receivable_due.xml
    assert parse.name("\u0004 Primary") == "Primary"
    assert parse.name("\u0004Primary") == "Primary"
    assert parse.name("शर्मा ट्रेडर्स") == "शर्मा ट्रेडर्स"
    assert parse.name("Duties & Taxes") == "Duties & Taxes"


def test_counter_leading_space():                                       # p01_A_counters_baseline.xml
    assert parse.counter(" 50") == 50 and parse.counter(" 965") == 965
    with pytest.raises(WireParseError):
        parse.counter("x")
