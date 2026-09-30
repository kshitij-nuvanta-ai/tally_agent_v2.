"""C33 regression: past-period voucher reads must return past-period data.

Live finding (2026-09-24, docs/backend-date-vars-audit-2026-09-24.md on the BI
branch): Tally honours SVFROMDATE/SVTODATE on a Voucher COLLECTION export only
when the variables are TYPED (``TYPE="Date"``). Untyped, it silently answers
for the company's CURRENT period — the Python date filter then empties that, so
"last year's sales" came back as a clean, empty success.

The mock Tally models this (current period = FY 2025-26, the seed company's
books; ``tests/fixtures/vouchers_prior_fy.xml`` holds earlier-FY vouchers the
way a multi-year company has them), so these tests go red if a builder ever
drops the type again. Only the network boundary is mocked: requests are built
by the production builders and parsed by the production parsers.
"""
from __future__ import annotations

import re

import pytest

from backend.tally_bridge.client import TallyClient
from backend.tally_bridge.mock_handler import mock_tally_request
from backend.tally_bridge.queries import vouchers as vq
from backend.tally_bridge.request_builder import (
    build_day_book,
    build_party_vouchers,
    build_profit_and_loss,
    build_sales_register,
)
from backend.tally_bridge.response_parser import parse_party_vouchers, parse_vouchers

PRIOR_FY = ("01-04-2024", "31-03-2025")
CURRENT_FY = ("01-04-2025", "31-03-2026")


def _untyped(xml: str) -> str:
    """The request exactly as the pre-fix backend shipped it (untyped dates)."""
    return (xml.replace('<SVFROMDATE TYPE="Date">', "<SVFROMDATE>")
               .replace('<SVTODATE TYPE="Date">', "<SVTODATE>"))


def _dates(raw: str) -> list[str]:
    return re.findall(r"<DATE[^>]*>(\d{8})</DATE>", raw)


@pytest.fixture
async def client():
    c = TallyClient()
    c.mock_mode = True
    yield c
    await c.close()


# --- Production query path (builder → mock Tally → parser) -----------------

async def test_prior_fy_sales_register_returns_prior_fy_sales(client):
    rows = await vq.sales_register(client, *PRIOR_FY)
    assert [r["voucher_number"] for r in rows] == ["S2425-004", "S2425-019", "S2425-031"]
    assert all("20240401" <= r["date"] <= "20250331" for r in rows)


async def test_prior_fy_purchase_register_returns_prior_fy_purchases(client):
    rows = await vq.purchase_register(client, *PRIOR_FY)
    assert [r["voucher_number"] for r in rows] == ["P2425-006", "P2425-014"]


async def test_prior_fy_day_book_returns_prior_fy_vouchers(client):
    rows = await vq.day_book(client, *PRIOR_FY)
    assert len(rows) == 7
    assert {r["voucher_type"] for r in rows} == {"Sales", "Purchase", "Payment", "Receipt"}
    assert all("20240401" <= r["date"] <= "20250331" for r in rows)


async def test_two_fys_back_single_day_window(client):
    rows = await vq.day_book(client, "01-06-2023", "02-06-2023")
    assert [r["voucher_number"] for r in rows] == ["S2324-011"]


async def test_current_fy_sales_register_unchanged(client):
    rows = await vq.sales_register(client, *CURRENT_FY)
    assert len(rows) == 16
    assert all(r["date"] >= "20250401" for r in rows)


async def test_multi_fy_window_spans_both_years(client):
    rows = await vq.sales_register(client, "01-04-2024", "31-03-2026")
    assert len(rows) == 3 + 16


# --- Mock models C33: untyped collection vars → current period -------------

def test_untyped_past_fy_sales_request_gets_current_period_data():
    raw = mock_tally_request(_untyped(build_sales_register(*PRIOR_FY)))
    ds = _dates(raw)
    assert ds, "mock returns the current period, like live Tally"
    assert all("20250401" <= d <= "20260331" for d in ds)
    # ...which the production parser's safety-net filter turns into nothing:
    assert parse_vouchers(raw, from_date=PRIOR_FY[0], to_date=PRIOR_FY[1]) == []


def test_untyped_past_day_book_request_is_empty_after_filter():
    raw = mock_tally_request(_untyped(build_day_book(*PRIOR_FY)))
    assert parse_vouchers(raw, from_date=PRIOR_FY[0], to_date=PRIOR_FY[1]) == []


def test_typed_past_fy_sales_request_gets_past_fy_data():
    raw = mock_tally_request(build_sales_register(*PRIOR_FY))
    ds = _dates(raw)
    assert ds and all("20240401" <= d <= "20250331" for d in ds)


def test_untyped_helper_really_strips_type():
    xml = _untyped(build_sales_register(*PRIOR_FY))
    assert "<SVFROMDATE>01-04-2024</SVFROMDATE>" in xml
    assert "<SVTODATE>31-03-2025</SVTODATE>" in xml


# --- Party vouchers (write-flow dedup / DN-CN lookup) ----------------------

def test_party_vouchers_typed_prior_fy_window_finds_prior_fy_invoice():
    raw = mock_tally_request(build_party_vouchers(
        "Apex Technologies Pvt Ltd", ["Sales"], *PRIOR_FY))
    refs = [v["reference"] for v in parse_party_vouchers(raw)]
    assert refs == ["INV-APX-2425-004"]


def test_party_vouchers_untyped_returns_current_period_only():
    raw = mock_tally_request(_untyped(build_party_vouchers(
        "Apex Technologies Pvt Ltd", ["Sales"], *PRIOR_FY)))
    rows = parse_party_vouchers(raw)
    assert rows, "untyped → current period (C33), not the asked window"
    assert all("20250401" <= r["date"] <= "20260331" for r in rows)


def test_party_vouchers_typed_window_excludes_out_of_window_rows():
    raw = mock_tally_request(build_party_vouchers(
        "Apex Technologies Pvt Ltd", ["Sales"], *CURRENT_FY))
    refs = [v["reference"] for v in parse_party_vouchers(raw)]
    assert refs == ["INV-APX-001", "INV-APX-002"]


# --- Reports: mock still reads the (typed or untyped) SVTODATE -------------

def test_pnl_typed_and_untyped_todate_both_honoured():
    typed = mock_tally_request(build_profit_and_loss("01-04-2025", "30-09-2025"))
    untyped = mock_tally_request(_untyped(build_profit_and_loss("01-04-2025", "30-09-2025")))
    full = mock_tally_request(build_profit_and_loss(*CURRENT_FY))
    assert typed == untyped
    assert typed != full
