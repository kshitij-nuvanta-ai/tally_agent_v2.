"""Integration tests — invoice Phase 2 (inventory line items) end-to-end.

Drives Orchestrator.process_file_upload + voucher_action against a real Postgres
session with Vision + stock-item read + the TallyWriter mocked. Gated on
TEST_DATABASE_URL.

Covers:
- a goods purchase (qty-bearing lines, one matched + one new) → entry is
  is_inventory with resolved line_items + available_stock_items.
- voucher_action on that entry → create_stock_item called for the NEW line,
  then the STOCK-based create_purchase_voucher with item tuples + reference.
- a services invoice (no qty) → accounting-only regression (ledger writer).
- sales mirror.
"""
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.context import SessionContext
from backend.agents.orchestrator import Orchestrator
from backend.api.chat import voucher_action
from backend.api.models import VoucherActionRequest
from backend.db.models import Conversation, User, Workspace
from tally_bridge.client import TallyClient
from tally_bridge.models import StockItem
from tests.fixtures import vision_docs

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set",
)

_LEDGERS = [
    {"name": "Croma Electronics", "parent_group": "Sundry Creditors"},
    {"name": "Infosys Ltd", "parent_group": "Sundry Debtors"},
    {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
    {"name": "Sales Accounts", "parent_group": "Sales Accounts"},
    {"name": "Cash", "parent_group": "Cash-in-hand"},
    {"name": "CGST Input", "parent_group": "Duties & Taxes"},
    {"name": "SGST Input", "parent_group": "Duties & Taxes"},
    {"name": "CGST Output", "parent_group": "Duties & Taxes"},
    {"name": "SGST Output", "parent_group": "Duties & Taxes"},
]
_STOCK_ITEMS = [
    StockItem(name="A4 Paper Ream 500 sheets", parent_group="Primary", base_units="Nos"),
    StockItem(name="HP Laptop 15s", parent_group="Primary", base_units="Nos"),
    StockItem(name="Logitech Wireless Mouse", parent_group="Primary", base_units="Nos"),
]
_SUCCESS = {"success": True, "last_vch_id": "777", "created": 1}


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
    user = User(email="invflow@example.com", password_hash="h", name="INV")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    ws = Workspace(user_id=user.id, name="INV Co", config={"mock_mode": True})
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


async def _upload(db_session, ctx, fixture, content=b"\xff\xd8\xff\xe0PDF"):
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
        "backend.services.stock_resolver.list_stock_items",
        new=AsyncMock(return_value=_STOCK_ITEMS),
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
async def test_goods_purchase_entry_is_inventory(db_session, ctx):
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-1")
    entry = result["data"]["entries"][0]
    assert entry["is_inventory"] is True
    assert len(entry["line_items"]) == 2
    by_desc = {li["description"]: li for li in entry["line_items"]}
    assert by_desc["A4 Paper Ream 500 sheets"]["matched_item"] == "A4 Paper Ream 500 sheets"
    assert by_desc["Brother Toner Cartridge TN-2380"]["create_new"] is True
    assert "A4 Paper Ream 500 sheets" in entry["available_stock_items"]


@pytest.mark.asyncio
async def test_goods_purchase_writes_stock_voucher(db_session, ctx):
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-2")
    entry = result["data"]["entries"][0]
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_item, patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    m_item.assert_awaited_once()  # only the new line
    m_vch.assert_awaited_once()
    kw = m_vch.await_args.kwargs
    assert len(kw["items"]) == 2
    assert kw["reference"] == "CRO-2026-9001"
    assert kw["reference_date"] == "20260210"
    assert kw["gst_mode"] == "intra"


@pytest.mark.asyncio
async def test_bill_alloc_gross_matches_builder_party_total(db_session, ctx):
    """Finding 1: the New Ref bill-allocation amount must equal the gross the
    builder computes from the item tuples (Σ qty×rate + GST), NOT Vision's
    extracted total. Reconciles exactly so the voucher balances."""
    from tally_bridge.import_builder import compute_invoice_gross

    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-GROSS")
    entry = result["data"]["entries"][0]
    # Simulate a user edit that diverges from Vision's total: bump a rate so
    # Σ(qty×rate)+GST no longer equals entry["amount"].
    entry["line_items"][0]["rate"] = 600.0  # was 500
    entry["amount"] = 99999.0  # stale Vision total — must NOT be used
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    kw = m_vch.await_args.kwargs
    expected = compute_invoice_gross(kw["items"], kw["gst_mode"])
    assert kw["bill_allocations"][0]["amount"] == expected
    assert expected != 99999.0  # never the stale Vision total


@pytest.mark.asyncio
async def test_preexisting_stock_group_altered_does_not_abort_write(db_session, ctx):
    """BUG 1 (live-caught): a create-new item references a stock group that
    ALREADY EXISTS (empty). Tally answers that group CREATE with
    CREATED=0, ALTERED=1, ERRORS=0, EXCEPTIONS=0 — benign. The inventory write
    must NOT abort with a voucher_error; the voucher must still post.

    The real create_stock_group runs (not mocked); its client returns the
    altered envelope, exercising _assert_master_persisted end-to-end.
    """
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-ALTERED")
    entry = result["data"]["entries"][0]
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))

    _ALTERED = (
        '<RESPONSE>'
        '<CREATED>0</CREATED><ALTERED>1</ALTERED><DELETED>0</DELETED>'
        '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
        '<LASTVCHID>0</LASTVCHID>'
        '</RESPONSE>'
    )

    async def _post_xml(self, xml):
        # The only un-mocked writer call that hits post_xml is create_stock_group.
        return _ALTERED

    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        # create_stock_group is NOT mocked — the real method + _assert_master_persisted run.
        "tally_bridge.client.TallyClient.post_xml",
        new=_post_xml,
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written", resp.data
    m_vch.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_qty_line_blocks_write(db_session, ctx):
    """Finding 2: a line with qty<=0 or rate<=0 must block the write."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-ZEROQTY")
    entry = result["data"]["entries"][0]
    entry["line_items"][0]["qty"] = 0  # zero qty
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "quantity or rate" in resp.message
    m_vch.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_rate_line_blocks_write(db_session, ctx):
    """Finding 2: rate<=0 also blocks."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-ZERORATE")
    entry = result["data"]["entries"][0]
    entry["line_items"][1]["rate"] = 0
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "quantity or rate" in resp.message
    m_vch.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_invoice_unquantified_lines_block_write(db_session, ctx):
    """Finding 3: an inventory invoice where the ORIGINAL doc had a line with no
    qty (silently dropped by resolve_line_items) must block the write and name
    the line(s) needing a quantity."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-MIXED")
    entry = result["data"]["entries"][0]
    # Simulate the orchestrator flag: at least one original line lacked a qty.
    entry["has_unquantified_lines"] = True
    entry["unquantified_descriptions"] = ["Freight charges"]
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "Freight charges" in resp.message
    m_vch.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_quantified_invoice_writes(db_session, ctx):
    """Finding 3 inverse: when every line is quantified (flag false/absent), the
    inventory write proceeds."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-ALLQTY")
    entry = result["data"]["entries"][0]
    assert not entry.get("has_unquantified_lines")
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    m_vch.assert_awaited_once()


