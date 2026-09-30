"""FastAPI dependency functions for injecting shared resources."""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.agents.context import SessionStore
from backend.config import settings
from backend.tally_bridge.client import TallyClient
from backend.utils.auth import AccessTokenError, decode_access_token

_bearer_scheme = HTTPBearer(auto_error=False)


def get_client(request: Request) -> TallyClient:
    """Return the app-level TallyClient singleton."""
    return request.app.state.tally_client


def get_session_store(request: Request) -> SessionStore:
    """Return the app-level SessionStore singleton."""
    return request.app.state.session_store


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """Extract and validate JWT from Authorization header. Returns user_id.

    In legacy mode (no DATABASE_URL), returns a placeholder user_id.
    """
    if not settings.db_mode:
        return "legacy-user"

    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        return decode_access_token(credentials.credentials, settings.JWT_SECRET)
    except AccessTokenError as exc:
        if exc.reason == "wrong_type":
            raise HTTPException(status_code=401, detail="Invalid token type")
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str | None:
    """Like get_current_user but returns None instead of raising for unauthenticated."""
    if not settings.db_mode:
        return "legacy-user"
    if not credentials:
        return None
    try:
        return decode_access_token(credentials.credentials, settings.JWT_SECRET)
    except AccessTokenError:
        return None
