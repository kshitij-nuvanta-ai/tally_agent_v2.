"""E2E tests — Group B voucher-type pipeline in DB mode + audit-row verification.

Requires TEST_DATABASE_URL. Complements the legacy-mode
``tests/e2e/test_data_entry_group_b.py`` by asserting the DB audit trail:

  - Upload persists an ``UploadedFile`` row (status "extracted").
  - A successful write persists a linked ``VoucherEntry`` row (status "written",
    correct ``voucher_type``, ``tally_voucher_number``, and edited ``voucher_data``).
  - Discard creates the ``UploadedFile`` row but NO ``VoucherEntry``.

Only the Claude Vision call is mocked (via ``vision_docs.vision_message`` reusing
the on-disk ``tests/fixtures/vision/*.json`` fixtures); routing, ledger lookup,
party-voucher fetch and the Tally write all run for real against the built-in
mock Tally handler.

The audit ``VoucherEntry`` row is only written when the write entry carries both
``file_id`` and ``conversation_id`` (non-nullable FK) — the frontend threads
these back from the review card. These tests thread them explicitly so the audit
path is genuinely exercised.
"""
import io
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from tests.fixtures import vision_docs

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — skipping DB Group B data entry tests",
)

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


# ---------------------------------------------------------------------------
# App fixture (mirrors tests/e2e/test_db_data_entry.py db_app)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_app(monkeypatch, tmp_path):
    from backend.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setattr(settings, "JWT_SECRET", "a" * 64)
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(
        settings, "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "test-key")
    )

    from backend.db.models import Base
    setup_engine = create_async_engine(_TEST_DB_URL)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from fastapi import FastAPI

    from backend.api import (
        auth,
        chat,
        companies,
        conversations,
        health,
        reports,
        tally_mode,
        usage,
        workspaces,
    )

    app = FastAPI()
    for module in (chat, health, companies, reports, tally_mode,
                   auth, workspaces, conversations, usage):
        app.include_router(module.router, prefix="/api")

    from backend.agents.context import SessionStore
    from backend.tally_bridge.client import TallyClient
    from backend.db.engine import init_engine

    tally_client = TallyClient(host="localhost", port=9000)
    tally_client.mock_mode = True
    app.state.tally_client = tally_client
    app.state.session_store = SessionStore(ttl_minutes=60)
    init_engine(_TEST_DB_URL)

    auth._register_attempts.clear()

    yield app

    auth._register_attempts.clear()
    await tally_client.close()
    from backend.db.engine import close_engine
    await close_engine()

    teardown_engine = create_async_engine(_TEST_DB_URL)
    async with teardown_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await teardown_engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register_and_get_token(client: AsyncClient) -> tuple[str, str]:
    email = f"gb-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post("/api/auth/register", json={
        "email": email, "password": "Str0ng!Pass#99", "name": "GB Tester",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


