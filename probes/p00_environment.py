"""Probe 0 — environment check; makes company A (S0 spec §7 "Probe 0"). Every pause / ask carries an Action (S0-D9)."""
from __future__ import annotations

import subprocess

from agent.tally.envelopes import build_company_list, wrap_collection
from agent.tally.xml_utils import read_objects
from probes.actions import Action
from probes.anchors import check_anchors
from probes.companies import COMPANIES, SEED_BACKUP, SEED_COMPANY
from probes.context import POPUP_HINT, ProbeContext
from probes.core import Outcome, PartResult, Probe

GUID_REQUEST = wrap_collection("S0CompanyGuid", "Company", ["Name", "GUID"])
EDITIONS = ("Edit Log", "standard")
LICENCES = ("licensed", "educational")


def wine_version() -> str | None:
    try:
        done = subprocess.run(["wine", "--version"], capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None


async def _guid(ctx: ProbeContext, step: str) -> str:
    rows = read_objects(await ctx.send(step, GUID_REQUEST), "COMPANY", ["Name", "GUID"])
    return rows[0]["GUID"] if len(rows) == 1 else ""


def _ask_one_of(ctx: ProbeContext, prompt: str, options: tuple[str, ...], action: Action) -> str:
    """Re-ask `prompt` until the answer matches one of `options` (case-insensitively); returns the canonical spelling."""
    lookup = {option.lower(): option for option in options}
    while True:
        answer = ctx.ask(prompt, action).strip()
        canonical = lookup.get(answer.lower())
        if canonical is not None:
            return canonical
        choices = " or ".join(f"'{option}'" for option in options)
        prompt = f"{answer!r} isn't {choices}. " + prompt


async def run_a(ctx: ProbeContext) -> PartResult:
    ctx.company_name = SEED_COMPANY
    wine = wine_version() or ctx.ask("`wine --version` didn't run here. Type the Wine version:", Action("wine_version"))
    ctx.observe("wine", wine)

    ctx.pause("Start TallyPrime under Wine (README.md 'Installing TallyPrime on macOS') with the XML server on port 9000.",
              Action("tally_running"))
    _, error = await ctx.try_send("company_list", build_company_list())
    if error is not None:
        if error["kind"] == "refused":
            return PartResult(Outcome.FAILED, f"Tally didn't answer on port 9000 ({error['kind']})",
                              spec_impact="S0 can't run under Wine on this Mac; S0 is blocked on Q29 (tier C machine).")
        if error["kind"] == "timeout":
            return PartResult(Outcome.BLOCKED, f"Tally didn't answer on port 9000 ({error['kind']}). {POPUP_HINT}")
        return PartResult(Outcome.BLOCKED, f"Tally didn't answer on port 9000 ({error['kind']})")

    ctx.pause(f"Restore {SEED_BACKUP} into a NEW, separate Tally data folder (not Bharat Traders' usual folder). "
              f"Open only that company and close every other company.", Action("restore_seed"))
    names = await ctx.company_names()
    if names != [SEED_COMPANY]:
        return PartResult(Outcome.BLOCKED, f"Expected only {SEED_COMPANY!r} open after the restore, got {names}")
    guid_before = await _guid(ctx, "guid_before_rename")

    anchors = await check_anchors(ctx, "anchors", tb_baseline=None)
    ctx.observe("anchors", {"receivable": anchors.receivable, "payable": anchors.payable, "tb_rows": anchors.tb_rows})
    if not anchors.ok:
        return PartResult(Outcome.BLOCKED, "Restored company doesn't match the seed figures: "
                          + "; ".join(anchors.problems) + ". Re-restore the seed backup.")

    data_folder = ctx.ask(f"Type the Tally data folder path shown for {SEED_COMPANY!r} (Company Info → this "
                          f"company's Data Path) — it must NOT be Bharat Traders' usual data folder:",
                          Action("data_folder"))
    ctx.observe("data_folder", data_folder)

    ctx.pause(f"In Tally, alter this company (Company menu, Alt+K → Alter) and rename it to {COMPANIES['A']!r}. "
              f"Keep it open.", Action("rename_company", {"from": SEED_COMPANY, "to": COMPANIES["A"]}))
    names = await ctx.company_names()
    if names != [COMPANIES["A"]]:
        return PartResult(Outcome.BLOCKED, f"Expected {COMPANIES['A']!r} after the rename, got {names}")
    ctx.company_name = COMPANIES["A"]
    guid_after = await _guid(ctx, "guid_after_rename")
    ctx.observe("guid_before_rename", guid_before)
    ctx.observe("guid_after_rename", guid_after)
    ctx.observe("guid_changed_on_rename", bool(guid_before) and guid_before != guid_after)
    if not guid_before:
        ctx.observe("guid_note", "GUID not readable via a Company collection; probe 2 decides the read.")

    tally_version = ctx.ask("Tally version (Help → About, e.g. 'TallyPrime 7.0'):", Action("tally_version"))
    edition = _ask_one_of(ctx, "Edition — type 'Edit Log' or 'standard':", EDITIONS, Action("edition"))
    licence = _ask_one_of(ctx, "Licence mode — type 'licensed' or 'educational':", LICENCES, Action("licence"))
    ctx.store.update_environment(wine=wine, tally_version=tally_version, edition=edition, licence=licence,
                                 recorded_by_probe=0)
    ctx.store.update_environment(company_a_tb_baseline=anchors.tb_rows)
    return PartResult(Outcome.CONFIRMED, f"Tally answers under Wine ({wine}); company A made; seed anchors match")


PROBE = Probe(
    id=0,
    name="environment",
    question="Does TallyPrime run under Wine here, and does the seed backup restore into company A with the known figures?",
    feeds=("all probes",),
    parts={"A": run_a},
    guard=False,
)
