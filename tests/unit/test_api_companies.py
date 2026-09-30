"""Tests for GET /api/companies endpoint."""

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.companies import router
from backend.api.dependencies import get_client, get_current_user
from tally_bridge.models import Company


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def mock_client():
    return AsyncMock()


def test_companies_returns_list(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    with patch("backend.api.companies.list_companies", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = [Company(name="Test Co"), Company(name="Other Co")]
        with TestClient(app) as tc:
            resp = tc.get("/api/companies")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["companies"]) == 2
    assert body["companies"][0]["name"] == "Test Co"


def test_companies_empty(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    with patch("backend.api.companies.list_companies", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = []
        with TestClient(app) as tc:
            resp = tc.get("/api/companies")
    assert resp.status_code == 200
    assert resp.json()["companies"] == []
