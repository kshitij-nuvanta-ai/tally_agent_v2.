"""Generate golden fixtures from a live Tally instance.

Usage:
    PYTHONPATH=. python tests/eval/generate_golden.py --host 192.168.18.219 --port 9000
"""

import argparse
import asyncio
import json
from pathlib import Path

from tally_bridge.client import TallyClient
from tally_bridge.queries import reports

GOLDEN_DIR = Path(__file__).parent / "golden"

# Map ground_truth_key → (query function, args, is_list_of_pydantic)
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


async def collect_golden(client: TallyClient) -> dict:
    """Run all ground truth queries against Tally."""
    golden = {}

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

    return golden


def save_golden(golden: dict, scenario_name: str | None = None) -> Path:
    """Save golden data to JSON file."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    if scenario_name:
        path = GOLDEN_DIR / f"{scenario_name}.json"
    else:
        path = GOLDEN_DIR / "all_reports.json"

    path.write_text(json.dumps(golden, indent=2, default=str))
    print(f"Golden fixtures saved: {path}")
    return path


async def main():
    parser = argparse.ArgumentParser(description="Generate golden fixtures from live Tally")
    parser.add_argument("--host", required=True, help="Tally host IP")
    parser.add_argument("--port", type=int, default=9000, help="Tally port (default: 9000)")
    args = parser.parse_args()

    print(f"Connecting to Tally at {args.host}:{args.port}...")
    client = TallyClient(host=args.host, port=args.port)

    try:
        healthy = await client.health_check()
        if not healthy:
            print(f"ERROR: Cannot connect to Tally at {args.host}:{args.port}")
            return

        print("Connected. Collecting golden fixtures...")
        golden = await collect_golden(client)

        # Save as single combined file
        save_golden(golden)

        # Also save per-scenario files for scenarios that reference ground truth
        import yaml
        scenarios_dir = Path(__file__).parent / "scenarios"
        for scenario_file in sorted(scenarios_dir.glob("*.yaml")):
            scenario = yaml.safe_load(scenario_file.read_text())
            scenario_golden = {}
            for turn in scenario.get("turns", []):
                gt_key = turn.get("expect", {}).get("ground_truth_key")
                if gt_key and gt_key in golden:
                    scenario_golden[gt_key] = golden[gt_key]

            if scenario_golden:
                save_golden(scenario_golden, scenario_file.stem)

        print(f"\nDone. {len(golden)} golden datasets collected.")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
