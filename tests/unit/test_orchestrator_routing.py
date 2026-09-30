"""Unit tests — orchestrator routes file uploads by doc_type (Group B, Task 9).

Each doc_type must select the correct voucher builder, the correct ledgers
(party under Sundry Creditors/Debtors + contra ledger), and produce a review
entry carrying the Group B fields (voucher_type, party_ledger, is_party_ledger,
bill_reference, party_vouchers/against_invoice_options, FX fields).

The Vision call and the Tally ledger fetch are mocked; routing is pure logic.
"""
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.context import SessionContext
from backend.agents.orchestrator import Orchestrator
from tests.fixtures import vision_docs

# Ledgers the mock Tally "returns" — covers party groups + contra ledgers +
# the six seed-company GST ledgers under Duties & Taxes.
_LEDGERS = [
    {"name": "Croma Electronics", "parent_group": "Sundry Creditors"},
    {"name": "Infosys Ltd", "parent_group": "Sundry Debtors"},
    {"name": "Anthropic PBC", "parent_group": "Sundry Creditors"},
    {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
    {"name": "Sales Accounts", "parent_group": "Sales Accounts"},
    {"name": "HDFC Bank", "parent_group": "Bank Accounts"},
    {"name": "Cash", "parent_group": "Cash-in-hand"},
    {"name": "Office Supplies", "parent_group": "Indirect Expenses"},
    {"name": "CGST Input", "parent_group": "Duties & Taxes"},
    {"name": "SGST Input", "parent_group": "Duties & Taxes"},
    {"name": "IGST Input", "parent_group": "Duties & Taxes"},
    {"name": "CGST Output", "parent_group": "Duties & Taxes"},
    {"name": "SGST Output", "parent_group": "Duties & Taxes"},
    {"name": "IGST Output", "parent_group": "Duties & Taxes"},
]

# Group tree the mock Tally "returns" — mirrors the seed company's custom
# sub-groups of Sundry Creditors/Debtors. ``parse_groups`` is patched to return
# this so the orchestrator's hierarchy-aware ledger selection can resolve a
# party sitting under a sub-group (e.g. "National Creditors").
_GROUPS = [
    {"name": "Sundry Creditors", "parent": "Current Liabilities"},
    {"name": "Sundry Debtors", "parent": "Current Assets"},
    {"name": "Local Creditors", "parent": "Sundry Creditors"},
    {"name": "National Creditors", "parent": "Sundry Creditors"},
    {"name": "North Zone Debtors", "parent": "Sundry Debtors"},
    {"name": "South Zone Debtors", "parent": "Sundry Debtors"},
]


class _FakeClient:
    async def post_xml(self, xml):
        return "<LEDGERS/>"


@contextmanager
def _temp_file():
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    os.close(fd)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


from backend.tally_bridge.models import StockItem

# Stock items the company already has (used by inventory goods detection).
_STOCK_ITEMS = [
    StockItem(name="A4 Paper Ream 500 sheets", parent_group="Primary", base_units="Nos"),
    StockItem(name="HP Laptop 15s", parent_group="Primary", base_units="Nos"),
    StockItem(name="Logitech Wireless Mouse", parent_group="Primary", base_units="Nos"),
]


async def _run_upload(fixture_name, party_vouchers=None, ledgers=None, groups=None):
    """Drive process_file_upload for a fixture; return the result dict.

    ``ledgers`` overrides the workspace Tally ledgers (default ``_LEDGERS``);
    pass an empty/non-matching list to force a NEW party → AI-suggestion path.
    ``groups`` overrides the Tally group tree (default ``_GROUPS``) used for
    hierarchy-aware party-ledger selection.
    """
    orch = Orchestrator()
    session = SessionContext(session_id="s1")
    with _temp_file() as path, patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message(fixture_name)),
    ), patch(
        "backend.tally_bridge.response_parser.parse_ledger_list",
        return_value=_LEDGERS if ledgers is None else ledgers,
    ), patch(
        "backend.tally_bridge.response_parser.parse_groups",
        return_value=_GROUPS if groups is None else groups,
    ), patch(
        "backend.services.stock_resolver.list_stock_items",
        new=AsyncMock(return_value=_STOCK_ITEMS),
    ), patch(
        "backend.tally_bridge.queries.vouchers.get_party_vouchers",
        new=AsyncMock(return_value=party_vouchers or []),
    ):
        result = await orch.process_file_upload(
            file_path=path,
            filename="doc.jpg",
            mime_type="image/jpeg",
            user_message="entry",
            client=_FakeClient(),
            session=session,
            file_id="file-1",
        )
    assert result["data"] is not None, result["message"]
    return result


