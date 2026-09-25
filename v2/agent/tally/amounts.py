"""Decimal amounts for Tally values: never float, and a missing value is never silently zero.

New in v2 (Part 1 §13): the current parse_amount returns 0.0 for anything it can't parse, which turns a parse
failure into a legitimate-looking zero (a false parity match).

Forex (Part 1 decision 15 / C47, S1 decision taken 2026-09-25): TallyPrime exports a foreign-currency amount — a forex
voucher line AND a forex ledger's Opening/ClosingBalance — as one expression, live (TallyPrime 7.0, Educational,
Wine 11.0; p22_B_usd_ledger.xml, p22_B_forex_sales.xml): `-$1609.71 @ ? 82.58/$ = -? 132929.85`. The base currency ₹
exports as a literal `?` followed by a space. `parse_forex` takes the STATED INR base (after `=`) as the amount —
no conversion at read time — and keeps face, currency and rate as structured fields. Without a stated base the base
is face × rate, ROUND_HALF_UP to paise, flagged `base_derived` (Tally's own rounding there is unmeasured).
`parse_decimal` stays strict (spec §11.2; probe 22 records that it raises); `parse_amount` is the entry point that
reads either form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class AmountParseError(ValueError):
    """The text is present but is not a plain number (e.g. a forex expression)."""

    def __init__(self, raw: str):
        super().__init__(f"Not a plain Tally amount: {raw!r}")
        self.raw = raw


def parse_decimal(text: str | None) -> Decimal | None:
    """'-1,048,846.53' → Decimal('-1048846.53'); '' or None → None; anything else raises AmountParseError."""
    if text is None or not text.strip():
        return None
    cleaned = text.strip().replace(",", "")
    if not _PLAIN_NUMBER.match(cleaned):
        raise AmountParseError(text)
    return Decimal(cleaned)


_NUM = r"\d[\d,]*(?:\.\d+)?"
_CURRENCY = r"[^\d\s@=/,.+?₹-]+"            # "$", "€", "USD" — never a digit, a separator or a base symbol
_BASE_SYMBOL = r"\?|₹|Rs\.?"                 # the base currency: `?` live (₹ mis-encoded), ₹, Rs. — or nothing
_FOREX = re.compile(
    rf"^\s*(?P<sign>-?)\s*(?P<cur>{_CURRENCY})\s*(?P<face>{_NUM})"
    rf"\s*@\s*(?P<rsym>{_BASE_SYMBOL})?\s*(?P<rate>{_NUM})\s*/\s*(?P<per>{_CURRENCY})"
    rf"(?:\s*=\s*(?P<bsign>-?)\s*(?P<bsym>{_BASE_SYMBOL})?\s*(?P<base>{_NUM}))?\s*$")
_PAISA = Decimal("0.01")


@dataclass(frozen=True)
class ForexAmount:
    base: Decimal            # signed INR amount (Dr negative, as a plain export) — the stated one, or face × rate
    face: Decimal            # signed foreign face value
    currency: str            # the foreign symbol as written ("$")
    rate: Decimal            # base-currency units per foreign unit
    base_symbol: str         # the base symbol as written ("?", "₹", "Rs.", "")
    base_derived: bool       # True: no "= base" part, so base = face × rate (HALF_UP to paise)
    raw: str


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def parse_forex(text: str) -> ForexAmount:
    """A Tally forex expression → ForexAmount; anything else (plain numbers included) raises AmountParseError."""
    match = _FOREX.match(text or "")
    if match is None or match["per"] != match["cur"]:
        raise AmountParseError(text)
    negative = match["sign"] == "-"
    face = _number(match["face"]) * (-1 if negative else 1)
    rate = _number(match["rate"])
    rate_symbol = match["rsym"] or ""
    if match["base"] is None:
        base = (face * rate).quantize(_PAISA, rounding=ROUND_HALF_UP)       # half-up = away from zero on a tie
        return ForexAmount(base, face, match["cur"], rate, rate_symbol, True, text)
    stated = _number(match["base"])
    signs_disagree = (match["bsign"] == "-") != negative and face != 0 and stated != 0
    if signs_disagree or (match["bsym"] or "") != rate_symbol:
        raise AmountParseError(text)
    base = stated * (-1 if match["bsign"] == "-" else 1)
    return ForexAmount(base, face, match["cur"], rate, rate_symbol, False, text)


def parse_amount(text: str | None) -> tuple[Decimal | None, ForexAmount | None]:
    """A plain amount or a forex expression → (INR amount, forex parts or None). '' / None → (None, None).
    Anything else raises AmountParseError. The INR amount of an expression is its (stated) base."""
    if text is None or not text.strip():
        return None, None
    if "@" in text:
        forex = parse_forex(text)
        return forex.base, forex
    return parse_decimal(text), None
