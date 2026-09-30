"""Integration tests — tool handlers execute against mock Tally server."""

import pytest

from tests.mocks.mock_tally_server import create_mock_tally_app
from tally_bridge.client import TallyClient
from backend.agents.tools import execute_tool


@pytest.fixture
async def mock_tally(aiohttp_server):
    app = create_mock_tally_app()
    server = await aiohttp_server(app)
    client = TallyClient(host="localhost", port=server.port)
    return client


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_get_trial_balance(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_trial_balance",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert result["data"]["report_name"] == "Trial Balance"
        assert len(result["data"]["rows"]) > 0

    @pytest.mark.asyncio
    async def test_get_profit_and_loss(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_profit_and_loss",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert result["data"]["report_name"] == "Profit and Loss"
        assert len(result["data"]["rows"]) > 0

    @pytest.mark.asyncio
    async def test_get_balance_sheet(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_balance_sheet",
            {"as_on_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert result["data"]["report_name"] == "Balance Sheet"

    @pytest.mark.asyncio
    async def test_list_companies(self, mock_tally):
        result = await execute_tool(mock_tally, "list_companies", {})
        assert result["success"] is True
        assert isinstance(result["data"], list)
        assert len(result["data"]) > 0
        # Verify structure: each item has a "name" key
        assert "name" in result["data"][0]
        assert result["data"][0]["name"] == "Bharat Traders Pvt Ltd"

    @pytest.mark.asyncio
    async def test_search_ledger(self, mock_tally):
        result = await execute_tool(
            mock_tally, "search_ledger", {"search_term": "Cash"}
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)
        assert len(result["data"]) >= 1
        assert any("Cash" in item["name"] for item in result["data"])

    @pytest.mark.asyncio
    async def test_search_ledger_no_match(self, mock_tally):
        result = await execute_tool(
            mock_tally, "search_ledger", {"search_term": "ZZZZNONEXISTENT"}
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)
        assert len(result["data"]) == 0

    @pytest.mark.asyncio
    async def test_get_sales_register(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_sales_register",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)
        assert len(result["data"]) > 0

    @pytest.mark.asyncio
    async def test_get_purchase_register(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_purchase_register",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_get_day_book(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_day_book",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_get_outstanding_receivables(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_outstanding_receivables",
            {"as_on_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)
        assert len(result["data"]) > 0
        # Verify structure has expected bill fields
        bill = result["data"][0]
        assert "party_name" in bill
        assert "bill_number" in bill
        assert "amount" in bill

    @pytest.mark.asyncio
    async def test_get_outstanding_payables(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_outstanding_payables",
            {"as_on_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_get_stock_summary(self, mock_tally):
        result = await execute_tool(
            mock_tally,
            "get_stock_summary",
            {"as_on_date": "31-03-2026"},
        )
        assert result["success"] is True
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, mock_tally):
        result = await execute_tool(mock_tally, "nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_connection_error_returns_error(self):
        bad_client = TallyClient(host="localhost", port=19999)
        result = await execute_tool(bad_client, "list_companies", {})
        assert "error" in result
        assert (
            "connection" in result["error"].lower()
            or "connect" in result["error"].lower()
        )


class TestTransportErrors:
    """Issue #9: TransportError variants must be caught gracefully."""

    @pytest.mark.asyncio
    async def test_server_drops_connection_mid_response(self, aiohttp_server):
        """Server that closes connection mid-response triggers TransportError."""
        from aiohttp import web
        from tally_bridge.exceptions import TallyConnectionError

        async def drop_connection(request: web.Request) -> web.Response:
            # Start writing a response then raise to simulate connection drop
            raise ConnectionResetError("simulated drop")

        app = web.Application()
        app.router.add_post("/", drop_connection)
        server = await aiohttp_server(app)

        client = TallyClient(host="localhost", port=server.port)
        result = await execute_tool(client, "list_companies", {})

        assert "error" in result
        # Should be caught as TallyConnectionError or returned as error dict
        assert isinstance(result["error"], str)

    @pytest.mark.asyncio
    async def test_xml_escaping_through_tool_execution(self, mock_tally):
        """Issue #6: Special chars in tool input go through full pipeline safely."""
        result = await execute_tool(
            mock_tally,
            "get_ledger_transactions",
            {
                "ledger_name": "M/s Sharma & Sons",
                "from_date": "01-04-2025",
                "to_date": "31-03-2026",
            },
        )
        # Should succeed (mock server returns day_book fixture for LedgerVchs)
        assert result["success"] is True
        assert isinstance(result["data"], list)


class TestConnectionPooling:
    """Issue #19: Reused httpx.AsyncClient works across multiple requests."""

    @pytest.mark.asyncio
    async def test_multiple_requests_reuse_same_client(self, mock_tally):
        """Multiple sequential tool calls through same TallyClient should all succeed."""
        r1 = await execute_tool(mock_tally, "list_companies", {})
        r2 = await execute_tool(mock_tally, "get_trial_balance", {"from_date": "01-04-2025", "to_date": "31-03-2026"})
        r3 = await execute_tool(mock_tally, "search_ledger", {"search_term": "Cash"})

        assert r1["success"] is True
        assert r2["success"] is True
        assert r3["success"] is True

    @pytest.mark.asyncio
    async def test_client_close_then_reopen(self, aiohttp_server):
        """After close(), creating a new client should work."""
        app = create_mock_tally_app()
        server = await aiohttp_server(app)

        client = TallyClient(host="localhost", port=server.port)
        r1 = await execute_tool(client, "list_companies", {})
        assert r1["success"] is True

        await client.close()

        # New client should work fine
        client2 = TallyClient(host="localhost", port=server.port)
        r2 = await execute_tool(client2, "list_companies", {})
        assert r2["success"] is True
        await client2.close()
