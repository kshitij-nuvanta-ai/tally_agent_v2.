"""Read-side view of company B's dataset for the company-B probes (S0 spec §4.3, §7).

Probe modules may not import `probes.setup` (the write code, S0-D8; tests/test_layers.py). The dataset generator
`setup/company_b_data.py` is pure (no I/O, no v2 imports), so this module is the one bridge. It imports nothing else
from `setup/`, and tests/test_layers.py pins both facts.

The count rule (S0 plan part 4, Global Constraints): a window's expected tags are every dataset voucher dated in it
without `skip_reason` (C36: never written). Cancelled and optional vouchers are included, because they are vouchers
that exist in Tally, even though Tally leaves them out of balances (C42). A window is exact when no unflagged tag is
missing and nothing unexpected, untagged, duplicated or out of the window came back. Whether the flagged tags came
back is recorded, never judged: probe 3's B part owns the flags (S0-D7).
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable

from probes.core import ProbeBlocked
from probes.reads import dmy, fill_month_request, parse_vouchers, tally_date, untyped_period_vars
from probes.safety import EDUCATIONAL_DATE_VAR_DAYS, check_educational_date_values
from probes.setup.company_b_data import (COMPANY_B_BOOKS_FROM, COMPANY_B_LAST_MONTH, COMPOUND_UNIT, HINDI_DEBTOR,
                                            OPENING_BILL_DATE, SALES_GST_VOUCHER_TYPE, TAG_PREFIX, USD_CURRENCY,
                                            USD_EXPORT_PARTY, Dataset, Expected, LedgerSpec, StockItemSpec, VoucherSpec,
                                            _educational_days, expected_figures, generate, quantity_unit)

if TYPE_CHECKING:
    from probes.context import ProbeContext

B_BOOKS_FROM_DATE = COMPANY_B_BOOKS_FROM
B_BOOKS_FROM = COMPANY_B_BOOKS_FROM.strftime("%d-%m-%Y")        # "01-04-2022"
# The dataset's last month (COMPANY_B_LAST_MONTH), end-of-month — was hard-coded "31-03-2026" (review M2/P8b).
_B_BOOKS_TO_DATE = date(COMPANY_B_LAST_MONTH.year, COMPANY_B_LAST_MONTH.month,
                        monthrange(COMPANY_B_LAST_MONTH.year, COMPANY_B_LAST_MONTH.month)[1])
B_BOOKS_TO = _B_BOOKS_TO_DATE.strftime("%d-%m-%Y")
# The company's current period: the FY that holds its last month (live: 1-Apr-2025..31-Mar-2026). C33: an untyped
# period variable silently answers for this period; C43: so does an educational one off day 1/2/31.
B_CURRENT_PERIOD = (date(_B_BOOKS_TO_DATE.year - (1 if _B_BOOKS_TO_DATE.month < 4 else 0), 4, 1), _B_BOOKS_TO_DATE)
# C43 (live 2026-09-24): Educational TallyPrime silently ignores a date static variable (SVFROMDATE/SVTODATE, typed)
# whose day is not one of these — the same rule it applies to voucher dates — and falls back to the current period.
# The constant and the check live in `safety` (Ruling Q4: one C43 implementation); this module re-exports them.
_TAG = re.compile(rf"^\[{re.escape(TAG_PREFIX)}:(\d+)\]")


def tag_of(narration: str) -> int | None:
    """The loader's tag at the start of a narration ('[S0-B:281] …' → 281); anything else → None."""
    match = _TAG.match((narration or "").strip())
    return int(match.group(1)) if match else None


def kind_label(v: VoucherSpec) -> str:
    """The storage kind probe 21 sizes by: base kind, then '+inventory' and '+bills' when the voucher has them."""
    return v.kind + ("+inventory" if v.inventory else "") + ("+bills" if v.bills else "")


@lru_cache(maxsize=2)
def dataset(licence: str) -> Dataset:
    return generate(licence)


@dataclass(frozen=True)
class WindowExpectation:
    written: dict[int, VoucherSpec]      # tag -> voucher, for every tag the loader wrote in the window
    flagged: frozenset[int]              # cancelled or optional among `written`
    skipped: frozenset[int]              # skip_reason: dated in the window, never written

    @property
    def unflagged(self) -> frozenset[int]:
        return frozenset(self.written) - self.flagged


