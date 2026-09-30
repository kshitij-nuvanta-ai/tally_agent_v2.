"""Integration tests — GST invoice upload → review card against mock Tally (HTTP).

Drives ``POST /api/chat/upload`` through the real FastAPI app with the aiohttp
mock Tally server (which serves ``tests/fixtures/ledger_list.xml`` — it includes
the six GST ledgers under "Duties & Taxes") and Claude Vision mocked. Confirms
the orchestrator resolves the Input/Output GST ledgers from the live ledger list
and populates ``gst_entries`` on the review entry for each invoice type.
"""
import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from backend.api.dependencies import get_client, get_current_user
from tally_bridge.client import TallyClient
from tests.fixtures import vision_docs
from tests.mocks.mock_tally_server import create_mock_tally_app

_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


@pytest.fixture
async def mock_tally(aiohttp_server):
    tally_app = create_mock_tally_app()
    server = await aiohttp_server(tally_app)
    client = TallyClient(host="localhost", port=server.port)
    yield client
    await client.close()


@pytest.fixture
async def upload_client(mock_tally, tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "JWT_SECRET", None)
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)

    from fastapi import FastAPI

    from backend.api import chat

    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[get_client] = lambda: mock_tally
    app.dependency_overrides[get_current_user] = lambda: "test-user"

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


async def _upload(client, fixture_name):
    with patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message(fixture_name)),
    ):
        return await client.post(
            "/api/chat/upload",
            files={"file": ("doc.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"message": "entry"},
        )


def _gst_names(entry):
    return {e["ledger"] for e in entry["gst_entries"]}


@pytest.mark.asyncio
async def test_gst_purchase_resolves_input_ledgers(upload_client):
    resp = await _upload(upload_client, "purchase_office_inr")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["voucher_type"] == "Purchase"
    assert _gst_names(entry) == {"CGST Input", "SGST Input"}
    # Contra (purchase) leg = base = total − GST; party gross = total.
    gst_total = sum(e["amount"] for e in entry["gst_entries"])
    assert abs(gst_total - 2342.0) < 0.01
    assert entry["amount"] == 15340.0


@pytest.mark.asyncio
async def test_gst_interstate_purchase_resolves_input_igst(upload_client):
    resp = await _upload(upload_client, "purchase_interstate_inr")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert _gst_names(entry) == {"IGST Input"}


@pytest.mark.asyncio
async def test_gst_sales_resolves_output_ledgers(upload_client):
    resp = await _upload(upload_client, "sales_service_inr")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["voucher_type"] == "Sales"
    assert _gst_names(entry) == {"CGST Output", "SGST Output"}
