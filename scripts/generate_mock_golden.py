"""Generate golden data from mock Tally handler for eval judging.

Usage:
    PYTHONPATH=. python scripts/generate_mock_golden.py
"""

import asyncio
import json
from pathlib import Path

from tally_bridge.client import TallyClient
from tally_bridge.queries import masters, reports

# Same queries as generate_golden.py but using mock mode
GROUND_TRUTH_QUERIES = {
    "trial_balance": {
        "fn": "trial_balance",
        "args": ("01-04-2025", "31-03-2026"),
        "type": "report",
    },
    "profit_and_loss": {
        "fn": "profit_and_loss",
        "args": ("01-04-2025", "31-03-2026"),
        "type": "report",
    },
    "balance_sheet": {
        "fn": "balance_sheet",
        "args": ("31-03-2026",),
        "type": "report",
    },
    "bills_receivable": {
        "fn": "bills_receivable",
        "args": ("31-03-2026",),
        "type": "list_pydantic",
    },
    "stock_summary": {
        "fn": "stock_summary",
        "args": ("31-03-2026",),
        "type": "list_dict",
    },
}


async def main():
    client = TallyClient()
    client.mock_mode = True

    golden = {}
    try:
        for key, spec in GROUND_TRUTH_QUERIES.items():
            fn = getattr(reports, spec["fn"])
            print(f"  Querying: {key}...")

            try:
                result = await fn(client, *spec["args"])

                if spec["type"] == "report":
                    golden[key] = result.model_dump(mode="json")
                elif spec["type"] == "list_pydantic":
                    golden[key] = [item.model_dump(mode="json") for item in result]
                elif spec["type"] == "list_dict":
                    golden[key] = result
                else:
                    golden[key] = result

                # Log summary
                if spec["type"] == "report":
                    rows = golden[key].get("rows", [])
                    print(f"    OK: {len(rows)} rows")
                elif isinstance(golden[key], list):
                    print(f"    OK: {len(golden[key])} items")
                else:
                    print(f"    OK")

            except Exception as e:
                print(f"    ERROR: {e}")
                golden[key] = {"error": str(e)}

    finally:
        await client.close()

    output_path = Path("tests/eval/golden/mock_golden.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(golden, f, indent=2, default=str)
    print(f"\nMock golden data written to {output_path} ({len(golden)} reports)")


if __name__ == "__main__":
    asyncio.run(main())
