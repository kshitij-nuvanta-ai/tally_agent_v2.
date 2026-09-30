"""Integration tests for FastAPI endpoints against a mock Tally server.

Each test spins up an aiohttp mock Tally server, creates a TallyClient
pointing at it, injects that client via FastAPI dependency overrides,
and exercises the real HTTP request -> FastAPI -> TallyClient -> mock Tally
server -> parsed JSON response cycle.

We use httpx.AsyncClient with ASGITransport so that the FastAPI app, the
TallyClient, and the mock Tally server all share the same async event loop.
"""

import httpx
import pytest
from httpx import ASGITransport

from backend.api.dependencies import get_client, get_current_user
from tally_bridge.client import TallyClient
from tests.mocks.mock_tally_server import create_mock_tally_app


@pytest.fixture
async def mock_tally(aiohttp_server):
    """Start a mock Tally HTTP server and return a TallyClient pointing at it."""
    tally_app = create_mock_tally_app()
    server = await aiohttp_server(tally_app)
    client = TallyClient(host="localhost", port=server.port)
    yield client
    await client.close()


@pytest.fixture
async def async_client(mock_tally):
    """Create an httpx.AsyncClient bound to the FastAPI app via ASGITransport.

    We import the app fresh and override get_client so the endpoints use
    the mock_tally client (which talks to the aiohttp mock server).
    """
    # Import the routers and build a minimal app to avoid lifespan issues
    from fastapi import FastAPI

    from backend.api import companies, health, reports

    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.include_router(companies.router, prefix="/api")
    app.include_router(reports.router, prefix="/api")

    # Override get_client to use the mock Tally server, and bypass auth for tests.
    app.dependency_overrides[get_client] = lambda: mock_tally
    app.dependency_overrides[get_current_user] = lambda: "test-user"

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# --- Health ---


async def test_health_returns_healthy(async_client):
    """GET /api/health returns status=healthy and tally_connected=True."""
    resp = await async_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["tally_connected"] is True
    assert "localhost" in body["tally_url"]


# --- Companies ---


async def test_companies_returns_parsed_list(async_client):
    """GET /api/companies returns company names from the fixture."""
    resp = await async_client.get("/api/companies")
    assert resp.status_code == 200
    body = resp.json()
    names = [c["name"] for c in body["companies"]]
    assert "Bharat Traders Pvt Ltd" in names
    assert len(names) == 1


# --- Reports ---


async def test_trial_balance_report(async_client):
    """GET /api/reports/trial_balance returns headers and rows."""
    resp = await async_client.get(
        "/api/reports/trial_balance",
        params={"from_date": "01-04-2025", "to_date": "31-03-2026"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "headers" in body
    assert "rows" in body
    assert len(body["rows"]) == 9
    # First row should be Capital Account
    flat_values = [str(v) for row in body["rows"] for v in row]
    assert any("Capital Account" in v for v in flat_values)


async def test_balance_sheet_report(async_client):
    """GET /api/reports/balance_sheet returns group amounts."""
    resp = await async_client.get(
        "/api/reports/balance_sheet",
        params={"as_on_date": "31-03-2026"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "headers" in body
    assert "rows" in body
    assert len(body["rows"]) == 7
    flat_values = [str(v) for row in body["rows"] for v in row]
    assert any("Capital Account" in v for v in flat_values)
    assert any("Fixed Assets" in v for v in flat_values)


async def test_day_book_report(async_client):
    """GET /api/reports/day_book returns voucher rows."""
    resp = await async_client.get(
        "/api/reports/day_book",
        params={"from_date": "01-10-2025", "to_date": "31-10-2025"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "headers" in body
    assert "rows" in body
    assert len(body["rows"]) == 8
    flat_values = [str(v) for row in body["rows"] for v in row]
    assert any("S001" in v for v in flat_values)
    assert any("PMT001" in v for v in flat_values)


async def test_bills_receivable_report(async_client):
    """GET /api/reports/bills_receivable returns outstanding bills."""
    resp = await async_client.get(
        "/api/reports/bills_receivable",
        params={"as_on_date": "31-03-2026"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "headers" in body
    assert "rows" in body
    assert len(body["rows"]) == 6
    flat_values = [str(v) for row in body["rows"] for v in row]
    assert any("Apex Technologies" in v for v in flat_values)


async def test_invalid_report_returns_400(async_client):
    """GET /api/reports/nonexistent returns HTTP 400."""
    resp = await async_client.get("/api/reports/nonexistent")
    assert resp.status_code == 400
    body = resp.json()
    assert "Unknown report" in body["detail"]


# --- Companies: ad-hoc host/port/mock params ---


async def test_companies_mock_param_returns_mock_company(async_client):
    """mock=true must answer from the built-in mock handler, regardless of app client."""
    resp = await async_client.get("/api/companies", params={"mock": "true"})
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["companies"]]
    assert "Bharat Traders Pvt Ltd" in names  # tests/fixtures/company_list.xml


async def test_companies_explicit_host_probes_that_tally(async_client, mock_tally):
    """host/port params probe that instance ad hoc (here: the aiohttp mock server)."""
    port = int(mock_tally.base_url.rsplit(":", 1)[1])
    resp = await async_client.get("/api/companies", params={"host": "localhost", "port": port})
    assert resp.status_code == 200
    assert len(resp.json()["companies"]) >= 1


async def test_companies_bad_host_returns_503(async_client):
    """Unreachable host/port must 503, not 500."""
    resp = await async_client.get("/api/companies", params={"host": "127.0.0.1", "port": 1})
    assert resp.status_code == 503


# --- Health: ad-hoc host/port params ---


async def test_health_bad_host_reports_degraded(async_client):
    resp = await async_client.get("/api/health", params={"host": "127.0.0.1", "port": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tally_connected"] is False
    assert body["status"] == "degraded"
    assert body["tally_url"] == "http://127.0.0.1:1"
    assert body["mode"] == "live"


async def test_health_explicit_host_reports_connected(async_client, mock_tally):
    port = int(mock_tally.base_url.rsplit(":", 1)[1])
    resp = await async_client.get("/api/health", params={"host": "localhost", "port": port})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tally_connected"] is True
    assert body["status"] == "healthy"
    assert body["mode"] == "live"


async def test_health_without_params_unchanged(async_client):
    resp = await async_client.get("/api/health")
    assert resp.status_code == 200
    assert {"status", "tally_connected", "tally_url", "mode"} <= resp.json().keys()


# --- Port range validation ---


async def test_companies_invalid_port_returns_422(async_client):
    """Port out of 1–65535 range must return 422, not 500."""
    resp = await async_client.get("/api/companies", params={"host": "h", "port": 99999})
    assert resp.status_code == 422
