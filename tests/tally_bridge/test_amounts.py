from decimal import Decimal

import pytest

from tally_bridge.amounts import AmountParseError, ForexAmount, parse_amount, parse_decimal, parse_forex


@pytest.mark.parametrize("text,expected", [
    ("-1048846.53", Decimal("-1048846.53")),
    ("0", Decimal("0")),
    ("1,23,456.00", Decimal("123456.00")),
    ("  55856.21 ", Decimal("55856.21")),
])
def test_plain_numbers(text, expected):
    value = parse_decimal(text)
    assert value == expected
    assert isinstance(value, Decimal)


@pytest.mark.parametrize("text", [None, "", "   "])
def test_missing_is_none_not_zero(text):
    assert parse_decimal(text) is None


def test_forex_expression_raises_with_raw_text():
    raw = "-$1,000.00 @ ₹83.00/$ = -₹83,000.00"
    with pytest.raises(AmountParseError) as info:
        parse_decimal(raw)
    assert info.value.raw == raw


# --- forex expressions (controller decision, S1 open decision C47: take the stated INR base after "=") -------------

LIVE_LEDGER = "-$1609.71 @ ? 82.58/$ = -? 132929.85"          # p22_B_usd_ledger.xml:51-52 (Closing and Opening)
LIVE_LINE_DR = "-$448.44 @ ? 82.99/$ = -? 37216.04"           # p22_B_forex_sales.xml / variant_V1b.xml
LIVE_LINE_CR = "$448.44 @ ? 82.99/$ = ? 37216.04"


@pytest.mark.parametrize("text, base, face, rate, base_symbol", [
    (LIVE_LEDGER, "-132929.85", "-1609.71", "82.58", "?"),
    (LIVE_LINE_DR, "-37216.04", "-448.44", "82.99", "?"),
    (LIVE_LINE_CR, "37216.04", "448.44", "82.99", "?"),
    ("-$1161.27 @ ? 82.58/$ = -? 95897.68", "-95897.68", "-1161.27", "82.58", "?"),
    ("-$448.44 @ ₹82.99/$ = -₹37216.04", "-37216.04", "-448.44", "82.99", "₹"),       # the UI's ₹, no space
    ("$448.44 @ ₹ 82.99/$ = ₹ 37216.04", "37216.04", "448.44", "82.99", "₹"),
    ("-$448.44 @ Rs. 82.99/$ = -Rs. 37216.04", "-37216.04", "-448.44", "82.99", "Rs."),
    ("-$448.44 @ Rs.82.99/$ = -Rs.37216.04", "-37216.04", "-448.44", "82.99", "Rs."),
    ("-$448.44 @ 82.99/$ = -37216.04", "-37216.04", "-448.44", "82.99", ""),           # no base symbol
    ("-$448.44@?82.99/$=-?37216.04", "-37216.04", "-448.44", "82.99", "?"),            # no spaces at all
    ("-$1,161.27 @ ? 82.58/$ = -? 95,897.68", "-95897.68", "-1161.27", "82.58", "?"),  # Western commas
    ("$1,00,000.00 @ ? 83.00/$ = ? 83,00,000.00", "8300000.00", "100000.00", "83.00", "?"),  # Indian commas
    ("  -$448.44 @ ? 82.99/$ = -? 37216.04  ", "-37216.04", "-448.44", "82.99", "?"),  # surrounding whitespace
])
def test_forex_expression_takes_the_stated_base(text, base, face, rate, base_symbol):
    fa = parse_forex(text)
    assert fa == ForexAmount(base=Decimal(base), face=Decimal(face), currency="$", rate=Decimal(rate),
                             base_symbol=base_symbol, base_derived=False, raw=text)
    assert isinstance(fa.base, Decimal) and isinstance(fa.face, Decimal) and isinstance(fa.rate, Decimal)


def test_the_stated_base_is_taken_as_is_not_recomputed():
    # C47: a forex ledger's balance is face total × the LATEST rate; the stated figure is authoritative even when it
    # is not face × rate to the paisa (no conversion at read time).
    fa = parse_forex("-$1609.71 @ ? 82.58/$ = -? 132929.00")
    assert fa.base == Decimal("-132929.00") and fa.base_derived is False


def test_a_word_currency_symbol():
    fa = parse_forex("USD 10.00 @ ? 80.00/USD = ? 800.00")
    assert (fa.currency, fa.face, fa.base) == ("USD", Decimal("10.00"), Decimal("800.00"))


