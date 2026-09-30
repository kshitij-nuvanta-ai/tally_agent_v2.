"""Sync skeleton: settings, IST clock, error type and the web-JWT check (S1 spec §6.1, §6.2, §9.4)."""
from datetime import date, datetime, timezone

from types import SimpleNamespace

import jwt
import pytest

from backend.api.sync_dependencies import web_user
from backend.sync.clock import FixedClock, current_fy_start, fy_start_of, ist_date
from backend.config import Settings
from backend.sync.errors import ApiError
from backend.utils.auth import AccessTokenError, decode_access_token


def test_ist_date_crosses_midnight_before_utc():
    assert ist_date(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)) == date(2026, 4, 1)


def test_fy_start_of():
    assert fy_start_of(date(2026, 3, 31)) == date(2025, 4, 1)
    assert fy_start_of(date(2026, 4, 1)) == date(2026, 4, 1)


def test_current_fy_uses_ist():
    assert current_fy_start(FixedClock(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc))) == date(2026, 4, 1)


def test_settings_defaults_and_web_secret_alias(monkeypatch):
    """The sync settings live on the one ``Settings`` class (M2); the web secret is its ``JWT_SECRET``. ``V2_PORT``
    is gone with the separate app. Both env spellings are covered in ``tests/unit/test_config_sync.py``."""
    monkeypatch.setenv("JWT_SECRET", "w" * 32)
    monkeypatch.delenv("V2_WEB_JWT_SECRET", raising=False)
    monkeypatch.delenv("INGEST_MAX_OBJECTS", raising=False)
    monkeypatch.delenv("PARITY_TOLERANCE_PAISE", raising=False)
    s = Settings(_env_file=None)
    assert (s.INGEST_MAX_OBJECTS, s.PARITY_TOLERANCE_PAISE, s.JWT_SECRET) == (500, 100, "w" * 32)
    assert not hasattr(s, "port")


def test_settings_refuse_short_device_secret():
    with pytest.raises(ValueError):
        Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://x/y", JWT_SECRET="w" * 32,
                 DEVICE_TOKEN_SECRET="short").validate_for_serving()


def test_settings_refuse_equal_secrets():
    """D6 controller ruling: the device secret must differ from the web JWT secret."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://x/y", JWT_SECRET="w" * 32,
                 DEVICE_TOKEN_SECRET="w" * 32).validate_for_serving()


def _request_with(token: str, secret: str) -> SimpleNamespace:
    """The two things ``web_user`` reads from a request: the Authorization header and the app's settings."""
    settings = Settings(_env_file=None, JWT_SECRET=secret)
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"},
                           app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


async def test_web_jwt_accepts_access_rejects_refresh_and_device_typ():
    """The token check is the app's one ``decode_access_token`` (M7); ``web_user`` is its sync-route adapter and
    keeps answering ``ApiError`` 401 ``token_invalid`` / ``token_expired``."""
    secret = "w" * 32
    uid = "6f1c1d0e-0000-4000-8000-000000000001"
    ok = jwt.encode({"sub": uid, "type": "access", "exp": 4102444800}, secret, algorithm="HS256")
    assert decode_access_token(ok, secret) == uid
    assert str(await web_user(_request_with(ok, secret))) == uid
    for bad in ({"sub": "u1", "type": "refresh", "exp": 4102444800},
                {"sub": "d1", "typ": "v2_device", "exp": 4102444800}):
        token = jwt.encode(bad, secret, algorithm="HS256")
        with pytest.raises(AccessTokenError) as reason:
            decode_access_token(token, secret)
        assert reason.value.reason == "wrong_type"
        with pytest.raises(ApiError) as err:
            await web_user(_request_with(token, secret))
        assert (err.value.status, err.value.code) == (401, "token_invalid")


async def test_web_user_tells_expired_from_invalid():
    secret = "w" * 32
    expired = jwt.encode({"sub": "u1", "type": "access", "exp": 1}, secret, algorithm="HS256")
    forged = jwt.encode({"sub": "u1", "type": "access", "exp": 4102444800}, "x" * 32, algorithm="HS256")
    for token, reason, code in ((expired, "expired", "token_expired"), (forged, "invalid", "token_invalid"),
                                ("not-a-jwt", "invalid", "token_invalid")):
        with pytest.raises(AccessTokenError) as why:
            decode_access_token(token, secret)
        assert why.value.reason == reason
        with pytest.raises(ApiError) as err:
            await web_user(_request_with(token, secret))
        assert (err.value.status, err.value.code) == (401, code)


# ``test_health_route_without_db`` (the separate sync app's ``GET /api/v2/health``) is rewritten against the one
# app in ``test_wiring.py::test_main_app_with_sync_builds_and_answers_health_without_an_engine``.
