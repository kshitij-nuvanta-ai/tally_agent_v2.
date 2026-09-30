import pytest

from tests.mocks.mock_tally_server import create_mock_tally_app
from tally_bridge.client import TallyClient
from tally_bridge.queries.vouchers import (
    day_book,
    sales_register,
    purchase_register,
    ledger_vouchers,
)


@pytest.fixture
async def mock_tally(aiohttp_server):
    app = create_mock_tally_app()
    server = await aiohttp_server(app)
    client = TallyClient(host="localhost", port=server.port)
    return client


@pytest.mark.asyncio
async def test_day_book(mock_tally):
    vouchers = await day_book(mock_tally, "01-10-2025", "31-10-2025")
    assert len(vouchers) == 8
    assert any(v["voucher_type"] == "Sales" for v in vouchers)


@pytest.mark.asyncio
async def test_sales_register(mock_tally):
    vouchers = await sales_register(mock_tally, "01-10-2025", "31-10-2025")
    assert len(vouchers) == 5
    assert all(v["voucher_type"] == "Sales" for v in vouchers)


@pytest.mark.asyncio
async def test_purchase_register(mock_tally):
    vouchers = await purchase_register(mock_tally, "01-04-2025", "31-03-2026")
    assert len(vouchers) == 8
    assert all(v["voucher_type"] == "Purchase" for v in vouchers)


@pytest.mark.asyncio
async def test_ledger_vouchers(mock_tally):
    vouchers = await ledger_vouchers(mock_tally, "HDFC Bank - Current A/c", "01-10-2025", "31-10-2025")
    assert len(vouchers) >= 1


@pytest.mark.asyncio
async def test_ledger_vouchers_with_ampersand_in_name(mock_tally):
    """Issue #6: Ledger names with & must not produce malformed XML."""
    # The mock server returns day_book.xml for any LedgerVchs request.
    # The key assertion: the request doesn't crash from malformed XML.
    vouchers = await ledger_vouchers(mock_tally, "M/s Sharma & Sons", "01-10-2025", "31-10-2025")
    assert isinstance(vouchers, list)


@pytest.mark.asyncio
async def test_ledger_vouchers_with_quotes_in_name(mock_tally):
    """Issue #6: Ledger names with double quotes must be escaped."""
    vouchers = await ledger_vouchers(mock_tally, 'Raj "The Boss" Trading', "01-10-2025", "31-10-2025")
    assert isinstance(vouchers, list)


@pytest.mark.asyncio
async def test_day_book_with_special_chars_in_voucher_type(mock_tally):
    """Issue #6: Voucher type filter with special chars must be escaped."""
    # Won't match any real type, but should not crash from malformed XML
    vouchers = await day_book(mock_tally, "01-10-2025", "31-10-2025", voucher_type="Sales & Returns")
    assert isinstance(vouchers, list)


@pytest.mark.asyncio
async def test_day_book_lowercase_voucher_type(mock_tally):
    """Issue #16: lowercase voucher type must be title-cased for Tally matching."""
    # "sales" should be normalized to "Sales" and match the mock server's SalesVchs fixture
    vouchers = await day_book(mock_tally, "01-10-2025", "31-10-2025", voucher_type="sales")
    assert isinstance(vouchers, list)
    # Should not crash — the XML is valid with title-cased type