def month_window(year: int, month: int, licence: str) -> tuple[date, date]:
    """The (from, to) window every company-B probe asks Tally for when it means "this calendar month" (C43, Ruling R3).

    Licensed: the 1st to the month's last day. Educational: the 1st to the last day ≤ month-end whose day is 1, 2 or 31
    — the 31st in a 31-day month, else the 2nd. Educational TallyPrime silently ignores a date variable on any other
    day and answers for the current period instead (C43: typed 01-06-2023..30-06-2023 returned 680 vouchers running to
    2026-03-31). The clamp loses nothing: an educational company cannot hold a voucher on any other day either
    (`_educational_days`, the loader's own rule), so 1st..2nd or 1st..31st still bounds the whole month exactly.
    [Educational mode — confirm on a licensed Tally.]
    """
    if licence not in ("licensed", "educational"):
        raise ValueError(f"unknown licence {licence!r}")
    last = monthrange(year, month)[1]
    if licence == "educational":
        last = max(day for day in _educational_days(year, month) if day in EDUCATIONAL_DATE_VAR_DAYS)
    return date(year, month, 1), date(year, month, last)


def check_date_vars(licence: str, *dates: str) -> None:
    """Refuse a request date an educational Tally would silently ignore (C43): sending it reads as a healthy answer
    for the wrong period, which is worse than not asking. Raises safety.GuardError — the same check, exception and
    date parser as the request-level guard in ProbeContext (Ruling Q4)."""
    check_educational_date_values(licence, *dates)


def expect_window(licence: str, start: date, end: date) -> WindowExpectation:
    in_window = [v for v in dataset(licence).vouchers if start <= v.date <= end]
    written = {v.tag: v for v in in_window if not v.skip_reason}
    return WindowExpectation(written=written,
                             flagged=frozenset(t for t, v in written.items() if v.cancelled or v.optional),
                             skipped=frozenset(v.tag for v in in_window if v.skip_reason))


def voucher_rows(text: str) -> list[dict[str, Any]]:
    """[{"tag", "date"}] for every voucher in a collection response (CMPINFO's counter skipped by parse_vouchers)."""
    return [{"tag": tag_of(v["header"].get("NARRATION", "")), "date": tally_date(v["header"].get("DATE", ""))}
            for v in parse_vouchers(text)]


def compare_tags(rows: list[dict[str, Any]], expected: WindowExpectation, start: date, end: date) -> dict[str, Any]:
    dates = [row["date"] for row in rows if row["date"] is not None]
    out_of_window = sum(1 for row in rows if row["date"] is None or not start <= row["date"] <= end)
    tags = [row["tag"] for row in rows if row["tag"] is not None]
    got = set(tags)
    result = {
        "returned": len(rows),
        "expected_written": len(expected.written),
        "expected_unflagged": len(expected.unflagged),
        "bounded": out_of_window == 0,
        "out_of_window": out_of_window,
        "date_span": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
        "untagged": len(rows) - len(tags),
        "missing": sorted(expected.unflagged - got),
        "extra": sorted(got - set(expected.written)),
        "duplicates": sorted(t for t in got if tags.count(t) > 1),
        "flagged_returned": sorted(expected.flagged & got),
        "flagged_missing": sorted(expected.flagged - got),
    }
    # I1: split "the request doesn't bound the window" (reach_ok False — a real request/reach failure) from "the
    # request bounded and returned every expected tag, but the books hold something the dataset doesn't" (drifted —
    # company B no longer matches its generated dataset; never a request failure). `match` keeps its original,
    # stricter meaning (reach_ok and not drifted) so every existing exactness check is unchanged.
    result["reach_ok"] = result["bounded"] and not result["missing"]
    result["drifted"] = result["reach_ok"] and bool(result["untagged"] or result["extra"] or result["duplicates"])
    result["match"] = result["reach_ok"] and not result["drifted"]
    return result


def drift_message(step: str, result: dict[str, Any]) -> str:
    """I1: the window bounded and every expected tag came back, but something extra came with it — an untagged
    row, an unexpected tag, or a duplicate. That means company B has drifted from its generated dataset (a hand
    edit, a partial reseed, …), not that the request under test failed to bound or fetch the window."""
    bits = []
    if result["untagged"]:
        bits.append(f"untagged {result['untagged']}")
    if result["extra"]:
        bits.append(f"extra {result['extra']}")
    if result["duplicates"]:
        bits.append(f"duplicates {result['duplicates']}")
    return (f"Company B differs from the dataset in {step}: {', '.join(bits)} — re-run `setup-b` verify or "
            "restore the backup before trusting this probe again.")


