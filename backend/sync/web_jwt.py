# Copied from: backend/api/dependencies.py @ 836ce4d (token check only)
# Changes: get_current_user's token-check logic only, as a pure function returning the user id — no FastAPI
# dependency, no legacy-mode bypass (v2 is DB-mode only); raises v2's ApiError instead of HTTPException so the
# v2 error handler (errors.install_error_handler) produces the v2 JSON shape.
from __future__ import annotations

import jwt

from backend.sync.errors import ApiError


def decode_web_access(token: str, secret: str) -> str:
    """Decode the current app's web access JWT (same secret, via V2Settings.web_jwt_secret/A12) and return the
    user id. Raises ApiError(401, "token_expired") on expiry, ApiError(401, "token_invalid") otherwise —
    including a wrong/missing ``type`` claim (a refresh token, or a device token's ``typ``)."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(401, "token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "token_invalid") from exc

    if payload.get("type") != "access":
        raise ApiError(401, "token_invalid")

    return payload["sub"]
