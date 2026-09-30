"""``POST /api/sync/company`` (S1 spec §7.5, §8.3 bind rows, §9.3 one active device / take-over, D5, D7, D14
GUID-only, Q4, Q30). Never writes ``workspaces`` (D5) — only reads it, through plain ``text()`` (§6.2).

Outcome order is A6 (controller ruling): 404/410 -> ``company_bound_elsewhere`` -> the rest of §7.5's table in
the order listed.

F22 (controller ruling): a naive ``session.get(..., with_for_update=True)`` on an unbound workspace returns
``None`` for two concurrent racers — nothing to lock — so both would ``INSERT`` and one gets an
``IntegrityError`` (a 500). Instead every bind attempts ``INSERT ... ON CONFLICT (workspace_id) DO NOTHING``
with this request's own values (a no-op if a row already exists, whoever's values happen to land first if the
row was genuinely unbound), then re-``SELECT ... FOR UPDATE``s the row and applies the rest of §7.5 to whatever
is actually there. Postgres itself serializes two concurrent ``INSERT``s of the same key (the second blocks on
the unique index until the first's transaction ends), so this is safe under real concurrency, not just in the
single-writer case.

Fix round 1 (review — 3 Important issues):
  1. ``sync_workspaces.active_device_id`` is never cleared by any revocation writer (``DELETE
     /api/devices/{id}``, logout, refresh-reuse revoke, workspace-deleted revoke — all touch only
     ``agent_devices``). So "another active device" for §7.5 purposes must be resolved at READ time
     (``_effective_active_device``): missing, revoked, inactive, or pointing at a device now active on a
     DIFFERENT workspace all count as "no active device".
  2. A device active on W1 could bind W2 without W1's ``active_device_id`` ever being cleared, so a later
     take-over of W1 would revoke the device that is now actually syncing W2. ``_activate`` now (a) clears
     ``active_device_id`` on every OTHER workspace currently pointing at the device being activated, and (b)
     only revokes ``prev`` when ``prev.workspace_id == sw.workspace_id`` (i.e. ``prev`` is genuinely this
     workspace's device, not a stale pointer).
  3. Controller ruling: D7's take-over guard (``takeover`` + login ≤10 min) applies to ANY bind that would
     displace another LIVE active device of the workspace, whatever GUID is being bound — not just the
     same-GUID branch. The different-GUID branch now runs the same guard, ahead of the accepted-batch check.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.utils.device_tokens import mint_access
from backend.sync.clock import Clock, ist_date
from backend.config import Settings
from backend.sync.errors import ApiError
from backend.db.sync_models import AgentDevice, SyncFyCoverage, SyncWorkspace
from backend.sync.coverage import fy_rows_for_bind
from contract.parse import WireParseError, tally_date


class BindRequest(BaseModel):
    """§7.5 request body."""

    workspace_id: uuid.UUID
    company_guid: str = Field(..., min_length=1, max_length=255)
    company_name: str = Field(..., min_length=1, max_length=255)
    books_from: str = Field(..., min_length=1, max_length=50)
    base_currency_name: str | None = Field(None, max_length=50)
    takeover: bool = False


@dataclass(frozen=True)
class _WorkspaceRow:
    id: uuid.UUID
    user_id: uuid.UUID
    is_deleted: bool


async def _load_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> _WorkspaceRow | None:
    row = (
        await session.execute(
            text("SELECT id, user_id, is_deleted FROM workspaces WHERE id = :id"), {"id": workspace_id}
        )
    ).mappings().first()
    if row is None:
        return None
    return _WorkspaceRow(id=row["id"], user_id=row["user_id"], is_deleted=bool(row["is_deleted"]))


async def _bound_elsewhere(
    session: AsyncSession, user_id: uuid.UUID, company_guid: str, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """A6: this GUID already bound to a DIFFERENT (live) workspace of the same user."""
    row = (
        await session.execute(
            text(
                "SELECT sw.workspace_id FROM sync_workspaces sw JOIN workspaces w ON w.id = sw.workspace_id "
                "WHERE w.user_id = :u AND w.is_deleted = false AND sw.tally_company_guid = :g "
                "AND sw.workspace_id != :ws"
            ),
            {"u": user_id, "g": company_guid, "ws": workspace_id},
        )
    ).first()
    return row[0] if row is not None else None


async def _has_accepted_batch(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM sync_batches WHERE workspace_id = :w AND status = 'accepted' LIMIT 1"),
            {"w": workspace_id},
        )
    ).first()
    return row is not None


async def _has_coverage(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM sync_fy_coverage WHERE workspace_id = :w LIMIT 1"), {"w": workspace_id}
        )
    ).first()
    return row is not None


async def _effective_active_device(session: AsyncSession, sw: SyncWorkspace) -> AgentDevice | None:
    """Fix round 1 #1: §7.5's "another active device" means a device that is genuinely live for THIS
    workspace right now, not merely whatever ``sync_workspaces.active_device_id`` last pointed at — no
    revocation writer clears that pointer. Missing, revoked, inactive, or pointing at a device that has since
    moved to a different workspace (fix round 1 #2) all resolve to "no active device"."""
    if sw.active_device_id is None:
        return None
    ad = await session.get(AgentDevice, sw.active_device_id)
    if ad is None or not ad.is_active or ad.revoked_at is not None or ad.workspace_id != sw.workspace_id:
        return None
    return ad


async def _create_coverage(session: AsyncSession, sw: SyncWorkspace, clock: Clock) -> None:
    rows = fy_rows_for_bind(sw.books_from, ist_date(clock.now()), None)
    for r in rows:
        session.add(
            SyncFyCoverage(
                workspace_id=sw.workspace_id,
                fy_start=r.fy_start,
                fy_end=r.fy_end,
                state="pending",
                months_done=[],
                months_complete=0,
                months_total=r.months_total,
            )
        )
    await session.flush()


async def _rebind(session: AsyncSession, sw: SyncWorkspace, body: BindRequest, books_from: date, clock: Clock) -> None:
    """Different GUID, no batch ever accepted (Q4 "wrong company"): replace the binding and recreate coverage."""
    now = clock.now()
    await session.execute(delete(SyncFyCoverage).where(SyncFyCoverage.workspace_id == sw.workspace_id))
    sw.tally_company_guid = body.company_guid
    sw.tally_company_name = body.company_name
    sw.books_from = books_from
    sw.base_currency_name = body.base_currency_name
    sw.sync_state = "awaiting_first_connection"
    sw.bound_at = now
    sw.updated_at = now
    await session.flush()
    await _create_coverage(session, sw, clock)


async def _guard_takeover(
    session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: BindRequest, settings: Settings, clock: Clock
) -> None:
    """Fix round 1 #3 (controller ruling): D7's take-over guard applies to ANY bind that would displace another
    LIVE active device of the workspace, whatever GUID is being bound. No-op if there's no other live device,
    or if the live device is this one. Raises 409 ``takeover_required`` / 401 ``reauth_required`` otherwise."""
    active = await _effective_active_device(session, sw)
    if active is None or active.id == device.id:
        return
    if not body.takeover:
        raise ApiError(409, "takeover_required", active_device=_device_info(active))
    login_age = clock.now() - (device.last_login_at or clock.now())
    if login_age > timedelta(minutes=settings.TAKEOVER_LOGIN_MAX_AGE_MINUTES):
        raise ApiError(401, "reauth_required")


async def _activate(session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, clock: Clock) -> None:
    """§9.3: revoke the previous active device (if any other, and only if it's actually THIS workspace's
    device — fix round 1 #2), flush, then activate this one — the partial unique index on ``agent_devices
    (workspace_id) WHERE is_active`` would otherwise fire inside the transaction if both rows were
    ``is_active`` at once. Also detaches this device from any OTHER workspace it was previously active on
    (fix round 1 #2), so a later take-over there doesn't revoke a device that has since moved here."""
    now = clock.now()

    await session.execute(
        update(SyncWorkspace)
        .where(SyncWorkspace.active_device_id == device.id, SyncWorkspace.workspace_id != sw.workspace_id)
        .values(active_device_id=None, updated_at=now)
    )

    if sw.active_device_id is not None and sw.active_device_id != device.id:
        prev = await session.get(AgentDevice, sw.active_device_id)
        if prev is not None and prev.is_active and prev.workspace_id == sw.workspace_id:
            prev.is_active = False
            prev.revoked_at = now
            prev.revoke_reason = "taken_over"
            await session.flush()

    device.workspace_id = sw.workspace_id
    device.is_active = True
    sw.active_device_id = device.id
    sw.updated_at = now
    await session.flush()


def _device_info(ad: AgentDevice) -> dict:
    return {
        "device_name": ad.device_name,
        "last_seen_at": ad.last_seen_at.isoformat() if ad.last_seen_at else None,
    }


def _cursors(sw: SyncWorkspace) -> dict:
    return {"alt_vch_id": sw.cursor_alt_vch_id, "alt_mst_id": sw.cursor_alt_mst_id}


async def _coverage_json(session: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == workspace_id).order_by(SyncFyCoverage.fy_start)
        )
    ).scalars().all()
    return [
        {
            "fy_start": r.fy_start.isoformat(),
            "fy_end": r.fy_end.isoformat(),
            "state": r.state,
            "months_total": r.months_total,
            "months_complete": r.months_complete,
        }
        for r in rows
    ]