async def fetch_window(ctx: "ProbeContext", step: str, template: str, licence: str, start: str, end: str, *,
                       untyped: bool = False) -> tuple[dict[str, Any], str]:
    """Fetch and decode one Voucher-collection month/day window through the ONE shared path probe 5 and probe 21
    both call (review M2/P8b — they used to decode the same kind of response two different ways: probe 5 read
    `ctx.send`'s sanitized text, probe 21 separately re-decoded `ctx.last_response.raw`). `start`/`end` are
    DD-MM-YYYY. Returns (compare_tags result, the raw response text) — `parse_vouchers` sanitizes internally, so
    the same raw text is safe both for tag comparison (`voucher_rows`) and for probe 21's byte-accurate size
    measurement (`voucher_blocks`), which is why this hands back the raw text rather than `ctx.send`'s return value.
    C43: under an educational licence a date off day 1/2/31 is refused before anything is sent (`check_date_vars`);
    callers build month windows with `month_window`.
    """
    check_date_vars(licence, start, end)
    xml = fill_month_request(template, ctx.company_name, start, end)
    await ctx.send(step, untyped_period_vars(xml) if untyped else xml)
    raw_text = ctx.last_response.raw.decode("utf-8")
    result = compare_tags(voucher_rows(raw_text), expect_window(licence, dmy(start), dmy(end)), dmy(start), dmy(end))
    return result, raw_text


def loaded_licence(environment: dict[str, Any]) -> str:
    """The licence company B was loaded under, once setup-b has finished cleanly; otherwise the part can't run."""
    if not environment.get("company_b_loaded_at"):
        raise ProbeBlocked("Company B isn't loaded yet (no company_b_loaded_at in results.json): run "
                           "`uv run python -m probes setup-b` to a clean finish first.")
    licence = environment.get("licence")
    if licence not in ("licensed", "educational"):
        raise ProbeBlocked("No licence recorded: run probe 0 first (company B's voucher dates depend on it).")
    return licence


@lru_cache(maxsize=2)
def expected(licence: str) -> Expected:
    """expected_figures of the dataset: computed from the dataset alone, never from Tally (spec §4.3). C42: flagged
    vouchers move nothing; C36: skipped ones don't exist."""
    return expected_figures(dataset(licence))


def ledger_balances_at(licence: str, day: date) -> dict[str, Decimal]:
    """Every dataset ledger's balance at the close of `day`, which must be a month end inside the books. Running
    balances from the books start: right for balance-sheet ledgers; a nominal ledger's is cumulative across FYs,
    which Tally resets every April — callers compare balance-sheet ledgers only, or a period starting at books start."""
    rows = {name: value for (name, when), value in expected(licence).ledger_month_end.items() if when == day}
    if not rows:
        raise ValueError(f"{day.isoformat()} is not a month end inside company B's books")
    return rows


def ledger_openings_at(licence: str, fy_start: date) -> dict[str, Decimal]:
    """Every dataset ledger's balance at the start of the FY beginning `fy_start` (1 April); the books-start one is
    the loader's signed opening (C30)."""
    rows = {name: value for (name, when), value in expected(licence).ledger_fy_opening.items() if when == fy_start}
    if not rows:
        raise ValueError(f"{fy_start.isoformat()} is not an FY start inside company B's books")
    return rows


def ledger_specs(licence: str) -> dict[str, LedgerSpec]:
    return {spec.name: spec for spec in dataset(licence).ledgers}


def item_specs(licence: str) -> dict[str, StockItemSpec]:
    return {spec.name: spec for spec in dataset(licence).items}


def qty_unit(licence: str, item: str) -> str:
    """The unit a quantity of `item` is written and read in (C40: a compound unit's FIRST unit, e.g. "Box")."""
    return quantity_unit(dataset(licence).units, item_specs(licence)[item].unit)


def stock_qty_at(licence: str, item: str, day: date) -> Decimal:
    return expected(licence).stock_month_end[(item, day)]


