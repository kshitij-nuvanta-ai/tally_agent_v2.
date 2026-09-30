from decimal import Decimal

import pytest

from tally_bridge.amounts import AmountParseError, parse_decimal
from probes.reads import ForexAmount, forex_base, parse_forex_amount


@pytest.mark.parametrize("text, fx, rate, base", [
    ("-$448.44 @ ₹82.99/$ = -₹37216.04", "-448.44", "82.99", "-37216.04"),       # review 2026-09-24's form
    ("$448.44 @ ₹ 82.99/$ = ₹ 37216.04", "448.44", "82.99", "37216.04"),         # spaces after the symbols
    ("$1,161.27 @ ₹82.58/$ = ₹95,897.68", "1161.27", "82.58", "95897.68"),       # Indian/Western commas
    ("-$448.44@₹82.99/$=-₹37216.04", "-448.44", "82.99", "-37216.04"),           # no spaces at all
    ("USD 10 @ 80/USD = 800", "10", "80", "800"),                                # a symbol word, no base symbol
])
def test_full_expression_parses(text, fx, rate, base):
    fa = parse_forex_amount(text)
    assert fa == ForexAmount(currency=fa.currency, fx=Decimal(fx), rate=Decimal(rate), rate_symbol=fa.rate_symbol,
                             base=Decimal(base), raw=text)
    assert forex_base(fa) == (Decimal(base), "stated")


def test_expression_without_base_computes_half_up():
    fa = parse_forex_amount("-$ 448.44 @ Rs. 82.99/$")
    assert (fa.currency, fa.rate_symbol, fa.base) == ("$", "Rs.", None)
    assert forex_base(fa) == (Decimal("-37216.04"), "computed")          # 448.44 × 82.99 = 37216.0356


@pytest.mark.parametrize("text", ["-37216.04", "", None, "37,216.04", "$448.44", "@ 82.99/$", "abc"])
def test_non_expressions_are_none(text):
    assert parse_forex_amount(text) is None


def test_parse_decimal_still_raises_on_the_expression():
    # spec §11.2: amounts.parse_decimal raising on a forex expression is expected, and carries the raw text
    with pytest.raises(AmountParseError) as err:
        parse_decimal("-$448.44 @ ₹82.99/$ = -₹37216.04")
    assert err.value.raw == "-$448.44 @ ₹82.99/$ = -₹37216.04"