def _entry(result):
    return result["data"]["entries"][0]


@pytest.mark.asyncio
async def test_payment_routes_to_payment_builder():
    result = await _run_upload("payment_petty_cash_inr")
    entry = _entry(result)
    assert entry["voucher_type"] == "Payment"
    assert entry["is_party_ledger"] is False
    assert entry.get("party_ledger") is None
    assert "available_payment_ledgers" in result["data"]


@pytest.mark.asyncio
async def test_purchase_routes_to_purchase_builder():
    result = await _run_upload("purchase_office_inr")
    entry = _entry(result)
    assert entry["voucher_type"] == "Purchase"
    assert entry["is_party_ledger"] is True
    assert entry["party_ledger"] == "Croma Electronics"
    assert entry["credit_ledger"] == "Croma Electronics"
    assert entry["bill_type"] == "New Ref"


@pytest.mark.asyncio
async def test_sales_routes_to_sales_builder():
    result = await _run_upload("sales_service_inr")
    entry = _entry(result)
    assert entry["voucher_type"] == "Sales"
    assert entry["is_party_ledger"] is True
    assert entry["party_ledger"] == "Infosys Ltd"
    assert entry["debit_ledger"] == "Infosys Ltd"
    assert entry["bill_type"] == "New Ref"


@pytest.mark.asyncio
async def test_debit_note_routes_with_against_invoice_options():
    sample = [
        {"voucher_number": "CRO-2026-5678", "date": "2026-02-10",
         "voucher_type": "Purchase", "amount": 15340.0, "reference": "CRO-2026-5678"},
    ]
    result = await _run_upload("debit_note_return_inr", party_vouchers=sample)
    entry = _entry(result)
    assert entry["voucher_type"] == "Debit Note"
    assert entry["is_party_ledger"] is True
    assert entry["bill_type"] == "Agst Ref"
    assert entry["bill_reference"] == "CRO-2026-5678"
    assert entry["against_invoice_options"]
    assert entry["party_vouchers"] == sample


@pytest.mark.asyncio
async def test_credit_note_routes_with_against_invoice_options():
    sample = [
        {"voucher_number": "INV-001", "date": "2026-03-01",
         "voucher_type": "Sales", "amount": 118000.0, "reference": "INV-001"},
    ]
    result = await _run_upload("credit_note_return_inr", party_vouchers=sample)
    entry = _entry(result)
    assert entry["voucher_type"] == "Credit Note"
    assert entry["bill_type"] == "Agst Ref"
    assert entry["party_vouchers"] == sample
    assert entry["against_invoice_options"]


@pytest.mark.asyncio
async def test_upload_intro_omits_volatile_amount_and_date():
    """The upload intro sentence must NOT embed the (edit-able) amount or date —
    those live only in the card `data` (which can be edited via save_draft) and
    would otherwise go stale. The intro keeps the party + review guidance.
    """
    result = await _run_upload("purchase_office_inr")
    message = result["message"]
    entry = _entry(result)
    # Party + guidance still present.
    assert "Croma Electronics" in message
    assert "Please review" in message
    # Volatile values are gone from the intro text — they live on the card.
    assert "— ₹" not in message
    assert f"on {entry['date']}" not in message
    assert f"{float(entry['amount']):,.2f}" not in message
    # The amount/date still live on the card `data`, the source of truth.
    assert entry["amount"]
    assert entry["date"]


