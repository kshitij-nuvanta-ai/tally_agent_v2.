"""Integration tests — invoice Phase 1 (reference + dedup) end-to-end.

Drives Orchestrator.process_file_upload + voucher_action against a real Postgres
session with Vision + the TallyWriter mocked. Gated on TEST_DATABASE_URL.

Covers:
- the review entry carries reference / reference_date / duplicate_of / status.
- B1: same file uploaded twice → 2nd entry status=duplicate, writer NOT called.
- B2: same invoice no via a different file → blocked.
- distinct invoice → writes, with REFERENCE present in the posted XML.
"""
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.context import SessionContext
from backend.agents.orchestrator import Orchestrator
from backend.api.chat import voucher_action
from backend.api.models import VoucherActionRequest
from backend.db.models import Conversation, UploadedFile, User, VoucherEntry, Workspace
from tally_bridge.client import TallyClient
from tests.fixtures import vision_docs

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)

_LEDGERS = [
    {"name": "Croma Electronics", "parent_group": "Sundry Creditors"},
    {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
    {"name": "Cash", "parent_group": "Cash-in-hand"},
]
_SUCCESS = {"success": True, "last_vch_id": "99", "created": 1}


@pytest.fixture(autouse=True)
def _enable_write(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(
        settings, "DATABASE_URL", os.environ["TEST_DATABASE_URL"], raising=False,
    )


@pytest_asyncio.fixture
async def ctx(db_session: AsyncSession):
    user = User(email="ddflow@example.com", password_hash="h", name="DD")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    ws = Workspace(user_id=user.id, name="DD Co", config={"mock_mode": True})
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    conv = Conversation(user_id=user.id, workspace_id=ws.id, title="t")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return user, ws, conv


class _FakeClient:
    async def post_xml(self, xml):
        return "<LEDGERS/>"


async def _upload(db_session, ctx, fixture, content=b"\xff\xd8\xff\xe0PDFBYTESA"):
    user, ws, conv = ctx
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, content)
    os.close(fd)
    orch = Orchestrator()
    session = SessionContext(session_id=str(conv.id))
    with patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message(fixture)),
    ), patch(
        "tally_bridge.response_parser.parse_ledger_list",
        return_value=_LEDGERS,
    ), patch(
        "tally_bridge.queries.vouchers.get_party_vouchers",
        new=AsyncMock(return_value=[]),
    ), patch(
        "backend.services.dedup.get_party_vouchers",
        new=AsyncMock(return_value=[]),
    ):
        result = await orch.process_file_upload(
            file_path=path, filename="doc.jpg", mime_type="image/jpeg",
            user_message="entry", client=_FakeClient(), session=session,
            file_id="ignored", db=db_session, user_id=str(user.id),
            workspace_id=str(ws.id), conversation_id=str(conv.id),
        )
    os.remove(path)
    return result


@pytest.mark.asyncio
async def test_entry_carries_reference_fields(db_session, ctx):
    result = await _upload(db_session, ctx, "purchase_service_inr")
    entry = result["data"]["entries"][0]
    # purchase_service_inr fixture: invoice_number=CRO-2026-5678, date 2026-02-10
    assert entry["reference"] == "CRO-2026-5678"
    assert entry["reference_date"] == "20260210"
    assert entry["status"] == "draft"
    assert entry["duplicate_of"] is None


@pytest.mark.asyncio
async def test_b1_same_file_twice_blocks_second(db_session, ctx):
    user, ws, conv = ctx
    same_bytes = b"\xff\xd8\xff\xe0IDENTICALFILE"
    first = await _upload(db_session, ctx, "purchase_service_inr", content=same_bytes)
    entry1 = first["data"]["entries"][0]
    assert entry1["status"] == "draft"

    # Write the first entry so a prior written VoucherEntry exists (party+ref).
    entry1["conversation_id"] = str(conv.id)
    entry1["file_id"] = first["data"]["file_id"]
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        await voucher_action(
            VoucherActionRequest(action="approve", entry=entry1,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )

    # Upload-time B1: the same file again is flagged "same file".
    second = await _upload(db_session, ctx, "purchase_service_inr", content=same_bytes)
    entry2 = second["data"]["entries"][0]
    assert entry2["status"] == "duplicate"
    assert entry2["duplicate_of"]["reason"] == "same file"

    # Write-time hard block: the server re-derives the B2 party+reference key
    # (matches the now-written entry1) and refuses to write.
    entry2["conversation_id"] = str(conv.id)
    req = VoucherActionRequest(
        action="approve", entry=entry2, workspace_id=str(ws.id),
        session_id=str(conv.id),
    )
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_b2_same_invoice_no_different_file_blocks(db_session, ctx):
    user, ws, conv = ctx
    # First upload + write to create a written VoucherEntry with the reference.
    first = await _upload(db_session, ctx, "purchase_service_inr",
                          content=b"FILE-ONE-BYTES")
    entry1 = first["data"]["entries"][0]
    entry1["conversation_id"] = str(conv.id)
    entry1["file_id"] = first["data"]["file_id"]
    req = VoucherActionRequest(
        action="approve", entry=entry1, workspace_id=str(ws.id),
        session_id=str(conv.id),
    )
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )

    # Second upload: DIFFERENT bytes, SAME invoice no + party → blocked by DB B2.
    second = await _upload(db_session, ctx, "purchase_service_inr",
                           content=b"FILE-TWO-DIFFERENT-BYTES")
    entry2 = second["data"]["entries"][0]
    assert entry2["status"] == "duplicate"
    assert entry2["duplicate_of"]["reason"] == "same invoice no for party"


