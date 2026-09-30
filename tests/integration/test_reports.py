import pytest

from tests.mocks.mock_tally_server import create_mock_tally_app
from tally_bridge.client import TallyClient
from tally_bridge.queries.reports import (
    trial_balance,
    profit_and_loss,
    balance_sheet,
    bills_receivable,
    bills_payable,
    stock_summary,
)


@pytest.fixture
async def mock_tally(aiohttp_server):
    app = create_mock_tally_app()
    server = await aiohttp_server(app)
    client = TallyClient(host="localhost", port=server.port)
    return client


@pytest.mark.asyncio
async def test_trial_balance(mock_tally):
    result = await trial_balance(mock_tally, "01-04-2025", "31-03-2026")
    assert result.report_name == "Trial Balance"
    assert len(result.rows) == 9


@pytest.mark.asyncio
async def test_profit_and_loss(mock_tally):
    result = await profit_and_loss(mock_tally, "01-04-2025", "31-03-2026")
    assert result.report_name == "Profit and Loss"
    assert len(result.rows) > 0


@pytest.mark.asyncio
async def test_balance_sheet(mock_tally):
    result = await balance_sheet(mock_tally, "31-03-2026")
    assert result.report_name == "Balance Sheet"


@pytest.mark.asyncio
async def test_bills_receivable(mock_tally):
    bills = await bills_receivable(mock_tally, "31-03-2026")
    assert len(bills) == 6
    assert any(b.party_name == "Apex Technologies Pvt Ltd" for b in bills)
    assert any(b.amount == 55000.0 for b in bills)


@pytest.mark.asyncio
async def test_bills_payable(mock_tally):
    # mock_handler maps Bills Payable → bills_payable.xml (5 outstanding payables)
    bills = await bills_payable(mock_tally, "31-03-2026")
    assert len(bills) == 5
    assert any(b.party_name == "Samsung India Electronics" for b in bills)


@pytest.mark.asyncio
async def test_stock_summary(mock_tally):
    items = await stock_summary(mock_tally, "31-03-2026")
    assert len(items) == 15
    assert items[0]["name"] == "Samsung 24 inch Monitor"
    assert items[0]["closing_quantity"] == 42.0