@pytest.mark.asyncio
async def test_purchase_reference_is_invoice_number():
    """A plain purchase's review reference comes from invoice_number (its OWN
    number), NOT original_invoice_ref."""
    result = await _run_upload("purchase_office_inr")
    entry = _entry(result)
    # purchase_office_inr: invoice_number = CRO-2026-5678, original_invoice_ref = null
    assert entry["reference"] == "CRO-2026-5678"
    # No "limited to exact file" warning when an invoice number is present.
    assert not any("limited to exact file" in w for w in entry["warnings"])


@pytest.mark.asyncio
async def test_sales_reference_is_invoice_number():
    result = await _run_upload("sales_goods_inr")
    entry = _entry(result)
    # sales_goods_inr: invoice_number = INV-9100
    assert entry["reference"] == "INV-9100"
    assert not any("limited to exact file" in w for w in entry["warnings"])


@pytest.mark.asyncio
async def test_missing_invoice_number_warns_limited_dedup():
    """No invoice_number → soft warning that dedup is limited to exact file."""
    result = await _run_upload("sales_service_inr")  # invoice_number = null
    entry = _entry(result)
    assert entry["reference"] is None
    assert any("limited to exact file" in w for w in entry["warnings"])


@pytest.mark.asyncio
async def test_debit_note_bill_reference_from_original_ref():
    """DN bill_reference (against-bill) still comes from original_invoice_ref,
    independent of the doc's own invoice_number."""
    sample = [
        {"voucher_number": "CRO-2026-5678", "date": "2026-02-10",
         "voucher_type": "Purchase", "amount": 15340.0, "reference": "CRO-2026-5678"},
    ]
    result = await _run_upload("debit_note_return_inr", party_vouchers=sample)
    entry = _entry(result)
    # against-bill is the ORIGINAL invoice (original_invoice_ref)
    assert entry["bill_reference"] == "CRO-2026-5678"
    # the DN's OWN number (invoice_number) populates reference, separately.
    assert entry["reference"] == "DN-2026-001"


@pytest.mark.asyncio
async def test_dedup_keys_on_invoice_number():
    """The business-key dedup call uses invoice_number as invoice_ref."""
    orch = Orchestrator()
    session = SessionContext(session_id="s1")
    fake_find = AsyncMock(return_value=None)
    with _temp_file() as path, patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message("purchase_office_inr")),
    ), patch(
        "backend.tally_bridge.response_parser.parse_ledger_list",
        return_value=_LEDGERS,
    ), patch(
        "backend.services.stock_resolver.list_stock_items",
        new=AsyncMock(return_value=_STOCK_ITEMS),
    ), patch(
        "backend.tally_bridge.queries.vouchers.get_party_vouchers",
        new=AsyncMock(return_value=[]),
    ), patch(
        "backend.services.dedup.find_duplicate", new=fake_find,
    ):
        await orch.process_file_upload(
            file_path=path, filename="doc.jpg", mime_type="image/jpeg",
            user_message="entry", client=_FakeClient(), session=session,
            file_id="file-1", db=object(), workspace_id="ws-1",
        )
    fake_find.assert_awaited_once()
    assert fake_find.await_args.kwargs["invoice_ref"] == "CRO-2026-5678"


def _gst_ledger_names(entry):
    return {e["ledger"] for e in entry.get("gst_entries", [])}


@pytest.mark.asyncio
async def test_purchase_gets_input_gst_legs():
    """Intrastate purchase resolves Input CGST/SGST from Duties & Taxes."""
    result = await _run_upload("purchase_office_inr")
    entry = _entry(result)
    assert _gst_ledger_names(entry) == {"CGST Input", "SGST Input"}
    # Contra (purchase) leg posts the BASE; party gross = base + GST.
    gst_total = sum(e["amount"] for e in entry["gst_entries"])
    assert abs((entry["amount"] - gst_total) - (15340.0 - 2342.0)) < 0.01


@pytest.mark.asyncio
async def test_interstate_purchase_gets_input_igst():
    result = await _run_upload("purchase_interstate_inr")
    entry = _entry(result)
    assert _gst_ledger_names(entry) == {"IGST Input"}


