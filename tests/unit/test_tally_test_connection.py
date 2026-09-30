"""Unit tests — POST /api/tally/test-connection (Group B, Task 8).

Verifies the endpoint reaches Tally via get_company_list and returns the
company names, and reports connected=False on failure.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "JWT_SECRET", None)
    from backend.main import app
    with TestClient(app) as tc:
        yield tc


def test_test_connection_returns_companies(client):
    with patch(
        "tally_bridge.queries.masters.get_company_list",
        new=AsyncMock(return_value=["Bharat Traders Pvt Ltd", "Acme Co"]),
    ):
        resp = client.post(
            "/api/tally/test-connection",
            json={"host": "localhost", "port": 9000},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert body["companies"] == ["Bharat Traders Pvt Ltd", "Acme Co"]
    assert body["error"] is None


def test_test_connection_reports_failure(client):
    with patch(
        "tally_bridge.queries.masters.get_company_list",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        resp = client.post(
            "/api/tally/test-connection",
            json={"host": "10.0.0.9", "port": 9000},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["companies"] == []
    assert "connection refused" in body["error"]
