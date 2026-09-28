"""Ingest storage (S1 spec §4.3, §4.5, §4.9, §12 steps 9-12, D3, D18, D19, F14).

Masters go through the ORM one row at a time (a batch carries few of them); vouchers and their lines go through
Core in bulk -- one statement per table per batch -- to meet §12's performance budget. Nothing here commits: the
pipeline owns the batch's single transaction.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import bindparam, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import fy_start_of
from v2.cloud.ingest.derive import base_type_walk, is_base_currency, nature_walk
from v2.cloud.ingest.parsed import PBalance, PMaster, PVoucher
from v2.cloud.models import (
    SyncFyCoverage, TallyBillAllocation, TallyCurrency, TallyGroup, TallyLedger, TallyStockGroup, TallyStockItem,
    TallyUnit, TallyVoucher, TallyVoucherInventoryLine, TallyVoucherLedgerLine, TallyVoucherType,
)
from v2.contract.parse import Amount, name as parse_name
from v2.contract.tally_rules import PRIMARY_PARENT

MASTER_MODELS = {
    "currency": TallyCurrency, "group": TallyGroup, "voucher_type": TallyVoucherType, "ledger": TallyLedger,
    "stock_group": TallyStockGroup, "unit": TallyUnit, "stock_item": TallyStockItem,
}
BILL_TYPES = {"New Ref": "new_ref", "Agst Ref": "agst_ref", "Advance": "advance", "On Account": "on_account"}

UpsertResult = Literal["inserted", "updated", "skipped_older"]


@dataclass(frozen=True)
class ResolvedVoucher:
    """§12 step 8 output for one voucher: every name it carries, resolved to a live master GUID."""
    voucher_type_guid: str
    party_ledger_guid: str | None
    line_guids: list[str]
    inventory_guids: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int(text) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _dec(text) -> Decimal | None:
    try:
        return Decimal(str(text).strip().replace(",", "")) if text is not None and str(text).strip() else None
    except InvalidOperation:
        return None


def _parent_name(p: PMaster) -> str:
    return p.parent if p.parent is not None else ""


def master_columns(p: PMaster, derived: dict) -> dict:
    """The kind-specific columns for one master (§4.3). `derived` carries the resolved GUIDs and derivations the
    pipeline computed (`parent_guid`, `group_guid`, `base_unit_guid`, `is_forex`)."""
    f = p.fields
    cols: dict = {"name": p.name, "alter_id": p.alter_id, "raw": p.raw, "is_deleted": False}
    if p.kind == "currency":
        cols.update(mailing_name=f.get("mailingname"), expanded_symbol=f.get("expandedsymbol"),
                    decimal_places=_int(f.get("decimalplaces")),
                    is_base=is_base_currency(f.get("expandedsymbol") or ""))
    elif p.kind == "group":
        cols.update(parent_name=_parent_name(p), parent_guid=derived.get("parent_guid"),
                    is_revenue=f.get("isrevenue"), affects_gross_profit=f.get("affectsgrossprofit"),
                    is_deemed_positive=f.get("isdeemedpositive"), reserved_name=f.get("reservedname") or None)
    elif p.kind == "voucher_type":
        cols.update(parent_name=_parent_name(p), parent_guid=derived.get("parent_guid"),
                    reserved_name=f.get("reservedname") or None)
    elif p.kind == "ledger":
        cols.update(parent_name=_parent_name(p), group_guid=derived.get("group_guid"),
                    currency_name=parse_name(f["currencyname"]) if f.get("currencyname") else None,
                    is_forex=bool(derived.get("is_forex")), is_bill_wise=f.get("isbillwiseon"),
                    tax_type=f.get("taxtype") or None, gst_duty_head=f.get("gstdutyhead") or None)
    elif p.kind == "stock_group":
        cols.update(parent_name=_parent_name(p), parent_guid=derived.get("parent_guid"))
    elif p.kind == "unit":
        cols.update(is_simple=f.get("issimpleunit"), base_units=f.get("baseunits") or None,
                    additional_units=f.get("additionalunits") or None, conversion=_dec(f.get("conversion")))
    elif p.kind == "stock_item":
        base_unit = parse_name(f.get("baseunits")) or None
        cols.update(parent_name=_parent_name(p), parent_guid=derived.get("parent_guid"), base_unit_name=base_unit,
                    base_unit_guid=derived.get("base_unit_guid"))
    return cols


async def _master_row(session: AsyncSession, model, ws_id: uuid.UUID, guid: str):
    return (await session.execute(select(model).where(model.workspace_id == ws_id, model.guid == guid))).scalar_one_or_none()


async def upsert_master(session: AsyncSession, ws_id: uuid.UUID, p: PMaster, derived: dict) -> UpsertResult:
    """§12 step 9: `incoming.alter_id >= stored.alter_id` replaces (a rename keeps the GUID, so lines still join),
    lower is `skipped_older`. Balances are NOT written here -- `apply_balance` orders them by `captured_at`."""
    model = MASTER_MODELS[p.kind]
    cols = master_columns(p, derived)
    row = await _master_row(session, model, ws_id, p.guid)
    if row is None:
        session.add(model(workspace_id=ws_id, guid=p.guid, **cols))
        await session.flush()
        return "inserted"
    if p.alter_id < row.alter_id:
        return "skipped_older"
    if p.kind == "ledger" and row.is_forex and (row.closing_fx_amount is not None or row.opening_fx_amount is not None):
        cols["is_forex"] = True                          # D30: "ever carried an expression amount" is sticky
    for key, value in cols.items():
        setattr(row, key, value)
    row.updated_at = _now()
    await session.flush()
    return "updated"


def _captured_at(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _money_cols(prefix: str, a: Amount | None) -> dict:
    """D3 for one ledger balance: stated INR base -> money column; face/rate in their own columns; `""` -> None
    (F14: never 0); an expression without a stated base -> money NULL (the caller marks `needs_tb`)."""
    if a is None:
        return {f"{prefix}_balance": None, f"{prefix}_fx_amount": None, f"{prefix}_fx_rate": None}
    return {f"{prefix}_balance": a.inr if a.stated else None, f"{prefix}_fx_amount": a.fx_amount,
            f"{prefix}_fx_rate": a.fx_rate}


def _balance_view(p: PBalance | PMaster) -> dict | None:
    """Normalise a balance-carrying object to {captured_at, closing?, opening?, qty?, qty_text?, value?} with a
    key present only when the object carried it. None if it carries no balance or no `captured_at`."""
    if isinstance(p, PBalance):
        out: dict = {"captured_at": p.captured_at, "texts": p.raw}
        if p.kind == "ledger_balance":
            out["closing"] = p.closing
            if p.raw.get("openingbalance"):
                out["opening"] = p.opening
        else:
            out["value"] = p.closing
            if "closingbalance" in p.raw:
                out["qty"], out["qty_text"] = p.qty, p.qty_text
        return out
    if "captured_at" not in p.fields:
        return None
    out = {"captured_at": _captured_at(p.fields["captured_at"]), "texts": p.raw}
    if p.kind == "ledger":
        if "closingbalance" in p.fields:
            out["closing"] = p.fields["closingbalance"]
        if "openingbalance" in p.fields:
            out["opening"] = p.fields["openingbalance"]
        return out if ("closing" in out or "opening" in out) else None
    if p.kind == "stock_item":
        if "closingvalue" in p.fields:
            out["value"] = p.fields["closingvalue"]
        if "closingbalance" in p.fields:
            out["qty"], out["qty_text"] = p.fields["closingbalance"], p.raw.get("closingbalance", "")
        return out if ("value" in out or "qty" in out) else None
    return None


async def apply_balance(session: AsyncSession, ws_id: uuid.UUID, p: PBalance | PMaster) -> Literal["applied", "stale"]:
    """§12 step 11: a balance applies only if its `captured_at` is newer than the stored `balance_captured_at`
    ("latest capture wins"); otherwise it is `stale` and nothing changes."""
    view = _balance_view(p)
    if view is None:
        raise ValueError("object carries no balance")
    is_ledger = p.kind in ("ledger", "ledger_balance")
    model = TallyLedger if is_ledger else TallyStockItem
    row = await _master_row(session, model, ws_id, p.guid)
    if row is None:
        raise LookupError("balance for an unknown master")     # resolution (§12 step 8) guarantees it exists
    captured = _captured_at(view["captured_at"])
    if row.balance_captured_at is not None and captured <= row.balance_captured_at:
        return "stale"
    row.balance_captured_at = captured
    if is_ledger:
        texts = dict(row.balance_text or {})
        expression = False
        if "closing" in view:
            closing: Amount | None = view["closing"]
            for k, v in _money_cols("closing", closing).items():
                setattr(row, k, v)
            texts["closing"] = view["texts"].get("closingbalance", "")
            row.balance_source = "needs_tb" if closing is not None and not closing.stated else "tally"
            expression |= closing is not None and closing.fx_amount is not None
        if "opening" in view:
            opening: Amount | None = view["opening"]
            for k, v in _money_cols("opening", opening).items():
                setattr(row, k, v)
            texts["opening"] = view["texts"].get("openingbalance", "")
            expression |= opening is not None and opening.fx_amount is not None
        fx_currency = next((a.fx_currency for a in (view.get("closing"), view.get("opening"))
                            if a is not None and a.fx_currency), None)
        if fx_currency is not None:
            row.fx_currency = fx_currency
        if expression:
            row.is_forex = True                           # D30: carried an expression amount
        row.balance_text = texts
    else:
        if "value" in view:
            value: Amount | None = view["value"]
            row.closing_value = value.inr if value is not None and value.stated else None
        if "qty" in view:
            row.closing_qty, row.closing_qty_text = view["qty"], view["qty_text"]
    row.updated_at = _now()
    await session.flush()
    return "applied"


# --- §12 step 10: derivations over the workspace's live groups / voucher types ----------------------------------


async def rederive_groups(session: AsyncSession, ws_id: uuid.UUID) -> list[tuple[str, str]]:
    """Re-derive `nature` / `primary_group` for every live group (a superset of "the moved master and its
    descendants", §12 step 10). The walk goes by GUID (`parent_guid`) mapped to current names, so a renamed
    parent never strands its children. Returns `(guid, warning_code)` for `is_revenue` disagreements."""
    groups = (await session.execute(select(TallyGroup).where(TallyGroup.workspace_id == ws_id,
                                                              TallyGroup.is_deleted.is_(False)))).scalars().all()
    name_of = {g.guid: g.name for g in groups}
    parents = {g.name: (name_of.get(g.parent_guid, g.parent_name) if g.parent_guid else PRIMARY_PARENT)
               for g in groups}
    warnings: list[tuple[str, str]] = []
    for g in groups:
        d = nature_walk(g.name, parents)
        warning = d.warning
        if d.nature is not None and g.is_revenue is not None and g.is_revenue != (d.nature in ("income", "expenses")):
            warning = "is_revenue_disagrees"
            warnings.append((g.guid, warning))
        if (g.nature, g.primary_group, g.derivation_warning) != (d.nature, d.primary_group, warning):
            g.nature, g.primary_group, g.derivation_warning = d.nature, d.primary_group, warning
    await session.flush()
    return warnings


async def rederive_voucher_types(session: AsyncSession, ws_id: uuid.UUID) -> list[tuple[str, str]]:
    """Re-derive `base_type` for every live voucher type (§4.4). Returns `(guid, "base_type_unresolved")` for
    types whose chain doesn't reach a reserved type."""
    vts = (await session.execute(select(TallyVoucherType).where(TallyVoucherType.workspace_id == ws_id,
                                                                  TallyVoucherType.is_deleted.is_(False)))).scalars().all()
    name_of = {v.guid: v.name for v in vts}
    parents = {v.name: name_of.get(v.parent_guid, v.parent_name) if v.parent_guid else v.parent_name for v in vts}
    reserved = {v.name: v.reserved_name for v in vts}
    warnings: list[tuple[str, str]] = []
    for v in vts:
        base = base_type_walk(v.name, parents, reserved)
        if base is None:
            warnings.append((v.guid, "base_type_unresolved"))
        if v.base_type != base:
            v.base_type = base
    await session.flush()
    return warnings


async def voucher_type_base_types(session: AsyncSession, ws_id: uuid.UUID, guids: set[str]) -> dict[str, str | None]:
    if not guids:
        return {}
    rows = (await session.execute(select(TallyVoucherType.guid, TallyVoucherType.base_type).where(
        TallyVoucherType.workspace_id == ws_id, TallyVoucherType.guid.in_(guids)))).all()
    return {g: b for g, b in rows}


# --- §4.9 raw window -------------------------------------------------------------------------------------------


async def raw_window_fys(session: AsyncSession, ws_id: uuid.UUID, today_ist: date) -> set[date]:
    """The newest two FY starts of the workspace's coverage (current + previous FY). With no coverage rows yet,
    the current and previous FY by `today_ist`."""
    rows = (await session.execute(select(SyncFyCoverage.fy_start).where(SyncFyCoverage.workspace_id == ws_id)
                                  .order_by(SyncFyCoverage.fy_start.desc()).limit(2))).scalars().all()
    if rows:
        return set(rows)
    current = fy_start_of(today_ist)
    return {current, date(current.year - 1, 4, 1)}


# --- §12 step 12: vouchers ----------------------------------------------------------------------------------------


def _fx(a: Amount) -> dict:
    return {"fx_currency": a.fx_currency, "fx_amount": a.fx_amount, "fx_rate": a.fx_rate}


def _child_rows(ws_id: uuid.UUID, voucher_id: uuid.UUID, v: PVoucher, r: ResolvedVoucher) -> tuple[list, list, list]:
    countable = not v.is_cancelled and not v.is_optional
    lines, inventory, bills = [], [], []
    for line, guid in zip(v.lines, r.line_guids):
        lines.append({"voucher_id": voucher_id, "workspace_id": ws_id, "line_no": line.line_no,
                      "ledger_name": line.ledger_name, "ledger_guid": guid, "amount": line.amount.inr,
                      "is_deemed_positive": line.is_deemed_positive, **_fx(line.amount), "voucher_date": v.date,
                      "countable": countable})
        for bill in line.bills:
            bills.append({"voucher_id": voucher_id, "workspace_id": ws_id, "ledger_line_no": line.line_no,
                          "ledger_guid": guid, "bill_name": bill.name,
                          "bill_type": BILL_TYPES.get(bill.bill_type or "", (bill.bill_type or "").strip().lower()
                                                      .replace(" ", "_")),
                          "amount": bill.amount.inr, **_fx(bill.amount), "credit_period_text": bill.credit_text or None,
                          "credit_period_days": bill.credit_days, "bill_date": bill.bill_date,
                          "voucher_date": v.date})
    for inv, guid in zip(v.inventory, r.inventory_guids):
        deemed = inv.is_deemed_positive if inv.is_deemed_positive is not None else inv.amount.inr < 0
        inventory.append({"voucher_id": voucher_id, "workspace_id": ws_id, "line_no": inv.line_no,
                          "stock_item_name": inv.stock_item_name, "stock_item_guid": guid,
                          "actual_qty": inv.actual_qty, "billed_qty": inv.billed_qty, "qty_text": inv.qty_text,
                          "rate": inv.rate, "rate_text": inv.rate_text, "amount": inv.amount.inr, **_fx(inv.amount),
                          "is_deemed_positive": deemed, "voucher_date": v.date})
    return lines, inventory, bills


def _voucher_row(ws_id: uuid.UUID, v: PVoucher, r: ResolvedVoucher, base_type: str | None, run_id: uuid.UUID,
                 keep_raw: bool) -> dict:
    return {"workspace_id": ws_id, "guid": v.guid, "master_id": int(v.master_id.strip()), "alter_id": v.alter_id,
            "date": v.date, "effective_date": v.effective_date, "voucher_type_name": v.voucher_type_name,
            "voucher_type_guid": r.voucher_type_guid, "base_type": base_type, "voucher_number": v.voucher_number,
            "reference": v.reference, "party_ledger_name": v.party_ledger_name,
            "party_ledger_guid": r.party_ledger_guid, "narration": v.narration, "is_cancelled": v.is_cancelled,
            "is_optional": v.is_optional, "is_post_dated": v.is_post_dated, "is_invoice": v.is_invoice,
            "has_forex": v.has_forex, "is_deleted": False, "deleted_at": None,
            "raw": v.raw if keep_raw else None, "run_id": run_id}


async def upsert_vouchers(session: AsyncSession, ws_id: uuid.UUID,
                          items: list[tuple[PVoucher, ResolvedVoucher, bool]], run_id: uuid.UUID,
                          base_types: dict[str, str | None]) -> list[UpsertResult]:
    """Bulk §12 step 12 for a batch's vouchers (results in `items` order). One SELECT for the stored alter_ids,
    one bulk INSERT / executemany UPDATE for the voucher rows, one DELETE per child table for the replaced
    vouchers, one bulk INSERT per child table. Two copies of one GUID in the same batch: the highest alter_id
    wins, the other is `skipped_older`."""
    results: list[UpsertResult | None] = [None] * len(items)
    winner: dict[str, int] = {}
    for i, (v, _, _) in enumerate(items):
        j = winner.get(v.guid)
        if j is None or v.alter_id >= items[j][0].alter_id:
            if j is not None:
                results[j] = "skipped_older"
            winner[v.guid] = i
        else:
            results[i] = "skipped_older"

    t = TallyVoucher.__table__
    stored = {}
    if winner:
        stored = {g: (vid, alter) for g, vid, alter in (await session.execute(
            select(t.c.guid, t.c.id, t.c.alter_id).where(t.c.workspace_id == ws_id, t.c.guid.in_(list(winner)))
        )).all()}

    inserts, updates, children = [], [], ([], [], [])
    replaced_ids: list[uuid.UUID] = []
    now = _now()
    for guid, i in winner.items():
        v, r, keep_raw = items[i]
        row = _voucher_row(ws_id, v, r, base_types.get(r.voucher_type_guid), run_id, keep_raw)
        if guid in stored:
            vid, stored_alter = stored[guid]
            if v.alter_id < stored_alter:
                results[i] = "skipped_older"
                continue
            updates.append({**row, "_id": vid, "updated_at": now})
            replaced_ids.append(vid)
            results[i] = "updated"
        else:
            vid = uuid.uuid4()
            inserts.append({**row, "id": vid, "first_seen_at": now, "updated_at": now})
            results[i] = "inserted"
        for acc, rows in zip(children, _child_rows(ws_id, vid, v, r)):
            acc.extend(rows)

    conn = await session.connection()
    if inserts:
        await conn.execute(t.insert(), inserts)
    if updates:
        cols = [k for k in updates[0] if k != "_id"]
        await conn.execute(update(t).where(t.c.id == bindparam("_id")).values({c: bindparam(c) for c in cols}),
                           updates)
    if replaced_ids:                                   # lines have no Tally identity: delete + re-insert (§4.5)
        for model in (TallyVoucherLedgerLine, TallyVoucherInventoryLine, TallyBillAllocation):
            await conn.execute(delete(model.__table__).where(model.__table__.c.voucher_id.in_(replaced_ids)))
    for model, rows in zip((TallyVoucherLedgerLine, TallyVoucherInventoryLine, TallyBillAllocation), children):
        if rows:
            await conn.execute(model.__table__.insert(), rows)
    return results  # type: ignore[return-value]  -- every slot is filled above


async def upsert_voucher(session: AsyncSession, ws_id: uuid.UUID, v: PVoucher, resolved: ResolvedVoucher,
                         run_id: uuid.UUID, keep_raw: bool) -> UpsertResult:
    """Single-voucher form of `upsert_vouchers` (the brief's interface)."""
    base_types = await voucher_type_base_types(session, ws_id, {resolved.voucher_type_guid})
    return (await upsert_vouchers(session, ws_id, [(v, resolved, keep_raw)], run_id, base_types))[0]