@pytest.mark.asyncio
async def test_sales_gets_output_gst_legs():
    result = await _run_upload("sales_service_inr")
    entry = _entry(result)
    assert _gst_ledger_names(entry) == {"CGST Output", "SGST Output"}


@pytest.mark.asyncio
async def test_debit_note_gets_input_gst_legs():
    sample = [
        {"voucher_number": "CRO-2026-5678", "date": "2026-02-10",
         "voucher_type": "Purchase", "amount": 15340.0, "reference": "CRO-2026-5678"},
    ]
    result = await _run_upload("debit_note_return_inr", party_vouchers=sample)
    entry = _entry(result)
    assert _gst_ledger_names(entry) == {"CGST Input", "SGST Input"}


@pytest.mark.asyncio
async def test_credit_note_gets_output_gst_legs():
    sample = [
        {"voucher_number": "INV-001", "date": "2026-03-01",
         "voucher_type": "Sales", "amount": 118000.0, "reference": "INV-001"},
    ]
    result = await _run_upload("credit_note_return_inr", party_vouchers=sample)
    entry = _entry(result)
    assert _gst_ledger_names(entry) == {"CGST Output", "SGST Output"}


@pytest.mark.asyncio
async def test_missing_gst_ledger_warns_no_leg():
    """When the needed GST ledger isn't in the ledger list, warn and omit the leg."""
    orch = Orchestrator()
    session = SessionContext(session_id="s1")
    no_gst_ledgers = [
        {"name": "Croma Electronics", "parent_group": "Sundry Creditors"},
        {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
    ]
    with _temp_file() as path, patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message("purchase_office_inr")),
    ), patch(
        "backend.tally_bridge.response_parser.parse_ledger_list",
        return_value=no_gst_ledgers,
    ):
        result = await orch.process_file_upload(
            file_path=path, filename="doc.jpg", mime_type="image/jpeg",
            user_message="entry", client=_FakeClient(), session=session,
            file_id="file-1",
        )
    entry = result["data"]["entries"][0]
    assert entry["gst_entries"] == []
    assert any("not found" in w for w in entry["warnings"])


@pytest.mark.asyncio
async def test_goods_purchase_is_inventory_with_line_items():
    """A purchase with quantity-bearing lines → inventory path."""
    result = await _run_upload("purchase_goods_inr")
    entry = _entry(result)
    assert entry["voucher_type"] == "Purchase"
    assert entry["is_inventory"] is True
    assert len(entry["line_items"]) == 2
    # First line matches a seeded item, second is new.
    by_desc = {li["description"]: li for li in entry["line_items"]}
    assert by_desc["A4 Paper Ream 500 sheets"]["matched_item"] == "A4 Paper Ream 500 sheets"
    assert by_desc["A4 Paper Ream 500 sheets"]["create_new"] is False
    assert by_desc["Brother Toner Cartridge TN-2380"]["create_new"] is True
    # Per-line ledger defaults to the chosen purchase contra ledger.
    assert by_desc["A4 Paper Ream 500 sheets"]["ledger"] == "Purchase Accounts"
    assert "A4 Paper Ream 500 sheets" in entry["available_stock_items"]
    assert entry["default_stock_group"] == "AI Imported Items"


@pytest.mark.asyncio
async def test_goods_sales_is_inventory_with_line_items():
    result = await _run_upload("sales_goods_inr")
    entry = _entry(result)
    assert entry["voucher_type"] == "Sales"
    assert entry["is_inventory"] is True
    assert len(entry["line_items"]) == 2
    by_desc = {li["description"]: li for li in entry["line_items"]}
    assert by_desc["HP Laptop 15s"]["matched_item"] == "HP Laptop 15s"
    # Per-line ledger defaults to the chosen sales contra ledger.
    assert by_desc["HP Laptop 15s"]["ledger"] == "Sales Accounts"


