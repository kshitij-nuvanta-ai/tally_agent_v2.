"""``POST /api/sync/{ws}/snapshots`` (S1 spec §4.6, §7.12, D8, D25): parse a Tally report's verbatim cells into
rows + a TB imbalance, then upsert on ``(workspace_id, report_type, as_on_date)`` — a newer ``captured_at`` wins.

TB imbalance (controller ruling F2, superseding A18's formula which omitted the top-level ``Profit & Loss A/c``
ledger row and gave a false non-zero on every multi-FY company): Σ of

  * first-occurrence **primary-group** rows (the TB is EXPLODEFLAG -- sub-group rows repeat their parent, and
    ``Opening Stock`` is nested inside the stock-bearing primary group's own row, never added separately --
    LESSONS rule 19);
  * the top-level LEDGER row directly under Primary, ``Profit & Loss A/c`` (``PL_ACCOUNT_LEDGER``), if present;
  * the top-level synthetic ``Unadjusted Forex Gain/Loss`` row (LESSONS rule 29c), if present -- 0 otherwise.

"Primary group" here means a row named one of the 15 reserved primary groups (``tally_rules.PRIMARY_NATURE``);
first occurrence only, because a primary group with no sub-groups of its own repeats itself verbatim under
EXPLODEFLAG (e.g. ``Capital Account`` appears twice, byte-identical). ``trial_balance_ledgerwise`` has no group
structure to net against -- its imbalance is simply Σ of every row.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_start_of
from v2.cloud.errors import ApiError
from v2.cloud.models import SyncWorkspace, TallyReportSnapshot
from v2.contract.parse import WireParseError
from v2.contract.parse import amount as parse_amount
from v2.contract.parse import tally_date
from v2.contract.models import SnapshotRequest
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


@dataclass(frozen=True)
class ParsedSnapshot:
    rows: list[dict]
    synthetic: list[str]
    imbalance: Decimal | None


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


def parse_cells(report_type: str, cells: list[dict]) -> ParsedSnapshot:
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
    if report_type == "trial_balance":
        seen: set[str] = set()
        total = Decimal("0")
        for r in rows:
            if r["name"] in PRIMARY_NATURE and r["name"] not in seen:
                seen.add(r["name"])
                if r["amount"] is not None:
                    total += Decimal(r["amount"])
        for special in (PL_ACCOUNT_LEDGER, UNADJUSTED_FOREX_ROW):
            row = next((r for r in rows if r["name"] == special), None)
            if row is not None and row["amount"] is not None:
                total += Decimal(row["amount"])
        imbalance = total
    elif report_type == "trial_balance_ledgerwise":
        imbalance = sum((Decimal(r["amount"]) for r in rows if r["amount"] is not None), Decimal("0"))

    return ParsedSnapshot(rows=rows, synthetic=synthetic, imbalance=imbalance)


async def store(session: AsyncSession, sw: SyncWorkspace, body: SnapshotRequest, clock: Clock) -> dict:
    """§7.12: D8 period convention (``from_date`` = the FY start containing ``as_on_date``), ``as_on_date >=
    books_from``, every amount cell parses (D3) -- else 422. Upsert on the key; a newer ``captured_at`` wins; an
    older one is a no-op (``stored: false``, the existing row's own values echoed back)."""
    try:
        as_on = tally_date(body.as_on_date)
        from_date = tally_date(body.from_date)
    except WireParseError:
        raise ApiError(422, "bad_period") from None
    if from_date != fy_start_of(as_on) or as_on < sw.books_from:
        raise ApiError(422, "bad_period")

    try:
        parsed = parse_cells(body.report_type, body.cells)
    except WireParseError:
        raise ApiError(422, "unparseable_amount") from None
    except ValueError:
        raise ApiError(422, "invalid_report_type") from None

    t = TallyReportSnapshot.__table__
    existing = (await session.execute(select(t.c.captured_at).where(
        t.c.workspace_id == sw.workspace_id, t.c.report_type == body.report_type, t.c.as_on_date == as_on
    ))).first()

    imbalance_str = str(parsed.imbalance) if parsed.imbalance is not None else None
    if existing is not None and body.captured_at <= existing.captured_at:
        return {"stored": False, "replaced": False, "row_count": len(parsed.rows),
                "synthetic_rows": parsed.synthetic, "imbalance": imbalance_str}

    values = dict(workspace_id=sw.workspace_id, report_type=body.report_type, from_date=from_date, as_on_date=as_on,
                  purpose=body.purpose, request_flags=body.request_flags, captured_at=body.captured_at,
                  counters=body.counters.model_dump(), cells=body.cells, rows=parsed.rows,
                  row_count=len(parsed.rows), synthetic_rows=parsed.synthetic, imbalance=parsed.imbalance)
    stmt = pg_insert(t).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "report_type", "as_on_date"],
        set_={k: stmt.excluded[k] for k in values if k not in ("workspace_id", "report_type", "as_on_date")})
    await session.execute(stmt)

    return {"stored": True, "replaced": existing is not None, "row_count": len(parsed.rows),
            "synthetic_rows": parsed.synthetic, "imbalance": imbalance_str}


__all__ = ["ParsedSnapshot", "parse_cells", "store"]
