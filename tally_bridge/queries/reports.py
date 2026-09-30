"""
Query functions for Tally financial reports.
"""

import logging
from datetime import date, datetime

from tally_bridge.client import TallyClient
from tally_bridge.models import ReportResponse, OutstandingBill
from tally_bridge.request_builder import (
    build_trial_balance,
    build_profit_and_loss,
    build_balance_sheet,
    build_bills_receivable,
    build_bills_payable,
    build_stock_summary,
    build_cash_flow,
)
from tally_bridge.response_parser import (
    detect_error,
    parse_trial_balance as _parse_tb,
    parse_profit_and_loss as _parse_pnl,
    parse_balance_sheet as _parse_bs,
    parse_bills,
    parse_stock_summary as _parse_stock,
    parse_cash_flow as _parse_cash_flow,
)
from tally_bridge.exceptions import TallyResponseError
from tally_bridge.dates import get_fy_start, format_for_tally

logger = logging.getLogger(__name__)


def _parse_tally_date(date_str: str) -> date | None:
    """Parse Tally date formats into date object.

    Tally uses multiple formats:
    - YYYYMMDD (voucher exports)
    - D-Mon-YY (bills: 2-Jul-25)
    - DD-MM-YYYY (request format)
    """
    if not date_str:
        return None
    for fmt in ("%Y%m%d", "%d-%b-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def trial_balance(
    client: TallyClient, from_date: str, to_date: str, company: str | None = None
) -> ReportResponse:
    """Fetch Trial Balance for a date range."""
    raw = await client.post_xml(build_trial_balance(from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    rows = _parse_tb(raw)
    return ReportResponse(
        report_name="Trial Balance",
        company=company or "",
        rows=rows,
    )


async def profit_and_loss(
    client: TallyClient, from_date: str, to_date: str, company: str | None = None
) -> ReportResponse:
    """Fetch Profit & Loss statement."""
    raw = await client.post_xml(build_profit_and_loss(from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    rows = _parse_pnl(raw)
    return ReportResponse(
        report_name="Profit and Loss",
        company=company or "",
        rows=rows,
    )


def _parse_tally_date_str(date_str: str) -> date:
    """Parse DD-MM-YYYY date string into a date object."""
    return datetime.strptime(date_str.strip(), "%d-%m-%Y").date()


async def profit_and_loss_period(
    client: TallyClient, from_date: str, to_date: str, company: str | None = None
) -> ReportResponse:
    """Fetch period-specific Profit & Loss.

    Tally's P&L report (TYPE=Data) returns unreliable data for partial
    Financial Year periods.  Only full-FY requests (from_date == FY start)
    are supported.  For monthly/quarterly breakdowns, callers should use
    get_sales_register or get_purchase_register instead.
    """
    from_dt = _parse_tally_date_str(from_date)
    fy_start = get_fy_start(from_dt)

    # If from_date is already FY start, fetch directly
    if from_dt == fy_start:
        logger.debug("P&L period: from_date is FY start, fetching directly")
        return await profit_and_loss(client, from_date, to_date, company)

    # Non-full-FY → Tally TYPE=Data P&L returns unreliable data for partial periods.
    raise TallyResponseError(
        "P&L for partial periods is unreliable via Tally's XML API. "
        "Use get_sales_register or get_purchase_register for monthly/quarterly "
        "breakdowns — they return accurate transaction-level data."
    )


async def balance_sheet(
    client: TallyClient, as_on_date: str, company: str | None = None
) -> ReportResponse:
    """Fetch Balance Sheet as on a date."""
    raw = await client.post_xml(build_balance_sheet(as_on_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    rows = _parse_bs(raw)
    return ReportResponse(
        report_name="Balance Sheet",
        company=company or "",
        rows=rows,
    )


async def bills_receivable(
    client: TallyClient, as_on_date: str, company: str | None = None
) -> list[OutstandingBill]:
    """Fetch outstanding receivables."""
    raw = await client.post_xml(build_bills_receivable(as_on_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    parsed = parse_bills(raw)
    return [
        OutstandingBill(
            party_name=b["party_name"],
            bill_number=b["bill_number"],
            bill_date=_parse_tally_date(b["bill_date"]) or date.today(),
            due_date=_parse_tally_date(b.get("due_date", "")),
            amount=b["amount"],
            pending_amount=b["pending_amount"],
            overdue_days=b.get("overdue_days"),
        )
        for b in parsed
    ]


async def bills_payable(
    client: TallyClient, as_on_date: str, company: str | None = None
) -> list[OutstandingBill]:
    """Fetch outstanding payables."""
    raw = await client.post_xml(build_bills_payable(as_on_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    parsed = parse_bills(raw)
    return [
        OutstandingBill(
            party_name=b["party_name"],
            bill_number=b["bill_number"],
            bill_date=_parse_tally_date(b["bill_date"]) or date.today(),
            due_date=_parse_tally_date(b.get("due_date", "")),
            amount=b["amount"],
            pending_amount=b["pending_amount"],
            overdue_days=b.get("overdue_days"),
        )
        for b in parsed
    ]


async def stock_summary(
    client: TallyClient, as_on_date: str, stock_group: str | None = None, company: str | None = None
) -> list[dict]:
    """Fetch stock/inventory summary."""
    raw = await client.post_xml(build_stock_summary(as_on_date, stock_group, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return _parse_stock(raw)


async def cash_flow(
    client: TallyClient,
    from_date: str,
    to_date: str,
    company: str | None = None,
) -> ReportResponse:
    """Fetch Cash Flow statement."""
    raw = await client.post_xml(build_cash_flow(from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    rows = _parse_cash_flow(raw)
    return ReportResponse(
        report_name="Cash Flow",
        company=company or "",
        from_date=datetime.strptime(from_date, "%d-%m-%Y").date(),
        to_date=datetime.strptime(to_date, "%d-%m-%Y").date(),
        rows=rows,
    )
