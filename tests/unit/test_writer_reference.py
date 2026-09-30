"""Unit tests — TallyWriter threads reference/reference_date into the builders
(Phase 1 Part A). Each write method must accept the supplier-invoice
reference + date and emit <REFERENCE>/<REFERENCEDATE> in the posted XML.
"""
from unittest.mock import AsyncMock

import pytest

from tally_bridge.writer import TallyWriter

SUCCESS_XML = """<RESPONSE>
<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>100</LASTVCHID><LASTMID>100</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>"""


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.post_xml.return_value = SUCCESS_XML
    return client


def _posted_xml(mock_client):
    return mock_client.post_xml.call_args[0][0]


@pytest.mark.asyncio
async def test_payment_threads_reference(mock_client):
    writer = TallyWriter(client=mock_client, company="Co")
    await writer.create_payment_voucher(
        date="20260210", debit_ledger="Travel", credit_ledger="Cash",
        amount=500.0, narration="cab",
        reference="INV-9", reference_date="20260210",
    )
    xml = _posted_xml(mock_client)
    assert "<REFERENCE>INV-9</REFERENCE>" in xml
    assert "<REFERENCEDATE>20260210</REFERENCEDATE>" in xml


@pytest.mark.asyncio
async def test_purchase_ledger_threads_reference(mock_client):
    writer = TallyWriter(client=mock_client, company="Co")
    await writer.create_purchase_voucher_ledger(
        date="20260210", party_ledger="Supplier", purchase_ledger="Purchases",
        amount=1000.0, narration="buy",
        reference="INV-9", reference_date="20260210",
    )
    xml = _posted_xml(mock_client)
    assert "<REFERENCE>INV-9</REFERENCE>" in xml
    assert "<REFERENCEDATE>20260210</REFERENCEDATE>" in xml


@pytest.mark.asyncio
async def test_sales_ledger_threads_reference(mock_client):
    writer = TallyWriter(client=mock_client, company="Co")
    await writer.create_sales_voucher_ledger(
        date="20260210", party_ledger="Customer", sales_ledger="Sales",
        amount=1000.0, narration="sell",
        reference="INV-9", reference_date="20260210",
    )
    xml = _posted_xml(mock_client)
    assert "<REFERENCE>INV-9</REFERENCE>" in xml
    assert "<REFERENCEDATE>20260210</REFERENCEDATE>" in xml


@pytest.mark.asyncio
async def test_debit_note_threads_reference(mock_client):
    writer = TallyWriter(client=mock_client, company="Co")
    await writer.create_debit_note(
        date="20260210", party_ledger="Supplier", purchase_ledger="Purchases",
        amount=1000.0, narration="return", bill_ref="ORIG-1",
        reference="INV-9", reference_date="20260210",
    )
    xml = _posted_xml(mock_client)
    assert "<REFERENCE>INV-9</REFERENCE>" in xml
    assert "<REFERENCEDATE>20260210</REFERENCEDATE>" in xml


@pytest.mark.asyncio
async def test_credit_note_threads_reference(mock_client):
    writer = TallyWriter(client=mock_client, company="Co")
    await writer.create_credit_note(
        date="20260210", party_ledger="Customer", sales_ledger="Sales",
        amount=1000.0, narration="return", bill_ref="ORIG-1",
        reference="INV-9", reference_date="20260210",
    )
    xml = _posted_xml(mock_client)
    assert "<REFERENCE>INV-9</REFERENCE>" in xml
    assert "<REFERENCEDATE>20260210</REFERENCEDATE>" in xml
