"""Web-facing device management (S1 spec §7.16): the user's devices, and revoking one from the web app."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.sync_dependencies import web_user
from backend.sync.db import session_dep
from backend.sync.errors import ApiError
from backend.db.sync_models import AgentDevice

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def list_devices(
    request: Request,
    session: AsyncSession = Depends(session_dep),
    user_id: uuid.UUID = Depends(web_user),
) -> list[dict]:
    devices = (
        await session.execute(select(AgentDevice).where(AgentDevice.user_id == user_id))
    ).scalars().all()

    out = []
    for d in devices:
        workspace_name = None
        if d.workspace_id is not None:
            row = (
                await session.execute(text("SELECT name FROM workspaces WHERE id = :id"), {"id": d.workspace_id})
            ).first()
            workspace_name = row[0] if row else None
        out.append(
            {
                "id": str(d.id),
                "device_name": d.device_name,
                "agent_version": d.agent_version,
                "workspace_id": str(d.workspace_id) if d.workspace_id else None,
                "workspace_name": workspace_name,
                "is_active": d.is_active,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                "revoke_reason": d.revoke_reason,
            }
        )
    return out


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    user_id: uuid.UUID = Depends(web_user),
) -> Response:
    device = await session.get(AgentDevice, device_id)
    if device is None or device.user_id != user_id:
        raise ApiError(404, "device_not_found")

    device.revoked_at = request.app.state.clock.now()
    device.revoke_reason = "user_removed"
    device.is_active = False
    await session.commit()
    return Response(status_code=204)
