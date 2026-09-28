"""V2 cloud skeleton: settings, IST clock, error type and the copied web-JWT check (S1 spec §6.1, §6.2, §9.4)."""
from datetime import date, datetime, timezone

import httpx
import jwt
import pytest

from v2.cloud.auth.web_jwt import decode_web_access
from v2.cloud.clock import FixedClock, current_fy_start, fy_start_of, ist_date
from v2.cloud.config import V2Settings
from v2.cloud.errors import ApiError
from v2.cloud.main import create_app


def test_ist_date_crosses_midnight_before_utc():
    assert ist_date(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)) == date(2026, 4, 1)


def test_fy_start_of():
    assert fy_start_of(date(2026, 3, 31)) == date(2025, 4, 1)
    assert fy_start_of(date(2026, 4, 1)) == date(2026, 4, 1)


def test_current_fy_uses_ist():
    assert current_fy_start(FixedClock(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc))) == date(2026, 4, 1)


def test_settings_defaults_and_web_secret_alias(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "w" * 32)
    monkeypatch.delenv("V2_WEB_JWT_SECRET", raising=False)
    s = V2Settings(_env_file=None)
    assert (s.port, s.ingest_max_objects, s.parity_tolerance_paise, s.web_jwt_secret) == (8100, 500, 100, "w" * 32)


def test_settings_refuse_short_device_secret():
    with pytest.raises(ValueError):
        V2Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", web_jwt_secret="w" * 32,
                   device_token_secret="short").validate_for_serving()


def test_settings_refuse_equal_secrets():
    """D6 controller ruling: the device secret must differ from the web JWT secret."""
    with pytest.raises(ValueError):
        V2Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", web_jwt_secret="w" * 32,
                   device_token_secret="w" * 32).validate_for_serving()


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
    app = create_app(V2Settings(_env_file=None, database_url="", web_jwt_secret="w" * 32,
                                device_token_secret="d" * 32))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v2/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
