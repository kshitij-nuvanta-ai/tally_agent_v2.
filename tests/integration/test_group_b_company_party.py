"""Integration tests for Group B query primitives against the mock Tally server.

Covers:
  - get_company_list (masters.py)  -> verified probe E7
  - get_party_vouchers (vouchers.py) -> verified probe E8
"""
import pytest

from tests.mocks.mock_tally_server import create_mock_tally_app
from tally_bridge.client import TallyClient
from tally_bridge.queries.masters import get_company_list
from tally_bridge.queries.vouchers import get_party_vouchers


@pytest.fixture
async def mock_tally(aiohttp_server):
    app = create_mock_tally_app()
    server = await aiohttp_server(app)
    return TallyClient(host="localhost", port=server.port)


@pytest.mark.asyncio
async def test_get_company_list(mock_tally):
    companies = await get_company_list(mock_tally)
    assert isinstance(companies, list)
    assert "Bharat Traders Pvt Ltd" in companies
    assert all(isinstance(c, str) for c in companies)


@pytest.mark.asyncio
async def test_get_party_vouchers_known_party(mock_tally):
    rows = await get_party_vouchers(
        mock_tally, "Apex Technologies Pvt Ltd", ["Sales"]
    )
    assert len(rows) >= 1
    for r in rows:
        assert r["party"] == "Apex Technologies Pvt Ltd"
        assert r["voucher_type"] == "Sales"
        assert isinstance(r["amount"], float)
        assert {"date", "voucher_number", "voucher_type", "party", "reference", "amount"} <= r.keys()


@pytest.mark.asyncio
async def test_get_party_vouchers_type_filter(mock_tally):
    rows = await get_party_vouchers(
        mock_tally, "Apex Technologies Pvt Ltd", ["Purchase"]
    )
    assert rows == []


@pytest.mark.asyncio
async def test_get_party_vouchers_unknown_party(mock_tally):
    rows = await get_party_vouchers(mock_tally, "Nobody Ltd", ["Sales"])
    assert rows == []
