"""Auth dependencies for v2 cloud routes (S1 spec §8.1, §9.4).

``web_user`` decodes the current app's own web JWT (A12, same secret). ``any_device`` and ``active_device``
implement the §8.1 device-check ladder: ``any_device`` covers checks 1-2 (token valid, device not revoked) plus
the per-device rate limit (check 6) for device endpoints that don't need a bound workspace (login/refresh/logout,
``GET /api/agent/workspaces``). ``active_device`` covers the full 1-6 ladder in order for a path that names a
workspace (``/api/sync/{ws}/*``, wired up in a later task): the rate limit is checked LAST, after the workspace
checks, so a request that will 403/410/409 anyway never consumes rate-limit budget.
"""
from __future__ import annotations

import uuid

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.utils.device_tokens import decode_access
from backend.sync.passwords import verify_password
from backend.sync.web_jwt import decode_web_access
from backend.sync.db import session_dep
from backend.sync.errors import ApiError
from backend.db.sync_models import AgentDevice, SyncWorkspace


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError(401, "token_invalid")
    return auth[len("Bearer ") :]


async def check_user_password(request: Request, session: AsyncSession, user_id: uuid.UUID, password: str) -> None:
    """S1 review I5: the relink password re-check (device §7.15 and web ``confirm_relink``) is throttled exactly
    like ``/login`` — the SAME per-email limiter, checked first, a hit recorded only on a FAILED attempt — so a
    stolen device token can't turn relink into a password oracle, and the two paths share one budget. 429
    ``rate_limited`` (+ ``Retry-After``) at capacity; 401 ``invalid_credentials`` on a wrong password."""
    row = (
        await session.execute(text("SELECT email, password_hash FROM users WHERE id = :i"), {"i": user_id})
    ).mappings().first()
    if row is None:
        raise ApiError(401, "invalid_credentials")
    key = row["email"].strip().lower()
    limiter = request.app.state.login_rate_limiter
    limiter.check(key)
    if not verify_password(password, row["password_hash"]):
        limiter.record(key)
        raise ApiError(401, "invalid_credentials")


async def web_user(request: Request) -> uuid.UUID:
    """Web JWT -> user id (§9.4). Raises ``ApiError(401, "token_invalid"/"token_expired")``."""
    token = _bearer_token(request)
    user_id = decode_web_access(token, request.app.state.settings.JWT_SECRET or "")
    return uuid.UUID(user_id)


async def _decode_and_load_device(request: Request, session: AsyncSession) -> AgentDevice:
    """§8.1 checks 1-2: a valid, non-revoked device access token."""
    settings = request.app.state.settings
    clock = request.app.state.clock
    token = _bearer_token(request)
    claims = decode_access(token, secret=settings.DEVICE_TOKEN_SECRET, now=clock.now())

    device = await session.get(AgentDevice, claims.device_id)
    if device is None:
        raise ApiError(401, "device_revoked", reason=None)
    if device.revoked_at is not None:
        raise ApiError(401, "device_revoked", reason=device.revoke_reason)
    return device


async def any_device(request: Request, session: AsyncSession = Depends(session_dep)) -> AgentDevice:
    """§8.1 checks 1-2 + the per-device rate limit — used by device endpoints with no ``{ws}`` in the path."""
    device = await _decode_and_load_device(request, session)
    request.app.state.device_rate_limiter.hit(str(device.id))
    return device


async def active_device(
    ws: uuid.UUID, request: Request, session: AsyncSession = Depends(session_dep)
) -> tuple[AgentDevice, SyncWorkspace]:
    """§8.1 checks 1-6 in order, for a path that names a workspace."""
    device = await _decode_and_load_device(request, session)  # 1-2

    if device.workspace_id != ws:
        raise ApiError(403, "wrong_workspace")  # 3

    row = (
        await session.execute(text("SELECT is_deleted FROM workspaces WHERE id = :id"), {"id": ws})
    ).first()
    if row is None or row[0]:
        if device.revoked_at is None:
            device.revoked_at = request.app.state.clock.now()
            device.revoke_reason = "workspace_deleted"
            device.is_active = False
            await session.commit()
        raise ApiError(410, "workspace_deleted")  # 4

    workspace = await session.get(SyncWorkspace, ws)
    if workspace is None or workspace.active_device_id != device.id or not device.is_active:
        raise ApiError(409, "not_active_device")  # 5

    request.app.state.device_rate_limiter.hit(str(device.id))  # 6
    return device, workspace


__all__ = ["web_user", "any_device", "active_device"]
