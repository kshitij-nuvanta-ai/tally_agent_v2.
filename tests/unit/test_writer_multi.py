"""Tests for TallyWriter Debit Note / Credit Note write methods (Group B Task 5).

Sales/Purchase write methods already exist (inventory-based) and are covered
elsewhere. This file covers only the NEW ledger-only DN/CN write methods, which
validate (party-ledger + bill allocation + amount>0 invariant), call the
correct builder, and parse the response.
"""
from unittest.mock import AsyncMock

import pytest

from tally_bridge.writer import TallyWriter, ValidationError, TallyWriteError

SUCCESS_XML = """<RESPONSE>
<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>100</LASTVCHID><LASTMID>100</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>"""

SILENT_DROP_XML = """<RESPONSE>
<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>1</EXCEPTIONS>
</RESPONSE>"""


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.post_xml.return_value = SUCCESS_XML
    return client


@pytest.mark.asyncio
class TestCreateDebitNote:
    async def test_success(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_debit_note(
            date="20260305", party_ledger="Supplier",
            purchase_ledger="Purchases", amount=1000.0,
            narration="return", bill_ref="INV-001",
        )
        assert result["success"] is True
        assert result["last_vch_id"] == "100"
        mock_client.post_xml.assert_called_once()
        xml = mock_client.post_xml.call_args[0][0]
        assert 'VCHTYPE="Debit Note"' in xml
        assert "INV-001" in xml
        assert "Agst Ref" in xml

    async def test_validation_error(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(ValidationError):
            await writer.create_debit_note(
                date="", party_ledger="S", purchase_ledger="P",
                amount=1000.0, narration="",
            )

    async def test_with_gst(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_debit_note(
            date="20260305", party_ledger="Supplier",
            purchase_ledger="Purchases", amount=11800.0,
            narration="return", gst_entries=[{"ledger": "INPUT CGST", "amount": 900.0}],
            bill_ref="INV-001",
        )
        assert result["success"] is True

    async def test_silent_drop_raises(self, mock_client):
        mock_client.post_xml.return_value = SILENT_DROP_XML
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(TallyWriteError):
            await writer.create_debit_note(
                date="20260305", party_ledger="Supplier",
                purchase_ledger="Purchases", amount=1000.0,
                narration="return",
            )

    async def test_unknown_ledger_validation(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(ValidationError):
            await writer.create_debit_note(
                date="20260305", party_ledger="Supplier",
                purchase_ledger="Purchases", amount=1000.0,
                narration="return", known_ledgers=["Purchases"],  # Supplier missing
            )


@pytest.mark.asyncio
class TestCreateCreditNote:
    async def test_success(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_credit_note(
            date="20260315", party_ledger="Customer",
            sales_ledger="Sales", amount=1000.0,
            narration="discount", bill_ref="INV-FEB-001",
        )
        assert result["success"] is True
        xml = mock_client.post_xml.call_args[0][0]
        assert 'VCHTYPE="Credit Note"' in xml
        assert "INV-FEB-001" in xml
        assert "Agst Ref" in xml

    async def test_validation_error(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(ValidationError):
            await writer.create_credit_note(
                date="", party_ledger="C", sales_ledger="S",
                amount=1000.0, narration="",
            )

    async def test_with_gst(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_credit_note(
            date="20260315", party_ledger="Customer",
            sales_ledger="Sales", amount=11800.0,
            narration="discount", gst_entries=[{"ledger": "OUTPUT CGST", "amount": 900.0}],
            bill_ref="INV-FEB-001",
        )
        assert result["success"] is True
