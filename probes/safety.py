"""Guards that run before any request is sent (S0 spec §4.5). A guard failure sends nothing."""
from __future__ import annotations

import re

from probes.reads import tally_date

_FORBIDDEN_REQUEST = (
    (re.compile(r"<NATIVEMETHOD\b[^>]*>[^<]*\*[^<]*</NATIVEMETHOD\s*>", re.IGNORECASE),
     "`*` as a NATIVEMETHOD crashes Tally (LESSONS §5)"),
    (re.compile(r"<FETCH\b[^>]*>[^<]*\*[^<]*</FETCH\s*>", re.IGNORECASE),
     "`*` as a FETCH value crashes Tally (LESSONS §5)"),
    (re.compile(r"\$\$InDateRange", re.IGNORECASE), "$$InDateRange crashes TallyPrime 7 (LESSONS §3)"),
)

_STAR_ENTITY = re.compile(r"&#(?:0*42|x0*2a);", re.IGNORECASE)


class GuardError(Exception):
    """A guard refused; nothing was sent."""


def _decode_star_entities(xml: str) -> str:
    """A numeric character reference for `*` (decimal or hex, any case) decoded before matching."""
    return _STAR_ENTITY.sub("*", xml)


def check_request(xml: str) -> None:
    decoded = _decode_star_entities(xml)
    for pattern, reason in _FORBIDDEN_REQUEST:
        if pattern.search(decoded):
            raise GuardError(f"Request refused: {reason}")


def check_mutation_allowed(expected: str, *, mutating: bool) -> None:
    """The company-name half of the mutation guard, checked before any request (even the company-list guard) is sent."""
    if mutating and "Probe" not in expected:
        raise GuardError(f"Refusing to change {expected!r}: only companies with 'Probe' in the name may be changed.")


def check_company(loaded: list[str], expected: str, *, mutating: bool) -> None:
    """Exactly one company loaded, and it's the expected one. Changes only on companies named '…Probe…'."""
    if not loaded:
        raise GuardError(f"No company open in Tally. Open {expected!r}.")
    if len(loaded) > 1:
        raise GuardError(f"{len(loaded)} companies are loaded ({', '.join(loaded)}). Close all but {expected!r}.")
    if loaded[0] != expected:
        raise GuardError(f"Tally has {loaded[0]!r} open, but this step expects {expected!r}.")
    check_mutation_allowed(expected, mutating=mutating)


# C43 (live 2026-09-24, LESSONS §15 rule 22): Educational TallyPrime silently ignores a date static variable whose day
# is not one of these — typed or untyped — and answers for the current period instead. Company-B month windows are
# clamped to them (company_b_view.month_window); every other date is chosen on them. This is the ONE implementation of
# the rule (Ruling Q4): ProbeContext's request guard, the anchors check and company_b_view.check_date_vars all call it,
# and every function below reads this constant at call time.
EDUCATIONAL_DATE_VAR_DAYS = (1, 2, 31)
_DATE_VAR = re.compile(r"<(SV[A-Z0-9]*DATE)\b[^>]*>([^<]*)</\1\s*>", re.IGNORECASE)


def _ignored_by_educational(value: str) -> bool:
    """True when `value` is a date (any format Tally reads — reads.tally_date, the one date parser) on a day an
    Educational Tally ignores. A placeholder (__FROM__) or a non-date is not a date Tally would read either."""
    day = tally_date(value)
    return day is not None and day.day not in EDUCATIONAL_DATE_VAR_DAYS


def educational_ignored_dates(xml: str) -> list[str]:
    """`NAME=value` for every date static variable an Educational Tally would silently replace (C43)."""
    return [f"{name}={value.strip()}" for name, value in _DATE_VAR.findall(xml) if _ignored_by_educational(value)]


def _refuse_educational(licence: str | None, bad: list[str]) -> None:
    """A licensed (or not-yet-recorded) licence passes: a licensed Tally honours any valid date, and probe 0 runs
    before any licence is known."""
    if licence == "educational" and bad:
        raise GuardError(f"Request refused: {', '.join(bad)} — Educational Tally ignores a date variable off day "
                         "1/2/31 and silently answers for the current period (C43, LESSONS §15 rule 22); choose "
                         "dates on those days (company_b_view.month_window for company-B months).")


def check_educational_dates(xml: str, licence: str | None) -> None:
    """Refuse, before sending, request XML carrying a date an Educational Tally would ignore."""
    _refuse_educational(licence, educational_ignored_dates(xml))


def check_educational_date_values(licence: str | None, *dates: str) -> None:
    """The same rule for bare date strings (company_b_view.check_date_vars), before any request is built."""
    _refuse_educational(licence, [d for d in dates if _ignored_by_educational(d)])