def _parse_books_from(text_value: str) -> date:
    try:
        return tally_date(text_value)
    except WireParseError as exc:
        raise ApiError(422, "invalid_books_from") from exc


async def _first_insert_attempt(session: AsyncSession, body: BindRequest, books_from: date, clock: Clock) -> None:
    """F22: ``INSERT ... ON CONFLICT (workspace_id) DO NOTHING`` — a no-op if the workspace is already bound
    (to this GUID or another one); otherwise this request's own values win the row if it gets there first.
    A separate, overridable function so DB tests can force two concurrent binds to race here deterministically
    (the same ``_TwoPartyBarrier`` technique as ``test_auth_api.py``'s refresh-race test)."""
    now = clock.now()
    stmt = (
        pg_insert(SyncWorkspace.__table__)
        .values(
            workspace_id=body.workspace_id,
            tally_company_guid=body.company_guid,
            tally_company_name=body.company_name,
            books_from=books_from,
            base_currency_name=body.base_currency_name,
            sync_state="awaiting_first_connection",
            bound_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id"])
    )
    await session.execute(stmt)


async def _lock_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> SyncWorkspace:
    """The row lock that serializes every other decision in ``bind()``. A separate function (like
    ``_first_insert_attempt``) so DB tests can force a deterministic race at this exact point too."""
    sw = await session.get(SyncWorkspace, workspace_id, with_for_update=True)
    assert sw is not None  # guaranteed by `_first_insert_attempt` having run first in this same transaction
    return sw


async def bind(
    session: AsyncSession, device: AgentDevice, body: BindRequest, settings: Settings, clock: Clock
) -> dict:
    ws = await _load_workspace(session, body.workspace_id)
    if ws is None or ws.user_id != device.user_id:
        raise ApiError(404, "workspace_not_found")
    if ws.is_deleted:
        raise ApiError(410, "workspace_deleted")

    elsewhere = await _bound_elsewhere(session, device.user_id, body.company_guid, body.workspace_id)
    if elsewhere is not None:
        raise ApiError(409, "company_bound_elsewhere", workspace_id=str(elsewhere))

    books_from = _parse_books_from(body.books_from)

    await _first_insert_attempt(session, body, books_from, clock)
    sw = await _lock_workspace(session, body.workspace_id)

    if sw.tally_company_guid != body.company_guid:
        # Fix round 1 #3 (controller ruling): D7's guard applies to ANY displacement, before the different-GUID
        # rules (no data -> re-bound, data exists -> 409) even get a chance to run.
        await _guard_takeover(session, sw, device, body, settings, clock)
        if await _has_accepted_batch(session, sw.workspace_id):
            raise ApiError(409, "workspace_bound_to_other_company")
        await _rebind(session, sw, body, books_from, clock)
        await _activate(session, sw, device, clock)
    else:
        has_coverage = await _has_coverage(session, sw.workspace_id)
        if not has_coverage:
            # Either genuinely fresh (our own insert above just created the row) or, in principle, a bound
            # row somehow missing coverage — either way there is nothing to conflict with yet.
            await _create_coverage(session, sw, clock)
            await _activate(session, sw, device, clock)
        else:
            active = await _effective_active_device(session, sw)
            if active is not None and active.id == device.id:
                pass  # no-op: this device is already the (genuinely live) active one (Part 1 §4 step 3)
            elif active is None:
                # Re-bind-when-empty (fix round 1 #1: resolved live, not from a stale pointer).
                await _activate(session, sw, device, clock)
            else:
                await _guard_takeover(session, sw, device, body, settings, clock)
                await _activate(session, sw, device, clock)

    await session.commit()

    access_token = mint_access(
        device.id, device.user_id, sw.workspace_id, secret=settings.DEVICE_TOKEN_SECRET,
        minutes=settings.DEVICE_ACCESS_MINUTES, now=clock.now(),
    )
    return {
        "bound": True,
        "sync_state": sw.sync_state,
        "cursors": _cursors(sw),
        "coverage": await _coverage_json(session, sw.workspace_id),
        "access_token": access_token,
    }
