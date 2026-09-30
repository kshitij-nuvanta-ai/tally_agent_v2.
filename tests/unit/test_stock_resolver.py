"""Unit tests — stock-item resolver (invoice entry Phase 2, inventory).

``resolve_line_items`` fetches the workspace's existing stock items, then for
each line item on the extracted document builds a resolved dict: fuzzy-matches
the description to an existing stock item (exact → token-overlap/contains above
a threshold), defaults the unit to "Nos", and falls back the GST rate from the
line → document GST rate → 0. Deterministic; the Tally read is mocked.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from backend.services.document_parser import (
    ExtractedDocument,
    GSTBreakdown,
    LineItem,
)
from backend.services.stock_resolver import (
    dropped_unquantified_descriptions,
    resolve_line_items,
)
from tally_bridge.models import StockItem


def _client_with_items(*names: str):
    """A fake TallyClient whose list_stock_items returns the given names."""
    client = AsyncMock()
    return client


def _patch_list(monkeypatch, *names: str):
    items = [StockItem(name=n, parent_group="Primary", base_units="Nos") for n in names]
    monkeypatch.setattr(
        "backend.services.stock_resolver.list_stock_items",
        AsyncMock(return_value=items),
    )


def _doc(line_items, gst=None):
    return ExtractedDocument(
        doc_type="purchase",
        vendor_name="Acme",
        party_name="Acme",
        date="2026-04-04",
        total_amount=Decimal("1000"),
        line_items=line_items,
        gst=gst,
    )


@pytest.mark.asyncio
async def test_exact_match_to_seeded_item(monkeypatch):
    _patch_list(monkeypatch, "A4 Paper Ream 500 sheets", "HP Laptop 15s")
    doc = _doc([LineItem(description="A4 Paper Ream 500 sheets",
                         amount=Decimal("500"), quantity=Decimal("5"),
                         rate=Decimal("100"), unit="Nos")])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert len(resolved) == 1
    r = resolved[0]
    assert r["matched_item"] == "A4 Paper Ream 500 sheets"
    assert r["create_new"] is False
    assert r["stock_name"] == "A4 Paper Ream 500 sheets"
    assert r["qty"] == 5.0
    assert r["rate"] == 100.0


@pytest.mark.asyncio
async def test_case_insensitive_exact_match(monkeypatch):
    _patch_list(monkeypatch, "HP Laptop 15s")
    doc = _doc([LineItem(description="  hp laptop 15s  ",
                         amount=Decimal("50000"), quantity=Decimal("1"),
                         rate=Decimal("50000"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["matched_item"] == "HP Laptop 15s"
    assert resolved[0]["create_new"] is False


@pytest.mark.asyncio
async def test_fuzzy_partial_token_overlap_match(monkeypatch):
    _patch_list(monkeypatch, "Logitech Wireless Mouse", "HP Laptop 15s")
    doc = _doc([LineItem(description="Logitech Mouse Wireless",
                         amount=Decimal("800"), quantity=Decimal("2"),
                         rate=Decimal("400"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["matched_item"] == "Logitech Wireless Mouse"
    assert resolved[0]["create_new"] is False


@pytest.mark.asyncio
async def test_no_match_marks_create_new(monkeypatch):
    _patch_list(monkeypatch, "HP Laptop 15s", "A4 Paper Ream 500 sheets")
    doc = _doc([LineItem(description="Brother Toner Cartridge TN-2380",
                         amount=Decimal("3000"), quantity=Decimal("1"),
                         rate=Decimal("3000"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    r = resolved[0]
    assert r["matched_item"] is None
    assert r["create_new"] is True
    assert r["stock_name"] == "Brother Toner Cartridge TN-2380"
    assert r["stock_group"] == "Primary"


@pytest.mark.asyncio
async def test_unit_defaults_to_nos_when_missing(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="New Thing", amount=Decimal("100"),
                         quantity=Decimal("1"), rate=Decimal("100"), unit=None)])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["unit"] == "Nos"


@pytest.mark.asyncio
async def test_unit_passthrough_when_present(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="Cable", amount=Decimal("100"),
                         quantity=Decimal("4"), rate=Decimal("25"), unit="Mtr")])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["unit"] == "Mtr"


@pytest.mark.asyncio
async def test_gst_rate_from_line(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="Item", amount=Decimal("100"),
                         quantity=Decimal("1"), rate=Decimal("100"),
                         gst_rate=Decimal("12"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["gst_rate"] == 12.0


@pytest.mark.asyncio
async def test_gst_rate_fallback_from_doc(monkeypatch):
    """When the line has no GST rate, fall back to the document GST rate."""
    _patch_list(monkeypatch)
    gst = GSTBreakdown(cgst_rate=Decimal("9"), cgst_amount=Decimal("9"),
                       sgst_rate=Decimal("9"), sgst_amount=Decimal("9"))
    doc = _doc(
        [LineItem(description="Item", amount=Decimal("100"),
                  quantity=Decimal("1"), rate=Decimal("100"))],
        gst=gst,
    )
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    # CGST 9 + SGST 9 = 18 combined GST rate
    assert resolved[0]["gst_rate"] == 18.0


@pytest.mark.asyncio
async def test_gst_rate_fallback_zero(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="Item", amount=Decimal("100"),
                         quantity=Decimal("1"), rate=Decimal("100"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["gst_rate"] == 0.0


@pytest.mark.asyncio
async def test_hsn_passthrough_blank_default(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="Item", amount=Decimal("100"),
                         quantity=Decimal("1"), rate=Decimal("100"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["hsn"] == ""


@pytest.mark.asyncio
async def test_lines_without_qty_are_skipped(monkeypatch):
    """No-qty lines don't make the inventory grid (they signal non-inventory)."""
    _patch_list(monkeypatch)
    doc = _doc([
        LineItem(description="Service Fee", amount=Decimal("500"), quantity=None),
        LineItem(description="Widget", amount=Decimal("200"),
                 quantity=Decimal("2"), rate=Decimal("100")),
    ])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert len(resolved) == 1
    assert resolved[0]["description"] == "Widget"


def test_dropped_unquantified_descriptions_lists_qty_null_lines():
    """Finding 3: report descriptions of lines dropped for missing qty so the
    inventory write can be blocked rather than silently under-posting."""
    doc = _doc([
        LineItem(description="Freight charges", amount=Decimal("500"), quantity=None),
        LineItem(description="Widget", amount=Decimal("200"),
                 quantity=Decimal("2"), rate=Decimal("100")),
        LineItem(description="Handling", amount=Decimal("100"), quantity=None),
    ])
    assert dropped_unquantified_descriptions(doc) == ["Freight charges", "Handling"]


def test_dropped_unquantified_descriptions_empty_when_all_quantified():
    doc = _doc([
        LineItem(description="Widget", amount=Decimal("200"),
                 quantity=Decimal("2"), rate=Decimal("100")),
    ])
    assert dropped_unquantified_descriptions(doc) == []


@pytest.mark.asyncio
async def test_amount_present(monkeypatch):
    _patch_list(monkeypatch)
    doc = _doc([LineItem(description="Item", amount=Decimal("250"),
                         quantity=Decimal("5"), rate=Decimal("50"))])
    resolved = await resolve_line_items(
        AsyncMock(), doc, direction="purchase", default_group="Primary",
    )
    assert resolved[0]["amount"] == 250.0
