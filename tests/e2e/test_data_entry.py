"""E2E test for expense data entry pipeline (mock mode)."""
import io
import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    # Force legacy (non-DB) mode so auth dependency is a no-op, even if the
    # developer has DATABASE_URL set in .env (Set A1 default).
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "JWT_SECRET", None)
    # Tests that write vouchers need the write flag enabled (default is False).
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    from backend.main import app
    with TestClient(app) as tc:
        yield tc


class TestDataEntryE2E:
    def test_upload_receipt_returns_review_card(self, client):
        """Upload a receipt image → get back a voucher review card."""
        # Mock Claude Vision response
        vision_response_text = json.dumps({
            "doc_type": "expense",
            "vendor_name": "Uber",
            "date": "2026-04-04",
            "total_amount": 500.0,
            "line_items": [{"description": "Ride", "amount": 500.0}],
            "gst": None,
            "payment_mode": "upi",
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=vision_response_text)]

        # Patch the module-level AsyncAnthropic client's messages.create
        # with an AsyncMock so `await` works in process_file_upload.
        with patch(
            "backend.agents.orchestrator.anthropic_client.messages.create",
            new=AsyncMock(return_value=mock_message),
        ):
            file_content = b"\xff\xd8\xff\xe0" + b"\x00" * 200
            response = client.post(
                "/api/chat/upload",
                files={"file": ("receipt.jpg", io.BytesIO(file_content), "image/jpeg")},
                data={"message": "lunch expense"},
            )

        assert response.status_code == 200, response.text
        data = response.json()
        assert "Uber" in data["message"]
        assert data["data"]["type"] == "voucher_review"
        assert len(data["data"]["entries"]) == 1
        entry = data["data"]["entries"][0]
        assert entry["status"] == "draft"
        assert entry["vendor_name"] == "Uber"
        assert entry["amount"] == 500.0

    def test_voucher_approve_writes_to_tally(self, client):
        """Posting approve action should call the writer (mock mode)."""
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": {
                    "id": "test-1",
                    "date": "20260404",
                    "debit_ledger": "Travel Expenses",
                    "credit_ledger": "Cash",
                    "amount": 500.0,
                    "narration": "Uber ride",
                    "gst_entries": [],
                },
                "company": "Test Co",
                "session_id": "test-session",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["data"]["type"] == "voucher_written"
        assert "successfully" in data["message"].lower()

    def test_voucher_discard(self, client):
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "discard",
                "entry": {"id": "test-1"},
                "session_id": "test-session",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["type"] == "voucher_discarded"

    def test_voucher_edit_writes_to_tally(self, client):
        """Edit action should be treated as approve — writes the (possibly edited) entry."""
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "edit",
                "entry": {
                    "id": "test-edit",
                    "date": "20260302",
                    "debit_ledger": "Bank Charges",
                    "credit_ledger": "Cash",
                    "amount": 123.45,
                    "narration": "Edited entry test",
                    "gst_entries": [],
                },
                "company": "Test Co",
                "session_id": "test",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["data"]["type"] == "voucher_written"

    def test_voucher_approve_creates_new_ledger_first(self, client):
        """When is_new_ledger=True, backend must create the ledger before the voucher."""
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": {
                    "id": "test-new",
                    "date": "20260302",
                    "debit_ledger": "Brand New Vendor Inc",
                    "credit_ledger": "Cash",
                    "amount": 100.00,
                    "narration": "First time vendor",
                    "gst_entries": [],
                    "is_new_ledger": True,
                    "suggested_parent": "Indirect Expenses",
                },
                "company": "Test Co",
                "session_id": "test",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        # Mock handler returns success for both ledger and voucher create
        assert data["data"]["type"] == "voucher_written"

    def test_voucher_action_missing_required_field(self, client):
        """Missing action field → 422 validation error."""
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "entry": {"id": "x"},
                "company": "Test Co",
            },
        )
        assert response.status_code == 422

    def test_voucher_action_unknown_action(self, client):
        """Unknown action → 422 from Literal validation."""
        response = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "explode",
                "entry": {"id": "x"},
                "company": "Test Co",
            },
        )
        assert response.status_code == 422


