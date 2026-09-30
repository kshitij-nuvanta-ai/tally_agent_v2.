"""Fixtures for E2E tests using mock Claude API + mock Tally server."""

import pytest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend.main import app
from tally_bridge.client import TallyClient
from tests.mocks.mock_claude_api import MockAnthropicClient


@pytest.fixture(autouse=True)
def _force_legacy_unless_db(request, monkeypatch):
    """Force legacy mode for non-DB tests, even if .env has DATABASE_URL."""
    module_name = request.node.module.__name__
    if "test_db_" in module_name:
        return  # DB tests manage their own config
    monkeypatch.setattr("backend.config.settings.DATABASE_URL", None)


@pytest.fixture
def make_mock_claude():
    """Factory to create a MockAnthropicClient with given responses."""
    def _factory(responses):
        return MockAnthropicClient(responses)
    return _factory


@pytest.fixture
async def e2e_client(aiohttp_server, make_mock_claude):
    """Provide an async test client + a function to set mock Claude responses."""
    from tests.mocks.mock_tally_server import create_mock_tally_app

    # Start mock Tally
    tally_app = create_mock_tally_app()
    server = await aiohttp_server(tally_app)
    tally_client = TallyClient(host="127.0.0.1", port=server.port)

    # Inject into FastAPI app state
    app.state.tally_client = tally_client
    from backend.agents.context import SessionStore
    app.state.session_store = SessionStore()

    transport = ASGITransport(app=app)
    async_client = AsyncClient(transport=transport, base_url="http://test")

    mock_clients = {}

    def set_responses(
        orchestrator_responses=None,
        query_agent_responses=None,
        analysis_agent_responses=None,
    ):
        """Configure mock Claude responses for each agent."""
        if orchestrator_responses is not None:
            mock_clients["orchestrator"] = MockAnthropicClient(orchestrator_responses)
        if query_agent_responses is not None:
            mock_clients["query_agent"] = MockAnthropicClient(query_agent_responses)
        if analysis_agent_responses is not None:
            mock_clients["analysis_agent"] = MockAnthropicClient(analysis_agent_responses)

    yield async_client, set_responses, mock_clients

    await async_client.aclose()
    await tally_client.close()
