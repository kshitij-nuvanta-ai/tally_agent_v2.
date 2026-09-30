"""Tests for auth API — password validation, rate limiting, and endpoint unit tests."""
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import router
from backend.api.dependencies import get_current_user
from backend.db.engine import get_db
from backend.utils.auth import validate_password


# ---- Existing tests (unchanged) ----

class TestPasswordValidation:
    def test_register_rejects_weak_password(self):
        errors = validate_password("weak")
        assert len(errors) >= 3

    def test_register_accepts_strong_password(self):
        errors = validate_password("Str0ng!Pass#99")
        assert errors == []


class TestRateLimit:
    # The store is the app's one login limiter (a SlidingWindow, shared with agent login — v2 merge M8), no
    # longer a module-level dict in backend.api.auth; web login's answer at the limit is unchanged.
    @staticmethod
    def _limiter():
        from backend.sync.clock import SystemClock
        from backend.utils.rate_limit import SlidingWindow
        return SlidingWindow(5, 15 * 60, SystemClock())

    def test_rate_limit_blocks_after_max_attempts(self):
        from fastapi import HTTPException
        from backend.api.auth import _check_rate_limit
        email = "ratelimit-test@example.com"
        limiter = self._limiter()
        limiter._hits[email] = [time.time() for _ in range(5)]
        with pytest.raises(HTTPException) as exc_info:
            _check_rate_limit(limiter, email)
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail == "Too many login attempts. Try again later."
        assert exc_info.value.headers is None

    def test_rate_limit_allows_under_threshold(self):
        from backend.api.auth import _check_rate_limit
        email = "ratelimit-ok@example.com"
        limiter = self._limiter()
        limiter._hits[email] = [time.time(), time.time()]
        _check_rate_limit(limiter, email)  # Should not raise


# ---- New endpoint unit tests ----

def _make_mock_user(user_id=None, email="test@example.com", name="Test User",
                    password_hash="$2b$12$fakehash", is_active=True):
    """Create a mock User ORM object."""
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    user.email = email
    user.name = name
    user.password_hash = password_hash
    user.is_active = is_active
    user.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return user


def _make_mock_db():
    """Create a mock async DB session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _force_db_mode(monkeypatch):
    """Auth endpoints only exist in db_mode; force it on for unit tests."""
    monkeypatch.setattr("backend.config.settings.DATABASE_URL", "postgresql+asyncpg://fake/db")
    monkeypatch.setattr("backend.config.settings.JWT_SECRET", "a" * 64)
    monkeypatch.setattr("backend.config.settings.JWT_ACCESS_TOKEN_EXPIRY_MINUTES", 30)
    monkeypatch.setattr("backend.config.settings.JWT_REFRESH_TOKEN_EXPIRY_DAYS", 7)


@pytest.fixture
def auth_app():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


class TestRegisterEndpoint:
    def test_register_success(self, auth_app):
        mock_db = _make_mock_db()
        # No existing user
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        user_id = uuid.uuid4()

        async def fake_refresh(user):
            user.id = user_id
            user.email = "new@example.com"
            user.name = "New User"

        mock_db.refresh = AsyncMock(side_effect=fake_refresh)

        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/register", json={
                "email": "new@example.com",
                "password": "Str0ng!Pass#99",
                "name": "New User",
            })

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "access_token" in body
        assert body["user"]["email"] == "new@example.com"

    def test_register_duplicate_email_409(self, auth_app):
        mock_db = _make_mock_db()
        existing_user = _make_mock_user(email="dupe@example.com")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/register", json={
                "email": "dupe@example.com",
                "password": "Str0ng!Pass#99",
                "name": "Dupe User",
            })

        assert resp.status_code == 409

    def test_register_weak_password_422(self, auth_app):
        mock_db = _make_mock_db()
        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/register", json={
                "email": "weak@example.com",
                "password": "weak",
                "name": "Weak User",
            })

        assert resp.status_code == 422


class TestLoginEndpoint:
    def test_login_success(self, auth_app):
        from backend.utils.auth import hash_password
        hashed = hash_password("Str0ng!Pass#99")
        user = _make_mock_user(email="login@example.com", password_hash=hashed)

        mock_db = _make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/login", json={
                "email": "login@example.com",
                "password": "Str0ng!Pass#99",
            })

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "access_token" in body
        assert body["user"]["email"] == "login@example.com"

    def test_login_invalid_password_401(self, auth_app):
        from backend.utils.auth import hash_password
        hashed = hash_password("Str0ng!Pass#99")
        user = _make_mock_user(email="badpass@example.com", password_hash=hashed)

        mock_db = _make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/login", json={
                "email": "badpass@example.com",
                "password": "WrongPassword!1",
            })

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid email or password"}
        # The failure is recorded on this app's own login limiter (one store per app, M8) — nothing to clean up.
        assert len(auth_app.state.login_rate_limiter._hits["badpass@example.com"]) == 1


class TestRefreshEndpoint:
    def test_refresh_valid_token(self, auth_app):
        from backend.utils.auth import create_refresh_token

        user_id = str(uuid.uuid4())
        refresh = create_refresh_token(user_id=user_id, secret="a" * 64, expiry_days=7)

        user = _make_mock_user()
        user.id = user_id

        mock_db = _make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/refresh", cookies={"refresh_token": refresh})

        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()

    def test_refresh_no_cookie_401(self, auth_app):
        mock_db = _make_mock_db()
        auth_app.dependency_overrides[get_db] = lambda: mock_db

        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/refresh")

        assert resp.status_code == 401


class TestMeEndpoint:
    def test_me_returns_user_info(self, auth_app):
        user_id = str(uuid.uuid4())
        user = _make_mock_user(user_id=user_id, email="me@example.com", name="Me User")

        mock_db = _make_mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        auth_app.dependency_overrides[get_db] = lambda: mock_db
        auth_app.dependency_overrides[get_current_user] = lambda: user_id

        with TestClient(auth_app) as tc:
            resp = tc.get("/api/auth/me")

        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "me@example.com"
        assert body["name"] == "Me User"


class TestLogoutEndpoint:
    def test_logout_clears_cookie(self, auth_app):
        with TestClient(auth_app) as tc:
            resp = tc.post("/api/auth/logout")

        assert resp.status_code == 200
        assert resp.json()["message"] == "Logged out"
        # Check that Set-Cookie header deletes the refresh_token
        set_cookie = resp.headers.get("set-cookie", "")
        assert "refresh_token" in set_cookie
