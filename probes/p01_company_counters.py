"""Probe 1 — which company counters move on which change (S0 spec §7 "Probe 1", §5.8 "Probe 1 revision").

Master steps run on a throwaway ledger (create → alter → alter again → delete), so company A is left as it was:
an empty-value alter is silently ignored by Tally (live 2026-09-22), so "clear the field again" can't be the revert.
In manual runs a person makes the changes at the Tally UI (S0-D3); in auto runs the operator makes them via XML (S0-D9).
"""
from __future__ import annotations

from agent.tally.envelopes import formula_string, wrap_collection
from agent.tally.xml_utils import read_objects
from probes.actions import Action
from probes.companies import THROWAWAY_DATE_TEXT
from probes.context import ProbeContext, select_company
from probes.core import Outcome, PartResult, Probe

CANDIDATE_FIELDS = ["GUID", "AltVchId", "AltMstId", "BooksFrom", "LastVoucherDate", "AlterID"]
COUNTERS_REQUEST = wrap_collection("S0CompanyCounters", "Company", ["Name", *CANDIDATE_FIELDS])
WATCHED = ["AltVchId", "AltMstId", "LastVoucherDate"]
THROWAWAY_LEDGER = "S0 Probe Ledger"
THROWAWAY_PARENT = "Indirect Expenses"
VOUCHER_REF = "p01-v1"
NARRATION = "S0-throwaway 1"
ALTERED_NARRATION = "S0-throwaway 1 (altered)"
EMAILS = ("s0probe-1@example.com", "s0probe-2@example.com")
# Probe 16 reads this observation: the books' reach BEFORE this probe's own 31-03-2026 throwaway voucher, which is
# what tells it whether Tally's current (F2) date is before that date. `present` below drops fields the Company
# collection exports empty, so LastVoucherDate can be missing entirely — recorded as available=False, never as "".
LAST_VOUCHER_DATE_BASELINE = "last_voucher_date_baseline"

DELETE_VOUCHER_NOTE = f"Delete the voucher with narration {NARRATION!r} (or {ALTERED_NARRATION!r}) if it still exists"
DELETE_LEDGER_NOTE = f"Delete ledger '{THROWAWAY_LEDGER}' if it exists"


def actions_for(company: str, ledger: str) -> list[tuple[str, str, str, Action]]:
    """(step, kind, instruction for a person, Action for the operator) in run order."""
    return [
        ("create_voucher", "voucher",
         f"Create a Payment voucher dated {THROWAWAY_DATE_TEXT}: Cash → {ledger}, ₹1, narration {NARRATION!r}. Save it.",
         Action("create_voucher", {"company": company, "ref": VOUCHER_REF, "ledger": ledger, "amount": "1.00",
                                   "narration": NARRATION})),
        ("alter_voucher", "voucher", f"Alter that voucher's narration to {ALTERED_NARRATION!r} and save.",
         Action("alter_voucher", {"company": company, "ref": VOUCHER_REF, "narration": ALTERED_NARRATION})),
        ("delete_voucher", "voucher", "Delete that voucher (open it, Alt+D).",
         Action("delete_voucher", {"company": company, "ref": VOUCHER_REF})),
        ("create_ledger", "master", f"Create a ledger {THROWAWAY_LEDGER!r} under {THROWAWAY_PARENT}.",
         Action("create_ledger", {"company": company, "name": THROWAWAY_LEDGER, "parent": THROWAWAY_PARENT})),
        ("alter_ledger", "master", f"Alter ledger {THROWAWAY_LEDGER!r}: set its E-Mail to {EMAILS[0]!r} and save.",
         Action("alter_ledger", {"company": company, "name": THROWAWAY_LEDGER, "email": EMAILS[0]})),
        ("alter_ledger_again", "master",
         f"Alter ledger {THROWAWAY_LEDGER!r} again: set its E-Mail to {EMAILS[1]!r} and save.",
         Action("alter_ledger", {"company": company, "name": THROWAWAY_LEDGER, "email": EMAILS[1]})),
        ("delete_ledger", "master", f"Delete ledger {THROWAWAY_LEDGER!r}.",
         Action("delete_ledger", {"company": company, "name": THROWAWAY_LEDGER})),
    ]


async def _pick_expense_ledger(ctx: ProbeContext) -> str:
    text = await ctx.send("ledger_list", wrap_collection("S0LedgerList", "Ledger", ["Name", "Parent"], ctx.company_name))
    rows = read_objects(text, "LEDGER", ["Name", "Parent"])
    if any(row["Name"] == "Bank Charges" for row in rows):
        return "Bank Charges"
    for row in rows:
        if row["Parent"] == "Indirect Expenses":
            return row["Name"]
    return ctx.ask("Type the name of an expense ledger in this company to use for the ₹1 test voucher:",
                   Action("expense_ledger"))


