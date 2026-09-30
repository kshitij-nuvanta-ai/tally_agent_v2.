"""Wire-value parsers (S1 spec §5.2). Strict: a value that is present but unparseable raises WireParseError; blank is
None; nothing is ever turned into zero (Part 1 §13).

Amounts: the plain-number rule and the forex grammar (with its stated-base check) are the bridge's
(tally_bridge.amounts.parse_decimal / forex_parts), not repeated here. `amount()` differs from the bridge's
`parse_amount` on purpose (S1 D3, ruling A3): a forex expression with no stated "= base" gives inr=None, stated=False,
where the bridge derives face x rate. S1 never computes a base. A value that cannot be read raises
WireParseError("unparseable_amount") rather than the bridge's AmountParseError."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from tally_bridge.amounts import AmountParseError, forex_parts, parse_decimal


class WireParseError(ValueError):
    def __init__(self, code: str, text: str | None):
        super().__init__(f"{code}: {text!r}")
        self.code, self.text = code, text


@dataclass(frozen=True)
class Amount:
    inr: Decimal | None
    fx_currency: str | None
    fx_amount: Decimal | None
    fx_rate: Decimal | None
    stated: bool
    text: str


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def amount(text: str | None) -> Amount | None:
    if text is None or not text.strip():
        return None
    try:
        if "@" not in text:
            return Amount(parse_decimal(text), None, None, None, True, text)
        parts = forex_parts(text)
    except AmountParseError:
        raise WireParseError("unparseable_amount", text) from None
    # No stated base: inr stays None (D3). The bridge's parse_forex would derive face x rate here.
    return Amount(parts.stated_base, parts.currency, parts.face, parts.rate, parts.stated_base is not None, text)


_DATE_FORMS = ("%Y%m%d", "%d-%b-%y", "%d-%m-%Y")


def tally_date(text: str) -> date:
    for form in _DATE_FORMS:
        try:
            return datetime.strptime((text or "").strip(), form).date()
        except ValueError:
            continue
    raise WireParseError("invalid_date", text)


def logical(text: str) -> bool:
    if text == "Yes":
        return True
    if text == "No":
        return False
    raise WireParseError("invalid_logical", text)


_LEADING = re.compile(r"^\s*(-?\d[\d,]*(?:\.\d+)?)")


def quantity(text: str | None) -> tuple[Decimal | None, str]:
    text = text or ""
    m = _LEADING.match(text)
    return (_dec(m.group(1)) if m else None), text


def rate(text: str | None) -> tuple[Decimal | None, str | None, str]:
    text = text or ""
    if not text.strip():
        return None, None, text
    number, _, unit = text.partition("/")
    unit = unit.strip() or None
    try:
        return Decimal(number.strip().replace(",", "")), unit, text
    except InvalidOperation:
        return None, unit, text                       # a currency-symbol rate: text kept, never parsed to a number


_DAYS = re.compile(r"^\s*(\d+)\s*Days?\s*$", re.IGNORECASE)


def credit_period(text: str | None) -> tuple[int | None, str]:
    text = text or ""
    m = _DAYS.match(text)
    return (int(m.group(1)) if m else None), text


def name(text: str | None) -> str:
    text = text or ""
    if text.startswith("\u0004"):
        text = text[1:].lstrip(" ")
    return text.strip()


def counter(text: str) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        raise WireParseError("invalid_counter", text) from None  # ruled deterministic/quarantinable, S1 task 8a review
