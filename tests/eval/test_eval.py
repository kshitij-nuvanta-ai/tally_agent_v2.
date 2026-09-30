"""Optional pytest entry point for the eval pipeline.

Run with:
    RUN_EVAL_TESTS=1 PYTHONPATH=. pytest tests/eval/test_eval.py -v -s
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EVAL_TESTS"),
    reason="RUN_EVAL_TESTS not set — skipping eval tests",
)


@pytest.mark.asyncio
async def test_eval_pipeline(
    scenario_name,
    frontend_url,
    tally_host,
    tally_port,
    use_live_ground_truth,
    eval_results_dir,
):
    """Run full eval pipeline: collect -> judge -> report.

    Can also be run standalone via:
        python tests/eval/collect.py && python tests/eval/judge.py && python tests/eval/report.py
    """
    from tests.eval.collect import (
        collect_scenario,
        list_scenarios,
        load_golden,
        load_scenario,
        save_transcript,
    )

    # Determine scenarios
    if scenario_name == "all":
        names = list_scenarios()
    else:
        names = [scenario_name]

    assert len(names) > 0, "No scenarios found"

    # Optionally collect live ground truth
    live_golden = {}
    if use_live_ground_truth:
        from tally_bridge.client import TallyClient
        from tally_bridge.queries import reports

        client = TallyClient(host=tally_host, port=tally_port)
        try:
            live_golden["trial_balance"] = (
                await reports.trial_balance(client, "01-04-2025", "31-03-2026")
            ).model_dump(mode="json")
            live_golden["profit_and_loss"] = (
                await reports.profit_and_loss(client, "01-04-2025", "31-03-2026")
            ).model_dump(mode="json")
            live_golden["balance_sheet"] = (
                await reports.balance_sheet(client, "31-03-2026")
            ).model_dump(mode="json")
            live_golden["bills_receivable"] = [
                b.model_dump(mode="json")
                for b in await reports.bills_receivable(client, "31-03-2026")
            ]
            live_golden["stock_summary"] = await reports.stock_summary(client, "31-03-2026")
        finally:
            await client.close()

    # Phase 1: Collect
    transcripts = []
    for name in names:
        print(f"\nCollecting scenario: {name}")
        scenario = load_scenario(name)
        golden = live_golden if live_golden else load_golden(name)

        transcript = await collect_scenario(
            scenario,
            frontend_url=frontend_url,
            golden_data=golden,
        )
        save_transcript(name, transcript)
        transcripts.append(transcript)

        # Basic assertions
        assert len(transcript["turns"]) == len(scenario["turns"])
        for turn in transcript["turns"]:
            assert turn["response_message"], f"Empty response for turn {turn['turn_index']}"

    # Phase 2: Judge (only if API key available)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from anthropic import AsyncAnthropic

        from tests.eval.judge import judge_transcript, save_scores

        import yaml
        from pathlib import Path

        client = AsyncAnthropic()
        model = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-4-6")
        all_scores = []

        scenarios_dir = Path(__file__).parent / "scenarios"
        for transcript in transcripts:
            scenario_file = transcript.get("scenario_file", "")
            scenario_path = scenarios_dir / f"{scenario_file}.yaml"
            if scenario_path.exists():
                scenario = yaml.safe_load(scenario_path.read_text())
                scores = await judge_transcript(client, model, transcript, scenario)
                all_scores.append(scores)

        if all_scores:
            save_scores(all_scores)

            # Phase 3: Report
            from tests.eval.report import generate_report

            report_path = generate_report(all_scores)
            assert report_path.exists()
            print(f"\nReport generated: {report_path}")

            # Verify scores are populated
            for scenario_scores in all_scores:
                for turn in scenario_scores["turns"]:
                    fc = turn.get("factual_correctness", {})
                    if isinstance(fc, dict) and "score" in fc:
                        assert fc["score"] > 0, "Score should not be zero"
    else:
        print("\nSkipping judge phase — ANTHROPIC_API_KEY not set")
        print("Run judge.py and report.py separately after setting the API key")
