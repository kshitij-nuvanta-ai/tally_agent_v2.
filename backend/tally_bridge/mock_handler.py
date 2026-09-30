"""In-process mock Tally handler.

Returns fixture XML data for Bharat Traders Pvt Ltd demo company.
For TYPE=Data reports (P&L, TB), parses SVTODATE from the request
and computes cumulative figures from voucher data — matching real
Tally's behavior of returning cumulative from FY start.

For TYPE=Collection (vouchers) the mock models live C33 behaviour
(2026-09-24): Tally honours SVFROMDATE/SVTODATE on a Voucher collection only
when the variables are TYPED (``TYPE="Date"``). Typed → vouchers inside the
window (current-FY fixture + ``vouchers_prior_fy.xml``); untyped or absent →
the company's CURRENT period (FY 2025-26, the whole current-FY fixture),
regardless of the dates asked. Python-side date filtering still applies.
"""
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

# Mock write state (for demo/testing — resets on reset_mock_state call)
_mock_vch_counter = 0


def reset_mock_state() -> None:
    """Reset mock state (for test isolation)."""
    global _mock_vch_counter
    _mock_vch_counter = 0


# Reports that return full fixture regardless of dates
STATIC_FIXTURES: dict[str, str] = {
    "List of Companies": "company_list.xml",
    "CustomLedgerList": "ledger_list.xml",
    "Trial Balance": "trial_balance.xml",
    "Balance Sheet": "balance_sheet.xml",
    "Bills Receivable": "bills_receivable.xml",
    "Bills Payable": "bills_payable.xml",
    "Stock Summary": "stock_summary.xml",
    "CustomStockItemList": "stock_items_list.xml",
    "CustomStockGroupList": "stock_groups_list.xml",
    "CustomGroupList": "groups_list.xml",
    "Cash Flow": "cash_flow.xml",
}

# Voucher collections — return full data, Python filters by date
VOUCHER_FIXTURES: dict[str, str] = {
    "DayBookVchs": "day_book.xml",
    "SalesVchs": "sales_register.xml",
    "PurchaseVchs": "purchase_register.xml",
    "LedgerVchs": "day_book.xml",
}

# Earlier-FY vouchers (a multi-year company's books) — only reachable with
# TYPED date variables, exactly like live Tally (C33).
PRIOR_FY_VOUCHER_FIXTURE = "vouchers_prior_fy.xml"

# The mock company's current period (Tally F2 / Alt+F2 period): what live
# Tally answers a voucher collection with when the date vars are untyped.
MOCK_CURRENT_PERIOD = ("20250401", "20260331")

# Date-aware reports — computed from voucher data
DATE_AWARE_REPORTS = {"Profit and Loss"}

_ERROR_RESPONSE = (
    "<ENVELOPE><BODY><DATA>Unknown request</DATA></BODY></ENVELOPE>"
)


@lru_cache(maxsize=32)
def _load_fixture(filename: str) -> str:
    """Load a fixture file, cached for performance.

    Call ``_load_fixture.cache_clear()`` to invalidate the cache during
    development (e.g. after editing fixture files on disk).
    """
    path = FIXTURES_DIR / filename
    if not path.exists():
        return _ERROR_RESPONSE
    return path.read_text()


_DATE_VAR_RE = r"<{tag}(\s+TYPE=\"Date\")?\s*>(\d{{2}})-(\d{{2}})-(\d{{4}})</{tag}>"


def _extract_date_var(xml_body: str, tag: str) -> tuple[str | None, bool]:
    """Extract a DD-MM-YYYY date static variable (typed or untyped).

    Returns ``(YYYYMMDD or None, typed)`` where ``typed`` is True when the
    variable carries ``TYPE="Date"``.
    """
    match = re.search(_DATE_VAR_RE.format(tag=tag), xml_body)
    if not match:
        return None, False
    typed, dd, mm, yyyy = match.groups()
    return f"{yyyy}{mm}{dd}", typed is not None


def _extract_svtodate(xml_body: str) -> str | None:
    """Extract SVTODATE value (typed or untyped) from request XML. Returns
    YYYYMMDD or None. Reports honour both forms on live Tally."""
    return _extract_date_var(xml_body, "SVTODATE")[0]


def _collection_window(xml_body: str) -> tuple[str, str]:
    """The (from, to) YYYYMMDD window live Tally would apply to a Voucher
    collection: the requested window only when BOTH vars are typed (C33),
    otherwise the company's current period."""
    frm, frm_typed = _extract_date_var(xml_body, "SVFROMDATE")
    to, to_typed = _extract_date_var(xml_body, "SVTODATE")
    if frm and to and frm_typed and to_typed:
        return frm, to
    return MOCK_CURRENT_PERIOD


_VOUCHER_RE = re.compile(r"<VOUCHER[ >].*?</VOUCHER>", re.S)
_VOUCHER_DATE_RE = re.compile(r"<DATE[^>]*>(\d{8})</DATE>")


def _voucher_collection_response(fixture_file: str, collection: str, xml_body: str) -> str:
    """Current-FY fixture ∪ prior-FY vouchers, restricted to the window live
    Tally would apply (see ``_collection_window``)."""
    frm, to = _collection_window(xml_body)
    if (frm, to) == MOCK_CURRENT_PERIOD:
        # Untyped / current period → the current-FY fixture, byte-for-byte.
        return _load_fixture(fixture_file)
    current = _load_fixture(fixture_file)
    prior_vouchers = _VOUCHER_RE.findall(_load_fixture(PRIOR_FY_VOUCHER_FIXTURE))
    wanted_type = {"SalesVchs": "Sales", "PurchaseVchs": "Purchase"}.get(collection)
    if wanted_type:
        prior_vouchers = [v for v in prior_vouchers if f'VCHTYPE="{wanted_type}"' in v]
    kept = []
    for v in _VOUCHER_RE.findall(current) + prior_vouchers:
        m = _VOUCHER_DATE_RE.search(v)
        if m is None or frm <= m.group(1) <= to:
            kept.append(v)
    body = "".join(kept)
    return f"<ENVELOPE>\n<BODY>\n<DATA>\n<COLLECTION>\n{body}\n</COLLECTION>\n</DATA>\n</BODY>\n</ENVELOPE>"