async def _create_workspace(client: AsyncClient, token: str, name: str) -> str:
    config = {"tally_host": "localhost", "tally_port": 9000,
              "mock_mode": True, "tally_company": "Test Co"}
    resp = await client.post(
        "/api/workspaces",
        json={"name": name, "config": config},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_conversation(client: AsyncClient, token: str, ws_id: str) -> str:
    resp = await client.post(
        f"/api/workspaces/{ws_id}/conversations",
        json={"title": "Group B flow"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _upload(ac, headers, fixture_name, ws_id, conv_id, message="data entry"):
    with patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message(fixture_name)),
    ):
        return await ac.post(
            "/api/chat/upload",
            files={"file": ("doc.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
            data={"message": message, "workspace_id": ws_id, "conversation_id": conv_id},
            headers=headers,
        )


def _write_entry(review_entry, file_id, conv_id, **overrides):
    """Build a voucher-action write payload, threading file_id + conversation_id
    so the VoucherEntry audit row is persisted."""
    payload = {
        "id": review_entry["id"],
        "voucher_type": review_entry["voucher_type"],
        "date": review_entry["date"],
        "debit_ledger": review_entry["debit_ledger"],
        "credit_ledger": review_entry["credit_ledger"],
        "amount": review_entry["amount"],
        "narration": review_entry["narration"],
        "gst_entries": review_entry.get("gst_entries", []),
        "party_ledger": review_entry.get("party_ledger"),
        "bill_reference": review_entry.get("bill_reference"),
        "is_new_ledger": review_entry.get("is_new_ledger", False),
        "suggested_parent": review_entry.get("suggested_parent"),
        "file_id": file_id,
        "conversation_id": conv_id,
    }
    payload.update(overrides)
    return payload


async def _voucher_entries(file_id):
    """Query VoucherEntry rows linked to a given uploaded-file id."""
    import backend.db.engine as engine_mod
    from backend.db.models import VoucherEntry
    async with engine_mod.async_session_factory() as session:
        rows = (await session.execute(
            select(VoucherEntry).where(VoucherEntry.file_id == uuid.UUID(file_id))
        )).scalars().all()
        return rows


async def _uploaded_file(file_id):
    import backend.db.engine as engine_mod
    from backend.db.models import UploadedFile
    async with engine_mod.async_session_factory() as session:
        return (await session.execute(
            select(UploadedFile).where(UploadedFile.id == uuid.UUID(file_id))
        )).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Purchase (INR) → UploadedFile + VoucherEntry audit rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_purchase_upload_write_creates_audit_rows(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Purchase Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_service_inr", ws_id, conv_id, "purchase")
        assert up.status_code == 200, up.text
        body = up.json()
        assert body["data"]["type"] == "voucher_review"
        file_id = body["data"]["file_id"]
        entry = body["data"]["entries"][0]
        assert entry["voucher_type"] == "Purchase"

        # UploadedFile row persisted at upload time.
        uf = await _uploaded_file(file_id)
        assert uf is not None
        assert uf.status == "extracted"
        assert uf.extracted_data["doc_type"] == "purchase"

        # Write → VoucherEntry audit row.
        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id),
                "company": "Test Co",
                "session_id": "db-purchase",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"

        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        ve = rows[0]
        assert ve.voucher_type == "Purchase"
        assert ve.status == "written"
        assert ve.tally_voucher_number  # mock returns a vch id
        assert ve.voucher_data["party_ledger"] == "Croma Electronics"


# ---------------------------------------------------------------------------
# Purchase (USD) → INR amount persisted in the audit row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_purchase_usd_writes_inr_audit_row(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "USD Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_saas_usd", ws_id, conv_id, "usd")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["original_currency"] == "USD"
        inr_amount = entry["amount"]
        assert inr_amount != entry["original_amount"]

        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id),
                "company": "Test Co",
                "session_id": "db-usd",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"

        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        # Audit row records the INR amount that was actually written.
        assert rows[0].voucher_data["amount"] == inr_amount


# ---------------------------------------------------------------------------
# Sales (INR)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_sales_upload_write_creates_audit_rows(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Sales Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "sales_service_inr", ws_id, conv_id, "sales")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Sales"
        assert entry["party_ledger"] == "Infosys Ltd"

        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id),
                "company": "Test Co",
                "session_id": "db-sales",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"
        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        assert rows[0].voucher_type == "Sales"


# ---------------------------------------------------------------------------
# Debit Note (INR) — against-invoice fetched from the real mock handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_debit_note_against_invoice_write(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "DN Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "debit_note_return_inr", ws_id, conv_id, "dn")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Debit Note"
        # Croma is seeded in the mock party-voucher map with a prior Purchase.
        assert entry["against_invoice_options"]
        ref = entry["against_invoice_options"][0]["reference"]

        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id, bill_reference=ref),
                "company": "Test Co",
                "session_id": "db-dn",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"
        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        assert rows[0].voucher_type == "Debit Note"
        assert rows[0].voucher_data["bill_reference"] == ref


# ---------------------------------------------------------------------------
# Credit Note (INR)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_credit_note_upload_write(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "CN Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "credit_note_return_inr", ws_id, conv_id, "cn")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Credit Note"

        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id),
                "company": "Test Co",
                "session_id": "db-cn",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"
        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        assert rows[0].voucher_type == "Credit Note"


# ---------------------------------------------------------------------------
# Discard flow — UploadedFile row created, NO VoucherEntry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_discard_creates_uploaded_file_no_voucher_entry(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Discard Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_service_inr", ws_id, conv_id, "discard")
        file_id = up.json()["data"]["file_id"]
        entry = up.json()["data"]["entries"][0]

        # UploadedFile exists after upload.
        assert await _uploaded_file(file_id) is not None

        # Discard.
        d = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "discard",
                "entry": {"id": entry["id"], "file_id": file_id,
                          "conversation_id": conv_id},
                "session_id": "db-discard",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert d.status_code == 200, d.text
        assert d.json()["data"]["type"] == "voucher_discarded"

        # No VoucherEntry written for a discarded entry.
        assert await _voucher_entries(file_id) == []


