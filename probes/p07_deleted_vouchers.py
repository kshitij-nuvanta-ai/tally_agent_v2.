"""Probe 7 — a deleted voucher vanishes and its GUID is never reused (S0 spec §7 "Probe 7"). Feeds R7.

Educational-sensitive (spec §4.6); the standard-edition cross-check is deferred to tier C (spec §8).
"""
from __future__ import annotations

from tally_bridge.xml_utils import read_objects
from probes.actions import Action
from probes.companies import THROWAWAY_DATE_TEXT, THROWAWAY_EXPENSE_LEDGER
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe
from probes.reads import voucher_request

TOMBSTONE_CANDIDATE = "IsDeleted"
FIELDS = ["GUID", "MasterId", "AlterID", "Narration", "Date", TOMBSTONE_CANDIDATE]
REFS = {"v2": "p07-v2", "v3": "p07-v3"}
NARRATIONS = {"v2": "S0-throwaway 2", "v3": "S0-throwaway 3"}


def _note(narration: str) -> str:
    return f"Delete the voucher with narration {narration!r} if it still exists"


async def _read(ctx: ProbeContext, step: str) -> list[dict[str, str]]:
    return read_objects(await ctx.send(step, voucher_request("S0P07Vouchers", FIELDS, ctx.company_name)),
                        "VOUCHER", FIELDS)


def _create(ctx: ProbeContext, key: str) -> None:
    ctx.on_abort(_note(NARRATIONS[key]))
    ctx.pause(f"Create a Payment voucher dated {THROWAWAY_DATE_TEXT}: Cash → {THROWAWAY_EXPENSE_LEDGER}, ₹1, narration "
              f"{NARRATIONS[key]!r}. Save it. (company: {ctx.company_name!r})",
              Action("create_voucher", {"company": ctx.company_name, "ref": REFS[key],
                                        "ledger": THROWAWAY_EXPENSE_LEDGER, "amount": "1.00",
                                        "narration": NARRATIONS[key]}))


def _delete(ctx: ProbeContext, key: str) -> None:
    ctx.pause(f"Delete the voucher with narration {NARRATIONS[key]!r} (open it, Alt+D). (company: {ctx.company_name!r})",
              Action("delete_voucher", {"company": ctx.company_name, "ref": REFS[key]}))


def _same_voucher(rows: list[dict[str, str]], voucher: dict[str, str]) -> list[dict[str, str]]:
    return [row for row in rows if row["GUID"] == voucher["GUID"] or row["MasterId"] == voucher["MasterId"]]


async def run_a(ctx: ProbeContext) -> PartResult:
    before = await ctx.counters()
    _create(ctx, "v2")
    created = [row for row in await _read(ctx, "throwaway_created") if row["Narration"] == NARRATIONS["v2"]]
    if len(created) != 1:
        return PartResult(Outcome.BLOCKED, f"Expected one voucher {NARRATIONS['v2']!r} after the create, found "
                                           f"{len(created)}")
    v2 = created[0]
    after_create = await ctx.counters()
    _delete(ctx, "v2")
    after_delete_rows = await _read(ctx, "after_delete")
    after_delete = await ctx.counters()
    lingering = _same_voucher(after_delete_rows, v2)
    tombstone = bool(lingering) and all(row[TOMBSTONE_CANDIDATE] == "Yes" for row in lingering)
    if not lingering:
        ctx.resolve_abort(_note(NARRATIONS["v2"]))

    _create(ctx, "v3")
    third = [row for row in await _read(ctx, "second_throwaway") if row["Narration"] == NARRATIONS["v3"]]
    if len(third) != 1:
        return PartResult(Outcome.BLOCKED, f"Expected one voucher {NARRATIONS['v3']!r} after the create, found "
                                           f"{len(third)}")
    v3 = third[0]
    _delete(ctx, "v3")
    v3_lingering = [row for row in _same_voucher(await _read(ctx, "after_second_delete"), v3)
                    if row[TOMBSTONE_CANDIDATE] != "Yes"]
    if not v3_lingering:
        ctx.resolve_abort(_note(NARRATIONS["v3"]))

    guid_reused = v3["GUID"] == v2["GUID"]
    master_id_reused = v3["MasterId"] == v2["MasterId"]
    ctx.observe("voucher_2", v2)
    ctx.observe("voucher_3", v3)
    ctx.observe("counters", {"before": before.get("AltVchId"), "after_create": after_create.get("AltVchId"),
                             "after_delete": after_delete.get("AltVchId")})
    ctx.observe("counters_moved_on_delete", after_delete.get("AltVchId") != after_create.get("AltVchId"))
    ctx.observe("lingering_after_delete", lingering)
    ctx.observe("tombstone_field_present", any(row[TOMBSTONE_CANDIDATE] for row in after_delete_rows))
    ctx.observe("guid_reused", guid_reused)
    ctx.observe("master_id_reused", master_id_reused)

    failures = []
    if guid_reused:
        failures.append("the second throwaway got the deleted voucher's GUID")
    if lingering and not tombstone:
        failures.append("the deleted voucher lingers in the collection with no deleted flag")
    if v3_lingering:
        failures.append("the second deleted voucher lingers too")
    if failures:
        return PartResult(Outcome.FAILED, "; ".join(failures) + " (delete it by hand, or run reset-a)",
                          spec_impact="The deletion compare (Part 1 R7) can't rely on a GUID vanishing and never "
                                      "coming back: it is redesigned before S2.")
    if tombstone:
        return PartResult(Outcome.DIFFERENT, "A deleted voucher comes back flagged IsDeleted = Yes",
                          spec_impact="Deleted vouchers export as tombstones: the deletion compare reads the flag "
                                      "instead of absence (Part 1 R7).")
    if master_id_reused:
        return PartResult(Outcome.DIFFERENT, "The deleted voucher's MasterID was reused (with a new GUID)",
                          spec_impact="MasterIDs are reused after a delete: the deletion compare and change detection key "
                                      "on GUID only, never MasterID (Part 1 R7).")
    return PartResult(Outcome.CONFIRMED, "A deleted voucher vanishes from the collection; the next voucher gets a new "
                                         "GUID and MasterID; AltVchId moves on the delete")


PROBE = Probe(
    id=7,
    name="deleted_vouchers",
    question="Does a deleted voucher vanish, and is its GUID never reused?",
    feeds=("R7",),
    parts={"A": run_a},
    requires=(0, 1),
    mutating=True,
    educational_sensitive=True,
)
