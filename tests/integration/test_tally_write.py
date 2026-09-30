"""Integration tests for Tally write operations using the mock handler.

Exercises the full stack: TallyWriter → import_builder (XML) → TallyClient →
mock_handler → response_parser → back to writer. No real Tally touched.
"""
import pytest
import pytest_asyncio

from tally_bridge.client import TallyClient
from tally_bridge.mock_handler import reset_mock_state
from tally_bridge.writer import TallyWriter, ValidationError


@pytest_asyncio.fixture
async def mock_client():
    """TallyClient in mock mode — reset counter before each test."""
    reset_mock_state()
    client = TallyClient(host="localhost", port=9000)
    client.mock_mode = True
    try:
        yield client
    finally:
        await client.close()


class TestTallyWriteIntegration:
    @pytest.mark.asyncio
    async def test_create_payment_voucher(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=500.00,
            narration="Integration test expense",
        )
        assert result["success"] is True
        assert result["created"] == 1
        assert result["last_vch_id"] is not None

    @pytest.mark.asyncio
    async def test_create_payment_with_gst(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Office Supplies",
            credit_ledger="Cash",
            amount=1180.00,
            narration="Stationery with GST",
            gst_entries=[
                {"ledger": "INPUT CGST", "amount": 90.00},
                {"ledger": "INPUT SGST", "amount": 90.00},
            ],
        )
        assert result["success"] is True
        assert result["created"] == 1

    @pytest.mark.asyncio
    async def test_validation_fails_before_tally_call(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(ValidationError, match="[Nn]arration"):
            await writer.create_payment_voucher(
                date="20260404",
                debit_ledger="Travel",
                credit_ledger="Cash",
                amount=500.00,
                narration="",  # Empty narration
            )

    @pytest.mark.asyncio
    async def test_create_ledger(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_ledger(
            name="Test Ledger",
            parent="Indirect Expenses",
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_create_ledger_with_gstin(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_ledger(
            name="Supplier ABC",
            parent="Sundry Creditors",
            gstin="29XXXXX1234Z",
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_create_group(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_group(
            name="Test Group",
            parent="Indirect Expenses",
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_cancel_voucher_returns_altered(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.cancel_voucher(
            voucher_type="Payment",
            master_id="301",
            date="20260405",
            narration="User cancelled",
        )
        assert result["success"] is True
        assert result["altered"] == 1

    @pytest.mark.asyncio
    async def test_delete_voucher(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.delete_voucher(
            voucher_type="Payment",
            master_id="301",
            date="20260405",
        )
        assert result["success"] is True
        assert result["deleted"] == 1

    @pytest.mark.asyncio
    async def test_delete_ledger(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.delete_ledger(name="Temp Ledger")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_delete_group(self, mock_client):
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.delete_group(name="Temp Group")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_create_then_delete_cycle(self, mock_client):
        """Verify a full create → delete cycle works end-to-end."""
        writer = TallyWriter(client=mock_client, company="Test Co")

        create_result = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=100.00,
            narration="Will be deleted",
        )
        assert create_result["success"] is True
        vch_id = create_result["last_vch_id"]
        assert vch_id is not None

        delete_result = await writer.delete_voucher(
            voucher_type="Payment",
            master_id=vch_id,
            date="20260404",
        )
        assert delete_result["success"] is True
        assert delete_result["deleted"] == 1

    @pytest.mark.asyncio
    async def test_counter_increments_between_creates(self, mock_client):
        """Each create should return a different LASTVCHID (mock_handler counter)."""
        writer = TallyWriter(client=mock_client, company="Test Co")

        r1 = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=100.00,
            narration="First",
        )
        r2 = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=200.00,
            narration="Second",
        )
        assert r1["last_vch_id"] != r2["last_vch_id"]
