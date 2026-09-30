"""Probe 4 — does `$AlterID > N` filter Voucher / Ledger / Group / StockItem correctly and safely? (S0 spec §7)

Feeds decision 9 and R6. Expected sets come from a full fetch of each type, compared by GUID.
"""
from __future__ import annotations

from tally_bridge.xml_utils import read_objects
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import master_request, voucher_request

OBJECTS: dict[str, tuple[str, list[str]]] = {
    "voucher": ("Voucher", ["GUID", "AlterID", "MasterId"]),
    "ledger": ("Ledger", ["Name", "GUID", "AlterID"]),
    "group": ("Group", ["Name", "GUID", "AlterID"]),
    "stockitem": ("StockItem", ["Name", "GUID", "AlterID"]),
}
FAILED_IMPACT = "The $AlterID filter can't drive incremental sync: decision 9 uses the rolling re-pull fallback (R6)."


def _request(key: str, company: str, threshold: int | None = None) -> str:
    object_type, fields = OBJECTS[key]
    filters = [("S0P04Alt", f"$AlterID > {threshold}")] if threshold is not None else None
    name = f"S0P04{object_type}"
    if object_type == "Voucher":
        return voucher_request(name, fields, company, filters=filters)
    return master_request(name, object_type, fields, company, filters=filters)


def _alter_ids(text: str, key: str) -> dict[str, int]:
    object_type, fields = OBJECTS[key]
    out: dict[str, int] = {}
    for row in read_objects(text, object_type.upper(), fields):
        if row["GUID"] and row["AlterID"].lstrip("-").isdigit():
            out[row["GUID"]] = int(row["AlterID"])
    return out


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    checks: dict[str, dict] = {}
    wrong: list[str] = []
    for key in OBJECTS:
        text, error = await ctx.try_send(f"{key}_full", _request(key, company))
        full = _alter_ids(text, key) if text is not None else {}
        if error is not None or not full:
            checks[key] = {"error": error or "no rows with GUID + AlterID"}
            wrong.append(f"{key}_full")
            continue
        top = max(full.values())
        filters: dict[str, dict] = {}
        for suffix, threshold in (("gt_m_minus_5", top - 5), ("gt_m", top), ("gt_0", 0)):
            expected = {guid for guid, alter_id in full.items() if alter_id > threshold}
            text, error = await ctx.try_send(f"{key}_{suffix}", _request(key, company, threshold))
            got = set(_alter_ids(text, key)) if text is not None else None
            filters[suffix] = {"threshold": threshold, "expected": len(expected),
                               "got": None if got is None else len(got), "ok": got == expected, "error": error}
            if got != expected:
                wrong.append(f"{key}_{suffix}")
        checks[key] = {"rows": len(full), "max_alterid": top, "filters": filters}
    ctx.observe("checks", checks)
    try:
        await ctx.counters("cheap_read_after")
    except ProbeBlocked as exc:
        return PartResult(Outcome.FAILED, f"Tally stopped answering after the filters ({exc})",
                          spec_impact=FAILED_IMPACT + " The filter can hang Tally, so the agent never sends it.")
    if wrong:
        return PartResult(Outcome.FAILED, f"$AlterID > N gave wrong results for: {', '.join(wrong)}",
                          spec_impact=FAILED_IMPACT)
    return PartResult(Outcome.CONFIRMED, "$AlterID > N returns exactly the expected objects for Voucher, Ledger, Group "
                                         "and StockItem, and Tally answers a cheap read afterwards")


PROBE = Probe(
    id=4,
    name="alterid_filter",
    question="Does the $AlterID > N filter return exactly the right objects for each type, without upsetting Tally?",
    feeds=("decision 9", "R6"),
    parts={"A": run_a},
    requires=(0, 1),
)