# ---------------------------------------------------------------------------
# Edit + write — audit row carries the EDITED values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_edit_then_write_persists_edited_values(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Edit Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_service_inr", ws_id, conv_id, "edit")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]

        edited_amount = 9999.0
        edited = _write_entry(
            entry, file_id, conv_id,
            amount=edited_amount,
            narration="Edited before write",
        )
        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "edit",
                "entry": edited,
                "company": "Test Co",
                "session_id": "db-edit",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"

        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        assert rows[0].voucher_data["amount"] == edited_amount
        assert rows[0].voucher_data["narration"] == "Edited before write"


# ---------------------------------------------------------------------------
# Reclassify — Vision says payment; user edits to Purchase, then writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_reclassify_payment_to_purchase(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Reclassify Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "payment_petty_cash_inr", ws_id, conv_id, "petty")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Payment"

        edited = _write_entry(
            entry, file_id, conv_id,
            voucher_type="Purchase",
            party_ledger="Croma Electronics",
            credit_ledger="Croma Electronics",
            debit_ledger="Purchase - Office Supplies",
            bill_reference="RECLASS-1",
            is_new_ledger=False,
        )
        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "edit",
                "entry": edited,
                "company": "Test Co",
                "session_id": "db-reclassify",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"

        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        # Audit row records the reclassified type, not the original Payment.
        assert rows[0].voucher_type == "Purchase"


# ---------------------------------------------------------------------------
# Regression — original B1a payment/expense path still works + audits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_payment_regression_upload_write(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "Payment Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "payment_petty_cash_inr", ws_id, conv_id, "cab")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Payment"
        assert entry["is_party_ledger"] is False

        w = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "approve",
                "entry": _write_entry(entry, file_id, conv_id),
                "company": "Test Co",
                "session_id": "db-payment",
                "workspace_id": ws_id,
            },
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written"
        rows = await _voucher_entries(file_id)
        assert len(rows) == 1
        assert rows[0].voucher_type == "Payment"


# ---------------------------------------------------------------------------
# Save draft — edited card persists to the Message row WITHOUT writing to Tally
# ---------------------------------------------------------------------------

async def _reload_card_entry(ac, headers, ws_id, conv_id):
    """Re-fetch the conversation and return the first voucher_review entry."""
    reload = await ac.get(
        f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers,
    )
    assert reload.status_code == 200, reload.text
    cards = [
        m for m in reload.json()["messages"]
        if m["role"] == "assistant"
        and isinstance(m["data"], dict)
        and m["data"].get("type") == "voucher_review"
    ]
    assert cards, "no persisted voucher_review card on reload"
    return cards[0]["data"]["entries"][0]


@pytest.mark.asyncio
async def test_db_save_draft_persists_edits_without_tally_write(db_app):
    """Upload a goods purchase → save_draft with an edited reference + line-item
    quantity → the edited values survive a conversation reload, the entry stays
    'draft', NO VoucherEntry audit row is created, and NO Tally write occurs."""
    from unittest.mock import patch as _patch

    from backend.tally_bridge import writer as writer_mod

    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "SaveDraft Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_goods_inr", ws_id, conv_id, "goods")
        assert up.status_code == 200, up.text
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Purchase"
        assert entry.get("line_items"), "fixture should carry line_items"
        # Sanity: the originating reference is NOT our edited value.
        assert entry.get("reference") != "EDITED-REF-999"

        # Build an edited copy: change the reference + the first line's quantity.
        edited = dict(entry)
        edited["reference"] = "EDITED-REF-999"
        edited_lines = [dict(li) for li in entry["line_items"]]
        edited_lines[0]["qty"] = 77
        edited_lines[0]["quantity"] = 77
        edited["line_items"] = edited_lines

        # Guard: a save_draft must NEVER instantiate the Tally writer.
        with _patch.object(
            writer_mod.TallyWriter, "__init__",
            side_effect=AssertionError("save_draft must not write to Tally"),
        ):
            sd = await ac.post(
                "/api/chat/voucher-action",
                json={
                    "action": "save_draft",
                    "entry": edited,
                    "company": "Test Co",
                    "session_id": "db-save-draft",
                    "workspace_id": ws_id,
                    "conversation_id": conv_id,
                },
                headers=headers,
            )
        assert sd.status_code == 200, sd.text
        body = sd.json()
        assert body["data"]["type"] == "voucher_draft_saved", body
        assert body["data"]["entry_id"] == entry["id"]

        # NO audit row was created.
        rows = await _voucher_entries(file_id)
        assert rows == [], f"save_draft must not create a VoucherEntry row, got {rows}"

        # Reload: the persisted card entry now carries the EDITED reference + qty,
        # and the status is still 'draft'.
        reloaded = await _reload_card_entry(ac, headers, ws_id, conv_id)
        assert reloaded["reference"] == "EDITED-REF-999", reloaded
        assert reloaded["status"] == "draft", reloaded
        assert reloaded["line_items"][0]["qty"] == 77, reloaded["line_items"][0]


