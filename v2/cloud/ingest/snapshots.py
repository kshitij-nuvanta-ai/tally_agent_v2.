"""``POST /api/sync/{ws}/snapshots`` (S1 spec §4.6, §7.12, D8, D25): parse a Tally report's verbatim cells into
rows + a TB imbalance, then upsert on ``(workspace_id, report_type, as_on_date)`` — a newer ``captured_at`` wins,
atomically (fix round 1, I1).

TB imbalance (controller ruling F2, superseding A18's formula which omitted the top-level ``Profit & Loss A/c``
ledger row and gave a false non-zero on every multi-FY company; extended by fix-round-1 ruling I6): Σ of

  * first-occurrence **top-level group** rows (the TB is EXPLODEFLAG -- sub-group rows repeat their parent, and
    ``Opening Stock`` is nested inside the stock-bearing group's own row, never added separately -- LESSONS
    rule 19). "Top-level group" is the workspace's own **stored, live** ``tally_groups`` masters whose parent is
    Primary (``parent_guid IS NULL``); this covers a user-renamed reserved group and a wholly custom group
    equally (ruling I6). When no group masters are stored yet, it falls back to the 15 reserved primary-group
    names (``tally_rules.PRIMARY_NATURE``) -- the only names crystallised before any first sync. (§10.6's rung-2
    row matching, a separate future-task concern, still matches groups by **reserved** primary-group name only
    -- it does not need this custom-group extension, because it walks the group tree from a resolved TB row
    rather than netting a company-wide imbalance baseline.)
  * top-level LEDGER rows directly under Primary (``group_guid IS NULL``, e.g. ``Profit & Loss A/c``), same
    stored-masters-with-a-fallback rule (ruling I6; falls back to the single reserved name ``PL_ACCOUNT_LEDGER``);
  * the top-level synthetic ``Unadjusted Forex Gain/Loss`` row (LESSONS rule 29c), if present -- 0 otherwise.

If no top-level group row is found at all (an empty TB, or one whose only content is non-group noise), the
imbalance is **not** a false ``0`` -- it is ``None`` (NULL, "unknown"), with a warning, per ruling I5: a
same-``alt_mst_id`` baseline of `0` would falsely flag every later real TB as ``discarded_stale`` (D10).

``trial_balance_ledgerwise`` has no group structure to net against -- its imbalance is simply Σ of every row.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_start_of
from v2.cloud.errors import ApiError
from v2.cloud.models import SyncWorkspace, TallyGroup, TallyLedger, TallyReportSnapshot
from v2.contract.models import SnapshotRequest
from v2.contract.parse import WireParseError
from v2.contract.parse import amount as parse_amount
from v2.contract.parse import tally_date
from v2.contract.tally_rules import PL_ACCOUNT_LEDGER, PRIMARY_NATURE, SYNTHETIC_TB_ROWS, UNADJUSTED_FOREX_ROW

NAME_KEYS: dict[str, str] = {
    "trial_balance": "dspdispname", "trial_balance_ledgerwise": "dspdispname", "stock_summary": "dspdispname",
    "balance_sheet": "dspdispname", "profit_and_loss": "dspdispname",
    "bills_receivable": "billref", "bills_payable": "billref",
}
AMOUNT_KEYS: dict[str, tuple[str, ...]] = {
    "trial_balance": ("dspcldramta", "dspclcramta"), "trial_balance_ledgerwise": ("dspcldramta", "dspclcramta"),
    "stock_summary": ("dspclamta",), "balance_sheet": ("bssubamt", "bsmainamt"),
    "profit_and_loss": ("plsubamt", "bsmainamt"), "bills_receivable": ("billcl",), "bills_payable": ("billcl",),
}
_TB_TYPES = ("trial_balance", "trial_balance_ledgerwise")
_NO_PRIMARY_ROWS_WARNING = "tb_no_top_level_group_rows"


@dataclass(frozen=True)
class ParsedSnapshot:
    rows: list[dict]
    synthetic: list[str]
    imbalance: Decimal | None
    warnings: list[str] = field(default_factory=list)


def _row_amount(cell: dict, keys: tuple[str, ...]) -> Decimal | None:
    """D3: sum of the present amount cells for one row. An unstated forex expression (no ``= base``) is
    ``unparseable_amount`` for a snapshot -- S1 never revalues one server-side."""
    total: Decimal | None = None
    for k in keys:
        a = parse_amount(cell.get(k))
        if a is None:
            continue
        if not a.stated:
            raise WireParseError("unparseable_amount", cell.get(k))
        total = a.inr if total is None else total + a.inr
    return total


def _row_notes(cell: dict, name_key: str, amount_keys: tuple[str, ...]) -> list[str]:
    return [f"{k}={v}" for k, v in cell.items() if k not in (name_key, *amount_keys) and v]


def parse_cells(report_type: str, cells: list[dict], top_level_group_names: Iterable[str] | None = None,
                top_level_ledger_names: Iterable[str] | None = None) -> ParsedSnapshot:
    """``top_level_group_names`` / ``top_level_ledger_names`` (ruling I6): the workspace's own stored, live
    top-level masters. ``None`` or empty -- the default, and every existing pure-unit-test call site -- falls
    back to the reserved-name lists (``PRIMARY_NATURE`` / ``PL_ACCOUNT_LEDGER``), exactly the pre-fix-round-1
    behaviour. Passing the real stored sets is ``store()``'s job (it has DB access; this function does not)."""
    if report_type not in NAME_KEYS:
        raise ValueError(f"unknown report type {report_type!r}")
    name_key, amount_keys = NAME_KEYS[report_type], AMOUNT_KEYS[report_type]

    rows: list[dict] = []
    for cell in cells:
        amt = _row_amount(cell, amount_keys)
        rows.append({"name": cell.get(name_key, ""), "amount": str(amt) if amt is not None else None,
                     "notes": _row_notes(cell, name_key, amount_keys)})

    synthetic: list[str] = []
    if report_type in _TB_TYPES:
        names = {r["name"] for r in rows}
        synthetic = sorted(n for n in SYNTHETIC_TB_ROWS if n in names)

    imbalance: Decimal | None = None
    warnings: list[str] = []
    if report_type == "trial_balance":
        group_names = set(top_level_group_names) if top_level_group_names else set(PRIMARY_NATURE)
        ledger_names = set(top_level_ledger_names) if top_level_ledger_names else {PL_ACCOUNT_LEDGER}

        seen: set[str] = set()
        total = Decimal("0")
        for r in rows:
            if r["name"] in group_names and r["name"] not in seen:
                seen.add(r["name"])
                if r["amount"] is not None:
                    total += Decimal(r["amount"])

        if seen:                                          # ruling I5: no top-level group row -> NULL, not 0
            for name in ledger_names:
                row = next((r for r in rows if r["name"] == name), None)
                if row is not None and row["amount"] is not None:
                    total += Decimal(row["amount"])
            forex_row = next((r for r in rows if r["name"] == UNADJUSTED_FOREX_ROW), None)
            if forex_row is not None and forex_row["amount"] is not None:
                total += Decimal(forex_row["amount"])
            imbalance = total
        else:
            warnings.append(_NO_PRIMARY_ROWS_WARNING)
    elif report_type == "trial_balance_ledgerwise":
        imbalance = sum((Decimal(r["amount"]) for r in rows if r["amount"] is not None), Decimal("0"))

    return ParsedSnapshot(rows=rows, synthetic=synthetic, imbalance=imbalance, warnings=warnings)


