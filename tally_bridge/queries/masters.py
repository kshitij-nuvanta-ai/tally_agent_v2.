"""
Query functions for Tally master data: companies, ledgers, groups, stock items.
"""

from tally_bridge.client import TallyClient
from tally_bridge.models import Company, Ledger, StockItem, AccountGroup
from tally_bridge.envelopes import build_company_list
from tally_bridge.request_builder import (
    build_list_ledgers,
    build_list_stock_items,
    build_list_stock_groups,
    build_list_groups,
)
from tally_bridge.response_parser import (
    detect_error,
    parse_ledger_list,
    parse_stock_items,
    parse_stock_group_list,
    parse_groups,
)
from tally_bridge.exceptions import TallyResponseError
from tally_bridge.xml_utils import parse_company_list


async def list_companies(client: TallyClient) -> list[Company]:
    """Fetch all companies loaded in TallyPrime."""
    return [Company(name=name) for name in await get_company_list(client)]


async def get_company_list(client: TallyClient) -> list[str]:
    """Fetch loaded company names via the verified probe-E7 envelope.

    Returns a plain list of company-name strings (used to populate the
    connect-company dropdown); ``list_companies`` wraps the same names in
    ``Company`` models.
    """
    raw = await client.post_xml(build_company_list())
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_company_list(raw)


async def list_ledgers(client: TallyClient, company: str | None = None) -> list[Ledger]:
    """Fetch all ledgers with parent groups and balances.

    When ``company`` is given, the read is scoped to that company via
    ``<SVCurrentCompany>`` (matches company-scoped writes). When None, reads
    Tally's active UI company (back-compat).
    """
    raw = await client.post_xml(build_list_ledgers(company=company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)

    parsed = parse_ledger_list(raw)
    return [Ledger(**row) for row in parsed]


async def search_ledger(client: TallyClient, search_term: str) -> list[Ledger]:
    """Search ledgers by partial name match (case-insensitive)."""
    all_ledgers = await list_ledgers(client)
    term_lower = search_term.lower()
    return [l for l in all_ledgers if term_lower in l.name.lower()]


async def list_stock_items(client: TallyClient) -> list[StockItem]:
    """Fetch all stock items with parent group, UOM, and closing values."""
    raw = await client.post_xml(build_list_stock_items())
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    parsed = parse_stock_items(raw)
    return [StockItem(**row) for row in parsed]


async def list_stock_groups(client: TallyClient) -> list[str]:
    """Fetch all stock group names (used to skip re-creating existing groups)."""
    raw = await client.post_xml(build_list_stock_groups())
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_stock_group_list(raw)


async def list_groups(client: TallyClient) -> list[AccountGroup]:
    """Fetch all account groups with parent hierarchy."""
    raw = await client.post_xml(build_list_groups())
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    parsed = parse_groups(raw)
    return [AccountGroup(**row) for row in parsed]
