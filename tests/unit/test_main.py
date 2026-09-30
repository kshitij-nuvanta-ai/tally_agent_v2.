"""Tests for the FastAPI application setup."""

import importlib

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from backend.api.dependencies import get_client, get_current_user
from tally_bridge.exceptions import TallyConnectionError, TallyResponseError


@pytest.fixture
def mock_tally_client():
    client = AsyncMock()
    client.base_url = "http://localhost:9000"
    client.health_check = AsyncMock(return_value=True)
    client.close = AsyncMock()
    return client


@pytest.fixture
def patched_app(mock_tally_client):
    """Reload backend.main with TallyClient patched so lifespan uses our mock.

    The patch must remain active through TestClient usage because TestClient
    triggers the lifespan on ``__enter__`` which calls ``TallyClient(...)``.
    """
    patcher = patch("backend.main.TallyClient", return_value=mock_tally_client)
    patcher.start()
    import backend.main
    importlib.reload(backend.main)
    yield backend.main.app
    patcher.stop()


def test_app_starts_and_health_works(patched_app, mock_tally_client):
    patched_app.dependency_overrides[get_client] = lambda: mock_tally_client
    patched_app.dependency_overrides[get_current_user] = lambda: "test-user"
    with TestClient(patched_app) as tc:
        resp = tc.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tally_connected"] is True
    patched_app.dependency_overrides.clear()


def test_tally_connection_error_returns_503(patched_app):
    @patched_app.get("/test-conn-error")
    async def trigger():
        raise TallyConnectionError("Tally unreachable")

    with TestClient(patched_app) as tc:
        resp = tc.get("/test-conn-error")
        assert resp.status_code == 503
        assert resp.json()["error"] == "Tally unreachable"


def test_tally_response_error_returns_502(patched_app):
    @patched_app.get("/test-resp-error")
    async def trigger():
        raise TallyResponseError("Invalid XML")

    with TestClient(patched_app) as tc:
        resp = tc.get("/test-resp-error")
        assert resp.status_code == 502
        assert resp.json()["error"] == "Invalid XML"


def test_cors_headers_present(patched_app):
    with TestClient(patched_app) as tc:
        resp = tc.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # With allow_credentials=True, the origin is echoed back instead of "*"
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_all_routers_registered(patched_app):
    routes = [r.path for r in patched_app.routes]
    assert "/api/chat" in routes
    assert "/api/health" in routes
    assert "/api/companies" in routes
    assert "/api/reports/{name}" in routes
