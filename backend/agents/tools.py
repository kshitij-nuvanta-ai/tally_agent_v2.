"""
Claude-compatible tool definitions and handler mapping for the Tally Bridge layer.

Exports:
    TALLY_TOOLS   — List of tool schema dicts (Claude tool-calling format).
    TOOL_HANDLERS — Dict mapping tool name to async handler function.
    execute_tool  — Single entry point: look up handler, call it, return result or error.
"""

from __future__ import annotations

import logging
from typing import Any

from tally_bridge.client import TallyClient
from backend.utils.date_utils import resolve_date_range as _resolve_date_range
from backend.utils.date_utils import validate_tally_date

logger = logging.getLogger(__name__)
from tally_bridge.exceptions import TallyConnectionError, TallyResponseError
from tally_bridge.models import ReportResponse, OutstandingBill, Ledger, Company
from tally_bridge.queries import masters, reports, vouchers


# ---------------------------------------------------------------------------
# Tool Schemas (Claude tool-calling format)
# ---------------------------------------------------------------------------

TALLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_trial_balance",
        "description": "Fetch the Trial Balance report from TallyPrime for a date range. Returns account names with debit/credit/closing balances.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format (e.g. 01-04-2025)",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format (e.g. 31-03-2026)",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_profit_and_loss",
        "description": "Fetch the Profit & Loss statement from TallyPrime for a date range. Returns income and expense groups with amounts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format (e.g. 01-04-2025)",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format (e.g. 31-03-2026)",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_balance_sheet",
        "description": "Fetch the Balance Sheet from TallyPrime as on a specific date. Returns assets and liabilities with amounts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "as_on_date": {
                    "type": "string",
                    "description": "Date for the balance sheet in DD-MM-YYYY format (e.g. 31-03-2026)",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["as_on_date"],
        },
    },
    {
        "name": "get_ledger_transactions",
        "description": "Fetch all voucher transactions for a specific ledger account from TallyPrime. Returns date, type, amount, narration for each entry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ledger_name": {
                    "type": "string",
                    "description": "Exact ledger name as it appears in Tally (e.g. 'Cash', 'HDFC Bank')",
                },
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["ledger_name", "from_date", "to_date"],
        },
    },
    {
        "name": "get_day_book",
        "description": "Fetch the Day Book (all voucher entries) from TallyPrime for a date range. Optionally filter by voucher type (Sales, Purchase, Payment, Receipt, Journal, Contra).",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "voucher_type": {
                    "type": "string",
                    "description": "Optional voucher type filter (e.g. 'Sales', 'Purchase', 'Payment', 'Receipt')",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_outstanding_receivables",
        "description": "Fetch all outstanding receivable bills from TallyPrime as on a date. Returns party name, bill number, bill date, due date, overdue days, amount, and pending amount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "as_on_date": {
                    "type": "string",
                    "description": "Date for outstanding check in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["as_on_date"],
        },
    },
    {
        "name": "get_outstanding_payables",
        "description": "Fetch all outstanding payable bills from TallyPrime as on a date. Returns party name, bill number, bill date, due date, overdue days, amount, and pending amount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "as_on_date": {
                    "type": "string",
                    "description": "Date for outstanding check in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["as_on_date"],
        },
    },
    {
        "name": "get_stock_summary",
        "description": "Fetch stock/inventory summary from TallyPrime as on a date. Returns item names, quantities, rates, and values. Optionally filter by stock group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "as_on_date": {
                    "type": "string",
                    "description": "Date for stock summary in DD-MM-YYYY format",
                },
                "stock_group": {
                    "type": "string",
                    "description": "Optional stock group to filter by (e.g. 'Primary')",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["as_on_date"],
        },
    },
    {
        "name": "get_sales_register",
        "description": "Fetch the Sales Register from TallyPrime for a date range. Returns all sales vouchers with party, amount, and line items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_purchase_register",
        "description": "Fetch the Purchase Register from TallyPrime for a date range. Returns all purchase vouchers with party, amount, and line items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "search_ledger",
        "description": "Search for ledger accounts in TallyPrime by partial name match (case-insensitive). Returns matching ledger names, parent groups, and balances.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_term": {
                    "type": "string",
                    "description": "Partial name to search for (e.g. 'bank', 'sales', 'hdfc')",
                },
            },
            "required": ["search_term"],
        },
    },
    {
        "name": "list_companies",
        "description": "List all companies currently loaded in TallyPrime. Returns company names.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_all_ledgers",
        "description": "List all ledger accounts in TallyPrime. Returns every ledger with its name, parent group, opening balance, and closing balance. Use this to discover all available ledger names or for top-N-by-balance queries.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_stock_items",
        "description": "List all stock/inventory items in TallyPrime with their stock group, unit of measure (UOM), closing quantity, rate, and value. Use this instead of get_stock_summary when you need stock group or UOM information.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_account_groups",
        "description": "List all account groups in TallyPrime with their parent group. Returns the full chart of accounts group hierarchy (e.g. Sales Accounts → Revenue, Bank Accounts → Current Assets).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_payment_register",
        "description": "Fetch all Payment vouchers from TallyPrime for a date range. Returns payment entries with payee, bank/cash account, amount, and narration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_receipt_register",
        "description": "Fetch all Receipt vouchers from TallyPrime for a date range. Returns receipt entries with payer, bank/cash account, amount, and narration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    {
        "name": "get_cash_flow",
        "description": "Fetch the Cash Flow Statement from TallyPrime for a date range. Returns cash flows from operating, investing, and financing activities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Start date in DD-MM-YYYY format",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date in DD-MM-YYYY format",
                },
                "company": {
                    "type": "string",
                    "description": "Company name in Tally. Optional — uses active company if omitted.",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
]