class TestFxDataEntryE2E:
    """End-to-end FX flow (mock Tally + mock Claude) — T10.

    Upload a USD document → review card carries INR amount + FX trail → approve
    writes a Payment voucher with the INR amount. The chat rate-override step of
    the full flow lives in the DB-mode fast path (T5) and is covered in
    ``tests/e2e/test_db_data_entry.py`` (DB-gated) — the legacy chat path does
    not implement it.
    """

    def _upload_usd(self, client, fixture_name="expense_usd_with_rate"):
        from tests.fixtures import fx_documents as fxdoc
        with patch(
            "backend.agents.orchestrator.anthropic_client.messages.create",
            new=AsyncMock(return_value=fxdoc.vision_message(fixture_name)),
        ):
            file_content = b"\xff\xd8\xff\xe0" + b"\x00" * 200
            return client.post(
                "/api/chat/upload",
                files={"file": ("receipt.jpg", io.BytesIO(file_content), "image/jpeg")},
                data={"message": "consulting expense"},
            )

    def test_usd_upload_review_then_approve_writes_inr(self, client):
        """Upload USD doc → INR review card → approve → Payment written with INR amount."""
        # Step 1: upload → review card
        upload_resp = self._upload_usd(client)
        assert upload_resp.status_code == 200, upload_resp.text
        data = upload_resp.json()
        assert data["data"]["type"] == "voucher_review"
        entry = data["data"]["entries"][0]
        # INR conversion + FX trail in narration
        assert entry["original_currency"] == "USD"
        assert entry["original_amount"] == 100.0
        assert entry["fx_rate"] == 83.5
        assert entry["amount"] == 8350.0  # 100 × 83.5
        assert "FX: USD 100.00 @ ₹83.50 = ₹8,350.00" in entry["narration"]

        # Step 2: approve the INR entry → Payment voucher written to (mock) Tally
        approve_resp = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": {
                    "id": entry["id"],
                    "date": entry["date"],
                    "debit_ledger": entry["debit_ledger"],
                    "credit_ledger": entry["credit_ledger"],
                    "amount": entry["amount"],  # INR
                    "narration": entry["narration"],  # carries FX trail
                    "gst_entries": entry["gst_entries"],
                },
                "company": "Test Co",
                "session_id": "fx-e2e",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text
        body = approve_resp.json()
        assert body["data"]["type"] == "voucher_written"
        assert "successfully" in body["message"].lower()

    def test_approve_no_rate_foreign_entry_is_blocked(self, client):
        """Finding 1: approving a foreign entry with no rate (amount 0, fx_rate 0)
        must NOT write — return a voucher_error telling the user to set a rate."""
        with patch(
            "tally_bridge.writer.TallyWriter.create_payment_voucher",
            new=AsyncMock(),
        ) as mock_write:
            resp = client.post(
                "/api/chat/voucher-action",
                json={
                    "action": "approve",
                    "entry": {
                        "id": "no-rate-1",
                        "date": "20260404",
                        "debit_ledger": "Consulting",
                        "credit_ledger": "Cash",
                        "amount": 0.0,
                        "narration": "USD consulting — no rate",
                        "gst_entries": [],
                        "original_currency": "USD",
                        "original_amount": 100.0,
                        "fx_rate": 0.0,
                    },
                    "company": "Test Co",
                    "session_id": "fx-blocked",
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["type"] == "voucher_error"
        assert "rate" in body["message"].lower()
        mock_write.assert_not_called()

    def test_approve_inr_entry_still_writes(self, client):
        """Finding 1 regression: a normal INR/positive entry still writes fine."""
        resp = client.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": {
                    "id": "inr-ok",
                    "date": "20260404",
                    "debit_ledger": "Travel Expenses",
                    "credit_ledger": "Cash",
                    "amount": 500.0,
                    "narration": "INR ride",
                    "gst_entries": [],
                    "original_currency": "INR",
                },
                "company": "Test Co",
                "session_id": "inr-ok",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["type"] == "voucher_written"
