"""E2E smoke test — legacy mode (no DATABASE_URL, no auth)."""
import pytest
from httpx import ASGITransport, AsyncClient

from backend.agents.context import SessionStore
from backend.main import app
from tally_bridge.client import TallyClient


@pytest.fixture
async def legacy_client():
    """Provide a test client with mock Tally wired into app state directly."""
    # Manually populate app state so lifespan isn't needed for basic tests
    tally_client = TallyClient(host="localhost", port=9000)
    tally_client.mock_mode = True
    app.state.tally_client = tally_client
    app.state.session_store = SessionStore(ttl_minutes=60)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    await tally_client.close()


async def test_legacy_health(legacy_client):
    resp = await legacy_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    # Health endpoint returns "healthy" in mock mode
    assert data["status"] in ("ok", "healthy", "degraded")
    assert data["tally_connected"] is True


async def test_legacy_chat_no_auth_required(legacy_client):
    resp = await legacy_client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert "message" in data
    assert "session_id" in data


async def test_legacy_chat_session_continuity(legacy_client):
    resp1 = await legacy_client.post("/api/chat", json={"message": "hello"})
    assert resp1.status_code == 200
    session_id = resp1.json()["session_id"]

    resp2 = await legacy_client.post("/api/chat", json={"message": "What is 2+2?", "session_id": session_id})
    assert resp2.status_code == 200
    assert resp2.json()["session_id"] == session_id
