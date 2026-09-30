"""Probe 8 — what a ledger rename does to GUIDs, AlterIDs and the names old vouchers export (S0 spec §7). Feeds R9."""
from __future__ import annotations

from tally_bridge.envelopes import formula_string
from tally_bridge.xml_utils import read_objects
from probes.actions import Action
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe
from probes.reads import master_request, parse_vouchers, primary_lines, voucher_request

LEDGER = "Rajesh Computers"
RENAMED = "Rajesh Computers S0"
LEDGER_FIELDS = ["Name", "GUID", "AlterID", "Parent"]
VOUCHER_FIELDS = ["GUID", "MasterId", "AlterID", "Date", "PartyLedgerName", "AllLedgerEntries", "LedgerEntries"]


async def _ledger(ctx: ProbeContext, step: str, name: str) -> dict[str, str] | None:
    xml = master_request("S0P08Ledger", "Ledger", LEDGER_FIELDS, ctx.company_name,
                         filters=[("S0P08Only", f"$Name = {formula_string(name)}")])
    rows = [row for row in read_objects(await ctx.send(step, xml), "LEDGER", LEDGER_FIELDS) if row["Name"] == name]
    return rows[0] if rows else None


async def _vouchers(ctx: ProbeContext, step: str, *, names: set[str] | None = None,
                    master_ids: set[str] | None = None) -> dict[str, dict]:
    """Vouchers referencing `names` (party or line), or the given Master IDs: MasterId → AlterID and exported names."""
    out: dict[str, dict] = {}
    for voucher in parse_vouchers(await ctx.send(step, voucher_request("S0P08Vouchers", VOUCHER_FIELDS,
                                                                       ctx.company_name))):
        header = voucher["header"]
        line_names = sorted({item["fields"].get("LEDGERNAME", "") for item in primary_lines(voucher)})
        party = header.get("PARTYLEDGERNAME", "")
        master_id = header.get("MASTERID", "")
        wanted = (master_id in master_ids) if master_ids is not None else \
            bool(names and (party in names or names & set(line_names)))
        if wanted:
            out[master_id] = {"alter_id": header.get("ALTERID", ""), "party": party, "line_names": line_names}
    return out


def _exported_name(vouchers: dict[str, dict]) -> str:
    names = {v["party"] for v in vouchers.values()} | {n for v in vouchers.values() for n in v["line_names"]}
    if RENAMED in names and LEDGER not in names:
        return "new name"
    if LEDGER in names and RENAMED not in names:
        return "old name"
    return "mixed"


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    before = await _ledger(ctx, "rename_before", LEDGER)
    if before is None:
        return PartResult(Outcome.BLOCKED, f"Ledger {LEDGER!r} isn't in {company!r}")
    if await _ledger(ctx, "rename_target_absent", RENAMED) is not None:
        return PartResult(Outcome.BLOCKED, f"A ledger {RENAMED!r} already exists (an earlier run?). Rename it back to "
                                           f"{LEDGER!r} or run reset-a, then re-run.")
    vouchers_before = await _vouchers(ctx, "rename_before_vouchers", names={LEDGER})
    if not vouchers_before:
        return PartResult(Outcome.BLOCKED, f"No vouchers reference {LEDGER!r}; nothing to observe")
    counters_before = await ctx.counters()
    note = f"Rename ledger {RENAMED!r} back to {LEDGER!r}"
    ctx.on_abort(note)
    ctx.pause(f"Rename ledger {LEDGER!r} to {RENAMED!r} (Alter → Ledger → Name) and save. (company: {company!r})",
              Action("rename_ledger", {"company": company, "from": LEDGER, "to": RENAMED}))
    after = await _ledger(ctx, "rename_after", RENAMED)
    if after is None:
        return PartResult(Outcome.BLOCKED, f"After the rename no ledger is called {RENAMED!r}")
    vouchers_after = await _vouchers(ctx, "rename_after_vouchers", master_ids=set(vouchers_before))
    counters_after = await ctx.counters()
    ctx.pause(f"Rename ledger {RENAMED!r} back to {LEDGER!r} and save. (company: {company!r})",
              Action("rename_ledger", {"company": company, "from": RENAMED, "to": LEDGER}))
    restored = await _ledger(ctx, "rename_restored", LEDGER)
    vouchers_restored = await _vouchers(ctx, "rename_restored_vouchers", master_ids=set(vouchers_before))
    if restored is None:
        return PartResult(Outcome.BLOCKED, f"The rename back to {LEDGER!r} isn't visible")
    ctx.resolve_abort(note)

    same_guid = after["GUID"] == before["GUID"] and restored["GUID"] == before["GUID"]
    alterids_changed = any(vouchers_after.get(mid, {}).get("alter_id") != v["alter_id"]
                           for mid, v in vouchers_before.items())
    exported = _exported_name(vouchers_after)
    ctx.observe("ledger", {"before": before, "after": after, "restored": restored})
    ctx.observe("same_guid", same_guid)
    ctx.observe("ledger_alterid_bumped", after["AlterID"] != before["AlterID"])
    ctx.observe("vouchers", {"before": vouchers_before, "after": vouchers_after, "restored": vouchers_restored})
    ctx.observe("vouchers_export", exported)
    ctx.observe("voucher_alterids_changed", alterids_changed)
    ctx.observe("counters_moved", {field: counters_after.get(field) != counters_before.get(field)
                                   for field in ("AltMstId", "AltVchId")})
    ctx.observe("restored_names", _exported_name(vouchers_restored))

    if not same_guid:
        return PartResult(Outcome.FAILED, "The ledger's GUID changed on rename",
                          spec_impact="R9's server-side rename cascade by GUID can't work: a rename looks like delete + "
                                      "create, and S1 re-links vouchers by name history instead.")
    differences = []
    if exported != "new name":
        differences.append(f"old vouchers export the {exported}")
    if alterids_changed:
        differences.append("old vouchers' AlterIDs changed")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences),
                          spec_impact="R9 is adjusted: " + ("vouchers keep the name they were entered with, so the server "
                                                            "maps lines to ledgers by GUID history; " if exported != "new name"
                                                            else "") +
                                      ("a rename re-sends the vouchers through AlterID, so the server cascade is a "
                                       "safety net." if alterids_changed else "voucher AlterIDs don't move, so the "
                                                                              "server cascade by GUID stays."))
    return PartResult(Outcome.CONFIRMED, "Same ledger GUID; old vouchers export the new name; their AlterIDs don't move",
                      spec_impact="R9 holds: the server renames by ledger GUID (vouchers aren't re-sent on a rename).")


PROBE = Probe(
    id=8,
    name="ledger_rename",
    question="On a ledger rename: which name do old vouchers export, do their AlterIDs move, does the GUID stay?",
    feeds=("R9",),
    parts={"A": run_a},
    requires=(0, 1),
    mutating=True,
)
