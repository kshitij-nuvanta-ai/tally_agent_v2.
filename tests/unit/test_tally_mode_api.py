"""Tests for tally-mode API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.api.dependencies import get_current_user
from tally_bridge.client import TallyClient


@pytest.fixture
async def client():
    tally_client = TallyClient()
    tally_client.mock_mode = False
    app.state.tally_client = tally_client
    from backend.agents.context import SessionStore
    app.state.session_store = SessionStore()

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.pop(get_current_user, None)
    app.state.tally_client.mock_mode = False
    await tally_client.close()


@pytest.mark.asyncio
class TestTallyModeAPI:
    async def test_get_tally_mode_default_live(self, client):
        resp = await client.get("/api/tally-mode")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "live"

    async def test_set_tally_mode_mock(self, client):
        resp = await client.post("/api/tally-mode", json={"mode": "mock"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "mock"

    async def test_set_tally_mode_live(self, client):
        await client.post("/api/tally-mode", json={"mode": "mock"})
        resp = await client.post("/api/tally-mode", json={"mode": "live"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "live"

    async def test_set_tally_mode_invalid(self, client):
        resp = await client.post("/api/tally-mode", json={"mode": "invalid"})
        assert resp.status_code == 422

    async def test_get_mode_after_set(self, client):
        await client.post("/api/tally-mode", json={"mode": "mock"})
        resp = await client.get("/api/tally-mode")
        assert resp.json()["mode"] == "mock"

    async def test_health_includes_mode_when_mock(self, client):
        await client.post("/api/tally-mode", json={"mode": "mock"})
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "mock"
        assert body["tally_connected"] is True

    async def test_health_mode_live_when_live(self, client):
        resp = await client.get("/api/health")
        body = resp.json()
        assert body["mode"] == "live"
