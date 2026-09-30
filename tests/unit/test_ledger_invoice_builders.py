"""Unit tests — ledger-only Purchase/Sales builders + writers (Group B, Task 8).

These are the document-driven counterparts to the stock-based seeder builders:
party (Sundry Creditors/Debtors) + contra ledger + GST, no inventory.
"""
from unittest.mock import AsyncMock

import pytest

from tally_bridge.import_builder import (
    build_create_purchase_voucher_ledger,
    build_create_sales_voucher_ledger,
)


def test_purchase_ledger_xml_has_party_and_contra():
    xml = build_create_purchase_voucher_ledger(
        date="20260210", party_ledger="Croma Electronics",
        purchase_ledger="Purchase Accounts", amount=15340.0,
        narration="Croma purchase", company="Bharat Traders",
        gst_entries=[{"ledger": "INPUT CGST", "amount": 1171.0},
                     {"ledger": "INPUT SGST", "amount": 1171.0}],
        bill_ref="CRO-5678",
    )
    assert 'VCHTYPE="Purchase"' in xml
    assert "<ISINVOICE>Yes</ISINVOICE>" in xml
    assert "Croma Electronics" in xml and "Purchase Accounts" in xml
    assert "ISPARTYLEDGER>Yes" in xml
    assert "INPUT CGST" in xml and "INPUT SGST" in xml
    # Party credit side (+amount), purchase ledger debit (-base).
    assert "<AMOUNT>15340.00</AMOUNT>" in xml
    assert "New Ref" in xml  # fresh bill allocation


def test_sales_ledger_xml_reversed_polarity():
    xml = build_create_sales_voucher_ledger(
        date="20260301", party_ledger="Infosys Ltd",
        sales_ledger="Sales Accounts", amount=118000.0,
        narration="Sale", company="Bharat Traders",
        gst_entries=[{"ledger": "OUTPUT CGST", "amount": 9000.0},
                     {"ledger": "OUTPUT SGST", "amount": 9000.0}],
    )
    assert 'VCHTYPE="Sales"' in xml
    assert "Infosys Ltd" in xml and "Sales Accounts" in xml
    # Party debit side on Sales → -amount.
    assert "<AMOUNT>-118000.00</AMOUNT>" in xml


def _voucher_el(xml_str: str):
    import xml.etree.ElementTree as ET
    return ET.fromstring(xml_str).find(".//VOUCHER")


def test_purchase_input_gst_on_debit_and_balances():
    """Purchase posts Input GST on the DEBIT side (Yes / negative) alongside the
    purchase contra; all legs sum to zero (party gross = base + GST)."""
    xml = build_create_purchase_voucher_ledger(
        date="20260210", party_ledger="Croma Electronics",
        purchase_ledger="Purchase Accounts", amount=15340.0,
        narration="purchase", company="Bharat Traders",
        gst_entries=[{"ledger": "CGST Input", "amount": 1171.0},
                     {"ledger": "SGST Input", "amount": 1171.0}],
    )
    v = _voucher_el(xml)
    gst = [e for e in v.findall("LEDGERENTRIES.LIST")
           if "Input" in (e.find("LEDGERNAME").text or "")]
    assert len(gst) == 2
    for e in gst:
        assert e.find("ISDEEMEDPOSITIVE").text == "Yes"
        assert float(e.find("AMOUNT").text) < 0
    amounts = [float(e.find("AMOUNT").text) for e in v.findall("LEDGERENTRIES.LIST")]
    assert abs(sum(amounts)) < 0.01


def test_sales_output_gst_on_credit_and_balances():
    """Sales posts Output GST on the CREDIT side (No / positive); legs balance."""
    xml = build_create_sales_voucher_ledger(
        date="20260301", party_ledger="Infosys Ltd",
        sales_ledger="Sales Accounts", amount=118000.0,
        narration="Sale", company="Bharat Traders",
        gst_entries=[{"ledger": "CGST Output", "amount": 9000.0},
                     {"ledger": "SGST Output", "amount": 9000.0}],
    )
    v = _voucher_el(xml)
    gst = [e for e in v.findall("LEDGERENTRIES.LIST")
           if "Output" in (e.find("LEDGERNAME").text or "")]
    assert len(gst) == 2
    for e in gst:
        assert e.find("ISDEEMEDPOSITIVE").text == "No"
        assert float(e.find("AMOUNT").text) > 0
    amounts = [float(e.find("AMOUNT").text) for e in v.findall("LEDGERENTRIES.LIST")]
    assert abs(sum(amounts)) < 0.01


def test_purchase_ledger_validates_amount_positive():
    with pytest.raises(ValueError):
        build_create_purchase_voucher_ledger(
            date="20260210", party_ledger="X", purchase_ledger="Y",
            amount=0.0, narration="n", company="Co",
        )


@pytest.mark.asyncio
async def test_writer_create_purchase_voucher_ledger_posts_xml():
    from tally_bridge.writer import TallyWriter

    client = AsyncMock()
    client.post_xml = AsyncMock(
        return_value="<RESPONSE><CREATED>1</CREATED><LASTVCHID>7</LASTVCHID></RESPONSE>"
    )
    writer = TallyWriter(client=client, company="Bharat Traders")
    result = await writer.create_purchase_voucher_ledger(
        date="20260210", party_ledger="Croma Electronics",
        purchase_ledger="Purchase Accounts", amount=15340.0,
        narration="purchase", gst_entries=None, bill_ref="CRO-5678",
    )
    assert result["success"] is True
    posted = client.post_xml.await_args.args[0]
    assert 'VCHTYPE="Purchase"' in posted
    assert "Croma Electronics" in posted