@pytest.mark.asyncio
async def test_master_create_failure_returns_clean_voucher_error(db_session, ctx):
    """Inventory-modal fix: a non-already-exists master-create failure (bad
    group, Tally down, transport timeout, a would-be blocking modal) must NOT
    hang or 500 — the caller converts it to a clean voucher_error and never
    reaches the voucher post. (Supersedes the old 'propagate the RuntimeError'
    contract: a raised error used to surface as a 500 / could wedge the gateway.)"""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-PROPAGATE")
    entry = result["data"]["entries"][0]
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    boom = RuntimeError("create_stock_item silently failed: Tally returned EXCEPTIONS=1")
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(side_effect=boom),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_error"
    assert "Brother Toner Cartridge TN-2380" in resp.message
    m_vch.assert_not_awaited()  # never reached the voucher post


@pytest.mark.asyncio
async def test_idempotent_swallows_already_exists(db_session, ctx):
    """Finding 4 inverse: a genuine already-exists failure is swallowed and the
    voucher still posts."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_goods_inr",
                           content=b"GOODS-PURCHASE-EXISTS")
    entry = result["data"]["entries"][0]
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    exists = RuntimeError(
        "create_stock_item silently failed: Stock Item already exists"
    )
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(side_effect=exists),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    m_vch.assert_awaited_once()


@pytest.mark.asyncio
async def test_services_purchase_is_accounting_only(db_session, ctx):
    """Regression: a no-qty purchase stays accounting-only (ledger writer)."""
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "purchase_saas_usd",
                           content=b"SERVICES-PURCHASE-1")
    entry = result["data"]["entries"][0]
    assert entry.get("is_inventory") in (False, None)


@pytest.mark.asyncio
async def test_goods_sales_writes_stock_voucher(db_session, ctx):
    user, ws, conv = ctx
    result = await _upload(db_session, ctx, "sales_goods_inr",
                           content=b"GOODS-SALES-1")
    entry = result["data"]["entries"][0]
    assert entry["is_inventory"] is True
    entry["conversation_id"] = str(conv.id)
    entry["file_id"] = result["data"]["file_id"]
    req = VoucherActionRequest(action="approve", entry=entry,
                               workspace_id=str(ws.id), session_id=str(conv.id))
    with patch(
        "tally_bridge.writer.TallyWriter.create_unit",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_group",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_stock_item",
        new=AsyncMock(return_value=_SUCCESS),
    ), patch(
        "tally_bridge.writer.TallyWriter.create_sales_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_vch, patch(
        "backend.services.dedup.get_party_vouchers", new=AsyncMock(return_value=[]),
    ), patch(
        # The inventory write path reads existing stock items (to avoid CREATEing
        # masters that already exist — the blocking-modal fix). The writer client
        # here is not in mock mode, so patch this read to the known fixture.
        "tally_bridge.queries.masters.list_stock_items",
        new=AsyncMock(return_value=list(_STOCK_ITEMS)),
    ):
        resp = await voucher_action(
            req, client=TallyClient("localhost", 9000),
            user_id=str(user.id), db=db_session,
        )
    assert resp.data["type"] == "voucher_written"
    m_vch.assert_awaited_once()
    assert m_vch.await_args.kwargs["party"] == "Infosys Ltd"
    assert m_vch.await_args.kwargs["items"][0][3] == "Sales Accounts"
