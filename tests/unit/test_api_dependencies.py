"""Tests for FastAPI dependency functions."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials

from backend.api.dependencies import get_client, get_current_user, get_session_store
from backend.agents.context import SessionStore
from tally_bridge.client import TallyClient
from backend.utils.auth import create_access_token


JWT_SECRET = "test-secret-that-is-long-enough-for-testing"
USER_ID = str(uuid.uuid4())


def test_get_client_returns_client_from_app_state():
    mock_request = MagicMock()
    mock_request.app.state.tally_client = TallyClient("localhost", 9000)
    result = get_client(mock_request)
    assert isinstance(result, TallyClient)


def test_get_session_store_returns_store_from_app_state():
    mock_request = MagicMock()
    mock_request.app.state.session_store = SessionStore(ttl_minutes=60)
    result = get_session_store(mock_request)
    assert isinstance(result, SessionStore)


# ---------------------------------------------------------------------------
# get_current_user tests (DB mode)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def force_db_mode(monkeypatch):
    """Enable DB mode and set a known JWT secret for all get_current_user tests."""
    monkeypatch.setattr("backend.config.settings.DATABASE_URL",
                        "postgresql+asyncpg://user:pass@localhost/test")
    monkeypatch.setattr("backend.config.settings.JWT_SECRET", JWT_SECRET)


@pytest.mark.asyncio
async def test_get_current_user_valid_token_returns_user_id():
    """A well-formed access token returns the embedded user_id."""
    token = create_access_token(USER_ID, JWT_SECRET, expiry_minutes=30)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    result = await get_current_user(credentials=credentials)
    assert result == USER_ID


@pytest.mark.asyncio
async def test_get_current_user_expired_token_raises_401():
    """An expired token raises HTTPException 401."""
    payload = {
        "sub": USER_ID,
        "type": "access",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        "iat": datetime.now(timezone.utc) - timedelta(minutes=31),
    }
    expired_token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=credentials)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_malformed_token_raises_401():
    """A token signed with the wrong secret raises HTTPException 401."""
    bad_token = create_access_token(USER_ID, "wrong-secret-value-here-12345678", expiry_minutes=30)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=bad_token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=credentials)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_missing_header_raises_401():
    """No credentials (missing Authorization header) raises HTTPException 401."""
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=None)
    assert exc_info.value.status_code == 401
