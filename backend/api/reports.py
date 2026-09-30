"""Reports endpoint — direct access to Tally reports bypassing the agent pipeline."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.dependencies import get_client
from backend.api.models import ReportResponse
from tally_bridge.client import TallyClient
from tally_bridge.queries import reports, vouchers

router = APIRouter()

# Reports that take from_date + to_date
_DATE_RANGE_REPORTS = {"trial_balance", "profit_and_loss"}

# Reports that take as_on_date
_AS_ON_DATE_REPORTS = {"balance_sheet", "bills_receivable", "bills_payable", "stock_summary"}

# Voucher reports that take from_date + to_date
_VOUCHER_REPORTS = {"day_book", "sales_register", "purchase_register"}

ALL_REPORT_NAMES = _DATE_RANGE_REPORTS | _AS_ON_DATE_REPORTS | _VOUCHER_REPORTS


def _to_table(data: Any) -> ReportResponse:
    """Convert various tally_bridge return types into a uniform ReportResponse."""
    # ReportResponse (from reports module) — has .rows list[dict]
    if hasattr(data, "rows"):
        rows = data.rows
        if rows:
            headers = list(rows[0].keys())
            return ReportResponse(
                headers=headers,
                rows=[list(r.values()) for r in rows],
            )
        return ReportResponse(headers=[], rows=[])

    # list[OutstandingBill] — Pydantic models
    if isinstance(data, list) and data and hasattr(data[0], "model_dump"):
        first = data[0].model_dump(mode="json")
        headers = list(first.keys())
        return ReportResponse(
            headers=headers,
            rows=[list(item.model_dump(mode="json").values()) for item in data],
        )

    # list[dict] — voucher data, stock summary
    if isinstance(data, list):
        if not data:
            return ReportResponse(headers=[], rows=[])
        headers = list(data[0].keys())
        return ReportResponse(
            headers=headers,
            rows=[list(row.values()) for row in data],
        )

    return ReportResponse(headers=[], rows=[])


@router.get("/reports/{name}", response_model=ReportResponse)
async def get_report(
    name: str,
    from_date: str | None = Query(None, description="Start date DD-MM-YYYY"),
    to_date: str | None = Query(None, description="End date DD-MM-YYYY"),
    as_on_date: str | None = Query(None, description="As-on date DD-MM-YYYY"),
    company: str | None = Query(None, description="Company name"),
    client: TallyClient = Depends(get_client),
) -> ReportResponse:
    if name not in ALL_REPORT_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown report: {name}. Valid reports: {sorted(ALL_REPORT_NAMES)}",
        )

    if name in _DATE_RANGE_REPORTS:
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from_date and to_date are required")
        func = getattr(reports, name)
        data = await func(client, from_date, to_date, company)
        return _to_table(data)

    if name in _AS_ON_DATE_REPORTS:
        date_val = as_on_date or to_date
        if not date_val:
            raise HTTPException(status_code=422, detail="as_on_date (or to_date) is required")
        if name == "stock_summary":
            data = await reports.stock_summary(client, date_val, company=company)
        else:
            func = getattr(reports, name)
            data = await func(client, date_val, company)
        return _to_table(data)

    # Voucher reports
    if not from_date or not to_date:
        raise HTTPException(status_code=422, detail="from_date and to_date are required")
    func = getattr(vouchers, name)
    data = await func(client, from_date, to_date, company=company)
    return _to_table(data)
