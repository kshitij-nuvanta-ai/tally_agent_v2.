"""Device-token sync routes (S1 spec §7.5-§7.15). Task 5 landed ``POST /api/sync/company``; task 6 added the
``{ws}``-scoped heartbeat/state/relink routes; task 7 adds runs (§7.8) and coverage (§7.10); task 8c adds batches
(§7.9); task 9 adds reconcile (§7.11) and snapshots (§7.12); task 10c adds parity (§7.14).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.api.dependencies import any_device, active_device
from v2.cloud.auth.passwords import verify_password
from v2.cloud.db import session_dep
from v2.cloud.errors import ApiError
from v2.cloud.ingest import pipeline
from v2.cloud.ingest import reconcile as reconcile_mod
from v2.cloud.ingest import snapshots as snapshots_mod
from v2.cloud.models import AgentDevice
from v2.cloud.parity import engine as parity_engine
from v2.cloud.sync import coverage, runs, state
from v2.cloud.sync.binding import BindRequest, bind
from v2.contract.models import BatchRequest, ParityRequest, ReconcileRequest, SnapshotRequest

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


# --- §7.7 helper reused by the runs routes below (coverage JSON, same shape as state._coverage_json) ---------


async def _coverage_json(session: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    return await state._coverage_json(session, workspace_id)


# --- §7.8 runs ---------------------------------------------------------------------------------------------


@router.post("/{ws}/runs")
async def post_run(
    ws: uuid.UUID,
    body: runs.RunCreate,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock
    run = await runs.open_run(session, sw, device, body, clock)
    await session.commit()
    return {
        "run_id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "scope": run.scope,
        "command_id": str(run.command_id) if run.command_id else None,
        "counters_at_start": run.counters_at_start,
        "coverage": await _coverage_json(session, sw.workspace_id),
    }


@router.patch("/{ws}/runs/{run_id}")
async def patch_run_route(
    ws: uuid.UUID,
    run_id: uuid.UUID,
    body: runs.RunPatch,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock
    run = await runs.patch_run(session, sw, device, run_id, body, clock)
    await session.commit()
    return {
        "run_id": str(run.id),
        "status": run.status,
        "cursors": {"alt_vch_id": sw.cursor_alt_vch_id, "alt_mst_id": sw.cursor_alt_mst_id},
        "sync_state": sw.sync_state,
        "cursor_after": run.cursor_after,
    }


# --- §7.10 coverage ------------------------------------------------------------------------------------------


class CoveragePatchRequest(BaseModel):
    fy_start: str = Field(..., min_length=1, max_length=20)
    month: str | None = Field(None, max_length=10)
    run_id: uuid.UUID | None = None
    action: Literal["add_fy"] | None = None

    @model_validator(mode="after")
    def _month_required_unless_add_fy(self) -> "CoveragePatchRequest":
        if self.action is None and not self.month:
            raise ValueError("month is required unless action='add_fy'")
        return self


@router.patch("/{ws}/coverage")
async def patch_coverage(
    ws: uuid.UUID,
    body: CoveragePatchRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock
    fy_start = date.fromisoformat(body.fy_start)

    if body.action == "add_fy":
        result = await coverage.add_fy(session, sw, fy_start, clock)
    else:
        result = await coverage.ack_month(session, sw, fy_start, body.month, clock)

    await session.commit()
    return result


# --- §7.9 batches (§12 ingest pipeline) --------------------------------------------------------------------------


@router.post("/{ws}/batches")
async def post_batch(
    ws: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> JSONResponse:
    """§7.9: a gzip (or plain) JSON batch. Step 1's limits are enforced while the body streams in, before any
    parse; the body is never logged."""
    device, sw = bound
    settings = request.app.state.settings
    clock = request.app.state.clock
    raw = await pipeline.read_body(request, settings)
    try:
        body = BatchRequest.model_validate(raw)
    except ValidationError:
        raise ApiError(422, "invalid_body", "shape") from None
    status, payload = await pipeline.ingest_batch(session, sw, device, body, pipeline.body_sha256(raw), settings,
                                                  clock)
    return JSONResponse(status_code=status, content=payload)


# --- §7.11 reconcile ------------------------------------------------------------------------------------------


@router.post("/{ws}/reconcile")
async def post_reconcile(
    ws: uuid.UUID,
    body: ReconcileRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock
    result = await reconcile_mod.reconcile(session, sw, device, body, clock)
    await session.commit()
    return result


# --- §7.12 snapshots ------------------------------------------------------------------------------------------


@router.post("/{ws}/snapshots")
async def post_snapshot(
    ws: uuid.UUID,
    body: SnapshotRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    device, sw = bound
    clock = request.app.state.clock
    result = await snapshots_mod.store(session, sw, body, clock)
    await session.commit()
    return result


# --- §7.14 parity ---------------------------------------------------------------------------------------------


@router.post("/{ws}/parity")
async def post_parity(
    ws: uuid.UUID,
    body: ParityRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    bound: tuple = Depends(active_device),
) -> dict:
    """§10: preconditions in order, rungs 1-2, classifier, ladder; one transaction (the engine commits), then the
    ops signal."""
    device, sw = bound
    return await parity_engine.run_parity(session, sw, body, request.app.state.settings, request.app.state.clock)
