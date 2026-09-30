"""Integration tests for the FX chat rate-override fast path (T5).

Drives ``_chat_db_mode`` directly against a real Postgres session so the full
recompute → persist → ChatResponse flow is exercised. Gated on TEST_DATABASE_URL.
"""
import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.chat import _chat_db_mode
from backend.api.models import ChatRequest
from backend.db.models import Conversation, Message, User, Workspace
from tally_bridge.client import TallyClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)


@pytest_asyncio.fixture
async def conv_ctx(db_session: AsyncSession):
    user = User(email="fx-test@example.com", password_hash="hash", name="FX Test")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    ws = Workspace(user_id=user.id, name="FX Co", config={"mock_mode": True})
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    conv = Conversation(user_id=user.id, workspace_id=ws.id, title="seeded")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return user, ws, conv


def _pending_entry(**overrides):
    entry = {
        "id": "e1",
        "voucher_type": "Payment",
        "date": "20260404",
        "vendor_name": "Acme Inc",
        "amount": 0.0,
        "debit_ledger": "Travel Expenses",
        "credit_ledger": "Cash",
        "narration": "Acme Inc — Consulting",
        "gst_entries": [],
        "status": "draft",
        "warnings": [
            "No conversion rate for USD — reply 'use rate <n>' to set it (entry can't be written yet)."
        ],
        "is_new_ledger": False,
        "suggested_parent": None,
        "original_currency": "USD",
        "original_amount": 100.0,
        "fx_rate": 0.0,
        "original_gst_entries": [{"ledger": "INPUT IGST", "amount": 18.0}],
    }
    entry.update(overrides)
    return entry


@pytest.mark.asyncio
async def test_rate_override_recomputes_from_blocked_state(db_session, conv_ctx):
    user, ws, conv = conv_ctx
    req = ChatRequest(
        message="use rate 90",
        workspace_id=str(ws.id),
        conversation_id=str(conv.id),
        pending_entry=_pending_entry(),
    )
    resp = await _chat_db_mode(req, TallyClient("localhost", 9000), str(user.id), db_session)

    assert resp.data["type"] == "voucher_review"
    entry = resp.data["entries"][0]
    assert entry["amount"] == 9000.0  # 100 × 90
    assert entry["fx_rate"] == 90.0
    assert entry["gst_entries"] == [{"ledger": "INPUT IGST", "amount": 1620.0}]  # 18 × 90
    # FX warning cleared
    assert entry["warnings"] == []
    assert "| FX: USD 100.00 @ ₹90.00 = ₹9,000.00" in entry["narration"]
    # No Tally query was run — chart is None and message confirms the rate
    assert resp.chart is None
    assert "90" in resp.message

    # Assistant message persisted with the voucher_review data
    result = await db_session.execute(
        select(Message).where(Message.conversation_id == conv.id, Message.role == "assistant")
    )
    msgs = result.scalars().all()
    assert len(msgs) == 1
    assert msgs[0].data["entries"][0]["amount"] == 9000.0


@pytest.mark.asyncio
async def test_non_override_message_falls_through_to_query(db_session, conv_ctx):
    user, ws, conv = conv_ctx
    req = ChatRequest(
        message="show me the trial balance",
        workspace_id=str(ws.id),
        conversation_id=str(conv.id),
        pending_entry=_pending_entry(),
    )
    resp = await _chat_db_mode(req, TallyClient("localhost", 9000), str(user.id), db_session)
    # Not a voucher_review override — it went through the normal query path.
    if resp.data is not None and isinstance(resp.data, dict):
        assert resp.data.get("type") != "voucher_review"


@pytest.mark.asyncio
async def test_rate_words_without_number_asks_for_clarification(db_session, conv_ctx):
    user, ws, conv = conv_ctx
    req = ChatRequest(
        message="what rate should I use?",
        workspace_id=str(ws.id),
        conversation_id=str(conv.id),
        pending_entry=_pending_entry(),
    )
    resp = await _chat_db_mode(req, TallyClient("localhost", 9000), str(user.id), db_session)
    assert resp.data is None
    assert "rate" in resp.message.lower()
    # The pending entry was NOT modified/echoed back.
    assert "voucher_review" not in str(resp.data)
