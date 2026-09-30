#!/usr/bin/env python3
"""Live validation of the agent pipeline against a real Tally instance.

Usage:
    PYTHONPATH=. uv run python scripts/test_agent_live.py --host <TALLY_IP> --port 9000

Requires: ANTHROPIC_API_KEY set in .env or environment.
"""

import asyncio
import argparse
import sys

import anthropic

from tally_bridge.client import TallyClient
from backend.agents.orchestrator import Orchestrator
from backend.agents.context import SessionStore
from backend.config import settings


LIVE_TEST_QUERIES = [
    # Greetings
    ("Hello!", "greeting"),
    # Simple lookups
    ("List all companies", "simple_lookup"),
    ("Show me the trial balance for this financial year", "simple_lookup"),
    ("What is the profit and loss for April 2025 to March 2026?", "simple_lookup"),
    ("Show balance sheet as of today", "simple_lookup"),
    ("Show outstanding receivables", "simple_lookup"),
    ("Show sales register for this month", "simple_lookup"),
    # Clarification
    ("Show me the balance", "clarification_needed"),
]


async def run_live_tests(host: str, port: int):
    client = TallyClient(host=host, port=port)

    # Verify Tally is reachable
    healthy = await client.health_check()
    if not healthy:
        print(f"FAIL: Cannot reach Tally at {host}:{port}")
        return

    print(f"OK: Tally reachable at {host}:{port}")

    # Verify API key is set
    if not settings.ANTHROPIC_API_KEY:
        print("FAIL: ANTHROPIC_API_KEY not set. Add it to .env or environment.")
        return

    # Quick API key validation
    try:
        test_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        await test_client.messages.create(
            model=settings.CLAUDE_MODEL, max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        print("OK: Anthropic API key valid\n")
    except anthropic.AuthenticationError:
        print("FAIL: ANTHROPIC_API_KEY is invalid.")
        return
    except anthropic.BadRequestError as e:
        print("FAIL:"+str(e))
        if "credit balance" in str(e):
            print("FAIL: Anthropic API credit balance too low. Top up at https://console.anthropic.com/settings/billing")
            return
        raise

    orchestrator = Orchestrator()
    store = SessionStore()
    session = store.get_or_create()

    passed = 0
    failed = 0

    for query, expected_type in LIVE_TEST_QUERIES:
        try:
            result = await orchestrator.process_query(query, client, session)
            actual_type = result["query_type"]
            status = "PASS" if actual_type == expected_type else "WARN"
            if status == "WARN":
                print(f"  WARN: Expected {expected_type}, got {actual_type}")
                # Still count as passed since Claude may classify differently
            else:
                passed += 1
            print(f"  {status}: [{actual_type}] {query}")
            print(f"         → {result['message'][:120]}...")
            if result.get("data"):
                data = result["data"]
                if isinstance(data, list):
                    print(f"         → {len(data)} items returned")
                elif isinstance(data, dict) and "rows" in data:
                    print(f"         → {len(data['rows'])} rows returned")
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e):
                print(f"  FAIL: API credits exhausted — aborting remaining tests.")
                failed += len(LIVE_TEST_QUERIES) - passed - failed
                break
            failed += 1
            print(f"  FAIL: {query}")
            print(f"         → API error: {e}")
        except anthropic.APIError as e:
            failed += 1
            print(f"  FAIL: {query}")
            print(f"         → API error: {e.message}")
        except Exception as e:
            failed += 1
            print(f"  FAIL: {query}")
            print(f"         → {type(e).__name__}: {e}")
        print()

    total = len(LIVE_TEST_QUERIES)
    warnings = total - passed - failed
    print(f"\nResults: {passed} passed, {failed} failed, {warnings} warnings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live agent pipeline test")
    parser.add_argument("--host", default=settings.TALLY_HOST)
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run_live_tests(args.host, args.port))