def _generate_cumulative_pnl(up_to_yyyymmdd: str | None) -> str:
    """Generate P&L XML with cumulative figures up to the given date."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "generate_fixtures",
        FIXTURES_DIR / "generate_fixtures.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.generate_profit_and_loss(up_to_date=up_to_yyyymmdd)


def _handle_import(xml_body: str) -> str:
    """Handle IMPORTDATA requests — simulate create/alter/delete operations.

    Recognizes ACTION attribute on VOUCHER/LEDGER/GROUP elements and returns
    a matching success response. Cancel returns ALTERED=1, Delete returns
    DELETED=1, anything else (including Create) returns CREATED=1 with an
    incrementing LASTVCHID.
    """
    global _mock_vch_counter

    if 'ACTION="Delete"' in xml_body:
        return """<RESPONSE>
<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>1</DELETED>
<LASTVCHID>0</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>"""

    if 'ACTION="Cancel"' in xml_body:
        return """<RESPONSE>
<CREATED>0</CREATED><ALTERED>1</ALTERED><DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>"""

    # Default: create
    _mock_vch_counter += 1
    return f"""<RESPONSE>
<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>{_mock_vch_counter}</LASTVCHID><LASTMID>{_mock_vch_counter}</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>"""


# Seeded parties that have invoices in the mock company, keyed by lookup name.
# Mirrors the live test company (`Bharat Traders Private Limited`) seed data.
_MOCK_PARTY_VOUCHERS: dict[str, list[dict]] = {
    "Apex Technologies Pvt Ltd": [
        {"number": "1", "date": "20250520", "type": "Sales",
         "reference": "INV-APX-001", "amount": "118000.00"},
        {"number": "7", "date": "20250812", "type": "Sales",
         "reference": "INV-APX-002", "amount": "88500.00"},
        # FY 2024-25 invoice (mirrors S2425-004 in vouchers_prior_fy.xml) —
        # only visible to a TYPED window reaching into the prior FY (C33).
        {"number": "S2425-004", "date": "20240515", "type": "Sales",
         "reference": "INV-APX-2425-004", "amount": "88000.00"},
    ],
    "Croma Electronics": [
        {"number": "3", "date": "20250610", "type": "Purchase",
         "reference": "PINV-CRM-001", "amount": "153400.00"},
    ],
}


def _handle_party_vouchers(xml_body: str) -> str:
    """Return a party's vouchers filtered by the requested voucher types.

    Matches the verified probe-E8 envelope (``build_party_vouchers``): a TDL
    Collection NAMEd ``PartyVouchers`` constrained by ``CHILDOF $$VchType<Type>``
    and a ``$PartyLedgerName = "<party>"`` FILTER. Unknown parties yield an
    empty COLLECTION. Dates follow C33 (see ``_collection_window``).
    """
    party_match = re.search(r'\$PartyLedgerName = "(.*?)"', xml_body)
    party = party_match.group(1) if party_match else ""
    requested_types = {m for m in re.findall(r"<CHILDOF>\$\$VchType(.*?)</CHILDOF>", xml_body)}

    rows = _MOCK_PARTY_VOUCHERS.get(party, [])
    frm, to = _collection_window(xml_body)
    rows = [r for r in rows if frm <= r["date"] <= to]
    if requested_types:
        rows = [r for r in rows if r["type"] in requested_types]

    vouchers_xml = "\n".join(
        f"""<VOUCHER>
<DATE>{r['date']}</DATE>
<VOUCHERNUMBER>{r['number']}</VOUCHERNUMBER>
<VOUCHERTYPENAME>{r['type']}</VOUCHERTYPENAME>
<PARTYLEDGERNAME>{party}</PARTYLEDGERNAME>
<REFERENCE>{r['reference']}</REFERENCE>
<AMOUNT>{r['amount']}</AMOUNT>
</VOUCHER>"""
        for r in rows
    )
    return f"""<ENVELOPE><BODY><DATA><COLLECTION>
{vouchers_xml}
</COLLECTION></DATA></BODY></ENVELOPE>"""


def mock_tally_request(xml_body: str) -> str:
    """Process an XML request and return mock fixture response."""
    # Handle write/import requests first
    if "Import Data" in xml_body:
        return _handle_import(xml_body)

    # Party voucher lookup (verified probe E8) — TDL collection filtered by party + type
    if "PartyVouchers" in xml_body:
        return _handle_party_vouchers(xml_body)

    # Check date-aware reports first
    for report_name in DATE_AWARE_REPORTS:
        if report_name in xml_body:
            svtodate = _extract_svtodate(xml_body)
            if report_name == "Profit and Loss":
                return _generate_cumulative_pnl(svtodate)

    # Static fixtures
    for report_name, fixture_file in STATIC_FIXTURES.items():
        if report_name in xml_body:
            return _load_fixture(fixture_file)

    # Voucher collections (C33-aware window, see _collection_window)
    for report_name, fixture_file in VOUCHER_FIXTURES.items():
        if report_name in xml_body:
            return _voucher_collection_response(fixture_file, report_name, xml_body)

    return _ERROR_RESPONSE
