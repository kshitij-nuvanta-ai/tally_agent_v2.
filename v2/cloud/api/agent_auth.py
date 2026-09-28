"""Agent device auth routes (S1 spec §7.1-7.4, §9): login, refresh, logout, and the bound/unbound workspace
list. Copied semantics (A9): the login limiter is keyed by lower-cased email and every attempt — success or
failure — counts against it, mirroring ``backend/api/auth.py``'s ``_check_rate_limit`` check placement (before
verifying credentials) but as a class the device limiter can share.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.api.dependencies import any_device
from v2.cloud.auth.device_tokens import hash_refresh, mint_access, new_refresh
from v2.cloud.auth.passwords import verify_password
from v2.cloud.db import session_dep
from v2.cloud.errors import ApiError
from v2.cloud.models import AgentDevice, SyncWorkspace

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent-auth"])


@router.post("/auth/login")
async def login(body: dict, request: Request, session: AsyncSession = Depends(session_dep)) -> dict:
    settings = request.app.state.settings
    clock = request.app.state.clock
    now = clock.now()

    email = str(body["email"]).strip().lower()
    request.app.state.login_rate_limiter.hit(email)  # every attempt counts, before verifying credentials

    row = (
        await session.execute(
            text("SELECT id, password_hash, name, is_active FROM users WHERE email = :e"), {"e": email}
        )
    ).mappings().first()
    if row is None or not verify_password(body["password"], row["password_hash"]):
        raise ApiError(401, "invalid_credentials")
    if not row["is_active"]:
        raise ApiError(403, "account_inactive")

    device_id = uuid.uuid4()
    refresh_token, refresh_hash = new_refresh()
    device = AgentDevice(
        id=device_id,
        user_id=row["id"],
        workspace_id=None,
        device_name=body["device_name"],
        agent_version=body.get("agent_version"),
        refresh_hash=refresh_hash,
        refresh_expires_at=now + timedelta(days=settings.device_refresh_days),
        last_login_at=now,
        is_active=False,
    )
    session.add(device)
    await session.commit()

    access_token = mint_access(
        device_id, row["id"], None, secret=settings.device_token_secret,
        minutes=settings.device_access_minutes, now=now,
    )
    return {
        "device_id": str(device_id),
        "access_token": access_token,
        "expires_in": settings.device_access_minutes * 60,
        "refresh_token": refresh_token,
        "user": {"id": str(row["id"]), "name": row["name"]},
    }


async def _revoke(session: AsyncSession, device: AgentDevice, now, reason: str) -> None:
    device.revoked_at = now
    device.revoke_reason = reason
    device.is_active = False
    await session.commit()


async def _workspace_deleted(session: AsyncSession, workspace_id) -> bool:
    row = (
        await session.execute(text("SELECT is_deleted FROM workspaces WHERE id = :id"), {"id": workspace_id})
    ).first()
    return row is None or bool(row[0])


@router.post("/auth/refresh")
async def refresh(body: dict, request: Request, session: AsyncSession = Depends(session_dep)) -> dict:
    settings = request.app.state.settings
    clock = request.app.state.clock
    now = clock.now()
    token = body["refresh_token"]
    presented_hash = hash_refresh(token)

    device = (
        await session.execute(select(AgentDevice).where(AgentDevice.refresh_hash == presented_hash))
    ).scalar_one_or_none()

    if device is None:
        prev = (
            await session.execute(select(AgentDevice).where(AgentDevice.refresh_prev_hash == presented_hash))
        ).scalar_one_or_none()
        if prev is not None and prev.revoked_at is None:
            await _revoke(session, prev, now, "refresh_reuse")
            logger.warning("v2.ops.integrity", extra={"event": "refresh_reuse", "device_id": str(prev.id)})
        raise ApiError(401, "device_revoked" if prev is not None else "token_invalid",
                        reason="refresh_reuse" if prev is not None else None)

    if device.revoked_at is not None:
        raise ApiError(401, "device_revoked", reason=device.revoke_reason)

    if device.workspace_id is not None and await _workspace_deleted(session, device.workspace_id):
        await _revoke(session, device, now, "workspace_deleted")
        raise ApiError(410, "workspace_deleted")

    if device.refresh_expires_at is not None and device.refresh_expires_at <= now:
        raise ApiError(401, "refresh_expired")

    new_token, new_hash = new_refresh()
    device.refresh_prev_hash = device.refresh_hash
    device.refresh_hash = new_hash
    device.refresh_expires_at = now + timedelta(days=settings.device_refresh_days)
    await session.commit()

    access_token = mint_access(
        device.id, device.user_id, device.workspace_id, secret=settings.device_token_secret,
        minutes=settings.device_access_minutes, now=now,
    )
    return {
        "device_id": str(device.id),
        "access_token": access_token,
        "expires_in": settings.device_access_minutes * 60,
        "refresh_token": new_token,
    }


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    session: AsyncSession = Depends(session_dep),
    device: AgentDevice = Depends(any_device),
) -> Response:
    now = request.app.state.clock.now()
    if device.workspace_id is not None and await _workspace_deleted(session, device.workspace_id):
        await _revoke(session, device, now, "workspace_deleted")
        raise ApiError(410, "workspace_deleted")

    await _revoke(session, device, now, "logout")
    return Response(status_code=204)


@router.get("/workspaces")
async def list_agent_workspaces(
    request: Request,
    session: AsyncSession = Depends(session_dep),
    device: AgentDevice = Depends(any_device),
) -> list[dict]:
    rows = (
        await session.execute(
            text("SELECT id, name FROM workspaces WHERE user_id = :u AND is_deleted = false"),
            {"u": device.user_id},
        )
    ).mappings().all()

    out = []
    for r in rows:
        sw = await session.get(SyncWorkspace, r["id"])
        active = None
        if sw is not None and sw.active_device_id is not None:
            ad = await session.get(AgentDevice, sw.active_device_id)
            if ad is not None:
                active = {
                    "device_name": ad.device_name,
                    "last_seen_at": ad.last_seen_at.isoformat() if ad.last_seen_at else None,
                }
        out.append(
            {
                "id": str(r["id"]),
                "name": r["name"],
                "bound_company_guid": sw.tally_company_guid if sw is not None else None,
                "bound_company_name": sw.tally_company_name if sw is not None else None,
                "active_device": active,
            }
        )
    return out
