"""Runner CLI: python -m probes {list,run,report,reset-a,setup-b,setup-c,anchors} (S0 spec §5.3, §5.8)."""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

from agent.tally.client import TallyClient
from probes.actions import Action
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.console import ConsoleIO, ProbeIO
from probes.core import Outcome, ProbeBlocked
from probes.operator.auto import AUTO_RUN_MODE, AutoOperator, build_auto_operator
from probes.operator.tally_control import OperatorError
from probes.registry import ALL_ORDER, ANCHORS_AFTER_A, ANCHORS_BEFORE_PARITY, FIRST_ORDER, PROBES, load_probe
from probes.report import render_report
from probes.results import ResultsStore
from probes.runner import run_anchor_check, run_order, run_probe

V2_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = V2_ROOT / "probes" / "results" / "results.json"
FIXTURES_DIR = V2_ROOT / "tests" / "fixtures" / "sync"
LOGS_DIR = V2_ROOT / "probes" / "results" / "logs"
DOCS_DIR = V2_ROOT / "docs"
MAX_PROBE_ID = PROBES[-1].id
MANUAL_RUN_MODE = "manual (S0-D3): a person performs each pause at the Tally UI"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m probes")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--results", type=Path, default=RESULTS_PATH)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    run = sub.add_parser("run")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("probe_id", nargs="?", type=int)
    which.add_argument("--first", action="store_true")
    which.add_argument("--all", action="store_true")
    run.add_argument("--company", choices=["A", "B", "C"])
    run.add_argument("--rerun", action="store_true")
    run.add_argument("--auto", action="store_true", help="the automated operator performs every pause (S0-D9)")
    run.add_argument("--allow-risky", action="store_true",
                     help="also send the probe steps known to be able to freeze Tally (probe 16's SVFROMDATE ledger "
                          "read); Tally may need a restart afterwards")
    run.add_argument("--stop-any-tally", action="store_true",
                     help="with --auto: the operator may stop a TallyPrime it didn't start")
    report = sub.add_parser("report")
    report.add_argument("--out", type=Path)
    reset = sub.add_parser("reset-a", help="fresh seed copy in s0probe, renamed to company A (S0 spec §5.8)")
    reset.add_argument("--stop-any-tally", action="store_true")
    setup_b = sub.add_parser("setup-b", help="load company B from the deterministic dataset (S0 spec §4.3)")
    setup_b.add_argument("--stop-any-tally", action="store_true")
    setup_b.add_argument("--licence", choices=["licensed", "educational"], default=None,
                         help="default: whatever probe 0 recorded in results.json")
    sub.add_parser("setup-c", help="company C's one ledger + one voucher (S0 spec §4.4) — open only company C first")
    anchors = sub.add_parser("anchors",
                             help="is company A intact? (S0 spec §4.2) — read-only, recorded in results.json")
    anchors.add_argument("--when", choices=["before_parity", "after_a_batch"], default="before_parity")
    return parser


def _status(store: ResultsStore, item) -> str:
    if item.deferred:
        return "⏭ deferred (Q29)"
    outcome = store.outcome(item.id)
    if outcome is None:
        return "not run" if item.module else "not built"
    if outcome is Outcome.PARTIAL:
        remaining = store.probe_entry(item.id).get("remaining") or []
        if remaining:
            return f"PARTIAL (remaining: {', '.join(remaining)})"
    return outcome.value


def _list(store: ResultsStore) -> int:
    for item in PROBES:
        print(f"{item.id:>2}  {item.name:<28} {item.companies:<5} {item.tier:<4} {_status(store, item)}")
    return 0


def _auto_log() -> Path:
    return LOGS_DIR / f"s0-auto-{date.today().isoformat()}.log"


