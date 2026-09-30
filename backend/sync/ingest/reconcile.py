"""``POST /api/sync/{ws}/reconcile`` (S1 spec §7.11, D19, D28, LESSONS rule 30).

Voucher scope: every LIVE (``is_deleted = false``) voucher in ``[from, to]`` whose GUID the agent's own
``present`` list does not carry is soft-deleted -- the voucher row stays with ``is_deleted = true`` /
``deleted_at`` set, but its lines / inventory lines / bill allocations are HARD-deleted (D19, same "no Tally
identity" rule ``store.upsert_vouchers`` already applies on replace). The ledgers those lines touched are
returned as ``reread_ledgers`` (Part 1 §4 "after a gap": their mirrored balance is now stale).

Masters scope: every LIVE master of ``master_type`` absent from ``present`` is soft-deleted UNLESS a live
voucher line / bill allocation / inventory line still references it -- Tally itself refuses that delete
(LESSONS rule 30, "Cannot be deleted!"); the whole reconcile call is refused ``409 master_in_use`` rather than
silently skipping just that one row, so the agent finds out immediately.

D28 guard: soft-deleting more than 20% AND more than 50 rows of the scope is refused ``409
reconcile_too_large`` unless resent with ``confirm_large: true`` after the agent re-reads its own list. Nothing
is mutated before the guard is evaluated, so a refused call leaves the workspace untouched.

Soft-delete is reversible: a re-sent voucher is re-stored with ``is_deleted = false`` (``store.upsert_vouchers``
/ ``_voucher_row`` always writes ``is_deleted=False, deleted_at=None`` -- unconditionally, whether the row is
new or replaced), so a wrongly-reconciled voucher self-heals the moment the agent's next batch carries it again.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.sync.clock import Clock
from backend.sync.errors import ApiError
from backend.sync.ingest.store import MASTER_MODELS
from backend.db.sync_models import (
    AgentDevice,
    SyncWorkspace,
    TallyBillAllocation,
    TallyVoucher,
    TallyVoucherInventoryLine,
    TallyVoucherLedgerLine,
)
from backend.sync.runs import require_open_run
from contract.models import ReconcileRequest

_IN_USE_LINE_MODEL = {"ledger": (TallyVoucherLedgerLine, "ledger_guid"), "stock_item": (TallyVoucherInventoryLine,
                      "stock_item_guid")}


def _check_guard(would_delete: int, scope_total: int, confirm_large: bool) -> None:
    """D28: refused only when BOTH thresholds are crossed -- more than 50 rows AND more than 20% of the scope."""
    if confirm_large or would_delete <= 50:
        return
    if would_delete * 5 > scope_total:                          # would_delete / scope_total > 20%
        raise ApiError(409, "reconcile_too_large", would_delete=would_delete, scope_total=scope_total)


def _refetch(present_by_guid: dict[str, int], stored: dict[str, int]) -> list[str]:
    """§7.11: a `present` GUID unknown to us, or whose reported `alter_id` is higher than our stored one, means
    Tally has an edit we haven't ingested yet."""
    return sorted(g for g, alter in present_by_guid.items() if g not in stored or alter > stored[g])


async def _stored_alters(session: AsyncSession, t, ws_id: uuid.UUID, guids: list[str]) -> dict[str, int]:
    """Fix round 1, I2: a stored-but-``is_deleted`` row must NOT count as "known" -- if it did, a `present`
    GUID whose only stored copy was (wrongly, or by a stale reconcile) soft-deleted would never be refetched,
    and the S1-R8 reversibility guarantee (a re-sent voucher/master comes back live) would only fire if the
    agent happened to resend it anyway. Treating it as unknown puts it in `refetch` on the very next reconcile
    that lists it, regardless of its alter_id."""
    if not guids:
        return {}
    rows = (await session.execute(select(t.c.guid, t.c.alter_id).where(
        t.c.workspace_id == ws_id, t.c.guid.in_(guids), t.c.is_deleted.is_(False)))).all()
    return {g: a for g, a in rows}


