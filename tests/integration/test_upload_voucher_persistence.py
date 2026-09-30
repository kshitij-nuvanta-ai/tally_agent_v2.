"""Integration tests — upload + voucher-action Message persistence.

Verifies the fix for the "review cards vanish on reload" bug:
  - POST /chat/upload persists a user Message and an assistant Message
    (whose data is the voucher_review card) so the card survives reload.
  - voucher_action discard / successful write update the originating
    review-card Message's entry status (so it reloads as deleted/written,
    not draft).

Gated on TEST_DATABASE_URL. Vision/Tally are mocked.
"""
import os
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.chat import (
    _update_persisted_voucher_status,
    chat_with_file,
    voucher_action,
)
from backend.api.models import VoucherActionRequest
from backend.db.models import Conversation, Message, User, VoucherEntry, Workspace
from backend.tally_bridge.client import TallyClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)

_SUCCESS = {"success": True, "last_vch_id": "77", "created": 1}


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(
        settings, "DATABASE_URL", os.environ["TEST_DATABASE_URL"], raising=False,
    )


@pytest_asyncio.fixture
async def ctx(db_session: AsyncSession):
    user = User(email="persist@example.com", password_hash="h", name="Persist")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    ws = Workspace(user_id=user.id, name="Persist Co", config={"mock_mode": True})
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    conv = Conversation(user_id=user.id, workspace_id=ws.id, title="t")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return user, ws, conv


class _FakeUploadFile:
    """Minimal stand-in for FastAPI UploadFile."""

    def __init__(self, filename, content_type, contents):
        self.filename = filename
        self.content_type = content_type
        self._contents = contents

    async def read(self):
        return self._contents


def _review_data(entry_id="e1", status="draft", file_id="f1"):
    return {
        "type": "voucher_review",
        "file_id": file_id,
        "entries": [{"id": entry_id, "status": status, "voucher_type": "Purchase"}],
    }


# ----- Change 1: /chat/upload persists Messages -----

@pytest.mark.asyncio
async def test_upload_persists_user_and_assistant_messages(db_session, ctx):
    user, ws, conv = ctx
    upload = _FakeUploadFile("invoice.jpg", "image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    result = {"message": "Review the entry below.", "data": _review_data()}

    with patch(
        "backend.agents.orchestrator.Orchestrator.process_file_upload",
        new=AsyncMock(return_value=result),
    ):
        resp = await chat_with_file(
            file=upload, message="Please book this",
            workspace_id=str(ws.id), conversation_id=str(conv.id),
            client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_review"

    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.created_at)
    )).scalars().all()
    assert len(msgs) == 2
    user_msg, asst_msg = msgs[0], msgs[1]
    assert user_msg.role == "user"
    assert user_msg.content == "Please book this [invoice.jpg]"
    assert asst_msg.role == "assistant"
    assert asst_msg.content == "Review the entry below."
    assert asst_msg.data["type"] == "voucher_review"


@pytest.mark.asyncio
async def test_upload_user_message_format_without_text(db_session, ctx):
    user, ws, conv = ctx
    upload = _FakeUploadFile("scan.pdf", "application/pdf", b"%PDF-1.4\n" + b"0" * 64)
    result = {"message": "Done.", "data": _review_data()}

    with patch(
        "backend.agents.orchestrator.Orchestrator.process_file_upload",
        new=AsyncMock(return_value=result),
    ):
        await chat_with_file(
            file=upload, message="",
            workspace_id=str(ws.id), conversation_id=str(conv.id),
            client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    msgs = (await db_session.execute(
        select(Message).where(Message.conversation_id == conv.id, Message.role == "user")
    )).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].content == "Uploading file [scan.pdf]"


# ----- Change 2: _update_persisted_voucher_status helper -----

@pytest.mark.asyncio
async def test_update_persisted_voucher_status_persists(db_session, ctx):
    user, ws, conv = ctx
    msg = Message(
        conversation_id=conv.id, role="assistant", content="Review",
        data=_review_data(entry_id="e1", status="draft"),
    )
    db_session.add(msg)
    await db_session.commit()

    await _update_persisted_voucher_status(db_session, str(conv.id), "e1", "written")
    await db_session.commit()

    # Read back via a query to confirm the in-place JSONB mutation persisted.
    reread = (await db_session.execute(
        select(Message).where(Message.id == msg.id)
    )).scalar_one()
    await db_session.refresh(reread)
    assert reread.data["entries"][0]["status"] == "written"


@pytest.mark.asyncio
async def test_update_persisted_voucher_status_noop_when_not_found(db_session, ctx):
    user, ws, conv = ctx
    # No matching message — should not raise.
    await _update_persisted_voucher_status(db_session, str(conv.id), "missing", "written")
    await db_session.commit()


# ----- Change 2: voucher_action updates persisted Message -----

