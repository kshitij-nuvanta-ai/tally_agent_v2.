"""Probe 19 — do AltVchId / AltMstId hold still across a multi-call capture? (S0 spec §7 "Probe 19")

Feeds the quiescence guard (Part 1 §6): read the counters right before and right after a parity capture and discard
the run if they moved. That needs quiet captures to be stable, a report view not to move them, and a voucher entered
mid-capture to move them.
"""
from __future__ import annotations

from typing import Callable

from tally_bridge.envelopes import wrap_report
from probes.actions import Action
from probes.companies import THROWAWAY_DATE_TEXT, THROWAWAY_EXPENSE_LEDGER
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe
from probes.reads import A_FY_FROM, A_FY_TO, master_request

QUIET_CAPTURES = 3
WATCHED = ("AltVchId", "AltMstId")
VOUCHER_REF = "p19-mid-capture"
NARRATION = "S0-throwaway 19"


def _watched(counters: dict[str, str]) -> dict[str, str]:
    return {field: counters.get(field, "") for field in WATCHED}


async def _capture(ctx: ProbeContext, prefix: str, between: Callable[[], None] | None = None) -> dict:
    """counters → ledger list → (between) → TB → counters, as a parity capture does."""
    start = _watched(await ctx.counters(f"{prefix}_counters_start"))
    await ctx.send(f"{prefix}_ledgers",
                   master_request("S0P19Ledgers", "Ledger", ["Name", "ClosingBalance"], ctx.company_name))
    if between is not None:
        between()
    await ctx.send(f"{prefix}_tb", wrap_report("Trial Balance", A_FY_FROM, A_FY_TO, ctx.company_name))
    end = _watched(await ctx.counters(f"{prefix}_counters_end"))
    return {"start": start, "end": end, "stable": start == end}


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    quiet = [await _capture(ctx, f"capture_quiet_{n}") for n in range(1, QUIET_CAPTURES + 1)]
    readings = [capture["start"] for capture in quiet] + [capture["end"] for capture in quiet]
    quiet_stable = all(reading == readings[0] for reading in readings)
    ctx.observe("quiet_captures", quiet)
    ctx.observe("quiet_stable", quiet_stable)

    ctx.pause("In Tally, open the Balance Sheet (Gateway of Tally → Balance Sheet), look at it, then close it. "
              "Change nothing.",
              Action("view_report", {"company": company, "report": "Balance Sheet", "from_date": A_FY_FROM,
                                     "to_date": A_FY_TO}))
    after_view = _watched(await ctx.counters("after_ui_view"))
    view_moved = after_view != quiet[-1]["end"]
    ctx.observe("after_ui_view", after_view)
    ctx.observe("view_moved_counters", view_moved)
    if ctx.run_mode == "auto":
        ctx.observe("ui_view", "not exercised (auto mode: XML export)")

    note = f"Delete the voucher with narration {NARRATION!r} if it still exists"

    def enter_voucher() -> None:
        ctx.on_abort(note)
        ctx.pause(f"Now, in the middle of a capture: create a Payment voucher dated {THROWAWAY_DATE_TEXT}: Cash → "
                  f"{THROWAWAY_EXPENSE_LEDGER}, ₹1, narration {NARRATION!r}. Save it. (company: {company!r})",
                  Action("create_voucher", {"company": company, "ref": VOUCHER_REF, "ledger": THROWAWAY_EXPENSE_LEDGER,
                                            "amount": "1.00", "narration": NARRATION}))

    moving = await _capture(ctx, "capture_moving", between=enter_voucher)
    ctx.pause(f"Delete the voucher with narration {NARRATION!r} (open it, Alt+D). (company: {company!r})",
              Action("delete_voucher", {"company": company, "ref": VOUCHER_REF}))
    ctx.resolve_abort(note)
    detected = not moving["stable"]
    ctx.observe("moving_capture", moving)
    ctx.observe("mid_capture_entry_detected", detected)

    if not quiet_stable:
        return PartResult(Outcome.FAILED, "Counters moved between reads with nobody entering anything",
                          spec_impact="The quiescence guard would abort every parity run: Part 1 §6 needs another "
                                      "quiescence signal before S2 (record which counter drifts).")
    if not detected:
        return PartResult(Outcome.FAILED, "A voucher entered mid-capture didn't move AltVchId / AltMstId",
                          spec_impact="The quiescence guard can't see a concurrent entry: parity needs another guard "
                                      "(e.g. re-read the lines after the snapshot) before S1 (Part 1 §6).")
    if view_moved:
        # M3/M5: in auto mode the "view" is an XML export, not a UI report view — don't claim the UI was exercised.
        viewed = ("A report view" if ctx.run_mode != "auto"
                  else "An XML report export (auto mode reads no UI, so a UI report view is still untested)")
        return PartResult(Outcome.DIFFERENT, f"{viewed} moved the counters",
                          spec_impact=f"{viewed} moves the counters, so the guard aborts while someone browses: the "
                                      "retry policy (Part 1 §6 'Schedule and trigger') must tolerate repeated "
                                      "aborted_moving runs.")
    view_note = ("a report view doesn't move the counters" if ctx.run_mode != "auto"
                else "the report view wasn't exercised (auto mode used an XML export instead)")
    return PartResult(Outcome.CONFIRMED, f"Quiet captures are stable, {view_note}, and a mid-capture voucher is detected")


PROBE = Probe(
    id=19,
    name="counter_stability",
    question="Do AltVchId / AltMstId hold still across a quiet multi-call capture, and move when a voucher is entered?",
    feeds=("quiescence guard",),
    parts={"A": run_a},
    requires=(0, 1),
    mutating=True,
)