@pytest.mark.asyncio
async def test_services_purchase_stays_accounting_only():
    """A purchase whose lines have no quantity → accounting-only (no inventory)."""
    result = await _run_upload("purchase_saas_usd")
    entry = _entry(result)
    assert entry.get("is_inventory") in (False, None)
    assert entry.get("line_items") in (None, [])


@pytest.mark.asyncio
async def test_services_sales_stays_accounting_only():
    result = await _run_upload("sales_service_inr")
    entry = _entry(result)
    assert entry.get("is_inventory") in (False, None)


@pytest.mark.asyncio
async def test_payment_never_inventory():
    result = await _run_upload("payment_petty_cash_inr")
    entry = _entry(result)
    assert entry.get("is_inventory") in (False, None)


# ---------------------------------------------------------------------------
# file_id linkage — EVERY entry (inventory AND non-inventory, every doc type)
# must carry the uploaded-file id so the original upload, the live card and the
# version trail are joinable via entry.file_id → uploaded_files.id.
# In unit mode (db=None) file_id is the value passed to process_file_upload
# ("file-1"); in DB mode it's reassigned to str(uploaded.id).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture",
    [
        "payment_petty_cash_inr",
        "purchase_goods_inr",       # inventory
        "sales_goods_inr",          # inventory
        "purchase_saas_usd",        # accounting-only purchase
        "sales_service_inr",        # accounting-only sales
    ],
)
@pytest.mark.asyncio
async def test_entry_always_carries_file_id(fixture):
    result = await _run_upload(fixture)
    entry = _entry(result)
    assert entry.get("file_id") == "file-1", (
        f"{fixture}: entry must carry file_id (got {entry.get('file_id')!r})"
    )


@pytest.mark.asyncio
async def test_inventory_entry_carries_file_id():
    """Regression: the goods/inventory path must NOT drop file_id."""
    result = await _run_upload("purchase_goods_inr")
    entry = _entry(result)
    assert entry["is_inventory"] is True
    assert entry["file_id"] == "file-1"


@pytest.mark.asyncio
async def test_fx_fields_present_on_foreign_purchase():
    result = await _run_upload("purchase_saas_usd")
    entry = _entry(result)
    assert entry["voucher_type"] == "Purchase"
    assert entry["original_currency"] == "USD"
    assert entry["fx_rate"] is not None
    assert entry["amount"] != entry["original_amount"]


@pytest.mark.asyncio
async def test_company_forwarded_to_vision_prompt():
    """process_file_upload must forward its company arg to build_vision_prompt,
    anchoring the classifier to the user's own company (purchase-vs-sales)."""
    orch = Orchestrator()
    session = SessionContext(session_id="s1")
    captured = {}

    def _capture_prompt(company_name=None):
        captured["company"] = company_name
        return "PROMPT"

    with _temp_file() as path, patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message("purchase_office_inr")),
    ), patch(
        "backend.tally_bridge.response_parser.parse_ledger_list",
        return_value=_LEDGERS,
    ), patch(
        "backend.services.stock_resolver.list_stock_items",
        new=AsyncMock(return_value=_STOCK_ITEMS),
    ), patch(
        "backend.tally_bridge.queries.vouchers.get_party_vouchers",
        new=AsyncMock(return_value=[]),
    ), patch(
        "backend.services.document_parser.build_vision_prompt",
        side_effect=_capture_prompt,
    ):
        await orch.process_file_upload(
            file_path=path,
            filename="doc.jpg",
            mime_type="image/jpeg",
            user_message="entry",
            client=_FakeClient(),
            session=session,
            file_id="file-1",
            company="Bharat Traders Private Limited",
        )

    assert captured["company"] == "Bharat Traders Private Limited"


