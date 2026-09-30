"""Unit tests for backend/agents/tools.py — tool schemas and handler mapping."""

import asyncio
import inspect

import pytest

from backend.agents.tools import TALLY_TOOLS, TOOL_HANDLERS, execute_tool, DATE_TOOLS, execute_date_tool


# ---------------------------------------------------------------------------
# Expected tool catalogue
# ---------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = [
    "get_trial_balance",
    "get_profit_and_loss",
    "get_balance_sheet",
    "get_ledger_transactions",
    "get_day_book",
    "get_outstanding_receivables",
    "get_outstanding_payables",
    "get_stock_summary",
    "get_sales_register",
    "get_purchase_register",
    "search_ledger",
    "list_companies",
    "list_all_ledgers",
    "list_stock_items",
    "list_account_groups",
    "get_payment_register",
    "get_receipt_register",
    "get_cash_flow",
]

# Tools that use from_date + to_date
DATE_RANGE_TOOLS = [
    "get_trial_balance",
    "get_profit_and_loss",
    "get_ledger_transactions",
    "get_day_book",
    "get_sales_register",
    "get_purchase_register",
    "get_payment_register",
    "get_receipt_register",
    "get_cash_flow",
]

# Tools that use as_on_date
AS_ON_DATE_TOOLS = [
    "get_balance_sheet",
    "get_outstanding_receivables",
    "get_outstanding_payables",
    "get_stock_summary",
]


def _tool_by_name(name: str) -> dict:
    """Helper to find a tool schema by name."""
    for t in TALLY_TOOLS:
        if t["name"] == name:
            return t
    raise KeyError(f"Tool {name!r} not found in TALLY_TOOLS")


# ---------------------------------------------------------------------------
# TestToolSchemas
# ---------------------------------------------------------------------------


