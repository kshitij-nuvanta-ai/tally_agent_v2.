"""
Query functions for Tally voucher data: day book, registers, ledger transactions.
"""

from datetime import date, datetime

from backend.tally_bridge.client import TallyClient
from backend.tally_bridge.request_builder import (
    build_day_book,
    build_ledger_vouchers,
    build_party_vouchers,
    build_sales_register,
    build_purchase_register,
)
from backend.tally_bridge.response_parser import (
    detect_error,
    parse_party_vouchers,
    parse_vouchers,
)
from backend.tally_bridge.exceptions import TallyResponseError
from backend.utils.date_utils import format_for_tally, get_fy_end, get_fy_start


def _parse_anchor(anchor: date | str | None) -> date | None:
    """Accept a date, 'YYYY-MM-DD' (Vision doc date), 'YYYYMMDD' (voucher
    entry / Tally) or 'DD-MM-YYYY'. Unparseable → None."""
    if anchor is None or isinstance(anchor, date):
        return anchor
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(anchor.strip(), fmt).date()
        except ValueError:
            continue
    return None


def party_voucher_window(
    anchor: date | str | None,
    today: date | None = None,
) -> tuple[str, str]:
    """Default (from, to) window (DD-MM-YYYY) for a party-voucher lookup.

    From the start of the FY *before* the earlier of (document date, today)
    to the end of the FY of the later of the two. Covers:
    - dedup: an invoice entered last FY and re-uploaded this FY, and a
      back-dated upload of an older invoice (anchor = its own date);
    - DN/CN: the original invoice usually precedes the note, often across the
      31-Mar boundary.
    Bounds are always day 1 / 31 (C43-safe on Educational Tally).
    Replaces the hard-coded 01-04-2025..31-03-2026 default, which TYPED date
    vars (C33) would have made binding — silently missing every invoice
    after 31-03-2026.
    """
    today = today or date.today()
    doc = _parse_anchor(anchor) or today
    earliest, latest = min(doc, today), max(doc, today)
    start = get_fy_start(earliest)
    return (
        format_for_tally(start.replace(year=start.year - 1)),
        format_for_tally(get_fy_end(latest)),
    )


async def day_book(
    client: TallyClient,
    from_date: str,
    to_date: str,
    voucher_type: str | None = None,
    company: str | None = None,
) -> list[dict]:
    """Fetch all voucher entries for a date range."""
    raw = await client.post_xml(build_day_book(from_date, to_date, voucher_type, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_vouchers(raw, from_date=from_date, to_date=to_date)


async def ledger_vouchers(
    client: TallyClient,
    ledger_name: str,
    from_date: str,
    to_date: str,
    company: str | None = None,
) -> list[dict]:
    """Fetch all vouchers for a specific ledger."""
    raw = await client.post_xml(build_ledger_vouchers(ledger_name, from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_vouchers(raw, from_date=from_date, to_date=to_date)


async def get_party_vouchers(
    client: TallyClient,
    party: str,
    voucher_types: list[str],
    from_date: str | None = None,
    to_date: str | None = None,
    company: str | None = None,
    anchor_date: date | str | None = None,
) -> list[dict]:
    """Fetch a party's vouchers filtered by type (verified probe E8).

    Used by the DN/CN flow to surface a party's original Sales/Purchase
    invoices ("which bill is this against?") and by the Tally duplicate-invoice
    check. Returns a list of dicts:
    {date, voucher_number, voucher_type, party, reference, amount}.

    Window: explicit ``from_date``/``to_date`` win; otherwise derived by
    ``party_voucher_window(anchor_date)`` (anchor = the document's date).
    """
    if not from_date or not to_date:
        d_from, d_to = party_voucher_window(anchor_date)
        from_date = from_date or d_from
        to_date = to_date or d_to
    raw = await client.post_xml(
        build_party_vouchers(party, voucher_types, from_date, to_date, company)
    )
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_party_vouchers(raw)


async def sales_register(
    client: TallyClient,
    from_date: str,
    to_date: str,
    company: str | None = None,
) -> list[dict]:
    """Fetch sales register."""
    raw = await client.post_xml(build_sales_register(from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_vouchers(raw, from_date=from_date, to_date=to_date)


async def purchase_register(
    client: TallyClient,
    from_date: str,
    to_date: str,
    company: str | None = None,
) -> list[dict]:
    """Fetch purchase register."""
    raw = await client.post_xml(build_purchase_register(from_date, to_date, company))
    error = detect_error(raw)
    if error:
        raise TallyResponseError(error)
    return parse_vouchers(raw, from_date=from_date, to_date=to_date)
