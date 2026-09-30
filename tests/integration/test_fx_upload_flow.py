"""Integration tests — FX upload → review card against a mock Tally HTTP server (T10).

Drives ``POST /api/chat/upload`` through the real FastAPI app, with:
  - Tally reached over HTTP via the aiohttp mock Tally server (``get_client``
    override) — exercises the orchestrator's ledger-fetch round trip.
  - Claude Vision mocked (the upload pipeline's only external LLM call).

This complements two existing suites and deliberately avoids duplicating them:
  - ``tests/unit/test_file_upload_endpoint.py::TestFileUploadFxFields`` already
    covers USD-with-rate / USD-default / USD-no-rate-blocked / INR at the
    endpoint level (against the built-in mock handler, legacy mode). Here we
    re-verify the four review-card states over the HTTP mock-Tally transport
    and add the EUR per-currency-default case the unit suite lacks.
  - ``tests/integration/test_fx_chat_override_flow.py`` covers the chat
    rate-override fast path (DB-gated).

GST note: the upload path calls ``build_payment_voucher_data`` WITHOUT
``gst_ledgers``, so GST legs come out empty through the endpoint. GST-leg
scaling is asserted at the builder level (see ``test_usd_gst_legs_scaled``),
consistent with the spec's fixture-matrix note.
"""
import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from backend.api.dependencies import get_client, get_current_user
from backend.services.voucher_builder import build_payment_voucher_data
from tally_bridge.client import TallyClient
from tests.fixtures import fx_documents as fxdoc
from tests.mocks.mock_tally_server import create_mock_tally_app

_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


@pytest.fixture
async def mock_tally(aiohttp_server):
    """Start a mock Tally HTTP server and return a TallyClient pointing at it."""
    tally_app = create_mock_tally_app()
    server = await aiohttp_server(tally_app)
    client = TallyClient(host="localhost", port=server.port)
    yield client
    await client.close()


@pytest.fixture
async def upload_client(mock_tally, tmp_path, monkeypatch):
    """httpx client bound to a minimal FastAPI app exposing the chat router.

    Runs in legacy (non-DB) mode so auth is bypassed and the upload pipeline
    talks to the mock Tally HTTP server. Vision is mocked per-test.
    """
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
    """Upload a fixture document with Vision mocked to return that fixture."""
    with patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=fxdoc.vision_message(fixture_name)),
    ):
        return await client.post(
            "/api/chat/upload",
            files={"file": ("receipt.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"message": "expense"},
        )


@pytest.mark.asyncio
async def test_usd_with_doc_rate_review_card(upload_client):
    """USD doc with printed rate → INR amount = USD×rate, FX fields + trail, no warning."""
    resp = await _upload(upload_client, "expense_usd_with_rate")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["original_currency"] == "USD"
    assert entry["original_amount"] == 100.0
    assert entry["fx_rate"] == 83.5
    assert entry["amount"] == 8350.0  # 100 × 83.5
    assert "FX: USD 100.00 @ ₹83.50 = ₹8,350.00" in entry["narration"]
    assert not any(
        "default" in w.lower() or "no conversion" in w.lower()
        for w in entry["warnings"]
    )


@pytest.mark.asyncio
async def test_usd_no_rate_uses_default(upload_client, monkeypatch):
    """USD doc, no rate, default configured → default-used warning, amount converted."""
    from backend.config import settings
    monkeypatch.setattr(settings, "FX_DEFAULT_RATES", "USD:80")
    monkeypatch.setattr(settings, "FX_DEFAULT_RATE", 0.0)
    resp = await _upload(upload_client, "expense_usd_no_rate")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["fx_rate"] == 80.0
    assert entry["amount"] == 20000.0  # 250 × 80
    assert any(
        "default" in w.lower() and "use rate" in w.lower() for w in entry["warnings"]
    )


@pytest.mark.asyncio
async def test_eur_no_rate_uses_per_currency_default(upload_client, monkeypatch):
    """EUR doc, no rate → per-currency default (EUR:90) picked, not the USD default."""
    from backend.config import settings
    monkeypatch.setattr(settings, "FX_DEFAULT_RATES", "USD:80,EUR:90")
    monkeypatch.setattr(settings, "FX_DEFAULT_RATE", 0.0)
    resp = await _upload(upload_client, "expense_eur_no_rate")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["original_currency"] == "EUR"
    assert entry["fx_rate"] == 90.0
    assert entry["amount"] == 18000.0  # 200 × 90
    assert "FX: EUR 200.00 @ ₹90.00 = ₹18,000.00" in entry["narration"]
    assert any(
        "default" in w.lower() and "EUR" in w for w in entry["warnings"]
    )


@pytest.mark.asyncio
async def test_usd_no_rate_no_default_blocked(upload_client, monkeypatch):
    """USD doc, no rate, no default → write-blocked warning, amount flagged at 0."""
    from backend.config import settings
    monkeypatch.setattr(settings, "FX_DEFAULT_RATES", "")
    monkeypatch.setattr(settings, "FX_DEFAULT_RATE", 0.0)
    resp = await _upload(upload_client, "expense_usd_no_rate")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["fx_rate"] == 0.0
    assert entry["amount"] == 0.0
    assert any("no conversion rate" in w.lower() for w in entry["warnings"])


@pytest.mark.asyncio
async def test_inr_doc_regression(upload_client):
    """INR doc → rate 1, no FX trail, amount unchanged, no rate warning."""
    resp = await _upload(upload_client, "expense_inr")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["entries"][0]
    assert entry["original_currency"] == "INR"
    assert entry["original_amount"] == 500.0
    assert entry["fx_rate"] == 1.0
    assert entry["amount"] == 500.0
    assert "FX:" not in entry["narration"]
    assert not any("rate" in w.lower() for w in entry["warnings"])


def test_usd_gst_legs_scaled():
    """Builder-level: USD+GST fixture → each GST leg scaled by rate.

    The upload endpoint does not pass gst_ledgers (legs come out empty there),
    so GST-leg scaling is exercised where gst_ledgers CAN be passed: the builder.
    """
    doc = fxdoc.extracted("expense_usd_with_gst")
    assert doc.original_currency == "USD"
    assert doc.fx_rate == 80.0
    result = build_payment_voucher_data(
        doc=doc,
        expense_ledger="Parts Expense",
        payment_ledger="Bank",
        gst_ledgers={"cgst_input": "INPUT CGST", "sgst_input": "INPUT SGST"},
    )
    # total 118 USD × 80 = 9440 INR
    assert result.amount == pytest.approx(9440.0)
    # each GST leg 9 USD × 80 = 720 INR
    assert sorted(e["amount"] for e in result.gst_entries) == [720.0, 720.0]
    # original (foreign) legs preserved for chat-override recompute
    assert sorted(e["amount"] for e in result.original_gst_entries) == [9.0, 9.0]
    assert "FX: USD 118.00 @ ₹80.00 = ₹9,440.00" in result.narration
