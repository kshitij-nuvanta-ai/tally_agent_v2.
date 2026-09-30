"""Integration tests — DB audit trail (Group B, Task 14).

Verifies UploadedFile rows are written on file upload and VoucherEntry rows on
successful Tally writes, against a real Postgres session. Gated on
TEST_DATABASE_URL. The Vision call and the TallyWriter are mocked.
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
    # Ensure DB-mode audit branches run regardless of the dev's .env.
    monkeypatch.setattr(
        settings, "DATABASE_URL",
        os.environ["TEST_DATABASE_URL"], raising=False,
    )


@pytest_asyncio.fixture
async def ctx(db_session: AsyncSession):
    user = User(email="audit@example.com", password_hash="h", name="Audit")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    ws = Workspace(user_id=user.id, name="Audit Co", config={"mock_mode": True})
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


async def _upload(db_session, ctx, fixture):
    user, ws, conv = ctx
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, b"\xff\xd8\xff\xe0" + b"\x00" * 64)
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
    ):
        result = await orch.process_file_upload(
            file_path=path, filename="doc.jpg", mime_type="image/jpeg",
            user_message="entry", client=_FakeClient(), session=session,
            file_id="ignored",
            db=db_session, user_id=str(user.id),
            workspace_id=str(ws.id), conversation_id=str(conv.id),
        )
    os.remove(path)
    return result


@pytest.mark.asyncio
async def test_upload_creates_uploaded_file_row(db_session, ctx):
    user, ws, conv = ctx
    await _upload(db_session, ctx, "purchase_service_inr")
    rows = (await db_session.execute(
        select(UploadedFile).where(UploadedFile.workspace_id == ws.id)
    )).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.filename == "doc.jpg"
    assert row.mime_type == "image/jpeg"
    assert row.file_size > 0
    assert row.user_id == user.id
    assert row.status == "extracted"


@pytest.mark.asyncio
async def test_upload_returns_db_file_id(db_session, ctx):
    """The review card's file_id is the DB UploadedFile id, not the random one."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_service_inr")
    file_id = result["data"]["file_id"]
    row = (await db_session.execute(
        select(UploadedFile).where(UploadedFile.id == file_id)
    )).scalar_one_or_none()
    assert row is not None


@pytest.mark.asyncio
async def test_successful_write_creates_voucher_entry(db_session, ctx):
    user, ws, conv = ctx
    upload = await _upload(db_session, ctx, "purchase_service_inr")
    file_id = upload["data"]["file_id"]
    entry = upload["data"]["entries"][0]
    entry["file_id"] = file_id
    entry["conversation_id"] = str(conv.id)

    req = VoucherActionRequest(
        action="approve", entry=entry, workspace_id=str(ws.id),
        session_id=str(conv.id),
    )
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"

    ve = (await db_session.execute(
        select(VoucherEntry).where(VoucherEntry.workspace_id == ws.id)
    )).scalars().all()
    assert len(ve) == 1
    assert ve[0].voucher_type == "Purchase"
    assert ve[0].status == "written"
    assert ve[0].tally_voucher_number == "99"
    assert str(ve[0].file_id) == str(file_id)


@pytest.mark.asyncio
async def test_discard_creates_no_voucher_entry(db_session, ctx):
    user, ws, conv = ctx
    req = VoucherActionRequest(
        action="discard", entry={"id": "x"}, workspace_id=str(ws.id),
        session_id=str(conv.id),
    )
    resp = await voucher_action(
        req, client=TallyClient("localhost", 9000),
        user_id=str(user.id), db=db_session,
    )
    assert resp.data["type"] == "voucher_discarded"
    ve = (await db_session.execute(
        select(VoucherEntry).where(VoucherEntry.workspace_id == ws.id)
    )).scalars().all()
    assert ve == []
