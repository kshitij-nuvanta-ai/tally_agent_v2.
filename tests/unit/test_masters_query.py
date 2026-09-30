"""Unit tests for tally_bridge/queries/masters.py — list_stock_items and list_groups."""

import pytest
from unittest.mock import AsyncMock
from tally_bridge.queries.masters import list_stock_items


@pytest.mark.asyncio
async def test_list_stock_items_returns_stock_item_list():
    mock_client = AsyncMock()
    mock_client.post_xml.return_value = """<ENVELOPE><BODY><DATA><COLLECTION>
    <STOCKITEM><NAME>HP Laptop</NAME><PARENT>Electronics</PARENT>
    <BASEUNITS>Nos</BASEUNITS><CLOSINGBALANCE>10 Nos</CLOSINGBALANCE>
    <CLOSINGRATE>38000/Nos</CLOSINGRATE><CLOSINGVALUE>380000</CLOSINGVALUE></STOCKITEM>
    </COLLECTION></DATA></BODY></ENVELOPE>"""
    items = await list_stock_items(mock_client)
    assert len(items) == 1
    assert items[0].name == "HP Laptop"
    assert items[0].parent_group == "Electronics"


@pytest.mark.asyncio
async def test_list_groups_returns_group_list():
    mock_client = AsyncMock()
    mock_client.post_xml.return_value = """<ENVELOPE><BODY><DATA><COLLECTION>
    <GROUP><NAME>Sales Accounts</NAME><PARENT>Revenue</PARENT></GROUP>
    </COLLECTION></DATA></BODY></ENVELOPE>"""
    from tally_bridge.queries.masters import list_groups
    groups = await list_groups(mock_client)
    assert len(groups) == 1
    assert groups[0].name == "Sales Accounts"
    assert groups[0].parent == "Revenue"


@pytest.mark.asyncio
async def test_list_stock_groups_returns_names():
    mock_client = AsyncMock()
    mock_client.post_xml.return_value = """<ENVELOPE><BODY><DATA><COLLECTION>
    <STOCKGROUP NAME="Electronics"><NAME>Electronics</NAME></STOCKGROUP>
    <STOCKGROUP NAME="AI Imported Items"><NAME>AI Imported Items</NAME></STOCKGROUP>
    </COLLECTION></DATA></BODY></ENVELOPE>"""
    from tally_bridge.queries.masters import list_stock_groups
    groups = await list_stock_groups(mock_client)
    assert groups == ["Electronics", "AI Imported Items"]
