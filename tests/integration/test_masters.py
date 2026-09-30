import pytest

from tests.mocks.mock_tally_server import create_mock_tally_app
from tally_bridge.client import TallyClient
from tally_bridge.queries.masters import list_companies, list_ledgers, search_ledger


@pytest.fixture
async def mock_tally(aiohttp_server):
    app = create_mock_tally_app()
    server = await aiohttp_server(app)
    client = TallyClient(host="localhost", port=server.port)
    return client


@pytest.mark.asyncio
async def test_list_companies(mock_tally):
    companies = await list_companies(mock_tally)
    assert len(companies) == 1
    assert companies[0].name == "Bharat Traders Pvt Ltd"


@pytest.mark.asyncio
async def test_list_ledgers(mock_tally):
    ledgers = await list_ledgers(mock_tally)
    assert len(ledgers) == 34
    assert any(l.name == "Cash" for l in ledgers)


@pytest.mark.asyncio
async def test_search_ledger(mock_tally):
    results = await search_ledger(mock_tally, "HDFC")
    assert len(results) >= 1
    assert any("HDFC" in l.name for l in results)


@pytest.mark.asyncio
async def test_search_ledger_no_match(mock_tally):
    results = await search_ledger(mock_tally, "ZZZZNONEXISTENT")
    assert len(results) == 0
