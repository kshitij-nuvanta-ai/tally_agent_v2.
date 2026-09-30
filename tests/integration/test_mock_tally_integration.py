"""Integration tests: mock_handler → response_parser full cycle.

Verifies that mock fixture XML responses parse correctly through
the same pipeline used for live Tally responses.
"""

import pytest
from tally_bridge.client import TallyClient
from tally_bridge.queries import masters, reports


@pytest.fixture
async def mock_client():
    client = TallyClient()
    client.mock_mode = True
    yield client
    await client.close()


@pytest.mark.asyncio
class TestMockTallyIntegration:
    async def test_list_companies(self, mock_client):
        companies = await masters.list_companies(mock_client)
        assert len(companies) >= 1
        names = [c.name for c in companies]
        assert any("Bharat" in n for n in names)

    async def test_trial_balance(self, mock_client):
        result = await reports.trial_balance(mock_client, "01-04-2025", "31-03-2026")
        assert result.report_name == "Trial Balance"
        assert len(result.rows) > 0
        # Verify row structure has expected keys
        row = result.rows[0]
        assert "account_name" in row
        assert "closing_balance" in row

    async def test_profit_and_loss(self, mock_client):
        result = await reports.profit_and_loss(mock_client, "01-04-2025", "31-03-2026")
        assert result.report_name == "Profit and Loss"
        assert len(result.rows) > 0
        row = result.rows[0]
        assert "account_name" in row
        assert "closing_balance" in row

    async def test_balance_sheet(self, mock_client):
        result = await reports.balance_sheet(mock_client, "31-03-2026")
        assert result.report_name == "Balance Sheet"
        assert len(result.rows) > 0
        row = result.rows[0]
        assert "account_name" in row
        assert "closing_balance" in row

    async def test_stock_summary(self, mock_client):
        result = await reports.stock_summary(mock_client, "31-03-2026")
        assert len(result) > 0

    async def test_ledger_list(self, mock_client):
        ledgers = await masters.list_ledgers(mock_client)
        assert len(ledgers) > 0
        # Verify ledger structure
        ledger = ledgers[0]
        assert ledger.name