async def _ledger_alterid(ctx: ProbeContext, ledger: str, step: str) -> str:
    xml = wrap_collection("S0OneLedger", "Ledger", ["Name", "GUID", "AlterID"], ctx.company_name,
                          filters=[("S0OnlyLedger", f"$Name = {formula_string(ledger)}")])
    rows = read_objects(await ctx.send(step, xml), "LEDGER", ["Name", "AlterID"])
    return rows[0]["AlterID"] if rows else ""


async def run_a(ctx: ProbeContext) -> PartResult:
    row = select_company(await ctx.send("counters_candidates", COUNTERS_REQUEST), ctx.company_name, CANDIDATE_FIELDS)
    present = [field for field in CANDIDATE_FIELDS if row.get(field)]
    ctx.observe("fields_present", present)
    if not {"AltVchId", "AltMstId"} <= set(present):
        return PartResult(Outcome.FAILED, f"The Company collection doesn't expose AltVchId/AltMstId (got {present})",
                          spec_impact="No company-level change counters over XML: decision 9 falls back to a rolling "
                                      "re-pull of recent months + the daily GUID/AlterID compare (Part 1 R6).")
    ctx.confirm_request("company_counters", COUNTERS_REQUEST, fields=present)

    ledger = await _pick_expense_ledger(ctx)
    ctx.observe("ledger", ledger)
    if await _ledger_alterid(ctx, THROWAWAY_LEDGER, "throwaway_ledger_check"):
        return PartResult(Outcome.BLOCKED, f"Ledger {THROWAWAY_LEDGER!r} already exists (an earlier run?). Delete it in "
                          "Tally or run `uv run python -m probes reset-a`, then re-run — a duplicate "
                          "create freezes Tally (LESSONS §15 rule 10).")
    previous = await ctx.counters("counters_baseline")
    baseline_date = previous.get("LastVoucherDate", "")
    ctx.observe(LAST_VOUCHER_DATE_BASELINE, {"value": baseline_date, "available": bool(baseline_date),
                                             "note": "" if baseline_date else "not available: the Company collection "
                                                     "exports no LastVoucherDate for this company"})
    ledger_before = await _ledger_alterid(ctx, ledger, "ledger_before")
    ledger_after_voucher = ledger_before
    matrix: dict[str, dict] = {}
    for step, kind, instruction, action in actions_for(ctx.company_name, ledger):
        if step == "create_voucher":
            ctx.on_abort(DELETE_VOUCHER_NOTE)
        elif step == "create_ledger":
            ctx.on_abort(DELETE_LEDGER_NOTE)

        ctx.pause(f"{instruction} (company: {ctx.company_name!r})", action)
        current = await ctx.counters(f"counters_after_{step}")
        watched = [field for field in WATCHED if field in present]
        matrix[step] = {
            "kind": kind,
            "moved": {field: current.get(field) != previous.get(field) for field in watched},
            "values": {field: current.get(field) for field in watched},
        }
        if step == "create_voucher":
            ledger_after_voucher = await _ledger_alterid(ctx, ledger, "ledger_after_voucher")
        previous = current

        if step == "delete_voucher":
            ctx.resolve_abort(DELETE_VOUCHER_NOTE)
        elif step == "delete_ledger":
            ctx.resolve_abort(DELETE_LEDGER_NOTE)
    ctx.observe("matrix", matrix)
    ledger_moved = ledger_before != ledger_after_voucher
    ctx.observe("ledger_alterid_moved_on_voucher_entry", ledger_moved)

    deviations = []
    for step, entry in matrix.items():
        field = "AltVchId" if entry["kind"] == "voucher" else "AltMstId"
        if not entry["moved"].get(field):
            deviations.append(f"{field} did not move on {step}")
    if ledger_moved:
        deviations.append("entering a voucher moved the ledger's own AlterID")
    if deviations:
        return PartResult(Outcome.DIFFERENT, "; ".join(deviations),
                          spec_impact="Part 1 §4 change detection gets this matrix; decision 9 adds a rolling re-pull "
                                      "fallback for the changes that move no counter (R6), and 'Why the re-read' is "
                                      "revisited if a voucher entry moves the ledger's AlterID.")
    return PartResult(Outcome.CONFIRMED, "AltVchId moves on every voucher change, AltMstId on every master change; "
                                         "a voucher entry doesn't move the ledger's AlterID")


PROBE = Probe(
    id=1,
    name="company_counters",
    question="Which company counters (AltVchId, AltMstId, LastVoucherDate, a ledger's AlterID) move on which change?",
    feeds=("decision 9", "R6"),
    parts={"A": run_a},
    requires=(0,),
    mutating=True,
    educational_sensitive=True,
)
