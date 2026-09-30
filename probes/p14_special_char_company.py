"""Probe 14: `&` and `'` in the company name (S0 spec §7 "Probe 14", B). Feeds R13.

Three reads of the same Ledger list: the v2 envelope (company name escaped); the same with SVCurrentCompany naming a
company that isn't loaded (control: does Tally even read the variable with one company open?); and last, a deliberately
UNESCAPED copy (raw `&` — not well-formed XML), with a short timeout, after which Tally must still answer.
"""
from __future__ import annotations

from agent.tally.envelopes import esc
from agent.tally.xml_utils import detect_error, read_objects
from probes.company_b_view import HINDI_DEBTOR, ledger_specs, loaded_licence
from probes.context import POPUP_HINT, ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import master_request

COLLECTION = "S0P14Ledgers"
UNKNOWN_SUFFIX = " (S0 probe 14 — no such company)"
UNESCAPED_TIMEOUT_S = 10.0
ESCAPED_FAILED_IMPACT = ("An escaped company name doesn't reach its company: R13's escaping is not enough, and the agent "
                         "addresses the company another way (the gate's GUID check, probe 2) before S2.")
TOLERATED_IMPACT = ("Tally accepts the raw `&` in SVCurrentCompany: escaping stays mandatory in v2 (a malformed request "
                    "is never sent), but it isn't what makes such names work (R13).")
# M1: two wordings, chosen by whether the unknown-company control shows SVCurrentCompany is actually read (`ok`
# False) or ignored (`ok` True) with one company loaded. The old single CONFIRMED_IMPACT claimed escaping "is what
# makes" the company reachable even when IGNORED_VAR_NOTE, appended right after it, said the variable doesn't
# select the company at all — the two sentences contradicted each other.
CONFIRMED_IMPACT = ("R13 holds: the v2 envelope's escaping is what makes `Sharma & Sons' Probe Traders` reachable, and "
                    "the unescaped form fails as recorded.")
CONFIRMED_IGNORED_VAR_IMPACT = ("R13 holds for well-formedness: escaping keeps the request well-formed (the unescaped "
                                "form fails as recorded), but with one company loaded SVCurrentCompany doesn't select "
                                "the company — it doesn't make `Sharma & Sons' Probe Traders` reachable on its own; "
                                "the agent addresses the company another way (the gate's GUID check, probe 2).")
IGNORED_VAR_NOTE = ("With one company loaded Tally answered a request naming a company that isn't open: SVCurrentCompany "
                    "can't be the agent's company check — the gate's GUID read (probe 2) is.")


def escaped_request(company: str) -> str:
    return master_request(COLLECTION, "Ledger", ["Name", "Parent"], company)


def unescaped(xml: str, company: str) -> str:
    escaped = f"<SVCurrentCompany>{esc(company)}</SVCurrentCompany>"
    if escaped not in xml:
        raise ValueError("the escaped SVCurrentCompany element isn't in the request")
    return xml.replace(escaped, f"<SVCurrentCompany>{company}</SVCurrentCompany>")


def shape(text: str | None, error: dict | None, expected: set[str], raw: bytes = b"") -> dict:
    if error is not None:
        return {"ok": False, "kind": error["kind"], "message": error["message"]}
    problem = detect_error(text)
    names = [] if problem else [row["Name"] for row in read_objects(text, "LEDGER", ["Name"])]
    return {"ok": problem is None and expected <= set(names), "kind": "answered", "error": problem,
            "ledgers": len(names), "has_hindi": HINDI_DEBTOR in names, "bytes": len(raw)}


async def _read(ctx: ProbeContext, step: str, xml: str, expected: set[str], timeout: float | None = None) -> dict:
    text, error = await ctx.try_send(step, xml, timeout=timeout)
    return shape(text, error, expected, ctx.last_response.raw if ctx.last_response else b"")


async def run_b(ctx: ProbeContext) -> PartResult:
    expected = set(ledger_specs(loaded_licence(ctx.store.environment)))
    company = ctx.company_name
    xml = escaped_request(company)
    escaped = await _read(ctx, "escaped_request", xml, expected)
    # I3: a timeout here means Tally is already not responding — sending the malformed request next would then be
    # blamed for a hang it didn't cause. Stop and name the step that actually timed out.
    if escaped["kind"] == "timeout":
        raise ProbeBlocked(f"escaped_request: Tally stopped answering ({escaped['message']}). {POPUP_HINT} The "
                           "unescaped (malformed) request was never sent.")
    unknown = await _read(ctx, "unknown_company_request", escaped_request(company + UNKNOWN_SUFFIX), expected)
    if unknown["kind"] == "timeout":
        raise ProbeBlocked(f"unknown_company_request: Tally stopped answering ({unknown['message']}). {POPUP_HINT} "
                           "The unescaped (malformed) request was never sent.")
    raw = await _read(ctx, "unescaped_request", unescaped(xml, company), expected, timeout=UNESCAPED_TIMEOUT_S)
    ctx.observe("escaped", escaped)
    ctx.observe("unknown_company", unknown)
    ctx.observe("unescaped", raw)
    try:
        ctx.observe("tally_after", await ctx.company_names())
    except ProbeBlocked as exc:
        raise ProbeBlocked(f"Tally stopped answering after the unescaped request ({exc}). {POPUP_HINT}") from exc
    note = f" {IGNORED_VAR_NOTE}" if unknown["ok"] else ""
    if not escaped["ok"]:
        return PartResult(Outcome.FAILED, f"The escaped request didn't return company B's ledgers ({escaped})",
                          spec_impact=ESCAPED_FAILED_IMPACT + note)
    if raw["ok"]:
        return PartResult(Outcome.DIFFERENT, "The unescaped company name worked too", spec_impact=TOLERATED_IMPACT + note)
    failure = raw.get("error") or raw.get("kind")
    # M1: CONFIRMED_IGNORED_VAR_IMPACT already carries the probe-2 wording, so it stands alone (no `note` append).
    impact = CONFIRMED_IGNORED_VAR_IMPACT if unknown["ok"] else CONFIRMED_IMPACT
    return PartResult(Outcome.CONFIRMED, f"Escaped: {escaped['ledgers']} ledgers incl. the Hindi one; unescaped: "
                                         f"{failure!r}; Tally answered afterwards", spec_impact=impact)


PROBE = Probe(
    id=14,
    name="special_char_company",
    question="Does the escaped envelope reach a company named with `&` and `'`, and how does an unescaped one fail?",
    feeds=("R13",),
    parts={"B": run_b},
    requires=(0,),
)
