"""Probe 17 — can the Trial Balance be exported at ledger level via TYPE=Data? (S0 spec §7 "Probe 17")

Feeds decision 11 and R3 (Part 1 §6 rung 2). Each candidate static variable is sent once with a 60 s timeout; after a
failed one Tally must still answer a cheap read, or the probe stops (Tally may be stuck computing).

Its dates are company A's current period, so C33 didn't change the window it measured. It is re-run in plan part 5
only so that the stored `ledger_level_tb` template is the typed form (the 2026-09-23 one predates `7f32848`).
"""
from __future__ import annotations

from decimal import Decimal

from tally_bridge.envelopes import COMPANY_PLACEHOLDER, wrap_report
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import (A_FY_FROM, A_FY_TO, ZERO, master_request, parse_parents, stock_bearing_groups,
                             tb_rows_any_depth, top_group)

# Candidate static variables that might switch the TB to ledger level. None is verified; the probe records which work.
EXPLODE_CANDIDATES: dict[str, dict[str, str]] = {
    "explodeflag": {"EXPLODEFLAG": "Yes"},                                  # Tally's Alt+F1 'Detailed' switch
    "svexplodeflag": {"SVEXPLODEFLAG": "Yes"},                              # the same, SV-prefixed like other export vars
    "isledgerwise": {"ISLEDGERWISE": "Yes"},                                # the TB's F5 'Ledger-wise' button (guessed)
    "ledgerwise": {"LEDGERWISE": "Yes"},                                    # a shorter spelling of that guess
    "explodealllevels": {"EXPLODEFLAG": "Yes", "EXPLODEALLLEVELS": "Yes"},  # detailed + every level (guessed)
}
CANDIDATE_TIMEOUT_S = 60.0


def _has_group_rows(rows: list[dict], ledger_parents: dict[str, str], group_parents: dict[str, str]) -> bool:
    """Does this response emit group rows at all? True iff some row names a group that is NOT also a ledger.

    This is the discriminator, NOT the row's name on its own. ISLEDGERWISE=Yes returns ledger rows only, and
    company A has a LEDGER named "Capital Account" as well as the group: judging by name alone consumed that ledger
    row as the group total, dropped it from the ledger rows, and manufactured the only mismatch the probe recorded
    (live 2026-09-23). An unambiguous group name — "Current Liabilities", "Sundry Creditors", … — can only come from
    a group row, so its presence is what says the response is group-and-ledger rather than ledger-only.
    """
    return any(row["name"] in group_parents and row["name"] not in ledger_parents for row in rows)


def evaluate(text: str, ledger_parents: dict[str, str], group_parents: dict[str, str],
             baseline: dict[str, str]) -> dict:
    """Ledger rows in an exploded TB, and whether their per-primary-group sums equal the group rows."""
    rows = tb_rows_any_depth(text)
    tops = set(baseline)
    stock_groups = stock_bearing_groups(group_parents)
    has_group_rows = _has_group_rows(rows, ledger_parents, group_parents)
    group_rows: dict[str, Decimal | None] = {}
    ledger_rows: list[dict] = []
    non_ledger_rows: list[str] = []
    for row in rows:
        name = row["name"]
        # Only a response that emits group rows may consume a name as one, and then only its first occurrence
        # (Tally prints the group before its children).
        if has_group_rows and name in tops and name not in group_rows:
            group_rows[name] = row["closing"]
        elif name in ledger_parents:
            ledger_rows.append(row)
        elif name not in group_parents:
            non_ledger_rows.append(name)      # e.g. the synthetic "Opening Stock" row: neither ledger nor group
    sums: dict[str, Decimal] = {}
    for row in ledger_rows:
        parent = ledger_parents[row["name"]]
        top = "Primary" if parent in ("", "Primary") else top_group(parent, group_parents)
        sums[top] = sums.get(top, ZERO) + (row["closing"] or ZERO)
    mismatches: dict[str, dict] = {}
    for group in sorted(tops - stock_groups):
        reference = group_rows.get(group)
        if reference is None:
            text_value = baseline.get(group, "")
            reference = Decimal(text_value) if text_value not in ("", "None") else ZERO
        if sums.get(group, ZERO) != reference:
            mismatches[group] = {"group_row": reference, "ledger_sum": sums.get(group, ZERO)}
    return {"rows": len(rows), "ledger_rows": len(ledger_rows), "sums_match": bool(ledger_rows) and not mismatches,
            "mismatches": mismatches, "stock_bearing_skipped": sorted(tops & stock_groups),
            "emits_group_rows": has_group_rows, "non_ledger_rows": sorted(set(non_ledger_rows))}


