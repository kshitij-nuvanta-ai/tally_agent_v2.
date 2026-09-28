"""Device-token sync routes (S1 spec §7.5-§7.15). Task 5 landed ``POST /api/sync/company``; task 6 adds the
``{ws}``-scoped heartbeat/state/relink routes. Later tasks add runs, batches, coverage, reconcile, snapshots,
parity.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.api.dependencies import any_device, active_device
from v2.cloud.auth.passwords import verify_password
from v2.cloud.db import session_dep
from v2.cloud.errors import ApiError
from v2.cloud.models import AgentDevice
from v2.cloud.sync import state
from v2.cloud.sync.binding import BindRequest, bind

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("/company")
async def bind_company(
    body: BindRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    device: AgentDevice = Depends(any_device),
) -> dict:
    settings = request.app.state.settings
    clock = request.app.state.clock
    return await bind(session, device, body, settings, clock)


# --- §7.6 heartbeat ------------------------------------------------------------------------------------------


class SeenCompany(BaseModel):
    guid: str | None = None
    name: str | None = None


class Counters(BaseModel):
    alt_vch_id: int | None = None
    alt_mst_id: int | None = None


class HeartbeatRequest(BaseModel):
    agent_version: str | None = Field(None, max_length=50)
    tally_version: str | None = Field(None, max_length=100)
    tally_status: str = Field(..., min_length=1, max_length=50)
    seen_company: SeenCompany | None = None
    counters: Counters | None = None
    last_error_code: str | None = Field(None, max_length=100)
    breaker: str | None = Field(None, max_length=20)
    outbox_depth: int | None = None
    pc_clock: str = Field(..., min_length=1, max_length=50)
    acked_commands: list[str] = Field(default_factory=list)


@router.post("/{ws}/heartbeat")
async def heartbeat(
    ws: uuid.UUID,
    body: HeartbeatRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    settings = request.app.state.settings
    clock = request.app.state.clock
    return await state.heartbeat(session, sw, device, body, settings, clock)


# --- §7.7 state ------------------------------------------------------------------------------------------


@router.get("/{ws}/state")
async def get_state(
    ws: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    return await state.get_state(session, sw)


# --- §7.15 re-link (device path) --------------------------------------------------------------------------


class RelinkRequest(BaseModel):
    new_company_guid: str = Field(..., min_length=1, max_length=255)
    company_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


@router.post("/{ws}/relink")
async def relink(
    ws: uuid.UUID,
    body: RelinkRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock

    if not sw.relink_prompt or sw.relink_prompt.get("guid") != body.new_company_guid:
        raise ApiError(409, "relink_not_prompted")

    row = (
        await session.execute(text("SELECT password_hash FROM users WHERE id = :i"), {"i": device.user_id})
    ).mappings().first()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise ApiError(401, "invalid_credentials")

    await state.apply_relink(session, sw, body.new_company_guid, body.company_name, clock, device.user_id)
    await session.commit()
    return {
        "applied": True,
        "sync_state": sw.sync_state,
        "restore_reason": sw.restore_reason,
        "previous_company_guids": sw.previous_company_guids,
    }
