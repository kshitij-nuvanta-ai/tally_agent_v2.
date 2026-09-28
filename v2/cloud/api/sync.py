"""Device-token sync routes (S1 spec §7.5-§7.15). Task 5 lands only ``POST /api/sync/company``; later tasks add
the ``{ws}``-scoped routes here (heartbeat, state, runs, batches, coverage, reconcile, snapshots, parity, relink).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.api.dependencies import any_device
from v2.cloud.db import session_dep
from v2.cloud.models import AgentDevice
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
