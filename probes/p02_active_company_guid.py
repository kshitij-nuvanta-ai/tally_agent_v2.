"""Probe 2 — cheapest correct read of the active company's GUID, and the no-company response (S0 spec §7).

The chosen read is for the S2 agent's per-cycle gate; the harness guard keeps using the company list.
"""
from __future__ import annotations

from agent.tally.envelopes import COMPANY_PLACEHOLDER, esc, wrap_collection
from agent.tally.xml_utils import read_objects
from probes.actions import Action
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe

CANDIDATES: dict[str, str] = {
    "a": wrap_collection("S0ActiveCompany", "Company", ["Name", "GUID"],
                         filters=[("S0IsActive", "$Name = ##SVCurrentCompany")]),
    "b": f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Object</TYPE>
<SUBTYPE>Company</SUBTYPE>
<ID TYPE="Name">{COMPANY_PLACEHOLDER}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES>
<FETCHLIST>
<FETCH>Name</FETCH>
<FETCH>GUID</FETCH>
</FETCHLIST>
</DESC>
</BODY>
</ENVELOPE>""",
    "c": wrap_collection("S0CompanyListGuid", "Company", ["Name", "GUID"]),
}


def _reading(text: str | None) -> dict:
    if text is None:
        return {"rows": 0, "name": "", "guid": ""}
    rows = [r for r in read_objects(text, "COMPANY", ["Name", "GUID"]) if r["Name"] or r["GUID"]]
    one = rows[0] if len(rows) == 1 else {"Name": "", "GUID": ""}
    return {"rows": len(rows), "name": one["Name"], "guid": one["GUID"]}


async def run_a(ctx: ProbeContext) -> PartResult:
    results: dict[str, dict] = {}
    for key, template in CANDIDATES.items():
        text, error = await ctx.try_send(f"active_{key}", template.replace(COMPANY_PLACEHOLDER, esc(ctx.company_name)))
        reading = _reading(text)
        correct = error is None and reading["rows"] == 1 and reading["name"] == ctx.company_name and bool(reading["guid"])
        size = ctx.last_response.response_bytes if ctx.last_response is not None else 0
        results[key] = {"correct": correct, "bytes": size, "error": error, **reading}
    ctx.observe("candidates", results)

    guids = {r["guid"] for r in results.values() if r["correct"]}
    if len(guids) > 1:
        return PartResult(Outcome.BLOCKED, f"Candidates disagree on the GUID ({sorted(guids)}); investigate by hand")
    cheap = sorted((results[k]["bytes"], k) for k in ("a", "b") if results[k]["correct"])
    chosen = cheap[0][1] if cheap else ("c" if results["c"]["correct"] else None)
    if chosen is None:
        return PartResult(Outcome.FAILED, "No candidate returned our company's GUID",
                          spec_impact="No way found to read the company GUID over XML: Part 1 decision 4 / R2 "
                                      "(GUID check) need another identity signal before S2.")
    ctx.confirm_request("active_company", CANDIDATES[chosen], candidate=chosen)
    ctx.observe("company_guid", results[chosen]["guid"])

    reopen_note = f"Open company {ctx.company_name!r} again (only that one)"
    ctx.on_abort(reopen_note)
    ctx.pause("Close every company in Tally (keep Tally running): Company menu (Alt+K) → Shut Company, until none is open.",
              Action("close_all_companies"))
    no_company: dict[str, dict] = {}
    for key, template in CANDIDATES.items():
        text, error = await ctx.try_send(f"active_{key}_no_company",
                                         template.replace(COMPANY_PLACEHOLDER, esc(ctx.company_name)))
        no_company[key] = {"error": error, **_reading(text), "snippet": (text or "")[:400]}
    ctx.observe("no_company", no_company)
    distinguishable = no_company[chosen]["error"] is not None or not no_company[chosen]["guid"]
    ctx.observe("no_company_distinguishable", distinguishable)
    ctx.pause(f"Open company {ctx.company_name!r} again (only that one).", Action("open_company", {"label": ctx.part}))
    await ctx.check_company()
    ctx.resolve_abort(reopen_note)

    if not distinguishable:
        return PartResult(Outcome.DIFFERENT, f"Candidate ({chosen}) still returns a GUID with no company open",
                          spec_impact="The S2 gate must also check the company list before trusting the GUID read "
                                      "(Part 1 §5 gate.py).")
    if chosen == "c":
        return PartResult(Outcome.DIFFERENT, "Only the full company list returns the GUID",
                          spec_impact="No cheap active-company read: the S2 gate reads the company list (heavier) "
                                      "every cycle (Part 1 §5 gate.py).")
    return PartResult(Outcome.CONFIRMED, f"Candidate ({chosen}) returns the GUID in {results[chosen]['bytes']} bytes; "
                                         "the no-company response is distinguishable")


PROBE = Probe(
    id=2,
    name="active_company_guid",
    question="What is the cheapest read of the active company's GUID, and what comes back with no company open?",
    feeds=("decision 4", "R2"),
    parts={"A": run_a},
    requires=(0,),
)
