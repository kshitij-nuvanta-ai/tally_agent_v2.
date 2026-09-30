"""Device tokens (S1 spec §9.1, D6): a short-lived JWT access token signed with a secret **separate** from the
web JWT secret, and an opaque, rotating refresh token whose plaintext is never stored — only its SHA-256 hex.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

from backend.sync.errors import ApiError

DEVICE_TYP = "v2_device"


@dataclass(frozen=True)
class DeviceClaims:
    device_id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID | None
    jti: str


def mint_access(
    device_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    *,
    secret: str,
    minutes: int,
    now: datetime,
) -> str:
    claims = {
        "sub": str(device_id),
        "uid": str(user_id),
        "ws": str(workspace_id) if workspace_id else None,
        "typ": DEVICE_TYP,
        "jti": secrets.token_hex(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_access(token: str, *, secret: str, now: datetime) -> DeviceClaims:
    """Decodes without letting PyJWT enforce ``exp``/``iat`` itself (both ``verify_exp`` and ``verify_iat``
    disabled), then compares both to the injected ``now`` so tests can use ``FixedClock`` deterministically —
    including a clock set ahead of real wall time. Without ``verify_iat: False`` here, PyJWT would still check
    ``iat`` against the REAL wall clock even though ``mint_access`` stamped it from the injected one, so a token
    minted with a clock ahead of real time would raise ``ImmatureSignatureError`` (task-7 review, Minor 1)."""
    try:
        payload = jwt.decode(
            token, secret, algorithms=["HS256"], options={"verify_exp": False, "verify_iat": False}
        )
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "token_invalid") from exc

    if payload.get("typ") != DEVICE_TYP:
        raise ApiError(401, "token_invalid")

    now_ts = int(now.timestamp())
    if now_ts >= int(payload.get("exp", 0)):
        raise ApiError(401, "token_expired")
    if now_ts < int(payload.get("iat", 0)):
        raise ApiError(401, "token_invalid")  # not yet valid per the injected clock — same zero-leeway semantics

    try:
        device_id = uuid.UUID(payload["sub"])
        user_id = uuid.UUID(payload["uid"])
        ws = payload.get("ws")
        workspace_id = uuid.UUID(ws) if ws else None
        jti = payload["jti"]
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ApiError(401, "token_invalid") from exc

    return DeviceClaims(device_id=device_id, user_id=user_id, workspace_id=workspace_id, jti=jti)


def new_refresh() -> tuple[str, str]:
    """Returns ``(token, sha256_hex)``. Only the hex digest is ever persisted (D6)."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh(token)


def hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
