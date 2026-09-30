"""Probe 5: do SVFROMDATE / SVTODATE bound a Voucher collection to a month and a day? (S0 spec §7 "Probe 5", B)

Feeds the S2 extractor's month chunks. C33 (live 2026-09-24): Tally honours the period variables only with
TYPE="Date". Untyped, it silently answers for the company's current period. v2's envelope renderer always types them,
so the typed form is the one under test. The untyped form is sent once as recorded evidence of the silent fallback,
and it never decides the outcome. One Trial Balance pair (typed / untyped) is recorded the same way. Whether an as-on
report is history is probe 18's B part to settle, not this probe's (S0-D7).

C43 (live 2026-09-24, run 1): Educational TallyPrime also ignores a TYPED date variable whose day is not 1, 2 or 31
and answers for the current period instead, so under an educational licence June 2023 is asked as
01-06-2023..02-06-2023 (company_b_view.month_window) — exact, because an educational company holds no voucher on
any other day. The window used is recorded in the observations and the summary.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any

from agent.tally.envelopes import COMPANY_PLACEHOLDER, wrap_report
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_TO, B_CURRENT_PERIOD, drift_message, fetch_window,
                                      loaded_licence, month_window)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import (FROM_PLACEHOLDER, TO_PLACEHOLDER, VOUCHER_MONTH_FIELDS, exploded_tb_rows,
                             primary_group_rows, untyped_period_vars, voucher_request)

MONTH = (2023, 6)          # FY 2023-24: outside the current period; no flagged voucher. Window: month_window (C43).
DAY = "01-06-2023"         # day 1: honoured under either licence
C43_REASON = "Tally ignores non-1/2/31 date variables — C43"
MONTH_COLLECTION = "S0VoucherMonth"
FORMULA_COLLECTION = "S0P05MonthFormula"
FORMULA_FILTER = "S0P05InWindow"
# Candidate only (spec §7 step 2; never $$InDateRange, LESSONS §3). A collection's scope is its period, so the formula
# runs inside a typed books-wide period, and the placeholders carry the window.
FORMULA_CANDIDATE = f'$Date >= $$Date:"{FROM_PLACEHOLDER}" AND $Date <= $$Date:"{TO_PLACEHOLDER}"'

TYPED_IMPACT = ('The extractor\'s month request types its period variables (TYPE="Date", C33): untyped, Tally '
                "silently answers for the company's current period, so an untyped chunk looks healthy and is wrong "
                "(Part 1 §5 extractor).")
TYPED_IMPACT_UNCONFIRMED = ('The extractor\'s month request types its period variables (TYPE="Date"); this run did '
                            "not reproduce C33's untyped silent-fallback claim, so that specific behaviour is not "
                            "confirmed evidence (Part 1 §5 extractor).")
FORMULA_IMPACT = ("Typed SVFROMDATE/SVTODATE don't bound a Voucher collection: the extractor bounds month chunks with "
                  "the $Date formula filter inside a books-wide typed period (Part 1 §5 extractor).")
DAY_IMPACT = ("A one-day window doesn't return exactly that day's vouchers, so month chunks can't auto-split to days: "
              "the extractor splits to half-months at most and the ~5k-voucher chunk cap is re-sized (Part 1 §5, R3).")
FAILED_IMPACT = ("Neither form bounds a Voucher collection: the extractor fetches wider windows and filters by date in "
                 "Python, and the chunk cap is re-sized (Part 1 §5 extractor, R3).")


def svdates_template() -> str:
    return voucher_request(MONTH_COLLECTION, VOUCHER_MONTH_FIELDS, COMPANY_PLACEHOLDER,
                           from_date=FROM_PLACEHOLDER, to_date=TO_PLACEHOLDER)


def formula_template() -> str:
    return voucher_request(FORMULA_COLLECTION, VOUCHER_MONTH_FIELDS, COMPANY_PLACEHOLDER, from_date=B_BOOKS_FROM,
                           to_date=B_BOOKS_TO, filters=[(FORMULA_FILTER, FORMULA_CANDIDATE)])


def _dmy(day: date) -> str:
    return day.strftime("%d-%m-%Y")


def _window(licence: str) -> dict[str, Any]:
    start, end = month_window(*MONTH, licence)
    clamped = end.day != monthrange(*MONTH)[1]
    return {"licence": licence, "from": _dmy(start), "to": _dmy(end), "clamped": clamped,
            "reason": C43_REASON if clamped else None}


def _window_note(window: dict[str, Any]) -> str:
    return (f"{window['licence']}: to-date clamped to {window['to']} because {window['reason']}." if window["clamped"]
            else f"Window {window['from']}..{window['to']}.")


def _c33_reproduced(untyped: dict[str, Any]) -> bool:
    """C33: untyped, Tally silently answers for the company's current period. Reproduced when the untyped answer
    holds vouchers outside the requested window and every dated row falls inside the current period — live run 1:
    240 vouchers 2025-04-01..2026-03-31 for a June-2023 request. Independent of whether the typed form bounded."""
    span = untyped["date_span"]
    if not untyped["out_of_window"] or span is None:
        return False
    start, end = B_CURRENT_PERIOD
    return start.isoformat() <= span[0] and span[1] <= end.isoformat()


def _group_rows(text: str) -> dict[str, str]:
    return {name: str(row["closing_balance"]) for name, row in primary_group_rows(exploded_tb_rows(text)).items()}


async def _report_pair(ctx: ProbeContext, as_on: str) -> dict[str, Any]:
    # as_on is the month window's to-date: C43 applies to a report's SVTODATE too (30-06-2023 would be ignored).
    typed_xml = wrap_report("Trial Balance", B_BOOKS_FROM, as_on, ctx.company_name)
    typed_text = await ctx.send("report_tb_typed", typed_xml)
    typed_raw = ctx.last_response.raw
    untyped_text = await ctx.send("report_tb_untyped", untyped_period_vars(typed_xml))
    return {"as_on": as_on, "identical_bytes": typed_raw == ctx.last_response.raw,
            "typed": _group_rows(typed_text), "untyped": _group_rows(untyped_text),
            "note": "Evidence only: probe 18's B part settles whether an as-on report is history (S0-D7)."}


def _untyped_evidence(untyped: dict[str, Any], reproduced: bool) -> str:
    span = untyped["date_span"]
    seen = f"untyped, Tally answered {untyped['returned']} voucher(s)" + (f" dated {span[0]}..{span[1]}" if span else "")
    return (f"C33 reproduced: {seen}." if reproduced
            else f"C33 NOT reproduced: {seen}. Review before trusting either form.")


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    window = _window(licence)
    ctx.observe("window", window)
    month_from, month_to = window["from"], window["to"]
    typed_template = svdates_template()
    typed, _ = await fetch_window(ctx, "month_svdates", typed_template, licence, month_from, month_to)
    ctx.observe("month_typed", typed)
    # I1: a bounded, complete window that also holds an untagged/extra/duplicate row is books drift, not a request
    # failure — never try the formula fallback for it, and never let it read as FAILED/FAILED_IMPACT.
    if typed["drifted"]:
        raise ProbeBlocked(drift_message("month_svdates", typed))
    untyped, _ = await fetch_window(ctx, "month_svdates_untyped", typed_template, licence, month_from, month_to,
                                    untyped=True)
    reproduced = _c33_reproduced(untyped)
    ctx.observe("month_untyped", untyped)
    ctx.observe("c33_reproduced", reproduced)

    form, template = ("svdates_typed", typed_template) if typed["reach_ok"] else (None, None)
    if form is None:
        formula, _ = await fetch_window(ctx, "month_formula", formula_template(), licence, month_from, month_to)
        ctx.observe("month_formula", formula)
        if formula["drifted"]:
            raise ProbeBlocked(drift_message("month_formula", formula))
        if formula["reach_ok"]:
            form, template = "formula", formula_template()
    day = None
    if form is not None:
        day, _ = await fetch_window(ctx, "day_svdates", template, licence, DAY, DAY)
        ctx.observe("day", day)
    ctx.observe("report_period_vars", await _report_pair(ctx, month_to))

    evidence = f"{_window_note(window)} {_untyped_evidence(untyped, reproduced)}"
    if form is None:
        return PartResult(Outcome.FAILED, "Neither typed SVFROMDATE/SVTODATE nor the $Date formula candidate bounds a "
                                          f"Voucher collection to June 2023. {evidence}", spec_impact=FAILED_IMPACT)
    ctx.confirm_request("voucher_month", template, form=form, fields=list(VOUCHER_MONTH_FIELDS),
                        day_window_exact=day["match"],
                        placeholders=[COMPANY_PLACEHOLDER, FROM_PLACEHOLDER, TO_PLACEHOLDER])
    notes, impacts = [], []
    if form == "formula":
        notes.append("only the $Date formula bounds June 2023")
        impacts.append(FORMULA_IMPACT)
    if not day["match"]:
        notes.append(f"a one-day window isn't exact (missing {day['missing']}, extra {day['extra']}, "
                     f"out of window {day['out_of_window']})")
        impacts.append(DAY_IMPACT)
    if impacts:
        return PartResult(Outcome.DIFFERENT, "; ".join(notes) + f". {evidence}", spec_impact=" ".join(impacts))
    return PartResult(Outcome.CONFIRMED, f"Typed SVFROMDATE/SVTODATE bound June 2023 exactly ({typed['returned']} "
                                         f"vouchers) and a one-day window returns exactly {DAY}'s {day['returned']}. "
                                         f"{evidence}",
                      spec_impact=TYPED_IMPACT if reproduced else TYPED_IMPACT_UNCONFIRMED)


PROBE = Probe(
    id=5,
    name="voucher_month_bounds",
    question="Do SVFROMDATE / SVTODATE bound a Voucher collection to a month and to a day, and in which form?",
    feeds=("extractor chunks", "C33"),
    parts={"B": run_b},
    requires=(0,),
    educational_sensitive=True,
)
