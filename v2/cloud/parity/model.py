"""Parity engine data model (S1 spec §10.2, §10.4-§10.6, §15.5). Pure: frozen dataclasses + small helpers, no DB.

Conventions: every amount is ``Decimal`` in INR with **debit negative** (Tally's sign, the same as voucher lines and
TB rows). ``diff`` on a :class:`Line` is ``tally - our``. Tolerance is a flat ₹1.00 (Q19), inclusive; every rung
function takes it as ``tol``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from v2.cloud.clock import fy_start_of
from v2.contract.parse import name as parse_name
from v2.contract.tally_rules import PL_ACCOUNT_LEDGER

TOL = Decimal("1.00")
ZERO = Decimal("0")

VERDICTS = frozenset({"match", "mismatch", "match_revalued", "not_applicable", "missing_in_db", "missing_in_tally"})
PROBLEM_VERDICTS = frozenset({"mismatch", "missing_in_db", "missing_in_tally"})
BS_NATURES = frozenset({"assets", "liabilities"})
NOMINAL_NATURES = frozenset({"income", "expenses"})


@dataclass(frozen=True)
class LedgerIn:
    """One live ledger as parity sees it. ``mirrored_closing`` is the INR base of this capture's ClosingBalance
    (``None`` when Tally exported ``""`` -- never 0, ruling F14 -- or an unstated expression, D3). The ``*_fx``
    fields are the master's face values; they are only meaningful when the as-on date lies in Tally's current
    period (ruling F3, see ``forex.scope_face``)."""
    guid: str
    name: str
    group_guid: str | None
    primary_group: str | None
    nature: str | None
    is_forex: bool
    mirrored_closing: Decimal | None
    closing_fx: Decimal | None
    closing_fx_rate: Decimal | None
    opening_fx: Decimal | None
    balance_source: str
    in_capture: bool


@dataclass(frozen=True)
class Sums:
    """``total``: countable lines in [E, as_on]. ``face`` / ``fy_total``: countable lines in [FY(as_on).start,
    as_on]. ``face_complete[g]`` is False if any such line on g has no ``fx_amount`` (a guid with no line in
    that window is absent, which reads as complete)."""
    total: dict[str, Decimal]
    face: dict[str, Decimal]
    face_complete: dict[str, bool]
    fy_total: dict[str, Decimal]


@dataclass(frozen=True)
class TbRow:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class Line:
    scope: str                       # "ledger" | "group"
    guid: str | None
    name: str
    our: Decimal | None
    tally: Decimal | None
    diff: Decimal | None             # tally - our
    verdict: str
    cause: str | None
    unrealised: Decimal | None = None
    our_fx: Decimal | None = None
    tally_fx: Decimal | None = None


@dataclass(frozen=True)
class LineFact:
    """One countable voucher line (cancelled / optional / deleted already excluded by the caller, §10.2)."""
    guid: str
    voucher_date: date
    amount: Decimal
    fx_amount: Decimal | None


def build_sums(facts: Iterable[LineFact], *, verified_edge: date, as_on: date) -> Sums:
    """§10.2 windows: [E, as_on] for ``total``; [FY(as_on).start, as_on] for ``face`` / ``fy_total`` /
    ``face_complete``. Post-dated lines inside the window count (probe 16)."""
    fy_start = fy_start_of(as_on)
    total: dict[str, Decimal] = {}
    face: dict[str, Decimal] = {}
    complete: dict[str, bool] = {}
    fy_total: dict[str, Decimal] = {}
    for f in facts:
        if f.voucher_date > as_on:
            continue
        if f.voucher_date >= verified_edge:
            total[f.guid] = total.get(f.guid, ZERO) + f.amount
        if f.voucher_date >= fy_start:
            fy_total[f.guid] = fy_total.get(f.guid, ZERO) + f.amount
            if f.fx_amount is None:
                complete[f.guid] = False
            else:
                face[f.guid] = face.get(f.guid, ZERO) + f.fx_amount
                complete.setdefault(f.guid, True)
    return Sums(total=total, face=face, face_complete=complete, fy_total=fy_total)


def tb_rows(parsed_rows: Iterable[dict]) -> list[TbRow]:
    """Snapshot rows (``snapshots.parse_cells(...).rows``: ``{"name", "amount": str | None}``) -> ``TbRow``s in
    document order. A row with no amount cell carries no balance and is dropped; names are D31-cleaned."""
    return [TbRow(parse_name(r["name"]), Decimal(r["amount"])) for r in parsed_rows if r.get("amount") is not None]


def synthetic_amount(rows: Iterable[TbRow], row_name: str) -> Decimal | None:
    """A synthetic TB row (``Opening Stock``, ``Unadjusted Forex Gain/Loss``) read from the SAME response
    (LESSONS rule 19); ``None`` when the TB doesn't carry it."""
    return next((r.amount for r in rows if r.name == row_name), None)


def is_pl_account(ledger: LedgerIn) -> bool:
    return parse_name(ledger.name) == PL_ACCOUNT_LEDGER


def compare(our: Decimal, tally: Decimal, tol: Decimal) -> str:
    return "match" if abs(tally - our) <= tol else "mismatch"


def is_clean(lines: Iterable[Line]) -> bool:
    """No line is a mismatch or a missing_*: the run's lines support status ``ok`` (§10.8)."""
    return not any(l.verdict in PROBLEM_VERDICTS for l in lines)
