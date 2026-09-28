"""Opening anchors (S1 spec D9, §10.2, §10.4). Pure.

The anchor for a verified span starting at ``E`` is the **ledger-level** TB as-on ``E - 1`` (a 31 March, C43-safe).
When ``E = books_from`` it is the ledger-level TB as-on ``books_from`` minus our own lines dated ``books_from`` --
no read of a date before the books begin. Never a master ``OpeningBalance`` (C46).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping

from v2.cloud.ingest.resolve import NameIndex, ResolveError
from v2.cloud.parity.model import ZERO, TbRow
from v2.contract.tally_rules import SYNTHETIC_TB_ROWS


@dataclass(frozen=True)
class AnchorPlan:
    as_on: date
    subtract_lines_dated: date | None


def plan(verified_edge: date, books_from: date) -> AnchorPlan:
    if verified_edge < books_from:
        raise ValueError("verified edge before books_from")
    if verified_edge == books_from:
        return AnchorPlan(books_from, books_from)
    return AnchorPlan(verified_edge - timedelta(days=1), None)


def require_ledgerwise(flags: Mapping[str, str] | None) -> None:
    """Only a TB requested with ``ISLEDGERWISE = Yes`` is ledger-level (probe 17). An ``EXPLODEFLAG`` TB is a
    flat group/sub-group listing -- it misses custom sub-groups and can't place ledgers (D29)."""
    if not flags or flags.get("ISLEDGERWISE") != "Yes":
        raise ValueError("not a ledger-level TB")


def resolve_rows(rows: list[TbRow], index: NameIndex) -> tuple[dict[str, Decimal], list[str]]:
    """Ledger-level TB rows -> ``{ledger guid: amount}`` via the D13 resolver (ledgers only). Synthetic rows
    (``Opening Stock``, ``Unadjusted Forex Gain/Loss``) are skipped; rows resolving to no live ledger come back in
    ``unresolved`` (document order, each name once). An ambiguous name raises ``ResolveError`` (retryable, §11)."""
    by_guid: dict[str, Decimal] = {}
    unresolved: list[str] = []
    for row in rows:
        if row.name in SYNTHETIC_TB_ROWS:
            continue
        try:
            guid = index.resolve("ledger", row.name)
        except ResolveError as exc:
            if exc.code != "missing_master":
                raise
            if row.name not in unresolved:
                unresolved.append(row.name)
            continue
        if guid in by_guid:
            raise ValueError(f"ledger row repeated in a ledger-level TB: {row.name!r}")
        by_guid[guid] = row.amount
    return by_guid, unresolved


def anchor_amounts(ledgerwise_rows: list[TbRow], index: NameIndex,
                   books_from_line_sums: dict[str, Decimal] | None) -> tuple[dict[str, Decimal], list[str]]:
    """The per-ledger anchor. ``books_from_line_sums`` (only for the ``E = books_from`` plan): Σ our countable
    lines dated ``books_from`` per ledger, subtracted -- including for a ledger absent from the TB (its balance at
    the end of day one is 0, so its anchor is minus its day-one lines). A ledger absent from both has anchor 0 and
    is simply not in the dict (§10.4)."""
    anchors, unresolved = resolve_rows(ledgerwise_rows, index)
    for guid, amount in (books_from_line_sums or {}).items():
        anchors[guid] = anchors.get(guid, ZERO) - amount
    return anchors, unresolved
