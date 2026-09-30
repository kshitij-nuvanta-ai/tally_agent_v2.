"""Fixtures for live E2E tests against real Tally + real Claude API.

Gated by RUN_LIVE_TESTS=1 environment variable (or --tally-mode mock).
Requires ANTHROPIC_API_KEY and a reachable Tally instance (live mode).
"""

import logging
import os

import pytest

from tally_bridge.client import TallyClient
from backend.agents.orchestrator import Orchestrator
from backend.agents.context import SessionStore


def pytest_configure(config):
    """Enable DEBUG-level logging for agent modules during live tests."""
    for name in ("backend.agents.orchestrator", "backend.agents.query_agent", "backend.agents.analysis_agent"):
        log = logging.getLogger(name)
        log.setLevel(logging.DEBUG)
        if not log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(name)s | %(levelname)s | %(message)s"))
            log.addHandler(handler)


def pytest_addoption(parser):
    parser.addoption("--host", default=os.environ.get("TALLY_HOST", "localhost"))
    parser.addoption("--port", type=int, default=int(os.environ.get("TALLY_PORT", "9000")))
    parser.addoption("--tally-mode", default="live", choices=["live", "mock"],
                     help="Tally mode: live (real Tally) or mock (built-in handler)")


def pytest_collection_modifyitems(config, items):
    """Skip tests unless RUN_LIVE_TESTS is set or --tally-mode mock is used."""
    tally_mode = config.getoption("--tally-mode", "live")
    if tally_mode == "mock" or os.environ.get("RUN_LIVE_TESTS"):
        return  # Don't skip
    skip_marker = pytest.mark.skip(reason="RUN_LIVE_TESTS not set and --tally-mode is not mock")
    for item in items:
        item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def tally_host(request):
    return request.config.getoption("--host")


@pytest.fixture(scope="session")
def tally_port(request):
    return request.config.getoption("--port")


@pytest.fixture(scope="session")
def tally_mode(request):
    return request.config.getoption("--tally-mode")


@pytest.fixture
async def tally_client(tally_host, tally_port, tally_mode):
    """TallyClient — real or mock depending on --tally-mode flag."""
    client = TallyClient(host=tally_host, port=tally_port)
    if tally_mode == "mock":
        client.mock_mode = True
    else:
        healthy = await client.health_check()
        if not healthy:
            pytest.skip(f"Tally unreachable at {tally_host}:{tally_port}")
    yield client
    await client.close()


@pytest.fixture
def orchestrator():
    return Orchestrator()


@pytest.fixture
def session_store():
    return SessionStore()


@pytest.fixture
def session(session_store):
    return session_store.get_or_create()