class TestToolSchemas:
    """Validate the TALLY_TOOLS list matches Claude tool-calling format."""

    def test_all_tools_have_required_fields(self):
        """Every tool must have name, description, and input_schema with type=object."""
        for tool in TALLY_TOOLS:
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool {tool.get('name')} missing 'description'"
            assert "input_schema" in tool, f"Tool {tool['name']} missing 'input_schema'"
            schema = tool["input_schema"]
            assert schema.get("type") == "object", (
                f"Tool {tool['name']} input_schema.type must be 'object'"
            )
            assert "properties" in schema, (
                f"Tool {tool['name']} input_schema missing 'properties'"
            )

    def test_tool_names_are_unique(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert len(names) == len(set(names)), "Duplicate tool names detected"

    def test_expected_tools_exist(self):
        """All 18 expected tool names must be present."""
        actual_names = {t["name"] for t in TALLY_TOOLS}
        for name in EXPECTED_TOOL_NAMES:
            assert name in actual_names, f"Expected tool {name!r} not found"
        assert len(TALLY_TOOLS) == 18

    def test_list_stock_items_tool_exists(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert "list_stock_items" in names

    def test_list_all_ledgers_tool_exists(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert "list_all_ledgers" in names

    def test_list_account_groups_tool_exists(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert "list_account_groups" in names

    def test_payment_register_tool_exists(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert "get_payment_register" in names

    def test_receipt_register_tool_exists(self):
        names = [t["name"] for t in TALLY_TOOLS]
        assert "get_receipt_register" in names

    def test_new_tools_have_handlers(self):
        for name in ["list_stock_items", "list_all_ledgers", "list_account_groups"]:
            assert name in TOOL_HANDLERS, f"Missing handler for {name}"

    def test_date_tools_require_dates(self):
        """Date-range tools must require from_date and to_date."""
        for name in DATE_RANGE_TOOLS:
            tool = _tool_by_name(name)
            schema = tool["input_schema"]
            required = schema.get("required", [])
            assert "from_date" in required, f"{name} must require from_date"
            assert "to_date" in required, f"{name} must require to_date"

    def test_as_on_date_tools(self):
        """Point-in-time tools must require as_on_date."""
        for name in AS_ON_DATE_TOOLS:
            tool = _tool_by_name(name)
            schema = tool["input_schema"]
            required = schema.get("required", [])
            assert "as_on_date" in required, f"{name} must require as_on_date"

    def test_ledger_transactions_requires_ledger_name(self):
        tool = _tool_by_name("get_ledger_transactions")
        required = tool["input_schema"].get("required", [])
        assert "ledger_name" in required

    def test_search_ledger_requires_search_term(self):
        tool = _tool_by_name("search_ledger")
        required = tool["input_schema"].get("required", [])
        assert "search_term" in required

    def test_list_companies_has_no_required_params(self):
        tool = _tool_by_name("list_companies")
        required = tool["input_schema"].get("required", [])
        assert required == [] or required is None or len(required) == 0

    def test_day_book_has_optional_voucher_type(self):
        tool = _tool_by_name("get_day_book")
        props = tool["input_schema"]["properties"]
        assert "voucher_type" in props
        required = tool["input_schema"].get("required", [])
        assert "voucher_type" not in required

    def test_stock_summary_has_optional_stock_group(self):
        tool = _tool_by_name("get_stock_summary")
        props = tool["input_schema"]["properties"]
        assert "stock_group" in props
        required = tool["input_schema"].get("required", [])
        assert "stock_group" not in required

    def test_descriptions_are_non_empty(self):
        for tool in TALLY_TOOLS:
            assert len(tool["description"]) > 10, (
                f"Tool {tool['name']} description too short"
            )


# ---------------------------------------------------------------------------
# TestToolHandlers
# ---------------------------------------------------------------------------


class TestToolHandlers:
    """Validate TOOL_HANDLERS dict matches TALLY_TOOLS."""

    def test_every_tool_has_handler(self):
        """Every tool in TALLY_TOOLS must have a corresponding handler."""
        tool_names = {t["name"] for t in TALLY_TOOLS}
        handler_names = set(TOOL_HANDLERS.keys())
        assert tool_names == handler_names, (
            f"Mismatch — missing handlers: {tool_names - handler_names}, "
            f"extra handlers: {handler_names - tool_names}"
        )

    def test_handlers_are_callable(self):
        for name, handler in TOOL_HANDLERS.items():
            assert callable(handler), f"Handler for {name!r} is not callable"

    def test_handlers_are_async(self):
        """All handlers must be async functions (coroutine functions)."""
        for name, handler in TOOL_HANDLERS.items():
            assert inspect.iscoroutinefunction(handler), (
                f"Handler for {name!r} must be async"
            )


# ---------------------------------------------------------------------------
# TestExecuteTool
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """Test the execute_tool entry point with mocked client."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await execute_tool(None, "nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_connection_error_returns_error(self):
        """TallyConnectionError should be caught and returned as error dict."""
        from unittest.mock import AsyncMock, patch
        from backend.tally_bridge.exceptions import TallyConnectionError

        mock_client = AsyncMock()
        with patch.dict(
            TOOL_HANDLERS,
            {"list_companies": AsyncMock(side_effect=TallyConnectionError("offline"))},
        ):
            result = await execute_tool(mock_client, "list_companies", {})
        assert "error" in result
        assert "offline" in result["error"]

    @pytest.mark.asyncio
    async def test_response_error_returns_error(self):
        from unittest.mock import AsyncMock, patch
        from backend.tally_bridge.exceptions import TallyResponseError

        mock_client = AsyncMock()
        with patch.dict(
            TOOL_HANDLERS,
            {"list_companies": AsyncMock(side_effect=TallyResponseError("bad xml"))},
        ):
            result = await execute_tool(mock_client, "list_companies", {})
        assert "error" in result
        assert "bad xml" in result["error"]

    @pytest.mark.asyncio
    async def test_success_returns_data(self):
        from unittest.mock import AsyncMock, patch

        mock_client = AsyncMock()
        with patch.dict(
            TOOL_HANDLERS,
            {"list_companies": AsyncMock(return_value=[{"name": "Acme"}])},
        ):
            result = await execute_tool(mock_client, "list_companies", {})
        assert result == {"success": True, "data": [{"name": "Acme"}]}

    @pytest.mark.asyncio
    async def test_unexpected_error_logs_exception(self):
        """Issue #7: Unexpected exceptions must be logged before returning error."""
        from unittest.mock import AsyncMock, patch

        mock_client = AsyncMock()
        with patch.dict(
            TOOL_HANDLERS,
            {"list_companies": AsyncMock(side_effect=RuntimeError("boom"))},
        ):
            with patch("backend.agents.tools.logger") as mock_logger:
                result = await execute_tool(mock_client, "list_companies", {})

                mock_logger.exception.assert_called_once()

        assert "error" in result
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestDateToolExecution
# ---------------------------------------------------------------------------


class TestDateToolExecution:
    """Test the execute_date_tool entry point."""

    def test_resolve_date_range_q2(self):
        result = execute_date_tool("resolve_date_range", {"description": "Q2"})
        assert result["success"] is True
        assert "from_date" in result["data"]

    def test_unknown_date_tool(self):
        result = execute_date_tool("unknown_tool", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# TestExecuteToolDateValidation
# ---------------------------------------------------------------------------


class TestExecuteToolDateValidation:
    """Test date validation and autofix in execute_tool."""

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_garbage_from_date(self):
        """Garbage date string must be rejected before reaching Tally."""
        from unittest.mock import AsyncMock
        client = AsyncMock()
        result = await execute_tool(client, "get_day_book", {
            "from_date": "garbage", "to_date": "31-03-2026"
        })
        assert "error" in result
        assert "DD-MM-YYYY" in result["error"]
        client.post_xml.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_garbage_to_date(self):
        """Bad to_date also rejected."""
        from unittest.mock import AsyncMock
        client = AsyncMock()
        result = await execute_tool(client, "get_trial_balance", {
            "from_date": "01-04-2025", "to_date": "not-a-date"
        })
        assert "error" in result
        assert "DD-MM-YYYY" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_rejects_garbage_as_on_date(self):
        """as_on_date param validated too."""
        from unittest.mock import AsyncMock
        client = AsyncMock()
        result = await execute_tool(client, "get_balance_sheet", {
            "as_on_date": "31/03/2026"
        })
        assert "error" in result
        assert "DD-MM-YYYY" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_autofixes_iso_from_date(self):
        """ISO YYYY-MM-DD auto-converted to DD-MM-YYYY before calling handler."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value={"report_name": "Trial Balance", "rows": []})
        with patch.dict(TOOL_HANDLERS, {"get_trial_balance": mock_handler}):
            result = await execute_tool(client, "get_trial_balance", {
                "from_date": "2025-04-01", "to_date": "2026-03-31"
            })
        call_kwargs = mock_handler.call_args
        assert call_kwargs is not None
        _, kwargs = call_kwargs
        assert kwargs["from_date"] == "01-04-2025"
        assert kwargs["to_date"] == "31-03-2026"

    @pytest.mark.asyncio
    async def test_execute_tool_autofixes_iso_as_on_date(self):
        """ISO as_on_date also auto-converted."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value={"report_name": "Balance Sheet", "rows": []})
        with patch.dict(TOOL_HANDLERS, {"get_balance_sheet": mock_handler}):
            result = await execute_tool(client, "get_balance_sheet", {
                "as_on_date": "2026-03-31"
            })
        _, kwargs = mock_handler.call_args
        assert kwargs["as_on_date"] == "31-03-2026"

    @pytest.mark.asyncio
    async def test_execute_tool_valid_dates_pass_through(self):
        """Valid DD-MM-YYYY dates pass through unchanged."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value=[])
        with patch.dict(TOOL_HANDLERS, {"get_sales_register": mock_handler}):
            result = await execute_tool(client, "get_sales_register", {
                "from_date": "01-04-2025", "to_date": "31-03-2026"
            })
        _, kwargs = mock_handler.call_args
        assert kwargs["from_date"] == "01-04-2025"
        assert kwargs["to_date"] == "31-03-2026"

    @pytest.mark.asyncio
    async def test_execute_tool_no_dates_skips_validation(self):
        """Tools without date params skip validation."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value=[{"name": "Test Co"}])
        with patch.dict(TOOL_HANDLERS, {"list_companies": mock_handler}):
            result = await execute_tool(client, "list_companies", {})
        assert "error" not in result
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_tool_unknown_tool(self):
        """Unknown tool name returns error."""
        from unittest.mock import AsyncMock
        client = AsyncMock()
        result = await execute_tool(client, "nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_tally_connection_error(self):
        """TallyConnectionError is caught and returned as error dict."""
        from unittest.mock import AsyncMock, patch
        from backend.tally_bridge.exceptions import TallyConnectionError
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(side_effect=TallyConnectionError("Tally unreachable"))
        with patch.dict(TOOL_HANDLERS, {"get_trial_balance": mock_handler}):
            result = await execute_tool(client, "get_trial_balance", {
                "from_date": "01-04-2025", "to_date": "31-03-2026"
            })
        assert "error" in result
        assert "unreachable" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_success_wraps_data(self):
        """Successful tool call wraps result in {success: True, data: ...}."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value=[{"name": "Cash", "parent": "Cash-in-Hand"}])
        with patch.dict(TOOL_HANDLERS, {"search_ledger": mock_handler}):
            result = await execute_tool(client, "search_ledger", {"search_term": "cash"})
        assert result["success"] is True
        assert result["data"] == [{"name": "Cash", "parent": "Cash-in-Hand"}]

    @pytest.mark.asyncio
    async def test_execute_tool_tally_response_error(self):
        """TallyResponseError is caught and returned as error dict."""
        from unittest.mock import AsyncMock, patch
        from backend.tally_bridge.exceptions import TallyResponseError
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(side_effect=TallyResponseError("Invalid XML response"))
        with patch.dict(TOOL_HANDLERS, {"get_day_book": mock_handler}):
            result = await execute_tool(client, "get_day_book", {
                "from_date": "01-04-2025", "to_date": "30-04-2025"
            })
        assert "error" in result
        assert "Invalid XML" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_does_not_mutate_caller_dict(self):
        """execute_tool must not mutate the caller's tool_input dict."""
        from unittest.mock import AsyncMock, patch
        from backend.agents.tools import TOOL_HANDLERS
        client = AsyncMock()
        mock_handler = AsyncMock(return_value=[])
        original_input = {"from_date": "2025-04-01", "to_date": "2026-03-31"}
        with patch.dict(TOOL_HANDLERS, {"get_sales_register": mock_handler}):
            await execute_tool(client, "get_sales_register", original_input)
        # Caller's dict must be unchanged (ISO format preserved)
        assert original_input["from_date"] == "2025-04-01"
        assert original_input["to_date"] == "2026-03-31"


# ---------------------------------------------------------------------------
# TestCrashScenarioEndToEnd — exact scenario that crashed Tally
# ---------------------------------------------------------------------------


class TestCrashScenarioEndToEnd:
    """End-to-end tests verifying bad dates never reach Tally XML.

    These use the REAL handlers (not mocks) — only the HTTP client is mocked.
    This proves the full chain: execute_tool → handler → request_builder → XML.
    """

    @pytest.mark.asyncio
    async def test_iso_date_produces_correct_xml_for_sales_register(self):
        """The exact crash scenario: Claude sends ISO dates for sales register.
        Verify the XML sent to Tally has DD-MM-YYYY, not YYYY-MM-DD."""
        from unittest.mock import AsyncMock
        from backend.tally_bridge.client import TallyClient

        # Mock only the HTTP layer — real handler + real request_builder
        client = AsyncMock(spec=TallyClient)
        client.post_xml = AsyncMock(return_value="<ENVELOPE><COLLECTION></COLLECTION></ENVELOPE>")

        result = await execute_tool(client, "get_sales_register", {
            "from_date": "2025-04-01",  # ISO format — what Claude might send
            "to_date": "2026-03-31",
        })

        # Verify post_xml was called (handler ran)
        client.post_xml.assert_called_once()
        xml_sent = client.post_xml.call_args[0][0]

        # The XML must contain DD-MM-YYYY dates, NOT ISO format
        assert "01-04-2025" in xml_sent
        assert "31-03-2026" in xml_sent
        assert "2025-04-01" not in xml_sent  # ISO must NOT appear
        assert "2026-03-31" not in xml_sent
        # Must NOT have broken $$InDateRange (crashes Tally)
        assert "InDateRange" not in xml_sent

    @pytest.mark.asyncio
    async def test_iso_date_produces_correct_xml_for_trial_balance(self):
        """ISO dates auto-fixed for trial balance report XML."""
        from unittest.mock import AsyncMock
        from backend.tally_bridge.client import TallyClient

        client = AsyncMock(spec=TallyClient)
        # Trial balance returns XML with DSPACCNAME/DSPACCINFO sibling pairs
        client.post_xml = AsyncMock(return_value="<ENVELOPE><DSPACCNAME><DSPDISPNAME>Test</DSPDISPNAME></DSPACCNAME><DSPACCINFO><DSPCLAMT>100</DSPCLAMT></DSPACCINFO></ENVELOPE>")

        result = await execute_tool(client, "get_trial_balance", {
            "from_date": "2025-04-01",
            "to_date": "2026-03-31",
        })

        xml_sent = client.post_xml.call_args[0][0]
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml_sent
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml_sent
        assert "2025-04-01" not in xml_sent
        assert "2026-03-31" not in xml_sent

    @pytest.mark.asyncio
    async def test_garbage_date_never_reaches_tally(self):
        """Garbage date must be rejected — no HTTP call to Tally at all."""
        from unittest.mock import AsyncMock
        from backend.tally_bridge.client import TallyClient

        client = AsyncMock(spec=TallyClient)

        result = await execute_tool(client, "get_sales_register", {
            "from_date": "not-a-real-date",
            "to_date": "31-03-2026",
        })

        assert "error" in result
        assert "DD-MM-YYYY" in result["error"]
        client.post_xml.assert_not_called()  # Tally never contacted
