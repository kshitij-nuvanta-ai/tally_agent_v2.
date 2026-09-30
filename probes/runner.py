"""Run probe parts: guard → prerequisites → part → record (S0 spec §5.3, §6), plus the anchors steps (§4.2)."""
from __future__ import annotations

from datetime import datetime

from tally_bridge.client import TallyClient
from probes.actions import Action
from probes.anchors import check_anchors_direct
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.console import ProbeIO
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.registry import OrderStep, is_anchor_step, load_probe
from probes.results import ResultsStore
from probes.safety import GuardError, check_company, check_mutation_allowed

EDUCATIONAL_SUFFIX = " [Educational mode — confirm on a licensed Tally]"
RESET_HINT = ("Reset it with `uv run python -m probes reset-a` (or re-restore the seed backup), "
              "then re-run.")


def _check_requires(probe: Probe, store: ResultsStore) -> None:
    missing = [r for r in probe.requires if store.outcome(r) not in (Outcome.CONFIRMED, Outcome.DIFFERENT)]
    if missing:
        raise ProbeBlocked(f"Run probe(s) {', '.join(map(str, missing))} first.")


def _switch(io: ProbeIO, label: str) -> str | None:
    """Ask for the company switch. Returns why it couldn't be done (the automated operator can fail), else None."""
    instruction = f"Switch Tally to company {label}: {COMPANIES[label]!r} — close every other company."
    if not io.interactive:
        io.say(f"(non-interactive) assuming: {instruction}")
        return None
    try:
        io.wait(instruction, Action("open_company", {"label": label}))
    except ProbeBlocked as exc:
        return f"couldn't switch Tally to company {label}: {exc}"
    return None


async def run_probe(probe: Probe, *, labels: list[str] | None, client: TallyClient, store: ResultsStore,
                    capture: Capture, io: ProbeIO, allow_risky: bool = False) -> Outcome:
    all_parts = list(probe.part_labels)
    labels = labels or list(probe.parts)
    outcome = store.outcome(probe.id) or Outcome.PARTIAL
    for index, label in enumerate(labels):
        if label not in probe.parts:
            raise ValueError(f"Probe {probe.id} has no part {label!r} (has {', '.join(probe.parts)})")
        switch_error = _switch(io, label) if index > 0 else None
        ctx = ProbeContext(probe=probe, part=label, company_name=COMPANIES[label], client=client,
                           store=store, capture=capture, io=io, allow_risky=allow_risky)
        io.say(f"--- probe {probe.id} ({probe.name}) part {label}: {COMPANIES[label]}")
        interrupted = False
        try:
            if switch_error:
                raise ProbeBlocked(switch_error)
            _check_requires(probe, store)
            if probe.guard:
                check_mutation_allowed(ctx.company_name, mutating=probe.mutating)
                check_company(await ctx.company_names(), ctx.company_name, mutating=probe.mutating)
                if store.confirmed("company_counters"):
                    ctx.company_guid = (await ctx.counters()).get("GUID") or None
            result = await probe.parts[label](ctx)
        except (ProbeBlocked, GuardError) as exc:
            result = PartResult(Outcome.BLOCKED, str(exc))
        except KeyboardInterrupt:
            result = PartResult(Outcome.BLOCKED, "Interrupted by operator (Ctrl-C)")
            interrupted = True
        except Exception as exc:  # noqa: BLE001 - the harness records this instead of crashing (F1)
            result = PartResult(Outcome.BLOCKED, f"Harness error: {type(exc).__name__}: {exc}")

        result.observations = {**ctx.observations, **result.observations}
        if getattr(io, "run_mode", "manual") == "auto":
            result.observations["run_mode"] = "auto"
        if probe.educational_sensitive and store.environment.get("licence") == "educational":
            result.observations["tags"] = ["Educational"]
            result.summary += EDUCATIONAL_SUFFIX
        if result.outcome is Outcome.BLOCKED and ctx.abort_notes:
            result.observations["cleanup_needed"] = list(ctx.abort_notes)

        outcome = store.record_part(probe.id, label, result, all_parts=all_parts, fixtures=ctx.fixtures,
                                    manual_steps=ctx.manual_steps, ran_at=datetime.now().astimezone())
        io.say(f"    part {label}: {result.outcome.value} — {result.summary}")
        if result.outcome is Outcome.BLOCKED and ctx.abort_notes:
            io.say("CLEANUP NEEDED in Tally:")
            for note in ctx.abort_notes:
                io.say(f"  - {note}")
        if interrupted:
            raise KeyboardInterrupt
    return outcome


async def run_anchor_check(step: str, label: str, *, client: TallyClient, store: ResultsStore, io: ProbeIO) -> bool:
    """One anchors check in an ordered run (S0 spec §4.2), recorded in results.json. True if company A is intact."""
    when = step.split(":", 1)[1]
    baseline = store.environment.get("company_a_tb_baseline")
    receivable = payable = None
    if baseline is None:
        problems = ["No TB baseline yet: run probe 0 first."]
    else:
        try:
            result = await check_anchors_direct(client, COMPANIES[label], baseline, store.environment.get("licence"))
        except Exception as exc:  # noqa: BLE001 - a failed check is recorded, never a crash
            problems = [f"{type(exc).__name__}: {exc}"]
        else:
            problems, receivable, payable = result.problems, result.receivable, result.payable
    store.record_anchor_check(when=when, label=label, ok=not problems, problems=problems, receivable=receivable,
                              payable=payable, ran_at=datetime.now().astimezone())
    io.say(f"--- anchors check ({when}) company {label}: "
           + ("OK" if not problems else "FAILED — " + "; ".join(problems)))
    return not problems


async def run_order(order: list[OrderStep], *, client: TallyClient, store: ResultsStore,
                    capture: Capture, io: ProbeIO, rerun: bool = False, allow_risky: bool = False) -> None:
    current: str | None = None
    for step, label in order:
        if is_anchor_step(step):
            if label != current:
                error = _switch(io, label)
                if error:
                    io.say(f"Stopped: {error}")
                    return
                current = label
            if not await run_anchor_check(step, label, client=client, store=store, io=io):
                io.say(f"Stopped: company {label} failed the anchors check. {RESET_HINT}")
                return
            continue
        probe = load_probe(step)
        if probe is None:
            io.say(f"--- probe {step}: not built yet, skipped")
            continue
        if label not in probe.parts:
            io.say(f"--- probe {step} part {label}: not built yet, skipped")
            continue
        existing = store.part_outcome(step, label)
        if not rerun and existing in (Outcome.CONFIRMED, Outcome.DIFFERENT, Outcome.FAILED):
            io.say(f"--- probe {step} part {label}: already {existing.value}, skipped (use --rerun)")
            continue
        if label != current and probe.guard:
            error = _switch(io, label)
            if error:
                io.say(f"Stopped: {error}")
                return
        current = label
        await run_probe(probe, labels=[label], client=client, store=store, capture=capture, io=io,
                        allow_risky=allow_risky)
        outcome = store.part_outcome(step, label)
        if outcome not in (Outcome.CONFIRMED, Outcome.DIFFERENT):
            io.say(f"Stopped: probe {step} part {label} is {outcome.value}. Fix that, then re-run.")
            return
