"""Web-facing sync routes (S1 spec §7.13, §7.15 web half, §9.4): ``sync-status`` and server-side commands, both
web JWT + owner-only.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.api.dependencies import check_user_password, web_user
from v2.cloud.db import session_dep
from v2.cloud.errors import ApiError
from v2.cloud.models import SyncWorkspace
from v2.cloud.sync import commands, state

router = APIRouter(prefix="/api/workspaces", tags=["web-sync"])


async def _owned_sync_workspace(session: AsyncSession, ws: uuid.UUID, user_id: uuid.UUID) -> SyncWorkspace:
    """404 (never 403/410 — Q28/§9.4: only the owner may even learn whether ``ws`` exists) for: not found, not
    this user's, deleted, or never bound (no ``sync_workspaces`` row yet)."""
    row = (
        await session.execute(text("SELECT id, user_id, is_deleted FROM workspaces WHERE id = :i"), {"i": ws})
    ).mappings().first()
    if row is None or row["user_id"] != user_id or row["is_deleted"]:
        raise ApiError(404, "workspace_not_found")
    sw = await session.get(SyncWorkspace, ws)
    if sw is None:
        raise ApiError(404, "workspace_not_found")
    return sw


# --- §7.13 sync-status -------------------------------------------------------------------------------------


@router.get("/{ws}/sync-status")
async def get_sync_status(
    ws: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    user_id: uuid.UUID = Depends(web_user),
) -> dict:
    sw = await _owned_sync_workspace(session, ws, user_id)
    return await state.sync_status(session, sw)


# --- §7.15 web commands (recheck_now / confirm_resync / confirm_relink) -----------------------------------


class WebCommandRequest(BaseModel):
    """§7.15's closed set of web commands, each with its own required fields — anything outside this shape is
    a 422 (fix round 1 / Important 2), never silently stored and delivered to the agent."""

    type: Literal["recheck_now", "confirm_resync", "confirm_relink"]
    scope: Literal["company", "fy"] | None = None
    fy_start: str | None = None
    password: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _validate_per_type_fields(self) -> "WebCommandRequest":
        if self.type == "confirm_resync":
            if self.scope is None:
                raise ValueError("scope is required for confirm_resync")
            if self.scope == "fy" and not self.fy_start:
                raise ValueError("fy_start is required when scope is 'fy'")
        if self.type == "confirm_relink" and not self.password:
            raise ValueError("password is required for confirm_relink")
        return self


@router.post("/{ws}/sync/commands")
async def post_command(
    ws: uuid.UUID,
    body: WebCommandRequest,
    request: Request,
    session: AsyncSession = Depends(session_dep),
    user_id: uuid.UUID = Depends(web_user),
) -> dict:
    sw = await _owned_sync_workspace(session, ws, user_id)
    clock = request.app.state.clock

    if body.type == "confirm_relink":
        # §7.15: relink is applied AT ONCE from the web too — same `state.apply_relink` service the device
        # path uses — never merely queued as a pending sync_command the agent would have to deliver back.
        if not sw.relink_prompt:
            raise ApiError(409, "relink_not_prompted")
        # `body.password` is guaranteed non-empty here — the model validator above requires it for this type.
        await check_user_password(request, session, user_id, body.password)       # I5: throttled like /login

        new_guid = sw.relink_prompt.get("guid")
        new_name = sw.relink_prompt.get("name")
        await state.apply_relink(session, sw, new_guid, new_name, clock, user_id)
        await session.commit()
        return {
            "applied": True,
            "sync_state": sw.sync_state,
            "restore_reason": sw.restore_reason,
            "previous_company_guids": sw.previous_company_guids,
        }

    params = body.model_dump(exclude={"type", "password"}, exclude_none=True)
    cmd = await commands.enqueue(session, ws, body.type, params, requested_by="web")
    if body.type == "confirm_resync":
        # S1 review I3: the newest user confirm supersedes any still-open one (not bound to a running run).
        await commands.cancel_open_resyncs(session, ws, clock, keep=cmd.id)
    await session.commit()
    return {"id": str(cmd.id), "type": cmd.type, "params": cmd.params or {}, "status": cmd.status}