@pytest.mark.parametrize("text, base, face", [
    ("-$448.44 @ 82.99/$", "-37216.04", "-448.44"),               # 448.44 × 82.99 = 37216.0356 → .04
    ("$448.44 @ ? 82.99/$", "37216.04", "448.44"),
    ("$448.44 @ ₹82.99/$", "37216.04", "448.44"),
    ("$1.25 @ 0.50/$", "0.63", "1.25"),                          # exact tie 0.625 → half-up
    ("-$1.25 @ 0.50/$", "-0.63", "-1.25"),                       # tie on a debit rounds away from zero too
    ("$1.21 @ 0.50/$", "0.61", "1.21"),                          # 0.605 → 0.61
    ("$1.23 @ 0.50/$", "0.62", "1.23"),                          # 0.615 → 0.62
])
def test_expression_without_a_base_derives_it_half_up(text, base, face):
    fa = parse_forex(text)
    assert (fa.base, fa.face, fa.base_derived) == (Decimal(base), Decimal(face), True)
    assert fa.base.as_tuple().exponent == -2


def test_derived_base_keeps_the_symbol_as_written():
    assert parse_forex("-$448.44 @ Rs. 82.99/$").base_symbol == "Rs."
    assert parse_forex("-$448.44 @ 82.99/$").base_symbol == ""


@pytest.mark.parametrize("text", [
    "-$448.44 @ ? 82.99/€ = -? 37216.04",        # per-unit symbol ≠ the face's currency
    "-$448.44 @ ? 82.99/$ = ? 37216.04",         # face is a debit, base a credit: the signs disagree
    "$448.44 @ ? 82.99/$ = -? 37216.04",
    "$448.44 @ ? 82.99 = ? 37216.04",            # no per-unit
    "$448.44 @ /$ = ? 37216.04",                 # no rate
    "448.44 @ ? 82.99/$ = ? 37216.04",           # no currency on the face
    "$448.44 @ ? 82.99/$ = ? ",                  # "=" with no base
    "$448.44 @ ? 82.99/$ = ? 37216.04 extra",
    "$448.44 @ € 82.99/$ = € 37216.04",          # not a base-currency symbol
    "$448.44 @ ? 82.99/$ = ₹ 37216.04",          # rate and base disagree on the base symbol
    "--$448.44 @ ? 82.99/$ = --? 37216.04",
    "$ @ ? 82.99/$",
    "@ 82.99/$",
    "$448.44",
    "abc",
    "-448.44",                                   # a plain number is not an expression
])
def test_malformed_expressions_raise_with_the_raw_text(text):
    with pytest.raises(AmountParseError) as info:
        parse_forex(text)
    assert info.value.raw == text


# --- parse_amount: plain OR forex, one entry point for the agent's parsers ----------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("-1048846.53", Decimal("-1048846.53")),      # sign convention pinned: a debit stays negative, as parse_decimal
    ("1,23,456.00", Decimal("123456.00")),
    ("0", Decimal("0")),
])
def test_parse_amount_plain_numbers_are_unchanged(text, expected):
    assert parse_amount(text) == (expected, None)
    assert parse_amount(text)[0] == parse_decimal(text)


@pytest.mark.parametrize("text", [None, "", "   "])
def test_parse_amount_missing_is_none(text):
    assert parse_amount(text) == (None, None)


def test_parse_amount_forex_gives_the_base_and_the_parts():
    value, fx = parse_amount(LIVE_LEDGER)
    assert value == Decimal("-132929.85") and value == fx.base
    assert (fx.face, fx.currency, fx.rate, fx.base_derived) == (Decimal("-1609.71"), "$", Decimal("82.58"), False)
    # sign convention: the forex base carries the same Dr-negative sign a plain export of that balance would
    assert value == parse_decimal("-132929.85")


@pytest.mark.parametrize("text", ["abc", "1.2.3", "$448.44", "-$448.44 @ ? 82.99/€ = -? 37216.04", "12 Nos"])
def test_parse_amount_still_raises_on_anything_else(text):
    with pytest.raises(AmountParseError) as info:
        parse_amount(text)
    assert info.value.raw == text


def test_parse_decimal_stays_strict_on_an_expression():
    # spec §11.2 and probe 22's "parse_decimal_raises" evidence depend on the strict plain parser
    with pytest.raises(AmountParseError):
        parse_decimal(LIVE_LEDGER)