async def _run(args, store: ResultsStore, transport: httpx.AsyncBaseTransport | None, io: ProbeIO) -> int:
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        client = TallyClient(args.host, args.port, transport=transport)
        capture = Capture(args.fixtures)
        try:
            if args.first or args.all:
                await run_order(FIRST_ORDER if args.first else ALL_ORDER, client=client, store=store, capture=capture,
                                io=io, rerun=args.rerun, allow_risky=args.allow_risky)
                return 0
            if not 0 <= args.probe_id <= MAX_PROBE_ID:
                print(f"No probe {args.probe_id} (probes are 0–{MAX_PROBE_ID}).")
                return 2
            probe = load_probe(args.probe_id)
            if probe is None:
                print(f"Probe {args.probe_id} is not built yet (S0 plan part 2 or 3).")
                return 2
            if args.company and args.company not in probe.parts:
                print(f"Probe {args.probe_id} has no part {args.company} (has {', '.join(sorted(probe.parts))}).")
                return 2
            labels = [args.company] if args.company else None
            if getattr(io, "run_mode", "manual") == "auto" and probe.guard:
                first = labels[0] if labels else next(iter(probe.parts))
                try:
                    io.wait(f"Open company {first}: {COMPANIES[first]!r}", Action("open_company", {"label": first}))
                except ProbeBlocked as exc:
                    print(f"Couldn't open company {first}: {exc}")
                    return 1
            await run_probe(probe, labels=labels, client=client, store=store, capture=capture, io=io,
                            allow_risky=args.allow_risky)
            return 0
        finally:
            await client.close()
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _reset_a(args, store: ResultsStore, operator: AutoOperator | None) -> int:
    auto = operator or build_auto_operator(host=args.host, port=args.port, log_path=_auto_log(),
                                           stop_any_tally=args.stop_any_tally)
    try:
        auto.reset_company_a()
    except OperatorError as exc:
        print(f"reset-a failed: {exc}")
        return 1
    finally:
        if operator is None:
            auto.close()
    store.update_environment(company_a_reset_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    print(f"Company A reset: a fresh copy of the seed company, renamed to {COMPANIES['A']!r}.")
    return 0


def _setup_b(args, store: ResultsStore, operator: AutoOperator | None) -> int:
    auto = operator or build_auto_operator(host=args.host, port=args.port, log_path=_auto_log(),
                                           stop_any_tally=args.stop_any_tally)
    licence = args.licence or store.environment.get("licence", "licensed")
    try:
        report = auto.setup_company_b(licence=licence)
    except OperatorError as exc:
        print(f"setup-b failed: {exc}")
        return 1
    finally:
        if operator is None:
            auto.close()
    print("Created / skipped:")
    for kind in report.created:
        print(f"  {kind}: created {report.created[kind]}, skipped {report.skipped[kind]}")
    if report.pauses:
        print("Pauses:")
        for pause in report.pauses:
            print(f"  - {pause}")
    if report.notes:
        print("Notes:")
        for note in report.notes:
            print(f"  - {note}")
    if report.problems:
        print("Problems:")
        for problem in report.problems:
            print(f"  - {problem}")
        print(f"setup-b failed: {len(report.problems)} problem(s) found — see above.")
        return 1
    store.update_environment(company_b_loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    print(f"Company B loaded: {COMPANIES['B']!r} is ready.")
    return 0


def _setup_c(args, store: ResultsStore, transport) -> int:
    from probes.safety import GuardError
    from probes.setup.company_c import CompanyCLoadError, load_company_c
    from probes.setup.writes import TallyWriter, WriteFailed, WriteRefused
    http = httpx.Client(base_url=f"http://{args.host}:{args.port}", transport=transport, trust_env=False)
    try:
        report = load_company_c(TallyWriter(http, say=print))
    except (CompanyCLoadError, WriteFailed, WriteRefused, GuardError) as exc:
        print(f"setup-c failed: {exc}")
        return 1
    finally:
        http.close()
    print(f"Created: {', '.join(report.created) or 'nothing'}; already there: {', '.join(report.skipped) or 'nothing'}")
    store.update_environment(company_c_loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    print(f"Company C loaded: {COMPANIES['C']!r} is ready.")
    return 0


async def _anchors(args, store: ResultsStore, transport: httpx.AsyncBaseTransport | None, io: ProbeIO) -> int:
    """Spec §4.2's anchors check on its own, for single `run` sessions (ordered runs already include it)."""
    step = ANCHORS_BEFORE_PARITY if args.when == "before_parity" else ANCHORS_AFTER_A
    client = TallyClient(args.host, args.port, transport=transport)
    try:
        return 0 if await run_anchor_check(step, "A", client=client, store=store, io=io) else 1
    finally:
        await client.close()


def main(argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
         io: ProbeIO | None = None, operator: AutoOperator | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.company and (args.first or args.all):
        parser.error("--company cannot be used with --first/--all")
    if args.command == "run" and args.auto and args.non_interactive:
        parser.error("--auto cannot be used with --non-interactive")
    store = ResultsStore(args.results)
    if args.command == "list":
        return _list(store)
    if args.command == "report":
        out = args.out or DOCS_DIR / f"bi-s0-probe-results-{date.today().isoformat()}.md"
        out.write_text(render_report(store, date.today().isoformat()), encoding="utf-8")
        print(f"Wrote {out}")
        return 0
    if args.command == "reset-a":
        return _reset_a(args, store, operator)
    if args.command == "setup-b":
        return _setup_b(args, store, operator)
    if args.command == "setup-c":
        return _setup_c(args, store, transport)
    if args.command == "anchors":
        return asyncio.run(_anchors(args, store, transport, io or ConsoleIO(interactive=not args.non_interactive)))
    if args.auto:
        auto = operator or build_auto_operator(host=args.host, port=args.port, log_path=_auto_log(),
                                               stop_any_tally=args.stop_any_tally)
        store.update_environment(run_mode=AUTO_RUN_MODE)
        try:
            return asyncio.run(_run(args, store, transport, auto))
        finally:
            if operator is None:
                auto.close()
    store.update_environment(run_mode=MANUAL_RUN_MODE)
    return asyncio.run(_run(args, store, transport, io or ConsoleIO(interactive=not args.non_interactive)))


if __name__ == "__main__":
    sys.exit(main())