@pytest.mark.asyncio
async def test_distinct_invoice_writes_with_reference(db_session, ctx):
    user, ws, conv = ctx
    upload = await _upload(db_session, ctx, "purchase_service_inr",
                           content=b"UNIQUE-FILE-XYZ")
    entry = upload["data"]["entries"][0]
    assert entry["status"] == "draft"
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = upload["data"]["file_id"]

    req = VoucherActionRequest(
        action="approve", entry=entry, workspace_id=str(ws.id),
        session_id=str(conv.id),
    )
    posted = {}

    async def _capture(self, **kwargs):
        posted.update(kwargs)
        return _SUCCESS

    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=_capture,
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    assert posted["reference"] == "CRO-2026-5678"
    assert posted["reference_date"] == "20260210"


@pytest.mark.asyncio
async def test_write_time_recheck_blocks_even_with_client_status_draft(db_session, ctx):
    """Finding 1: a known business-key duplicate is blocked at write time even when
    the client lies with status='draft' — the server re-derives from party+reference.
    """
    user, ws, conv = ctx
    # First upload + write to create a written VoucherEntry with the reference.
    first = await _upload(db_session, ctx, "purchase_service_inr", content=b"WT-FILE-ONE")
    entry1 = first["data"]["entries"][0]
    entry1["conversation_id"] = str(conv.id)
    entry1["file_id"] = first["data"]["file_id"]
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        await voucher_action(
            VoucherActionRequest(action="approve", entry=entry1,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )

    # Client re-submits the SAME party+reference but lies: status='draft'.
    dup_entry = dict(entry1)
    dup_entry["id"] = "client-forged"
    dup_entry["status"] = "draft"
    dup_entry.pop("duplicate_of", None)
    dup_entry["file_id"] = None
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        resp = await voucher_action(
            VoucherActionRequest(action="approve", entry=dup_entry,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "duplicate" in resp.message.lower()
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_reference_to_non_duplicate_allows_write(db_session, ctx):
    """Finding 1: editing the reference to a NON-duplicate value lets the write proceed."""
    user, ws, conv = ctx
    first = await _upload(db_session, ctx, "purchase_service_inr", content=b"WT-EDIT-ONE")
    entry1 = first["data"]["entries"][0]
    entry1["conversation_id"] = str(conv.id)
    entry1["file_id"] = first["data"]["file_id"]
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        await voucher_action(
            VoucherActionRequest(action="approve", entry=entry1,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )

    # Second doc with same party but a CORRECTED (different) invoice no.
    second = await _upload(db_session, ctx, "purchase_service_inr", content=b"WT-EDIT-TWO")
    entry2 = second["data"]["entries"][0]
    entry2["conversation_id"] = str(conv.id)
    entry2["file_id"] = second["data"]["file_id"]
    entry2["reference"] = "CRO-2026-9999-CORRECTED"  # user fixed a mis-read invoice no
    entry2["status"] = "duplicate"  # stale client flag — must be ignored

    posted = {}

    async def _capture(self, **kwargs):
        posted.update(kwargs)
        return _SUCCESS

    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=_capture,
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        resp = await voucher_action(
            VoucherActionRequest(action="edit", entry=entry2,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    assert posted["reference"] == "CRO-2026-9999-CORRECTED"


@pytest.mark.asyncio
async def test_write_time_recheck_normalizes_reference(db_session, ctx):
    """Finding 1+2: trimmed/case-different reference still detected at write time."""
    user, ws, conv = ctx
    first = await _upload(db_session, ctx, "purchase_service_inr", content=b"WT-NORM-ONE")
    entry1 = first["data"]["entries"][0]
    entry1["conversation_id"] = str(conv.id)
    entry1["file_id"] = first["data"]["file_id"]
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        await voucher_action(
            VoucherActionRequest(action="approve", entry=entry1,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )

    dup_entry = dict(entry1)
    dup_entry["id"] = "norm-dup"
    dup_entry["status"] = "draft"
    dup_entry["file_id"] = None
    # written ref is "CRO-2026-5678"; resubmit with trailing space + lower case.
    dup_entry["reference"] = "  cro-2026-5678 "
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ):
        resp = await voucher_action(
            VoucherActionRequest(action="approve", entry=dup_entry,
                                 workspace_id=str(ws.id), session_id=str(conv.id)),
            client=TallyClient("localhost", 9000), user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "duplicate" in resp.message.lower()
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_uploaded_file_row_stores_content_hash(db_session, ctx):
    user, ws, conv = ctx
    await _upload(db_session, ctx, "purchase_service_inr", content=b"HASHME")
    row = (await db_session.execute(
        select(UploadedFile).where(UploadedFile.workspace_id == ws.id)
    )).scalars().first()
    assert row.content_hash is not None and len(row.content_hash) == 64