async def _top_level_group_names(session: AsyncSession, ws_id: uuid.UUID) -> set[str]:
    rows = (await session.execute(select(TallyGroup.name).where(
        TallyGroup.workspace_id == ws_id, TallyGroup.is_deleted.is_(False), TallyGroup.parent_guid.is_(None)
    ))).scalars().all()
    return set(rows)


async def _top_level_ledger_names(session: AsyncSession, ws_id: uuid.UUID) -> set[str]:
    rows = (await session.execute(select(TallyLedger.name).where(
        TallyLedger.workspace_id == ws_id, TallyLedger.is_deleted.is_(False), TallyLedger.group_guid.is_(None)
    ))).scalars().all()
    return set(rows)


async def store(session: AsyncSession, sw: SyncWorkspace, body: SnapshotRequest, clock: Clock) -> dict:
    """§7.12: D8 period convention (``from_date`` = the FY start containing ``as_on_date``), ``as_on_date >=
    books_from``, every amount cell parses (D3) -- else 422. Upsert on the key with a single atomic
    ``INSERT ... ON CONFLICT ... WHERE captured_at < excluded.captured_at RETURNING ...`` (fix round 1, I1): a
    newer ``captured_at`` always wins, and which of two overlapping posts reaches Postgres last never decides
    the outcome -- only the ``captured_at`` values do. An equal-or-older capture is a no-op (``stored: false``,
    describing the row that IS stored, not the rejected body -- fix round 1)."""
    try:
        as_on = tally_date(body.as_on_date)
        from_date = tally_date(body.from_date)
    except WireParseError:
        raise ApiError(422, "bad_period") from None
    if from_date != fy_start_of(as_on) or as_on < sw.books_from:
        raise ApiError(422, "bad_period")

    top_groups: set[str] | None = None
    top_ledgers: set[str] | None = None
    if body.report_type == "trial_balance":
        top_groups = await _top_level_group_names(session, sw.workspace_id) or None
        top_ledgers = await _top_level_ledger_names(session, sw.workspace_id) or None

    try:
        parsed = parse_cells(body.report_type, body.cells, top_groups, top_ledgers)
    except WireParseError:
        raise ApiError(422, "unparseable_amount") from None
    except ValueError:
        raise ApiError(422, "invalid_report_type") from None

    t = TallyReportSnapshot.__table__
    values = dict(workspace_id=sw.workspace_id, report_type=body.report_type, from_date=from_date, as_on_date=as_on,
                  purpose=body.purpose, request_flags=body.request_flags, captured_at=body.captured_at,
                  counters=body.counters.model_dump(), cells=body.cells, rows=parsed.rows,
                  row_count=len(parsed.rows), synthetic_rows=parsed.synthetic, imbalance=parsed.imbalance)
    update_cols = {k: v for k, v in values.items() if k not in ("workspace_id", "report_type", "as_on_date")}

    stmt = pg_insert(t).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "report_type", "as_on_date"],
        set_={k: stmt.excluded[k] for k in update_cols},
        where=(t.c.captured_at < stmt.excluded.captured_at),
    ).returning(t.c.row_count, t.c.synthetic_rows, t.c.imbalance, literal_column("(xmax = 0)").label("inserted"))
    row = (await session.execute(stmt)).first()

    if row is not None:                                   # inserted, or replaced by a genuinely newer capture
        return {"stored": True, "replaced": not row.inserted, "row_count": row.row_count,
                "synthetic_rows": row.synthetic_rows,
                "imbalance": str(row.imbalance) if row.imbalance is not None else None,
                "warnings": parsed.warnings}

    # WHERE was not satisfied: an equal-or-older capture lost the race (or arrived after). Describe what IS
    # stored, never the rejected incoming body (fix round 1 clarification of the older-capture response shape).
    stored = (await session.execute(select(t.c.row_count, t.c.synthetic_rows, t.c.imbalance).where(
        t.c.workspace_id == sw.workspace_id, t.c.report_type == body.report_type, t.c.as_on_date == as_on
    ))).first()
    return {"stored": False, "replaced": False, "row_count": stored.row_count,
            "synthetic_rows": stored.synthetic_rows,
            "imbalance": str(stored.imbalance) if stored.imbalance is not None else None, "warnings": []}


__all__ = ["ParsedSnapshot", "parse_cells", "store"]
