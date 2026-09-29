"""Probe 13 — does a backup restore change the company GUID or MasterIDs? (S0 spec §7 "Probe 13"). Feeds R8.

Last in batch 4: it restores company A. In auto mode the backup / restore are file copies (S0-D9, spec §5.8).
"""
from __future__ import annotations

from v2.agent.tally.xml_utils import read_objects
from v2.probes.actions import Action
from v2.probes.companies import THROWAWAY_DATE_TEXT, THROWAWAY_EXPENSE_LEDGER
from v2.probes.context import ProbeContext
from v2.probes.core import Outcome, PartResult, Probe
from v2.probes.reads import voucher_request

FIELDS = ["GUID", "MasterId", "Narration"]
BACKUP_TAG = "p13"
VOUCHER_REF = "p13-v4"
NARRATION = "S0-throwaway 4"
SAMPLE = 5
CLEANUP_NOTE = f"Delete the voucher with narration {NARRATION!r} (or run `uv run --project v2 python -m v2.probes reset-a`)"
AUTO_LIMIT_NOTE = "[auto: file-level backup/restore — Tally's Backup/Restore screens not exercised]"


def _number(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


async def _vouchers(ctx: ProbeContext, step: str) -> list[dict[str, str]]:
    return read_objects(await ctx.send(step, voucher_request("S0P13Vouchers", FIELDS, ctx.company_name)),
                        "VOUCHER", FIELDS)


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    before_rows = await _vouchers(ctx, "before_backup")
    sample = sorted((row for row in before_rows if _number(row["MasterId"]) is not None),
                    key=lambda row: _number(row["MasterId"]))[:SAMPLE]
    if not sample:
        return PartResult(Outcome.BLOCKED, "No vouchers with a numeric MasterID to sample")
    before = await ctx.counters("before_backup_counters")
    ctx.pause("Back up company A: Gateway of Tally → Alt+Y (Data) → Backup, to a folder of your choice.",
              Action("backup_company", {"label": ctx.part, "tag": BACKUP_TAG}))
    await ctx.check_company()
    ctx.on_abort(CLEANUP_NOTE)
    ctx.pause(f"Create a Payment voucher dated {THROWAWAY_DATE_TEXT}: Cash → {THROWAWAY_EXPENSE_LEDGER}, ₹1, narration "
              f"{NARRATION!r}. Save it. (company: {company!r})",
              Action("create_voucher", {"company": company, "ref": VOUCHER_REF, "ledger": THROWAWAY_EXPENSE_LEDGER,
                                        "amount": "1.00", "narration": NARRATION}))
    after_throwaway = await ctx.counters("after_throwaway")
    ctx.pause(f"Close company A, restore the backup you just took over A's data folder (Alt+Y → Restore), then open "
              f"{company!r} again (only it).", Action("restore_company", {"label": ctx.part, "tag": BACKUP_TAG}))
    await ctx.check_company()
    after_rows = await _vouchers(ctx, "after_restore")
    if not after_rows:
        return PartResult(Outcome.BLOCKED, "Nothing read back after the restore")
    after = await ctx.counters("after_restore_counters")

    by_master_id = {row["MasterId"]: row["GUID"] for row in after_rows}
    throwaway_gone = not any(row["Narration"] == NARRATION for row in after_rows)
    same_guid = bool(before.get("GUID")) and after.get("GUID") == before.get("GUID")
    rose = (_number(after_throwaway.get("AltVchId")) or 0) > (_number(before.get("AltVchId")) or 0)
    back_below = (_number(after.get("AltVchId")) or 0) < (_number(after_throwaway.get("AltVchId")) or 0)
    ids_kept = all(by_master_id.get(row["MasterId"]) == row["GUID"] for row in sample)
    method = ("file copy of the company folder with Tally stopped (auto)" if ctx.run_mode == "auto"
              else "Tally Backup / Restore screens (manual)")
    ctx.observe("restore_method", method)
    ctx.observe("counters", {"before": before, "after_throwaway": after_throwaway, "after_restore": after})
    ctx.observe("sample_master_ids", [row["MasterId"] for row in sample])
    ctx.observe("same_guid", same_guid)
    ctx.observe("counters_rose_on_throwaway", rose)
    ctx.observe("counters_back_below", back_below)
    ctx.observe("throwaway_gone", throwaway_gone)
    ctx.observe("master_ids_and_guids_kept", ids_kept)
    suffix = f" {AUTO_LIMIT_NOTE}" if ctx.run_mode == "auto" else ""

    if not throwaway_gone:
        return PartResult(Outcome.BLOCKED, "The throwaway voucher is still there after the restore — the restore didn't "
                                           "happen" + suffix)
    ctx.resolve_abort(CLEANUP_NOTE)
    before_vch, throwaway_vch = _number(before.get("AltVchId")), _number(after_throwaway.get("AltVchId"))
    if before_vch is None or throwaway_vch is None or not rose:
        return PartResult(Outcome.BLOCKED, "The counters did not rise on the throwaway voucher, or AltVchId is unreadable "
                                           "- can't judge whether they fall back" + suffix)
    differences, impacts = [], []
    if not same_guid:
        differences.append("the company GUID changed on restore")
        impacts.append("A restore shows up as 'new GUID, same name' (the Q25 re-link path), not restore_detected; "
                       "Part 1 §4 'Company identity changes' is updated.")
    if not back_below:
        differences.append("the counters didn't fall back below the post-change values")
        impacts.append("restore_detected can't rely on AltVchId going down; Part 1 §4 'Company identity changes' uses "
                       "another signal.")
    if not ids_kept:
        differences.append("existing vouchers' MasterIDs / GUIDs changed")
        impacts.append("R8: after a restore every voucher looks new, so the post-restore resync replaces the company's "
                       "vouchers wholesale.")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences) + suffix, spec_impact=" ".join(impacts))
    return PartResult(Outcome.CONFIRMED, "The restore keeps the company GUID and the MasterIDs; the counters fall back "
                                         "below the post-change values; the throwaway voucher is gone" + suffix)


PROBE = Probe(
    id=13,
    name="backup_restore",
    question="After a backup restore, are the company GUID and MasterIDs the same, and do the counters fall back?",
    feeds=("R8",),
    parts={"A": run_a},
    requires=(0, 1),
    mutating=True,
)
