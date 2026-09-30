#!/usr/bin/env python3
"""
Quick connectivity test for TallyPrime.

Usage:
    python scripts/test_tally_connection.py
    python scripts/test_tally_connection.py --host 192.168.1.100 --port 9000
"""

import argparse
import asyncio
import sys

from tally_bridge.client import TallyClient
from tally_bridge.exceptions import TallyConnectionError
from tally_bridge.queries.masters import list_companies, list_ledgers


async def test_connection(host: str, port: int) -> None:
    client = TallyClient(host=host, port=port)

    print(f"Testing connection to TallyPrime at {client.base_url}...")
    print()

    # Test 1: Health check
    print("[1/3] Health check...", end=" ")
    healthy = await client.health_check()
    if not healthy:
        print("FAILED")
        print("  TallyPrime is not reachable or no company is loaded.")
        print(f"  Ensure Tally is running on {host}:{port} with a company open.")
        sys.exit(1)
    print("OK")

    # Test 2: List companies
    print("[2/3] Listing companies...", end=" ")
    try:
        companies = await list_companies(client)
        print(f"OK — {len(companies)} company(ies) found")
        for c in companies:
            print(f"  - {c.name}")
    except Exception as e:
        print(f"FAILED — {e}")
        sys.exit(1)

    # Test 3: List ledgers
    print("[3/3] Fetching ledger list...", end=" ")
    try:
        ledgers = await list_ledgers(client)
        print(f"OK — {len(ledgers)} ledger(s) found")
        if ledgers:
            print(f"  First 5: {', '.join(l.name for l in ledgers[:5])}")
    except Exception as e:
        print(f"FAILED — {e}")
        sys.exit(1)

    print()
    print("All checks passed! TallyPrime is ready.")


def main():
    parser = argparse.ArgumentParser(description="Test TallyPrime connectivity")
    parser.add_argument("--host", default="localhost", help="Tally host (default: localhost)")
    parser.add_argument("--port", default=9000, type=int, help="Tally port (default: 9000)")
    args = parser.parse_args()

    asyncio.run(test_connection(args.host, args.port))


if __name__ == "__main__":
    main()
