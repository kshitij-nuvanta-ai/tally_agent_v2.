"""Tests for GET /api/reports/{name} endpoint."""

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.reports import router
from backend.api.dependencies import get_client
from tally_bridge.models import ReportResponse as TallyReportResponse, OutstandingBill
from datetime import date


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def mock_client():
    return AsyncMock()


def test_trial_balance(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    tally_resp = TallyReportResponse(
        report_name="Trial Balance",
        company="Test",
        rows=[{"account_name": "Cash", "debit_amount": 1000, "credit_amount": 0, "closing_balance": 1000}],
    )
    with patch("backend.api.reports.reports.trial_balance", new_callable=AsyncMock) as mock_tb:
        mock_tb.return_value = tally_resp
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/trial_balance?from_date=01-04-2025&to_date=31-03-2026")
    assert resp.status_code == 200
    body = resp.json()
    assert "headers" in body
    assert "rows" in body
    assert len(body["rows"]) == 1


def test_balance_sheet(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    tally_resp = TallyReportResponse(
        report_name="Balance Sheet",
        company="Test",
        rows=[{"group_name": "Current Assets", "amount": 50000}],
    )
    with patch("backend.api.reports.reports.balance_sheet", new_callable=AsyncMock) as mock_bs:
        mock_bs.return_value = tally_resp
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/balance_sheet?as_on_date=31-03-2026")
    assert resp.status_code == 200


def test_bills_receivable(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    bills = [
        OutstandingBill(
            party_name="Customer A", bill_number="INV-001",
            bill_date=date(2025, 7, 1), amount=10000, pending_amount=10000,
        ),
    ]
    with patch("backend.api.reports.reports.bills_receivable", new_callable=AsyncMock) as mock_br:
        mock_br.return_value = bills
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/bills_receivable?as_on_date=31-03-2026")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1


def test_stock_summary(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    stock_data = [{"item_name": "Widget", "quantity": 100, "rate": 50, "value": 5000}]
    with patch("backend.api.reports.reports.stock_summary", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = stock_data
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/stock_summary?as_on_date=31-03-2026")
    assert resp.status_code == 200


def test_invalid_report_name(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    with TestClient(app) as tc:
        resp = tc.get("/api/reports/nonexistent?from_date=01-04-2025&to_date=31-03-2026")
    assert resp.status_code == 400


def test_missing_required_dates_for_date_range_report(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    with TestClient(app) as tc:
        resp = tc.get("/api/reports/trial_balance")
    assert resp.status_code == 422


def test_day_book(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    vouchers = [
        {"date": "20250701", "voucher_type": "Sales", "voucher_number": "S-001",
         "party_name": "Customer A", "amount": 10000},
    ]
    with patch("backend.api.reports.vouchers.day_book", new_callable=AsyncMock) as mock_db:
        mock_db.return_value = vouchers
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/day_book?from_date=01-04-2025&to_date=31-03-2026")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1


def test_sales_register(app, mock_client):
    app.dependency_overrides[get_client] = lambda: mock_client
    vouchers = [{"date": "20250701", "voucher_type": "Sales", "amount": 5000}]
    with patch("backend.api.reports.vouchers.sales_register", new_callable=AsyncMock) as mock_sr:
        mock_sr.return_value = vouchers
        with TestClient(app) as tc:
            resp = tc.get("/api/reports/sales_register?from_date=01-04-2025&to_date=31-03-2026")
    assert resp.status_code == 200
