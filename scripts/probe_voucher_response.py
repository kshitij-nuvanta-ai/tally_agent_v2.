"""Probe: re-attempt one purchase voucher (no existing P001 in Tally) and
capture the raw response to understand silent drops."""
import asyncio

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import build_create_purchase_voucher
from tally_bridge.response_parser import parse_import_response

COMPANY = "Bharat Traders Private Limited"


async def main():
    client = TallyClient()
    items = [
        ("Samsung 24 inch Monitor", 25, 11000, "Purchase - Electronics", "Nos", 18),
        ("Samsung Galaxy Tab A8",   20, 13500, "Purchase - Electronics", "Nos", 18),
    ]
    xml = build_create_purchase_voucher(
        date="20250928",
        voucher_number="P001",
        party="Samsung India Electronics",
        items=items,
        narration="Invoice #P001 - Samsung stock",
        gst_mode="intra",
        company=COMPANY,
    )
    print("=== REQUEST XML ===")
    print(xml)
    print("\n=== RESPONSE ===")
    resp = await client.post_xml(xml)
    print(resp)
    print("\n=== PARSED ===")
    print(parse_import_response(resp))


if __name__ == "__main__":
    asyncio.run(main())