def first_voucher(licence: str, wanted: Callable[[VoucherSpec], bool]) -> VoucherSpec:
    """The earliest (date, then tag) voucher the loader wrote, not flagged, that `wanted` accepts."""
    for v in sorted(dataset(licence).vouchers, key=lambda v: (v.date, v.tag)):
        if not v.skip_reason and not v.cancelled and not v.optional and wanted(v):
            return v
    raise LookupError("no such voucher in company B's dataset")


B_CUSTOM_VOUCHER_TYPE = SALES_GST_VOUCHER_TYPE     # made in the UI under Sales before setup-b (spec §4.3)
B_CUSTOM_VOUCHER_TYPE_BASE = "Sales"
_CREDIT_DAYS = re.compile(r"^\s*(\d+)\s*Days?\s*$", re.IGNORECASE)


def credit_days(text: str | None) -> int | None:
    """'30 Days' → 30: Tally's BILLCREDITPERIOD text (live: `<BILLCREDITPERIOD JD=… P="30 Days">30 Days`) and the
    dataset's `credit_period`. Anything else → None."""
    match = _CREDIT_DAYS.match(text or "")
    return int(match.group(1)) if match else None


def written_vouchers(licence: str) -> dict[int, VoucherSpec]:
    """tag → voucher for every voucher the loader wrote (C36: skipped ones never exist in Tally)."""
    return {v.tag: v for v in dataset(licence).vouchers if not v.skip_reason}


# Plan part 7 (probe 22): company B's USD export party and the symbol of its currency, re-exported for the probes.
USD_CURRENCY_SYMBOL = USD_CURRENCY.symbol


def forex_vouchers(licence: str) -> dict[int, VoucherSpec]:
    """tag → voucher for every written foreign-currency voucher (company B: the USD export sales 101/102)."""
    return {t: v for t, v in written_vouchers(licence).items() if v.currency != "INR"}


def flagged_tags(licence: str) -> tuple[frozenset[int], frozenset[int]]:
    """(cancelled, optional) among the written vouchers — company B's 201/202 and 301/302 (probe 3 B)."""
    written = written_vouchers(licence).values()
    return frozenset(v.tag for v in written if v.cancelled), frozenset(v.tag for v in written if v.optional)


@dataclass(frozen=True)
class BillTerm:
    name: str
    party: str
    bill_date: date                # its voucher's date; the opening bill's is OPENING_BILL_DATE
    credit_period: str | None      # the dataset's text ("30 Days"); None = none (the opening bill)
    flagged: bool                  # opened by a cancelled/optional voucher — Tally keeps it out of bills (C42)

    @property
    def credit_days(self) -> int | None:
        return credit_days(self.credit_period)

    @property
    def due(self) -> date:
        """Bill date + credit period in calendar days (the rule probe 23 B checks Tally's BILLDUE against)."""
        return self.bill_date + timedelta(days=self.credit_days or 0)


def bill_terms(licence: str) -> dict[str, BillTerm]:
    """Every bill the loader opened, by name: each ledger's opening bill, and each New Ref on a written voucher."""
    terms: dict[str, BillTerm] = {}
    for spec in dataset(licence).ledgers:
        if spec.opening_bill:
            terms[spec.opening_bill] = BillTerm(spec.opening_bill, spec.name, OPENING_BILL_DATE, None, False)
    for v in written_vouchers(licence).values():
        for bill in v.bills:
            if bill.bill_type == "New Ref":
                terms[bill.name] = BillTerm(bill.name, v.party, v.date, bill.credit_period, v.cancelled or v.optional)
    return terms


def stock_opening_at(licence: str, item: str, fy_start: date) -> Decimal:
    """The item's quantity at the start of the FY beginning `fy_start`: setup's opening at the books start, else the
    close of the day before (a month end). C46: for the current FY this, not the books-start figure, is what
    StockItem.OpeningBalance exports."""
    if fy_start == B_BOOKS_FROM_DATE:
        return item_specs(licence)[item].opening_qty or Decimal("0")
    return stock_qty_at(licence, item, fy_start - timedelta(days=1))


def r9_candidate(licence: str) -> tuple[str, str, str]:
    """(ledger, its parent, the parent to try it under) for probe 25 B's duplicate-name attempt (R9; spec §4.3 note,
    moved to probe 25 B on 2026-09-24): the first creditor under a custom sub-group, tried under Sundry Debtors."""
    custom = {group.name for group in dataset(licence).groups}
    spec = next(s for s in dataset(licence).ledgers if s.parent in custom)
    return spec.name, spec.parent, "Sundry Debtors"