@pytest.mark.asyncio
async def test_db_save_draft_no_conversation_id_is_graceful_noop(db_app):
    """save_draft with no conversation_id must return success without persisting
    (deferred-creation edge) rather than 400 — the local state still holds."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "SaveDraftNoConv Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_goods_inr", ws_id, conv_id, "goods")
        entry = dict(up.json()["data"]["entries"][0])
        entry["reference"] = "WONT-PERSIST"

        sd = await ac.post(
            "/api/chat/voucher-action",
            json={
                "action": "save_draft",
                "entry": entry,
                "company": "Test Co",
                "session_id": "db-save-draft-noconv",
                "workspace_id": ws_id,
                # conversation_id intentionally omitted
            },
            headers=headers,
        )
        assert sd.status_code == 200, sd.text
        assert sd.json()["data"]["type"] == "voucher_draft_saved"

        # The originating card was NOT mutated (graceful no-op).
        reloaded = await _reload_card_entry(ac, headers, ws_id, conv_id)
        assert reloaded["reference"] != "WONT-PERSIST", reloaded


# ---------------------------------------------------------------------------
# REFRESH ROUND-TRIP tests (CLAUDE.md § "Test reality, not an ideal" rule 7).
#
# Re-fetch the conversation exactly as rehydration does (GET endpoint → messages
# ordered by created_at) and assert the WHOLE user-visible state survived: the
# card's key fields (incl. line-item qty/rate, GST, voucher_type) AND status AND
# the result/assistant messages AND message counts. Covers the goods/line-item
# card, the silent edit→reload, duplicate-block→reload, reclassify→write→reload,
# and the edit→write→reload chain at the Group B (voucher-type) surface.
# ---------------------------------------------------------------------------


async def _reload_messages(ac, headers, ws_id, conv_id):
    reload = await ac.get(
        f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers,
    )
    assert reload.status_code == 200, reload.text
    return reload.json()["messages"]


def _review_cards(messages):
    return [
        m for m in messages
        if m["role"] == "assistant"
        and isinstance(m["data"], dict)
        and m["data"].get("type") == "voucher_review"
    ]


def _result_msgs(messages, data_type):
    return [
        m for m in messages
        if m["role"] == "assistant"
        and isinstance(m["data"], dict)
        and m["data"].get("type") == data_type
    ]


@pytest.mark.asyncio
async def test_db_refresh_roundtrip_goods_card_line_items_intact(db_app):
    """(a) Upload a goods purchase → reload: the card carries ALL its key fields —
    party, amount, voucher_type, reference/invoice_number, GST, and line-item
    qty/rate — not just one attribute, status='draft'."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "RTGoods Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_goods_inr", ws_id, conv_id, "goods")
        assert up.status_code == 200, up.text
        live = up.json()["data"]["entries"][0]
        assert live.get("line_items"), "fixture must carry line items"

        cards = _review_cards(await _reload_messages(ac, headers, ws_id, conv_id))
        assert len(cards) == 1
        e = cards[0]["data"]["entries"][0]

        # WHOLE-state assertion across the card.
        assert e["voucher_type"] == "Purchase"
        assert e["party_ledger"] == live["party_ledger"]
        assert e["amount"] == live["amount"]
        assert e["status"] == "draft"
        # GST survived.
        if live.get("gst_entries"):
            assert e["gst_entries"] == live["gst_entries"]
        # Line items survived with qty + rate intact (served card uses "qty").
        assert len(e["line_items"]) == len(live["line_items"])
        assert e["line_items"][0]["qty"] == live["line_items"][0]["qty"]
        assert e["line_items"][0]["rate"] == live["line_items"][0]["rate"]
        # No terminal message yet.
        assert not _result_msgs(
            await _reload_messages(ac, headers, ws_id, conv_id), "voucher_written"
        )


