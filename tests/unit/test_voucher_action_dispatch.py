"""Unit tests — voucher_action dispatches by voucher_type (Group B, Task 8).

The /chat/voucher-action endpoint must call the correct TallyWriter method for
each voucher_type, passing party_ledger / contra ledger / bill_ref / gst as
appropriate, and preserve the existing Payment path + the FX no-rate guard.
The writer methods are mocked; we assert which method got called and with what.
"""
import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "JWT_SECRET", None)
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    from backend.main import app
    with TestClient(app) as tc:
        yield tc


_SUCCESS = {"success": True, "last_vch_id": "42", "created": 1}


def _post(client, entry, action="approve"):
    return client.post(
        "/api/chat/voucher-action",
        json={"action": action, "entry": entry, "company": "Test Co",
              "session_id": "s"},
    )


def test_payment_dispatch_calls_create_payment_voucher(client):
    entry = {
        "id": "p1", "voucher_type": "Payment", "date": "20260404",
        "debit_ledger": "Travel Expenses", "credit_ledger": "Cash",
        "amount": 500.0, "narration": "Uber", "gst_entries": [],
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_payment_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m.assert_awaited_once()
    assert m.await_args.kwargs["debit_ledger"] == "Travel Expenses"


def test_purchase_dispatch_calls_purchase_writer(client):
    entry = {
        "id": "pu1", "voucher_type": "Purchase", "date": "20260210",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Croma Electronics",
        "party_ledger": "Croma Electronics", "amount": 15340.0,
        "narration": "Croma purchase", "gst_entries": [],
        "bill_reference": "CRO-5678", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert "purchase" in resp.json()["message"].lower()
    m.assert_awaited_once()
    kw = m.await_args.kwargs
    assert kw["party_ledger"] == "Croma Electronics"
    assert kw["purchase_ledger"] == "Purchase Accounts"
    assert kw["bill_ref"] == "CRO-5678"


def test_sales_dispatch_calls_sales_writer(client):
    entry = {
        "id": "sa1", "voucher_type": "Sales", "date": "20260301",
        "debit_ledger": "Infosys Ltd", "credit_ledger": "Sales Accounts",
        "party_ledger": "Infosys Ltd", "amount": 118000.0,
        "narration": "Sale", "gst_entries": [],
        "bill_reference": "INV-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_sales_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert "sales" in resp.json()["message"].lower()
    m.assert_awaited_once()
    kw = m.await_args.kwargs
    assert kw["party_ledger"] == "Infosys Ltd"
    assert kw["sales_ledger"] == "Sales Accounts"


def test_debit_note_dispatch_calls_create_debit_note(client):
    # Correct DN convention (live test 2026-06-09): party on DEBIT, purchase-
    # returns contra on CREDIT. chat.py dispatches purchase_ledger=credit_ledger.
    entry = {
        "id": "dn1", "voucher_type": "Debit Note", "date": "20260305",
        "debit_ledger": "Croma Electronics", "credit_ledger": "Purchase Returns",
        "party_ledger": "Croma Electronics", "amount": 4718.0,
        "narration": "Return", "gst_entries": [],
        "bill_reference": "CRO-5678", "bill_type": "Agst Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_debit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert "debit note" in resp.json()["message"].lower()
    m.assert_awaited_once()
    kw = m.await_args.kwargs
    assert kw["party_ledger"] == "Croma Electronics"
    assert kw["purchase_ledger"] == "Purchase Returns"
    assert kw["party_ledger"] != kw["purchase_ledger"]
    assert kw["bill_ref"] == "CRO-5678"


def test_credit_note_dispatch_calls_create_credit_note(client):
    # Correct CN convention (live test 2026-06-09): party on CREDIT, sales-
    # returns contra on DEBIT. chat.py dispatches sales_ledger=debit_ledger.
    entry = {
        "id": "cn1", "voucher_type": "Credit Note", "date": "20260315",
        "debit_ledger": "Sales Returns", "credit_ledger": "Infosys Ltd",
        "party_ledger": "Infosys Ltd", "amount": 11800.0,
        "narration": "Discount CN", "gst_entries": [],
        "bill_reference": "INV-FEB-001", "bill_type": "Agst Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_credit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert "credit note" in resp.json()["message"].lower()
    m.assert_awaited_once()
    kw = m.await_args.kwargs
    assert kw["party_ledger"] == "Infosys Ltd"
    assert kw["sales_ledger"] == "Sales Returns"
    assert kw["party_ledger"] != kw["sales_ledger"]
    assert kw["bill_ref"] == "INV-FEB-001"


def _inventory_purchase_entry():
    return {
        "id": "inv1", "voucher_type": "Purchase", "date": "20260210",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Croma Electronics",
        "party_ledger": "Croma Electronics", "amount": 70210.0,
        "narration": "Goods purchase", "gst_entries": [
            {"ledger": "CGST Input", "amount": 5355.0},
            {"ledger": "SGST Input", "amount": 5355.0},
        ],
        "bill_reference": "CRO-9001", "bill_type": "New Ref",
        "reference": "CRO-9001", "reference_date": "20260210",
        "is_inventory": True,
        "default_stock_group": "Primary",
        "available_stock_items": ["A4 Paper Ream 500 sheets"],
        "line_items": [
            {"description": "A4 Paper Ream 500 sheets", "qty": 10.0, "rate": 500.0,
             "unit": "Nos", "gst_rate": 18.0, "amount": 5000.0,
             "matched_item": "A4 Paper Ream 500 sheets", "create_new": False,
             "stock_name": "A4 Paper Ream 500 sheets", "stock_group": "Primary",
             "hsn": "", "ledger": "Purchase Accounts"},
            {"description": "Brother Toner Cartridge", "qty": 5.0, "rate": 10900.0,
             "unit": "Nos", "gst_rate": 18.0, "amount": 54500.0,
             "matched_item": None, "create_new": True,
             "stock_name": "Brother Toner Cartridge", "stock_group": "Primary",
             "hsn": "8443", "ledger": "Purchase Accounts"},
        ],
    }


def test_inventory_purchase_creates_new_stock_item_then_voucher(client):
    """An inventory purchase: the new line's stock item is created (idempotent),
    then the STOCK-based create_purchase_voucher is called with item tuples."""
    entry = _inventory_purchase_entry()
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
    ) as m_vch:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    # Only the NEW line gets a stock-item create.
    m_item.assert_awaited_once()
    assert m_item.await_args.kwargs.get("name", m_item.await_args.args[0] if m_item.await_args.args else None) == "Brother Toner Cartridge" or \
        "Brother Toner Cartridge" in m_item.await_args.args
    # Stock voucher built with item tuples + reference.
    m_vch.assert_awaited_once()
    kw = m_vch.await_args.kwargs
    items = kw["items"]
    assert len(items) == 2
    names = {t[0] for t in items}
    assert names == {"A4 Paper Ream 500 sheets", "Brother Toner Cartridge"}
    # tuple shape: (name, qty, rate, ledger, uom, gst_rate)
    a4 = next(t for t in items if t[0] == "A4 Paper Ream 500 sheets")
    assert a4[1] == 10.0 and a4[2] == 500.0 and a4[3] == "Purchase Accounts"
    assert a4[4] == "Nos" and a4[5] == 18
    assert kw["gst_mode"] == "intra"
    assert kw["reference"] == "CRO-9001"
    assert kw["reference_date"] == "20260210"
    assert kw["party"] == "Croma Electronics"


def test_inventory_purchase_skips_create_for_matched_only(client):
    """When all lines match existing items, no stock item is created."""
    entry = _inventory_purchase_entry()
    entry["line_items"] = [entry["line_items"][0]]  # only the matched line
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
    ) as m_vch:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    m_item.assert_not_awaited()
    m_vch.assert_awaited_once()


def test_inventory_sales_calls_stock_sales_voucher(client):
    entry = _inventory_purchase_entry()
    entry.update({
        "voucher_type": "Sales", "party_ledger": "Infosys Ltd",
        "debit_ledger": "Infosys Ltd", "credit_ledger": "Sales Accounts",
        "gst_entries": [
            {"ledger": "IGST Output", "amount": 12600.0},
        ],
    })
    for li in entry["line_items"]:
        li["ledger"] = "Sales Accounts"
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
    ) as m_vch:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    m_vch.assert_awaited_once()
    kw = m_vch.await_args.kwargs
    assert kw["gst_mode"] == "inter"  # IGST → interstate
    assert kw["items"][0][3] == "Sales Accounts"


def test_non_inventory_purchase_uses_ledger_writer(client):
    """Regression: a Purchase without is_inventory still uses the ledger writer."""
    entry = {
        "id": "pu9", "voucher_type": "Purchase", "date": "20260210",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Croma Electronics",
        "party_ledger": "Croma Electronics", "amount": 15340.0,
        "narration": "Service", "gst_entries": [],
        "bill_reference": "CRO-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_stock:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    m_ledger.assert_awaited_once()
    m_stock.assert_not_awaited()


def test_debit_note_edit_form_mapping_keeps_legs_distinct(client):
    """Contract guard: an edit-form DN entry (party on DEBIT, purchase-returns on
    CREDIT — the correct return polarity) must dispatch with the party as
    party_ledger and the CONTRA (credit_ledger) as purchase_ledger, kept distinct.
    """
    entry = {
        "id": "dn2", "voucher_type": "Debit Note", "date": "20260305",
        # correct edit-form convention: debit = party, credit = returns ledger
        "debit_ledger": "Croma Electronics", "credit_ledger": "Purchase Returns",
        "party_ledger": "Croma Electronics", "amount": 4718.0,
        "narration": "Return", "gst_entries": [],
        "bill_reference": "CRO-5678", "bill_type": "Agst Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_debit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    kw = m.await_args.kwargs
    assert kw["purchase_ledger"] == "Purchase Returns"
    assert kw["party_ledger"] == "Croma Electronics"
    assert kw["purchase_ledger"] != kw["party_ledger"]


def test_credit_note_edit_form_mapping_keeps_legs_distinct(client):
    """Contract guard: an edit-form CN entry (party on CREDIT, sales-returns on
    DEBIT — the correct return polarity) must dispatch with the party as
    party_ledger and the CONTRA (debit_ledger) as sales_ledger, kept distinct.
    """
    entry = {
        "id": "cn2", "voucher_type": "Credit Note", "date": "20260315",
        # correct edit-form convention: debit = returns ledger, credit = party
        "debit_ledger": "Sales Returns", "credit_ledger": "Infosys Ltd",
        "party_ledger": "Infosys Ltd", "amount": 11800.0,
        "narration": "Discount CN", "gst_entries": [],
        "bill_reference": "INV-FEB-001", "bill_type": "Agst Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_credit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    kw = m.await_args.kwargs
    assert kw["sales_ledger"] == "Sales Returns"
    assert kw["party_ledger"] == "Infosys Ltd"
    assert kw["sales_ledger"] != kw["party_ledger"]


def test_default_voucher_type_is_payment(client):
    """Legacy entries without voucher_type still write a Payment (regression)."""
    entry = {
        "id": "x", "date": "20260404", "debit_ledger": "Travel", "credit_ledger": "Cash",
        "amount": 500.0, "narration": "n", "gst_entries": [],
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_payment_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    m.assert_awaited_once()


@pytest.mark.parametrize(
    "voucher_type,writer_method",
    [
        ("Purchase", "create_purchase_voucher_ledger"),
        ("Sales", "create_sales_voucher_ledger"),
        ("Debit Note", "create_debit_note"),
        ("Credit Note", "create_credit_note"),
    ],
)
def test_empty_party_ledger_blocks_party_voucher(client, voucher_type, writer_method):
    """Finding 3: a party voucher with empty/missing party_ledger must return a
    voucher_error and NOT call the writer.
    """
    entry = {
        "id": "np1", "voucher_type": voucher_type, "date": "20260301",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Sales Accounts",
        "party_ledger": "", "amount": 1000.0, "narration": "n", "gst_entries": [],
    }
    with patch(
        f"tally_bridge.writer.TallyWriter.{writer_method}",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_error"
    assert "party ledger" in resp.json()["message"].lower()
    m.assert_not_awaited()


@pytest.mark.parametrize(
    "voucher_type,writer_method,extra",
    [
        ("Payment", "create_payment_voucher", {}),
        ("Purchase", "create_purchase_voucher_ledger", {"party_ledger": "Acme"}),
        ("Sales", "create_sales_voucher_ledger", {"party_ledger": "Acme"}),
        ("Debit Note", "create_debit_note", {"party_ledger": "Acme"}),
        ("Credit Note", "create_credit_note", {"party_ledger": "Acme"}),
    ],
)
def test_reference_passed_to_writer(client, voucher_type, writer_method, extra):
    """Phase 1 Part A: the supplier invoice no/date threads through to every writer."""
    entry = {
        "id": "ref1", "voucher_type": voucher_type, "date": "20260210",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Sales Accounts",
        "amount": 1000.0, "narration": "n", "gst_entries": [],
        "reference": "SUP-INV-77", "reference_date": "20260210",
        **extra,
    }
    with patch(
        f"tally_bridge.writer.TallyWriter.{writer_method}",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    kw = m.await_args.kwargs
    assert kw["reference"] == "SUP-INV-77"
    assert kw["reference_date"] == "20260210"


@pytest.mark.parametrize("action", ["approve", "edit"])
def test_client_sent_duplicate_status_is_not_trusted_in_legacy(client, action):
    """Phase 1 Part B / Finding 1: the server NEVER trusts the client-sent
    ``status``. The authoritative duplicate block is the server-side re-derive
    (DB mode only — covered in tests/integration/test_invoice_dedup_flow.py). In
    legacy mode (no DB) there is no persistence to re-derive against, so a
    client-asserted status=duplicate must NOT by itself block a write.
    """
    entry = {
        "id": "dup1", "voucher_type": "Purchase", "date": "20260210",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Acme",
        "party_ledger": "Acme", "amount": 1000.0, "narration": "n",
        "gst_entries": [], "status": "duplicate",
        "duplicate_of": {"voucher_no": "P-1", "date": "20260201", "reason": "same file"},
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry, action=action)
    assert resp.status_code == 200, resp.text
    # Client status is ignored; the write proceeds (no server-side DB re-derive
    # available in legacy mode).
    assert resp.json()["data"]["type"] == "voucher_written"
    m.assert_awaited_once()


def test_valid_party_ledger_still_writes(client):
    """Finding 3 happy path: a Purchase WITH a party_ledger still writes."""
    entry = {
        "id": "ok1", "voucher_type": "Purchase", "date": "20260301",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Acme",
        "party_ledger": "Acme", "amount": 1000.0, "narration": "n", "gst_entries": [],
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m.assert_awaited_once()


def test_new_ledger_purchase_creates_party_not_debit_ledger(client):
    """BUG fix: for a Purchase with is_new_ledger, the ledger to create is the
    PARTY (Sundry Creditors), NOT the debit_ledger (the purchase account, which
    already exists). Using debit_ledger re-parents the existing purchase ledger.
    """
    entry = {
        "id": "nl1", "voucher_type": "Purchase", "date": "20260210",
        "debit_ledger": "Purchase - Electronics", "credit_ledger": "Brand New Supplier Co",
        "party_ledger": "Brand New Supplier Co", "amount": 15340.0,
        "narration": "Purchase from new supplier", "gst_entries": [],
        "is_new_ledger": True, "suggested_parent": "Sundry Creditors",
        "bill_reference": "NS-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m_ledger.assert_awaited_once()
    kw = m_ledger.await_args.kwargs
    name = kw.get("name", m_ledger.await_args.args[0] if m_ledger.await_args.args else None)
    parent = kw.get("parent", m_ledger.await_args.args[1] if len(m_ledger.await_args.args) > 1 else None)
    assert name == "Brand New Supplier Co"
    assert name != "Purchase - Electronics"
    assert parent == "Sundry Creditors"
    # BUG fix: party ledger (Sundry Creditor) MUST be bill-wise so the payable
    # carries a bill reference (New Ref) for future "pay against bill".
    is_billwise = kw.get(
        "is_billwise",
        m_ledger.await_args.args[2] if len(m_ledger.await_args.args) > 2 else None,
    )
    assert is_billwise is True


def test_new_ledger_sales_creates_billwise_party(client):
    """BUG fix: for a Sales with is_new_ledger, the new PARTY ledger (Sundry
    Debtor) MUST be bill-wise so the receivable carries a bill reference."""
    entry = {
        "id": "nl_s", "voucher_type": "Sales", "date": "20260210",
        "debit_ledger": "Brand New Customer Co", "credit_ledger": "Sales - Electronics",
        "party_ledger": "Brand New Customer Co", "amount": 15340.0,
        "narration": "Sale to new customer", "gst_entries": [],
        "is_new_ledger": True, "suggested_parent": "Sundry Debtors",
        "bill_reference": "NS-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_sales_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m_ledger.assert_awaited_once()
    kw = m_ledger.await_args.kwargs
    parent = kw.get("parent", m_ledger.await_args.args[1] if len(m_ledger.await_args.args) > 1 else None)
    assert parent == "Sundry Debtors"
    is_billwise = kw.get(
        "is_billwise",
        m_ledger.await_args.args[2] if len(m_ledger.await_args.args) > 2 else None,
    )
    assert is_billwise is True


def test_new_ledger_debit_note_creates_billwise_party(client):
    """BUG fix: Debit Note with is_new_ledger creates a Sundry Creditor party
    ledger that MUST be bill-wise."""
    entry = {
        "id": "nl_dn", "voucher_type": "Debit Note", "date": "20260210",
        "debit_ledger": "Brand New Supplier Co", "credit_ledger": "Purchase Returns",
        "party_ledger": "Brand New Supplier Co", "amount": 1000.0,
        "narration": "Debit note to new supplier", "gst_entries": [],
        "is_new_ledger": True, "suggested_parent": "Sundry Creditors",
        "bill_reference": "DN-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_debit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m_ledger.assert_awaited_once()
    kw = m_ledger.await_args.kwargs
    is_billwise = kw.get(
        "is_billwise",
        m_ledger.await_args.args[2] if len(m_ledger.await_args.args) > 2 else None,
    )
    assert is_billwise is True


def test_new_ledger_credit_note_creates_billwise_party(client):
    """BUG fix: Credit Note with is_new_ledger creates a Sundry Debtor party
    ledger that MUST be bill-wise."""
    entry = {
        "id": "nl_cn", "voucher_type": "Credit Note", "date": "20260210",
        "debit_ledger": "Sales Returns", "credit_ledger": "Brand New Customer Co",
        "party_ledger": "Brand New Customer Co", "amount": 1000.0,
        "narration": "Credit note to new customer", "gst_entries": [],
        "is_new_ledger": True, "suggested_parent": "Sundry Debtors",
        "bill_reference": "CN-1", "bill_type": "New Ref",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_credit_note",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m_ledger.assert_awaited_once()
    kw = m_ledger.await_args.kwargs
    is_billwise = kw.get(
        "is_billwise",
        m_ledger.await_args.args[2] if len(m_ledger.await_args.args) > 2 else None,
    )
    assert is_billwise is True


def test_new_ledger_payment_still_creates_debit_ledger(client):
    """For a Payment with is_new_ledger, the new ledger remains the expense
    (debit) ledger — the mapping describes the debit side for Payment."""
    entry = {
        "id": "nl2", "voucher_type": "Payment", "date": "20260404",
        "debit_ledger": "Brand New Expense", "credit_ledger": "Cash",
        "amount": 500.0, "narration": "New expense", "gst_entries": [],
        "is_new_ledger": True, "suggested_parent": "Indirect Expenses",
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m_ledger, patch(
        "tally_bridge.writer.TallyWriter.create_payment_voucher",
        new=AsyncMock(return_value=_SUCCESS),
    ):
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_written"
    m_ledger.assert_awaited_once()
    kw = m_ledger.await_args.kwargs
    name = kw.get("name", m_ledger.await_args.args[0] if m_ledger.await_args.args else None)
    assert name == "Brand New Expense"
    # Regression guard: expense (Indirect Expenses) ledgers must stay
    # non-bill-wise.
    is_billwise = kw.get(
        "is_billwise",
        m_ledger.await_args.args[2] if len(m_ledger.await_args.args) > 2 else None,
    )
    assert is_billwise is not True


def test_fx_no_rate_guard_blocks_foreign_write(client):
    """Foreign-currency entry with no rate must NOT call any writer."""
    entry = {
        "id": "fx", "voucher_type": "Purchase", "date": "20260301",
        "debit_ledger": "Purchase Accounts", "credit_ledger": "Acme",
        "party_ledger": "Acme", "amount": 0.0, "narration": "n",
        "gst_entries": [], "original_currency": "USD", "fx_rate": 0.0,
    }
    with patch(
        "tally_bridge.writer.TallyWriter.create_purchase_voucher_ledger",
        new=AsyncMock(return_value=_SUCCESS),
    ) as m:
        resp = _post(client, entry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["type"] == "voucher_error"
    m.assert_not_awaited()