DATE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "resolve_date_range",
        "description": "Convert a relative date expression to exact DD-MM-YYYY dates using the Indian Financial Year calendar. Use ONLY for relative expressions like 'this month', 'last quarter', 'YTD'. For specific months ('April 2025') or FY ('FY 2025-26'), compute dates yourself to save tool calls.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Natural-language date expression (e.g. 'Q2', 'this month', 'last quarter', 'current FY', 'last 3 months', 'YTD', 'Q2 2025-26')",
                },
            },
            "required": ["description"],
        },
    },
]


def execute_date_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute a date resolution tool synchronously."""
    if tool_name == "resolve_date_range":
        return {"success": True, "data": _resolve_date_range(tool_input["description"])}
    return {"error": f"Unknown date tool: {tool_name!r}"}


# ---------------------------------------------------------------------------
# Handler functions — each maps a tool call to a tally_bridge query
# ---------------------------------------------------------------------------


async def _handle_trial_balance(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.trial_balance(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )
    return result.model_dump(mode="json")


async def _handle_profit_and_loss(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.profit_and_loss_period(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )
    return result.model_dump(mode="json")


async def _handle_balance_sheet(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.balance_sheet(
        client, kwargs["as_on_date"], kwargs.get("company")
    )
    return result.model_dump(mode="json")


async def _handle_ledger_transactions(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.ledger_vouchers(
        client, kwargs["ledger_name"], kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )


async def _handle_day_book(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.day_book(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("voucher_type"), kwargs.get("company")
    )


async def _handle_outstanding_receivables(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.bills_receivable(
        client, kwargs["as_on_date"], kwargs.get("company")
    )
    return [b.model_dump(mode="json") for b in result]


async def _handle_outstanding_payables(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.bills_payable(
        client, kwargs["as_on_date"], kwargs.get("company")
    )
    return [b.model_dump(mode="json") for b in result]


async def _handle_stock_summary(client: TallyClient, **kwargs: Any) -> Any:
    return await reports.stock_summary(
        client, kwargs["as_on_date"], kwargs.get("stock_group"), kwargs.get("company")
    )


async def _handle_sales_register(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.sales_register(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )


async def _handle_purchase_register(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.purchase_register(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )


async def _handle_search_ledger(client: TallyClient, **kwargs: Any) -> Any:
    result = await masters.search_ledger(client, kwargs["search_term"])
    return [l.model_dump(mode="json") for l in result]


async def _handle_list_companies(client: TallyClient, **kwargs: Any) -> Any:
    result = await masters.list_companies(client)
    return [c.model_dump(mode="json") for c in result]


async def _handle_list_all_ledgers(client: TallyClient, **kwargs: Any) -> Any:
    result = await masters.list_ledgers(client)
    return [l.model_dump(mode="json") for l in result]


async def _handle_list_stock_items(client: TallyClient, **kwargs: Any) -> Any:
    result = await masters.list_stock_items(client)
    return [s.model_dump(mode="json") for s in result]


async def _handle_list_account_groups(client: TallyClient, **kwargs: Any) -> Any:
    result = await masters.list_groups(client)
    return [g.model_dump(mode="json") for g in result]


async def _handle_payment_register(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.day_book(
        client, kwargs["from_date"], kwargs["to_date"], "Payment", kwargs.get("company")
    )


async def _handle_receipt_register(client: TallyClient, **kwargs: Any) -> Any:
    return await vouchers.day_book(
        client, kwargs["from_date"], kwargs["to_date"], "Receipt", kwargs.get("company")
    )


async def _handle_cash_flow(client: TallyClient, **kwargs: Any) -> Any:
    result = await reports.cash_flow(
        client, kwargs["from_date"], kwargs["to_date"], kwargs.get("company")
    )
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# TOOL_HANDLERS — maps tool name to its async handler
# ---------------------------------------------------------------------------

TOOL_HANDLERS: dict[str, Any] = {
    "get_trial_balance": _handle_trial_balance,
    "get_profit_and_loss": _handle_profit_and_loss,
    "get_balance_sheet": _handle_balance_sheet,
    "get_ledger_transactions": _handle_ledger_transactions,
    "get_day_book": _handle_day_book,
    "get_outstanding_receivables": _handle_outstanding_receivables,
    "get_outstanding_payables": _handle_outstanding_payables,
    "get_stock_summary": _handle_stock_summary,
    "get_sales_register": _handle_sales_register,
    "get_purchase_register": _handle_purchase_register,
    "search_ledger": _handle_search_ledger,
    "list_companies": _handle_list_companies,
    "list_all_ledgers": _handle_list_all_ledgers,
    "list_stock_items": _handle_list_stock_items,
    "list_account_groups": _handle_list_account_groups,
    "get_payment_register": _handle_payment_register,
    "get_receipt_register": _handle_receipt_register,
    "get_cash_flow": _handle_cash_flow,
}


# ---------------------------------------------------------------------------
# execute_tool — single entry point for the agent layer
# ---------------------------------------------------------------------------

_DATE_PARAMS = {"from_date", "to_date", "as_on_date"}


async def execute_tool(
    client: TallyClient,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Execute a Tally tool by name with given inputs.

    Returns:
        {"success": True, "data": <result>}  on success
        {"error": "<message>"}                on failure
    """
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name!r}"}

    # Validate and autofix date parameters (shallow copy to avoid mutating caller's dict)
    validated_input = dict(tool_input)
    for param in _DATE_PARAMS:
        if param in validated_input:
            try:
                validated_input[param] = validate_tally_date(validated_input[param], autofix=True)
            except ValueError as exc:
                return {"error": str(exc)}

    try:
        result = await handler(client, **validated_input)
        return {"success": True, "data": result}
    except TallyConnectionError as exc:
        return {"error": str(exc)}
    except TallyResponseError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.exception("Unexpected error in tool %r", tool_name)
        return {"error": f"Unexpected error in {tool_name!r}: {exc}"}
