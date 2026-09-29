"""Probe 10 — what Tally answers when something is wrong (S0 spec §7 "Probe 10"). Feeds the S2 gate (R26).

Order: popup → no company → Tally quit → back up with company A (probe 13 follows in batch 4), then the Educational row.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from v2.agent.tally.envelopes import COMPANY_PLACEHOLDER, esc, wrap_collection, wrap_report
from v2.agent.tally.xml_utils import detect_error
from v2.probes.actions import Action
from v2.probes.companies import POPUP_STOCK_GROUP
from v2.probes.context import ProbeContext
from v2.probes.core import Outcome, PartResult, Probe
from v2.probes.licence import LICENCE_REQUEST, parse_licence_info
from v2.probes.reads import A_FY_FROM, A_FY_TO, master_request

POPUP_TIMEOUT_S = 10.0
STOCK_GROUP_NOTE = (f"Cleanup: delete stock group {POPUP_STOCK_GROUP!r} created by probe 10 (the duplicate create succeeded, so no "
                    "popup was raised and the group did not exist before)")
POPUP_NOTE = "Dismiss the popup in Tally (or restart Tally) and open company {company!r} only"
REOPEN_NOTE = "Start TallyPrime and open company {company!r} only"


def fallback_cheap_read() -> str:
    """Used only if probe 2's request isn't stored (it always is once probe 2 ran — `requires` includes 2)."""
    return wrap_collection("S0P10Cheap", "Company", ["Name", "GUID"],
                           filters=[("S0P10Active", "$Name = ##SVCurrentCompany")])


def _cheap_read(ctx: ProbeContext) -> str:
    confirmed = ctx.store.confirmed("active_company")
    template = confirmed["xml_template"] if confirmed else fallback_cheap_read()
    return template.replace(COMPANY_PLACEHOLDER, esc(ctx.company_name))


def body_kind(text: str) -> str:
    if "<ERRORMSG>" in text.upper() or detect_error(text) is not None:
        return "error message"
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return "not XML"
    has_objects = any(len(child) or child.attrib for coll in root.iter("COLLECTION") for child in coll)
    has_rows = root.find(".//DSPACCNAME") is not None or root.find(".//BILLFIXED") is not None
    return "data" if has_objects or has_rows else "empty"


def _shape(text: str | None, error: dict | None) -> dict:
    if error is not None:
        return {"transport": error["kind"], "body": "", "tally_error": None, "snippet": ""}
    return {"transport": "ok", "body": body_kind(text), "tally_error": detect_error(text), "snippet": text[:300]}


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    cheap = _cheap_read(ctx)
    popup_note, reopen_note = POPUP_NOTE.format(company=company), REOPEN_NOTE.format(company=company)

    ctx.on_abort(popup_note)
    ctx.pause("Make Tally busy with a modal: open a voucher entry screen (e.g. Vouchers → F5 Payment) and leave it open, "
              "unsaved.", Action("raise_popup", {"company": company}))
    popup_raised = getattr(ctx.io, "popup_raised", None)
    if popup_raised is False:
        ctx.on_abort(STOCK_GROUP_NOTE)      # the "duplicate" create really created the group
    popup = _shape(*await ctx.try_send("popup_read", cheap, timeout=POPUP_TIMEOUT_S))
    ctx.pause(f"Dismiss it (Esc, don't save) and make sure only {company!r} is open.",
              Action("dismiss_popup", {"label": ctx.part}))
    after_text, after_error = await ctx.try_send("after_popup", cheap)
    after = _shape(after_text, after_error)
    if after_error is not None:
        ctx.observe("popup", popup)
        ctx.observe("after_popup", after)
        return PartResult(Outcome.BLOCKED, f"Tally didn't recover after the popup was dismissed ({after_error['kind']})")
    ctx.resolve_abort(popup_note)

    ctx.on_abort(reopen_note)
    ctx.pause("Close every company in Tally (Alt+K → Shut Company); keep Tally running.", Action("close_all_companies"))
    collection = _shape(*await ctx.try_send("no_company_collection",
                                            master_request("S0P10Ledgers", "Ledger", ["Name"], company)))
    report = _shape(*await ctx.try_send("no_company_report", wrap_report("Trial Balance", A_FY_FROM, A_FY_TO, company)))
    ctx.pause("Quit TallyPrime completely (Ctrl+Q).", Action("quit_tally"))
    quit_shape = _shape(*await ctx.try_send("tally_quit", cheap))
    ctx.pause(f"Start TallyPrime again and open company {company!r} only.", Action("open_company", {"label": ctx.part}))
    await ctx.check_company()
    ctx.resolve_abort(reopen_note)

    if ctx.store.environment.get("licence") == "educational":
        edu_text, edu_error = await ctx.try_send("educational_licence_info", LICENCE_REQUEST)
        info = parse_licence_info(edu_text) if edu_text is not None else None
        educational = {"transport": edu_error["kind"] if edu_error else "ok",
                       "body": f"educational={info.educational}, release {info.release or '?'}" if info else "",
                       "tally_error": None, "snippet": (edu_text or "")[:300],
                       "gate_action": "flag the company 'Educational' (dates restricted); never treat as an error"}
    else:
        educational = {"transport": "", "body": "not applicable", "tally_error": None, "snippet": "",
                       "gate_action": "—"}

    if popup["transport"] == "timeout" and popup_raised is not False:
        popup_gate_action = "back off; tell the user to check Tally for an open popup (LESSONS §15 rule 10)"
    elif popup["transport"] == "timeout":
        # The duplicate create went through, so nothing was raised: this row is a timeout, not popup evidence.
        popup_gate_action = ("back off; but no popup was raised (the duplicate create succeeded), so this row says "
                             "nothing about popups — the read timed out for another reason")
    elif popup_raised is False:
        popup_gate_action = "popup not raised"
    else:
        popup_gate_action = "reads still answer during this popup: the gate can't see it (it only matters to writes)"
    table = [
        {"condition": "popup / modal open", **popup, "gate_action": popup_gate_action},
        {"condition": "after the popup is dismissed", **after, "gate_action": "carry on"},
        {"condition": "no company open (collection)", **collection,
         "gate_action": "treat as 'company closed': skip the cycle, no alert"},
        {"condition": "no company open (report)", **report, "gate_action": "same as the collection row"},
        {"condition": "Tally not running", **quit_shape, "gate_action": "treat as 'Tally closed': skip the cycle, no alert"},
        {"condition": "Educational mode", **educational},
    ]
    ctx.observe("gate_table", table)
    ctx.observe("popup_raised", popup_raised)
    if popup_raised is False:
        ctx.observe("cleanup_needed", [STOCK_GROUP_NOTE])
    blind = [row["condition"] for row in table[2:5]
             if row["transport"] == "ok" and row["body"] == "data" or
             (row["condition"] == "Tally not running" and row["transport"] != "refused")]
    if blind:
        return PartResult(Outcome.DIFFERENT, f"Not distinguishable from a healthy answer: {', '.join(blind)}",
                          spec_impact="The S2 gate can't read these conditions from the response alone: it also checks "
                                      "the company list before trusting a read (Part 1 §5 gate.py).")
    return PartResult(Outcome.CONFIRMED, "No company, no report and Tally-not-running each have their own shape "
                                         f"(popup read: {popup['transport']}); gate table recorded")


PROBE = Probe(
    id=10,
    name="error_shapes",
    question="What does Tally answer with no company open, a popup open, Tally closed, and in Educational mode?",
    feeds=("R26", "S2 gate"),
    parts={"A": run_a},
    requires=(0, 1, 2),
    mutating=True,
)
