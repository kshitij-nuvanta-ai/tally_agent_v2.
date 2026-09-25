# Copied from: v2/agent/tally/amounts.py @ 5abb6a1
# Changes: _FOREX grammar and the stated-base rule of parse_forex as-is, folded into amount() -> Amount; changed per
# S1 D3: an expression without a stated "= base" gives inr=None, stated=False (the source derives face x rate; S1
# never computes a base). Plain numbers as parse_decimal, with WireParseError("unparseable_amount"). The other
# parsers (tally_date, logical, quantity, rate, credit_period, name, counter) are new.
"""Wire-value parsers (S1 spec §5.2). Strict: a value that is present but unparseable raises WireParseError; blank is
None; nothing is ever turned into zero (Part 1 §13). Forex grammar per the header above (ruling A3, D3)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


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


_PLAIN = re.compile(r"^-?\d+(\.\d+)?$")
_NUM = r"\d[\d,]*(?:\.\d+)?"
_CURRENCY = r"[^\d\s@=/,.+?₹-]+"
_BASE_SYMBOL = r"\?|₹|Rs\.?"
_FOREX = re.compile(
    rf"^\s*(?P<sign>-?)\s*(?P<cur>{_CURRENCY})\s*(?P<face>{_NUM})"
    rf"\s*@\s*(?P<rsym>{_BASE_SYMBOL})?\s*(?P<rate>{_NUM})\s*/\s*(?P<per>{_CURRENCY})"
    rf"(?:\s*=\s*(?P<bsign>-?)\s*(?P<bsym>{_BASE_SYMBOL})?\s*(?P<base>{_NUM}))?\s*$")


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def amount(text: str | None) -> Amount | None:
    if text is None or not text.strip():
        return None
    if "@" not in text:
        cleaned = text.strip().replace(",", "")
        if not _PLAIN.match(cleaned):
            raise WireParseError("unparseable_amount", text)
        return Amount(Decimal(cleaned), None, None, None, True, text)
    m = _FOREX.match(text)
    if m is None or m["per"] != m["cur"]:
        raise WireParseError("unparseable_amount", text)
    negative = m["sign"] == "-"
    face = _dec(m["face"]) * (-1 if negative else 1)
    fx_rate = _dec(m["rate"])
    if m["base"] is None:
        return Amount(None, m["cur"], face, fx_rate, False, text)
    stated = _dec(m["base"])
    if ((m["bsign"] == "-") != negative and face != 0 and stated != 0) or (m["bsym"] or "") != (m["rsym"] or ""):
        raise WireParseError("unparseable_amount", text)
    return Amount(stated * (-1 if m["bsign"] == "-" else 1), m["cur"], face, fx_rate, True, text)


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
        raise WireParseError("invalid_counter", text) from None