@pytest.mark.asyncio
async def test_db_refresh_roundtrip_save_draft_whole_state_no_message(db_app):
    """(b) Edit (save_draft) → reload: EVERY edited field persisted (reference AND
    line-item qty AND narration), status stays 'draft', and NO assistant chat
    message was added (save_draft is silent) — and the card count stays at one."""
    from unittest.mock import patch as _patch
    from backend.tally_bridge import writer as writer_mod

    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "RTDraft Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_goods_inr", ws_id, conv_id, "goods")
        entry = up.json()["data"]["entries"][0]

        edited = dict(entry)
        edited["reference"] = "RT-DRAFT-REF-321"
        edited["narration"] = "RT edited narration"
        edited_lines = [dict(li) for li in entry["line_items"]]
        edited_lines[0]["qty"] = 88
        edited_lines[0]["quantity"] = 88
        edited["line_items"] = edited_lines

        with _patch.object(
            writer_mod.TallyWriter, "__init__",
            side_effect=AssertionError("save_draft must not write to Tally"),
        ):
            sd = await ac.post(
                "/api/chat/voucher-action",
                json={
                    "action": "save_draft",
                    "entry": edited,
                    "company": "Test Co",
                    "session_id": "rt-draft",
                    "workspace_id": ws_id,
                    "conversation_id": conv_id,
                },
                headers=headers,
            )
        assert sd.status_code == 200, sd.text
        assert sd.json()["data"]["type"] == "voucher_draft_saved"

        messages = await _reload_messages(ac, headers, ws_id, conv_id)
        cards = _review_cards(messages)
        assert len(cards) == 1
        e = cards[0]["data"]["entries"][0]
        # Every edited field survived.
        assert e["reference"] == "RT-DRAFT-REF-321"
        assert e["narration"] == "RT edited narration"
        assert e["line_items"][0]["quantity"] == 88
        assert e["status"] == "draft"
        # Silent: NO draft-saved assistant message persisted.
        assert not _result_msgs(messages, "voucher_draft_saved")