@pytest.mark.asyncio
async def test_discard_updates_persisted_message_to_deleted(db_session, ctx):
    user, ws, conv = ctx
    msg = Message(
        conversation_id=conv.id, role="assistant", content="Review",
        data=_review_data(entry_id="e1", status="draft"),
    )
    db_session.add(msg)
    await db_session.commit()

    # conversation_id is threaded via the request body (production shape),
    # NOT injected into the entry dict.
    req = VoucherActionRequest(
        action="discard",
        entry={"id": "e1"},
        workspace_id=str(ws.id), session_id=str(conv.id),
        conversation_id=str(conv.id),
    )
    resp = await voucher_action(
        req, client=TallyClient("localhost", 9000),
        user_id=str(user.id), db=db_session,
    )
    assert resp.data["type"] == "voucher_discarded"

    reread = (await db_session.execute(
        select(Message).where(Message.id == msg.id)
    )).scalar_one()
    await db_session.refresh(reread)
    assert reread.data["entries"][0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_successful_write_updates_persisted_message_to_written(db_session, ctx):
    user, ws, conv = ctx
    msg = Message(
        conversation_id=conv.id, role="assistant", content="Review",
        data=_review_data(entry_id="e1", status="draft"),
    )
    db_session.add(msg)
    await db_session.commit()

    # No conversation_id inside the entry dict — production never puts it there.
    entry = {
        "id": "e1",
        "voucher_type": "Purchase",
        "date": "01-04-2025",
        "party_ledger": "Croma Electronics",
        "debit_ledger": "Purchase Accounts",
        "credit_ledger": "Croma Electronics",
        "amount": 1000.0,
        "narration": "test",
    }
    req = VoucherActionRequest(
        action="approve", entry=entry, workspace_id=str(ws.id),
        session_id=str(conv.id), conversation_id=str(conv.id),
    )
    with patch(
        "backend.services.dedup.find_business_key_duplicate",
        new=AsyncMock(return_value=None),
    ), patch(
        "backend.tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"

    reread = (await db_session.execute(
        select(Message).where(Message.id == msg.id)
    )).scalar_one()
    await db_session.refresh(reread)
    assert reread.data["entries"][0]["status"] == "written"

    # The VoucherEntry audit row (previously a silent no-op because conv_id
    # was always None in production) must now actually persist.
    ves = (await db_session.execute(
        select(VoucherEntry).where(VoucherEntry.conversation_id == conv.id)
    )).scalars().all()
    assert len(ves) == 1
    assert ves[0].status == "written"


@pytest.mark.asyncio
async def test_write_time_dedup_recheck_anchors_on_entry_date(db_session, ctx):
    """C33 (typed date vars): the write-time Tally duplicate re-check must pass
    the entry's own date as the party-voucher window anchor (was a hard-coded
    FY 2025-26 window)."""
    user, ws, conv = ctx
    entry = {
        "id": "e1", "voucher_type": "Purchase", "date": "20260315",
        "party_ledger": "Croma Electronics", "debit_ledger": "Purchase Accounts",
        "credit_ledger": "Croma Electronics", "amount": 1000.0,
        "narration": "test", "reference": "PINV-CRM-009",
    }
    req = VoucherActionRequest(
        action="approve", entry=entry, workspace_id=str(ws.id),
        session_id=str(conv.id), conversation_id=str(conv.id),
    )
    fake = AsyncMock(return_value=None)
    with patch("backend.services.dedup.find_business_key_duplicate", new=fake), patch(
        "backend.tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["doc_date"] == "20260315"


# ----- Change 2 (Defect 2): upload-first conversation gets a title -----

@pytest.mark.asyncio
async def test_upload_first_sets_conversation_title(db_session, ctx):
    """Uploading into a fresh, untitled conversation titles it and bumps order."""
    user, ws, _ = ctx
    # Fresh conversation with no title (upload-first scenario).
    fresh = Conversation(user_id=user.id, workspace_id=ws.id, title=None)
    db_session.add(fresh)
    await db_session.commit()
    await db_session.refresh(fresh)
    assert fresh.title is None
    prior_updated = fresh.updated_at

    upload = _FakeUploadFile("invoice.jpg", "image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    result = {"message": "Review the entry below.", "data": _review_data()}

    with patch(
        "backend.agents.orchestrator.Orchestrator.process_file_upload",
        new=AsyncMock(return_value=result),
    ):
        await chat_with_file(
            file=upload, message="Please book this invoice",
            workspace_id=str(ws.id), conversation_id=str(fresh.id),
            client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )

    reread = (await db_session.execute(
        select(Conversation).where(Conversation.id == fresh.id)
    )).scalar_one()
    await db_session.refresh(reread)
    assert reread.title is not None
    assert reread.title == "Please book this invoice [invoice.jpg]"
    assert reread.updated_at >= prior_updated