async def _reconcile_vouchers(session: AsyncSession, sw: SyncWorkspace, scope, present_by_guid: dict[str, int],
                              confirm_large: bool, clock: Clock) -> dict:
    if scope.from_ is None or scope.to is None:
        raise ApiError(422, "invalid_scope", detail="from/to required for a vouchers scope")
    ws_id = sw.workspace_id
    t = TallyVoucher.__table__
    scope_rows = (await session.execute(select(t.c.id, t.c.guid, t.c.alter_id).where(
        t.c.workspace_id == ws_id, t.c.is_deleted.is_(False), t.c.date >= scope.from_, t.c.date <= scope.to
    ))).all()
    scope_total = len(scope_rows)
    absent = [r for r in scope_rows if r.guid not in present_by_guid]
    _check_guard(len(absent), scope_total, confirm_large)

    reread: list[tuple[str, str]] = []
    if absent:
        ids = [r.id for r in absent]
        touched = (await session.execute(
            select(TallyVoucherLedgerLine.ledger_guid, TallyVoucherLedgerLine.ledger_name)
            .where(TallyVoucherLedgerLine.voucher_id.in_(ids)).distinct()
        )).all()
        reread = sorted({(g, n) for g, n in touched})

        now = clock.now()
        await session.execute(update(t).where(t.c.id.in_(ids)).values(is_deleted=True, deleted_at=now))
        for model in (TallyVoucherLedgerLine, TallyVoucherInventoryLine, TallyBillAllocation):  # D19: hard-delete
            await session.execute(delete(model.__table__).where(model.__table__.c.voucher_id.in_(ids)))

    stored = await _stored_alters(session, t, ws_id, list(present_by_guid))
    return {"soft_deleted": len(absent), "refetch": _refetch(present_by_guid, stored),
            "reread_ledgers": [{"guid": g, "name": n} for g, n in reread]}


async def _in_use_guids(session: AsyncSession, ws_id: uuid.UUID, master_type: str, guids: list[str]) -> set[str]:
    entry = _IN_USE_LINE_MODEL.get(master_type)
    if entry is None or not guids:
        return set()
    model, col = entry
    column = getattr(model, col)
    rows = (await session.execute(
        select(column).where(model.workspace_id == ws_id, column.in_(guids)).distinct()
    )).scalars().all()
    in_use = set(rows)
    if master_type == "ledger" and guids:
        bill_rows = (await session.execute(
            select(TallyBillAllocation.ledger_guid).where(
                TallyBillAllocation.workspace_id == ws_id, TallyBillAllocation.ledger_guid.in_(guids)
            ).distinct()
        )).scalars().all()
        in_use |= set(bill_rows)
    return in_use


async def _reconcile_masters(session: AsyncSession, sw: SyncWorkspace, scope, present_by_guid: dict[str, int],
                             confirm_large: bool, clock: Clock) -> dict:
    model = MASTER_MODELS.get(scope.master_type or "")
    if model is None:
        raise ApiError(422, "invalid_scope", detail="master_type")
    ws_id = sw.workspace_id
    t = model.__table__
    scope_rows = (await session.execute(select(t.c.id, t.c.guid, t.c.alter_id, t.c.name).where(
        t.c.workspace_id == ws_id, t.c.is_deleted.is_(False)
    ))).all()
    scope_total = len(scope_rows)
    absent = [r for r in scope_rows if r.guid not in present_by_guid]
    _check_guard(len(absent), scope_total, confirm_large)

    if absent:
        in_use = await _in_use_guids(session, ws_id, scope.master_type, [r.guid for r in absent])
        offender = next((r for r in absent if r.guid in in_use), None)
        if offender is not None:                                # LESSONS rule 30: Tally itself refuses this delete
            raise ApiError(409, "master_in_use", guid=offender.guid, name=offender.name)
        ids = [r.id for r in absent]
        await session.execute(update(t).where(t.c.id.in_(ids)).values(is_deleted=True))

    stored = await _stored_alters(session, t, ws_id, list(present_by_guid))
    return {"soft_deleted": len(absent), "refetch": _refetch(present_by_guid, stored), "reread_ledgers": []}


def _parse_run_id(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id)
    except ValueError:
        raise ApiError(404, "run_not_found") from None


async def reconcile(session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: ReconcileRequest,
                    clock: Clock) -> dict:
    run_id = _parse_run_id(body.run_id)
    await require_open_run(session, sw, device, run_id)          # 403 wrong_workspace / 409 run_closed

    # Fix round 1, controller ruling I3: `present_count` exists to catch a truncated upload (a list cut short by
    # a transport error, a bug, a size limit) BEFORE it silently soft-deletes real rows -- D28's guard alone
    # only catches a truncation large enough to cross its thresholds. Refused before any read of the scope, so
    # nothing is ever soft-deleted on a mismatch.
    if len(body.present) != body.present_count:
        raise ApiError(422, "reconcile_list_incomplete", present=len(body.present), present_count=body.present_count)

    present_by_guid = {p.guid: p.alter_id for p in body.present}
    if body.scope.kind == "vouchers":
        result = await _reconcile_vouchers(session, sw, body.scope, present_by_guid, body.confirm_large, clock)
    elif body.scope.kind == "masters":
        result = await _reconcile_masters(session, sw, body.scope, present_by_guid, body.confirm_large, clock)
    else:
        raise ApiError(422, "invalid_scope", detail="kind")

    sw.updated_at = clock.now()
    await session.flush()
    return result


__all__ = ["reconcile"]