@pytest.mark.asyncio
async def test_db_refresh_roundtrip_duplicate_block_card_and_message(db_app):
    """(e) Duplicate block → reload: the second (duplicate) entry is NOT written,
    its duplicate/error assistant message persisted exactly once, and that card
    remains in a non-written state."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "RTDup Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        entry_payload = {
            "id": "rt-dup-1",
            "voucher_type": "Purchase",
            "date": "20260210",
            "debit_ledger": "Purchase - Electronics",
            "credit_ledger": "Acme Supplies",
            "party_ledger": "Acme Supplies",
            "amount": 5000.0,
            "narration": "Purchase from Acme",
            "gst_entries": [],
            "reference": "RT-INV-DUP-88",
            "bill_reference": "RT-INV-DUP-88",
            "conversation_id": conv_id,
        }

        with patch(
            "backend.tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
            new=AsyncMock(return_value={"success": True, "last_vch_id": "301", "created": 1}),
        ):
            first = await ac.post(
                "/api/chat/voucher-action",
                json={"action": "approve", "entry": dict(entry_payload),
                      "company": "Test Co", "session_id": "rt-dup",
                      "workspace_id": ws_id, "conversation_id": conv_id},
                headers=headers,
            )
            assert first.status_code == 200, first.text
            assert first.json()["data"]["type"] == "voucher_written", first.text

            second = dict(entry_payload)
            second["id"] = "rt-dup-2"
            dup = await ac.post(
                "/api/chat/voucher-action",
                json={"action": "approve", "entry": second,
                      "company": "Test Co", "session_id": "rt-dup",
                      "workspace_id": ws_id, "conversation_id": conv_id},
                headers=headers,
            )
        assert dup.status_code == 200, dup.text
        assert dup.json()["data"]["type"] == "voucher_error", dup.text
        assert "duplicate" in dup.json()["message"].lower()

        messages = await _reload_messages(ac, headers, ws_id, conv_id)
        # The duplicate-block message persisted exactly once.
        errors = [m for m in _result_msgs(messages, "voucher_error")
                  if "duplicate" in m["content"].lower()]
        assert len(errors) == 1, f"expected 1 duplicate message, got {len(errors)}"
        # Exactly one write succeeded (one voucher_written result message).
        assert len(_result_msgs(messages, "voucher_written")) == 1


@pytest.mark.asyncio
async def test_db_refresh_roundtrip_reclassify_then_write(db_app):
    """(f) Reclassify then write → reload: Vision says Payment; user reclassifies
    to Purchase (persisted via save_draft — the production sequence: inline edit
    fires save_draft, THEN "Write to Tally" fires approve) and writes. The
    reloaded card reflects the NEW voucher_type AND status 'written', and the
    success result message persisted exactly once."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "RTReclass Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "payment_petty_cash_inr", ws_id, conv_id, "petty")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        assert entry["voucher_type"] == "Payment"

        edited = _write_entry(
            entry, file_id, conv_id,
            voucher_type="Purchase",
            party_ledger="Croma Electronics",
            credit_ledger="Croma Electronics",
            debit_ledger="Purchase - Office Supplies",
            bill_reference="RT-RECLASS-1",
            is_new_ledger=False,
        )

        # Production sequence step 1: inline reclassify is persisted via save_draft
        # (this is what swaps the WHOLE entry into the card, incl. voucher_type).
        sd = await ac.post(
            "/api/chat/voucher-action",
            json={"action": "save_draft", "entry": edited, "company": "Test Co",
                  "session_id": "rt-reclass", "workspace_id": ws_id,
                  "conversation_id": conv_id},
            headers=headers,
        )
        assert sd.status_code == 200, sd.text
        assert sd.json()["data"]["type"] == "voucher_draft_saved"

        # Production sequence step 2: "Write to Tally" fires approve.
        w = await ac.post(
            "/api/chat/voucher-action",
            json={"action": "approve", "entry": edited, "company": "Test Co",
                  "session_id": "rt-reclass", "workspace_id": ws_id},
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written", w.text

        messages = await _reload_messages(ac, headers, ws_id, conv_id)
        cards = _review_cards(messages)
        assert len(cards) == 1
        e = cards[0]["data"]["entries"][0]
        assert e["voucher_type"] == "Purchase", (
            f"reload shows the original Payment, not reclassified: {e.get('voucher_type')!r}"
        )
        assert e["status"] == "written", e
        assert len(_result_msgs(messages, "voucher_written")) == 1


@pytest.mark.asyncio
async def test_db_refresh_roundtrip_edit_then_write_shows_edited_values(db_app):
    """(g) The full chain that bit the user, at the Group B surface: upload goods
    purchase → save_draft with edited reference + line-item qty → approve(write
    success) → reload → the WRITTEN card shows the EDITED values (not the original
    upload values), status 'written', and the success message present once."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        token, _ = await _register_and_get_token(ac)
        ws_id = await _create_workspace(ac, token, "RTEditWrite Co")
        conv_id = await _create_conversation(ac, token, ws_id)
        headers = {"Authorization": f"Bearer {token}"}

        up = await _upload(ac, headers, "purchase_goods_inr", ws_id, conv_id, "goods")
        entry = up.json()["data"]["entries"][0]
        file_id = up.json()["data"]["file_id"]
        entry_id = entry["id"]

        # save_draft: edit reference + first line qty.
        edited = dict(entry)
        edited["reference"] = "RT-GB-EDIT-777"
        edited_lines = [dict(li) for li in entry["line_items"]]
        edited_lines[0]["qty"] = 33
        edited_lines[0]["quantity"] = 33
        edited["line_items"] = edited_lines
        sd = await ac.post(
            "/api/chat/voucher-action",
            json={"action": "save_draft", "entry": edited, "company": "Test Co",
                  "session_id": "rt-gb-editwrite", "workspace_id": ws_id,
                  "conversation_id": conv_id},
            headers=headers,
        )
        assert sd.status_code == 200, sd.text
        assert sd.json()["data"]["type"] == "voucher_draft_saved"

        # approve(write) the edited entry.
        write_payload = _write_entry(edited, file_id, conv_id)
        write_payload["id"] = entry_id
        w = await ac.post(
            "/api/chat/voucher-action",
            json={"action": "approve", "entry": write_payload, "company": "Test Co",
                  "session_id": "rt-gb-editwrite", "workspace_id": ws_id},
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["data"]["type"] == "voucher_written", w.text

        messages = await _reload_messages(ac, headers, ws_id, conv_id)
        cards = _review_cards(messages)
        assert len(cards) == 1
        e = cards[0]["data"]["entries"][0]
        assert e["reference"] == "RT-GB-EDIT-777", (
            f"reload shows original (not edited) reference: {e.get('reference')!r}"
        )
        assert e["line_items"][0]["quantity"] == 33, e["line_items"][0]
        assert e["status"] == "written", e
        assert len(_result_msgs(messages, "voucher_written")) == 1
