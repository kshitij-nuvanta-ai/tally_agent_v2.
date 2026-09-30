"""V2 cloud skeleton: settings, IST clock, error type and the copied web-JWT check (S1 spec §6.1, §6.2, §9.4)."""
from datetime import date, datetime, timezone

import httpx
import jwt
import pytest

from backend.sync.web_jwt import decode_web_access
from backend.sync.clock import FixedClock, current_fy_start, fy_start_of, ist_date
from backend.config import Settings
from backend.sync.errors import ApiError
from backend.sync.app import create_app


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


def test_web_jwt_accepts_access_rejects_refresh_and_device_typ():
    secret = "w" * 32
    ok = jwt.encode({"sub": "u1", "type": "access", "exp": 4102444800}, secret, algorithm="HS256")
    assert decode_web_access(ok, secret) == "u1"
    for bad in ({"sub": "u1", "type": "refresh", "exp": 4102444800},
                {"sub": "d1", "typ": "v2_device", "exp": 4102444800}):
        with pytest.raises(ApiError) as err:
            decode_web_access(jwt.encode(bad, secret, algorithm="HS256"), secret)
        assert err.value.code == "token_invalid"


async def test_health_route_without_db():
    app = create_app(Settings(_env_file=None, DATABASE_URL="", JWT_SECRET="w" * 32,
                              DEVICE_TOKEN_SECRET="d" * 32))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v2/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