def _caveats(results: dict[str, dict], working: str) -> str:
    """The two things a ledger-level TB consumer must know, built from what this run actually recorded."""
    chosen = results[working]
    parts = []
    extra = chosen.get("non_ledger_rows") or []
    if extra:
        parts.append(f"Caveat 1 — the response is not purely ledgers: {len(extra)} non-ledger row(s) come with it "
                     f"({', '.join(extra)}), a synthetic TB row no ledger carries, so the consumer matches rows "
                     f"against the ledger list and skips the rest.")
    blind = sorted(key for key, entry in results.items()
                   if key != working and entry.get("ledger_rows", 0) > 0 and entry.get("mismatches"))
    if blind:
        parts.append(f"Caveat 2 — {', '.join(sorted(v for key in blind for v in EXPLODE_CANDIDATES[key]))} is NOT a "
                     f"ledger-level route ({', '.join(blind)}): it stops at the second group level, so ledgers under a "
                     f"custom sub-group never appear and its per-group sums are short by exactly those ledgers "
                     f"({'; '.join(f'{key}: ' + ', '.join(sorted(results[key]['mismatches'])) for key in blind)}). "
                     f"That hits any company with custom sub-groups.")
    return " ".join(parts)


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    baseline = ctx.store.environment.get("company_a_tb_baseline") or {}
    if not baseline:
        return PartResult(Outcome.BLOCKED, "No TB baseline from probe 0 — run probe 0 first.")
    ledger_parents = parse_parents(await ctx.send(
        "ledger_list", master_request("S0P17Ledgers", "Ledger", ["Name", "Parent"], company)), "LEDGER")
    group_parents = parse_parents(await ctx.send(
        "group_list", master_request("S0P17Groups", "Group", ["Name", "Parent"], company)))
    results: dict[str, dict] = {}
    working: str | None = None
    for key, variables in EXPLODE_CANDIDATES.items():
        xml = wrap_report("Trial Balance", A_FY_FROM, A_FY_TO, company, extra_vars=variables)
        text, error = await ctx.try_send(f"tb_exploded_{key}", xml, timeout=CANDIDATE_TIMEOUT_S)
        entry: dict = {"variables": variables, "error": error}
        if error is None:
            entry.update(elapsed_ms=ctx.last_response.elapsed_ms, bytes=ctx.last_response.response_bytes,
                         **evaluate(text, ledger_parents, group_parents, baseline))
            if working is None and entry["sums_match"]:
                working = key
        else:
            try:
                names = await ctx.company_names()
            except ProbeBlocked as exc:
                entry["tally_after"] = f"no answer: {exc}"
                results[key] = entry
                ctx.observe("candidates", results)
                return PartResult(Outcome.FAILED, f"Candidate {key!r} ({error['kind']}) left Tally unresponsive — "
                                                  "restart Tally before anything else",
                                  spec_impact="An exploded TB can hang Tally: rung 2 stays at group level and "
                                              "month-bisect localises (decision 11, R3); the agent never sends it.")
            entry["tally_after"] = f"answered: {names}"
        results[key] = entry
    ctx.observe("candidates", results)
    ctx.observe("working_variable", working)
    if working is not None:
        chosen = results[working]
        ctx.confirm_request("ledger_level_tb", wrap_report("Trial Balance", A_FY_FROM, A_FY_TO, COMPANY_PLACEHOLDER,
                                                           extra_vars=EXPLODE_CANDIDATES[working]),
                            candidate=working, note="S2 substitutes SVFROMDATE / SVTODATE")
        return PartResult(Outcome.CONFIRMED,
                          f"{working!r} returns {chosen['ledger_rows']} ledger rows whose per-group sums equal the TB "
                          f"group totals ({chosen['elapsed_ms']} ms, {chosen['bytes']} bytes under Wine)",
                          spec_impact=f"Rung 2 CAN compare at ledger level via {EXPLODE_CANDIDATES[working]} — no need "
                                      f"to stay at group level — and month-bisect gets cheaper (decision 11, R3); "
                                      f"timing is re-measured on tier C. {_caveats(results, working)}")
    with_rows = [key for key, entry in results.items() if entry.get("ledger_rows", 0) > 0]
    if with_rows:
        return PartResult(Outcome.DIFFERENT, f"Ledger rows come back ({', '.join(with_rows)}) but their per-group sums "
                                             "don't equal the TB group rows",
                          spec_impact="The exploded TB's ledger rows don't sum to its groups: rung 2 stays group-level "
                                      "until the recorded mismatch is explained (decision 11, R3).")
    return PartResult(Outcome.FAILED, "No candidate variable returns ledger-level TB rows",
                      spec_impact="No ledger-level TB via TYPE=Data: rung 2 stays group-level and month-bisect does "
                                  "the localising (decision 11, R3).")


PROBE = Probe(
    id=17,
    name="ledger_level_tb",
    question="Can the Trial Balance be exported at ledger level via TYPE=Data, safely, and do its rows sum to the groups?",
    feeds=("decision 11", "R3"),
    parts={"A": run_a},
    requires=(0, 1),
)
