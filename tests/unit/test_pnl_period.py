"""Unit tests for period-specific P&L error handling."""
import pytest
from unittest.mock import AsyncMock, patch
from datetime import date

from tally_bridge.queries.reports import (
    _parse_tally_date_str,
    profit_and_loss_period,
)
from tally_bridge.exceptions import TallyResponseError
from tally_bridge.models import ReportResponse


# ---------------------------------------------------------------------------
# _parse_tally_date_str
# ---------------------------------------------------------------------------


def test_parse_tally_date_str_normal():
    assert _parse_tally_date_str("01-04-2025") == date(2025, 4, 1)


def test_parse_tally_date_str_end_of_month():
    assert _parse_tally_date_str("31-03-2026") == date(2026, 3, 31)


def test_parse_tally_date_str_with_whitespace():
    assert _parse_tally_date_str(" 15-07-2025 ") == date(2025, 7, 15)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_row(name: str, amount: float) -> dict:
    return {
        "account_name": name,
        "debit_amount": amount if amount < 0 else 0.0,
        "credit_amount": amount if amount > 0 else 0.0,
        "closing_balance": amount,
    }


def _make_report(rows: list[dict]) -> ReportResponse:
    return ReportResponse(report_name="Profit and Loss", company="", rows=rows)


# ---------------------------------------------------------------------------
# profit_and_loss_period — FY start (still works)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_period_fy_start_no_subtraction():
    """When from_date is FY start (April 1), should fetch directly without subtraction."""
    mock_client = AsyncMock()
    expected = _make_report([_make_row("Sales Accounts", 1000.0)])

    with patch(
        "tally_bridge.queries.reports.profit_and_loss",
        new_callable=AsyncMock,
        return_value=expected,
    ) as mock_pnl:
        result = await profit_and_loss_period(
            mock_client, "01-04-2025", "30-06-2025"
        )
        # Should call profit_and_loss only once (direct fetch)
        mock_pnl.assert_called_once_with(
            mock_client, "01-04-2025", "30-06-2025", None
        )
        assert result == expected


# ---------------------------------------------------------------------------
# profit_and_loss_period — non-full-FY raises TallyResponseError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_period_non_full_fy_raises_error():
    """profit_and_loss_period() raises TallyResponseError for non-full-FY ranges."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(mock_client, "01-07-2025", "31-07-2025")


@pytest.mark.asyncio
async def test_pnl_period_q2_raises_error():
    """Q2 (Jul-Sep) should raise error — partial period P&L is unreliable."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(mock_client, "01-07-2025", "30-09-2025")


@pytest.mark.asyncio
async def test_pnl_period_q3_raises_error():
    """Q3 (Oct-Dec) should raise error."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(mock_client, "01-10-2025", "31-12-2025")


@pytest.mark.asyncio
async def test_pnl_period_jan_raises_error():
    """January (Q4, crosses calendar year) should raise error."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(mock_client, "01-01-2026", "31-03-2026")


@pytest.mark.asyncio
async def test_pnl_period_single_month_raises_error():
    """Single-month non-FY-start should raise error."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(mock_client, "01-08-2025", "31-08-2025")


@pytest.mark.asyncio
async def test_pnl_period_with_company_raises_error():
    """Company parameter doesn't change the error behavior for non-FY-start."""
    mock_client = AsyncMock()
    with pytest.raises(TallyResponseError, match="unreliable"):
        await profit_and_loss_period(
            mock_client, "01-07-2025", "30-09-2025", "TestCo"
        )