# ---------------------------------------------------------------------------
# Regression guard — new-party suggested_parent direction (the "bug behind the
# bug"): the orchestrator passes the lowercase Vision doc_type to the mapper.
# A NEW sales party must land under "Sundry Debtors" (customer), NOT creditors.
# This exercises the full orchestrator → LedgerMapper._ai_suggest → entry-dict
# wiring with the REAL lowercase doc_type, so a casing mismatch can't recur
# silently.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_new_sales_party_suggested_parent_is_sundry_debtors():
    # Empty ledger list → party can't be matched → AI-suggestion (new ledger).
    result = await _run_upload("sales_service_inr", ledgers=[])
    entry = _entry(result)
    assert entry["voucher_type"] == "Sales"
    assert entry["is_new_ledger"] is True
    assert entry["suggested_parent"] == "Sundry Debtors", (
        "A new sales customer must be suggested under Sundry Debtors, not "
        f"Creditors — got {entry['suggested_parent']!r}"
    )


@pytest.mark.asyncio
async def test_new_purchase_party_suggested_parent_is_sundry_creditors():
    result = await _run_upload("purchase_office_inr", ledgers=[])
    entry = _entry(result)
    assert entry["voucher_type"] == "Purchase"
    assert entry["is_new_ledger"] is True
    assert entry["suggested_parent"] == "Sundry Creditors"


@pytest.mark.asyncio
async def test_mapper_lowercase_doctype_direction_matches_orchestrator():
    """Thin direct guard: the exact lowercase doc_type strings the orchestrator
    passes via mapper.find_mapping(party, doc_type, ...) must map to the right
    party group."""
    from backend.services.ledger_mapper import LedgerMapper

    mapper = LedgerMapper()
    sales = await mapper.find_mapping("Brand New Customer", "sales", tally_ledgers=[])
    assert sales.suggested_parent == "Sundry Debtors"
    credit = await mapper.find_mapping("Brand New Customer", "credit_note", tally_ledgers=[])
    assert credit.suggested_parent == "Sundry Debtors"
    purchase = await mapper.find_mapping("Brand New Supplier", "purchase", tally_ledgers=[])
    assert purchase.suggested_parent == "Sundry Creditors"


# ---------------------------------------------------------------------------
# Bug B — hierarchy-aware party-ledger selection. Real party ledgers commonly
# sit under CUSTOM SUB-GROUPS of Sundry Creditors/Debtors (e.g. "National
# Creditors" → "Sundry Creditors"). The selection must walk the group lineage,
# not require an exact parent_group == "sundry creditors" match, or the existing
# party gets filtered out → wrongly flagged is_new_ledger → blocked on write.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_supplier_under_subgroup_is_matched_not_flagged_new():
    """A supplier sitting under a sub-group of Sundry Creditors must be matched
    to its existing ledger (is_new_ledger=False), not flagged as new."""
    # "Croma Electronics" is the party on purchase_office_inr; place it ONLY
    # under the custom sub-group "National Creditors" (→ Sundry Creditors).
    ledgers = [
        {"name": "Croma Electronics", "parent_group": "National Creditors"},
        {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
        {"name": "CGST Input", "parent_group": "Duties & Taxes"},
        {"name": "SGST Input", "parent_group": "Duties & Taxes"},
    ]
    result = await _run_upload("purchase_office_inr", ledgers=ledgers)
    entry = _entry(result)
    assert entry["voucher_type"] == "Purchase"
    assert entry["is_new_ledger"] is False, (
        "Supplier under a Sundry Creditors sub-group must be matched to its "
        "existing ledger, not flagged as new."
    )
    assert entry["party_ledger"] == "Croma Electronics"
    assert entry["credit_ledger"] == "Croma Electronics"
    # The sub-grouped party must also surface in the edit-form dropdown.
    assert "Croma Electronics" in result["data"]["available_supplier_ledgers"]


@pytest.mark.asyncio
async def test_customer_under_subgroup_is_matched_not_flagged_new():
    """A customer under a sub-group of Sundry Debtors must be matched too."""
    ledgers = [
        {"name": "Infosys Ltd", "parent_group": "South Zone Debtors"},
        {"name": "Sales Accounts", "parent_group": "Sales Accounts"},
        {"name": "CGST Output", "parent_group": "Duties & Taxes"},
        {"name": "SGST Output", "parent_group": "Duties & Taxes"},
    ]
    result = await _run_upload("sales_service_inr", ledgers=ledgers)
    entry = _entry(result)
    assert entry["voucher_type"] == "Sales"
    assert entry["is_new_ledger"] is False
    assert entry["party_ledger"] == "Infosys Ltd"
    assert "Infosys Ltd" in result["data"]["available_customer_ledgers"]


@pytest.mark.asyncio
async def test_brand_new_party_still_flagged_new_with_hierarchy():
    """Guard: a party in NO group at all still yields is_new_ledger=True — the
    hierarchy walk must not accidentally match unrelated/absent ledgers."""
    ledgers = [
        {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
        {"name": "CGST Input", "parent_group": "Duties & Taxes"},
        {"name": "SGST Input", "parent_group": "Duties & Taxes"},
    ]
    result = await _run_upload("purchase_office_inr", ledgers=ledgers)
    entry = _entry(result)
    assert entry["is_new_ledger"] is True
    assert entry["suggested_parent"] == "Sundry Creditors"


@pytest.mark.asyncio
async def test_subgroup_selection_falls_back_when_groups_read_empty():
    """Robustness: if the groups read returns empty, selection falls back to the
    exact parent_group match (a directly-parented party still matches)."""
    ledgers = [
        {"name": "Croma Electronics", "parent_group": "Sundry Creditors"},
        {"name": "Purchase Accounts", "parent_group": "Purchase Accounts"},
        {"name": "CGST Input", "parent_group": "Duties & Taxes"},
        {"name": "SGST Input", "parent_group": "Duties & Taxes"},
    ]
    result = await _run_upload("purchase_office_inr", ledgers=ledgers, groups=[])
    entry = _entry(result)
    assert entry["is_new_ledger"] is False
    assert entry["party_ledger"] == "Croma Electronics"


# --- Party-voucher window anchor (C33 typed dates, 2026-09-24) ---------------
# With typed date vars Tally honours the lookup window literally, so the write
# flow must anchor it on the DOCUMENT's date (not a hard-coded FY 2025-26).

async def _upload_capturing(fixture_name, *, db=None, workspace_id=None):
    orch = Orchestrator()
    session = SessionContext(session_id="s1")
    fake_pv = AsyncMock(return_value=[])
    fake_find = AsyncMock(return_value=None)
    with _temp_file() as path, patch(
        "backend.agents.orchestrator.anthropic_client.messages.create",
        new=AsyncMock(return_value=vision_docs.vision_message(fixture_name)),
    ), patch(
        "backend.tally_bridge.response_parser.parse_ledger_list",
        return_value=_LEDGERS,
    ), patch(
        "backend.services.stock_resolver.list_stock_items",
        new=AsyncMock(return_value=_STOCK_ITEMS),
    ), patch(
        "backend.tally_bridge.queries.vouchers.get_party_vouchers", new=fake_pv,
    ), patch(
        "backend.services.dedup.find_duplicate", new=fake_find,
    ):
        await orch.process_file_upload(
            file_path=path, filename="doc.jpg", mime_type="image/jpeg",
            user_message="entry", client=_FakeClient(), session=session,
            file_id="file-1", db=db, workspace_id=workspace_id,
        )
    return fake_pv, fake_find


@pytest.mark.asyncio
async def test_debit_note_party_lookup_anchored_on_document_date():
    fake_pv, _ = await _upload_capturing("debit_note_return_inr")
    fake_pv.assert_awaited_once()
    assert fake_pv.await_args.kwargs["anchor_date"] == "2026-03-05"


@pytest.mark.asyncio
async def test_credit_note_party_lookup_anchored_on_document_date():
    fake_pv, _ = await _upload_capturing("credit_note_return_inr")
    fake_pv.assert_awaited_once()
    assert fake_pv.await_args.kwargs["anchor_date"] == "2026-03-15"


@pytest.mark.asyncio
async def test_dedup_receives_document_date():
    _, fake_find = await _upload_capturing("purchase_office_inr", db=object(), workspace_id="ws-1")
    fake_find.assert_awaited_once()
    assert fake_find.await_args.kwargs["doc_date"] == "2026-02-10"
