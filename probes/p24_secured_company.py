"""Probe 24 — secured companies (S0 spec §7 "Probe 24", company C). Feeds R2, R26 (Part 1 §12 item 24: does XML
export still work without extra credentials once the company is open, and do the error shapes differ?).

Manual only. Security and TallyVault are switched on in the TallyPrime UI, and a secured company's login / TallyVault
prompt would stop an unattended start (C44's Load= would reopen company C at the prompt), so `--auto` is refused. The
harness never asks for, stores or sends a username or password: the person types them into TallyPrime only.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from agent.tally.envelopes import COMPANY_PLACEHOLDER, build_company_list, esc
from agent.tally.xml_utils import detect_error, parse_company_list, read_objects
from probes.companies import (COMPANIES, COMPANY_C_BOOKS_FROM, COMPANY_C_BOOKS_TO, COMPANY_C_LEDGER,
                                 COMPANY_C_VOUCHER_NARRATION)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked, judge_halves
from probes.reads import fill_month_request, master_request, parse_vouchers

C = COMPANIES["C"]
PENDING_TIMEOUT = 10.0
NEEDED = (("active_company", 2), ("company_counters", 1), ("voucher_month", 5))
AUTO_REFUSED = ("Probe 24 is manual only: security and TallyVault are switched on in the TallyPrime UI, and their "
                "prompts block an unattended start. Run `run 24` without --auto, at the Mac.")
BASELINE_DRIFT = ("Company C isn't as `setup-c` leaves it (ledgers {ledgers}, vouchers {narrations}, errors {errors}) — "
                  "run `python -m probes setup-c` with only {company!r} open.")
SECURITY_ON = (f"Probe 24, step 1 — turn on security for {C!r}. In TallyPrime: K: Company → Alter → {C}. Set "
               "'Control user access to company data' to Yes, type a THROWAWAY username and password (never a real one; "
               "do not type them here or anywhere in the repo), repeat the password, and accept the screen (A: Accept). "
               "Log in if TallyPrime asks. Press Enter here once company C is open and you are at the Gateway of Tally.")
SECURITY_RESELECT = (f"Probe 24, step 2 — K: Company → Shut → {C}. Then K: Company → Select → {C}. STOP at the login "
                     "box: leave it open (do not log in yet) and press Enter here.")
LOGIN = "Probe 24, step 3 — log in to company C now, wait for the Gateway of Tally, then press Enter here."
VAULT_ON = (f"Probe 24, step 4 — turn on TallyVault for {C!r}: K: Company → Change TallyVault → {C}. Type a THROWAWAY "
            "TallyVault password twice (again: never here, never in the repo) and accept. If TallyPrime shuts or "
            "reopens the company, open it again (TallyVault password, then your login). Press Enter here once company "
            "C is open at the Gateway of Tally.")
VAULT_RESELECT = (f"Probe 24, step 5 — K: Company → Shut → {C}. Then K: Company → Select → {C}. STOP at the "
                  "TallyVault password box: leave it open and press Enter here.")
VAULT_OPEN = ("Probe 24, step 6 — type the TallyVault password (and your login when asked), wait for the Gateway of "
              "Tally, then press Enter here.")
PROMPT_STILL_OPEN = ("{stage}: prompt still open — Tally {why} after you pressed Enter. Finish the login / TallyVault "
                     "password in TallyPrime until the Gateway of Tally shows, then re-run probe 24 (restore the "
                     "company-C baseline backup first if security/TallyVault is already on). Nothing was judged.")
CONFIRMED_IMPACT = ("No credentials in the agent (R2, R26): once the person has logged in / entered the TallyVault "
                    "password in TallyPrime, XML export of a secured or vaulted company is unchanged (same GUID, same "
                    "data). Onboarding: open the company in TallyPrime as usual.")
HALF_CONFIRMED_IMPACT = ("{label} on: once the person has {action} in TallyPrime, XML export is unchanged (same GUID, "
                         "same data) — no credentials in the agent (R2, R26).")


def half_confirmed_impact(half: str) -> str:
    """A CONFIRMED half's impact, scoped to that half (C47 review I1): `judge_halves` keeps every half's impact, so
    the whole-probe CONFIRMED_IMPACT beside another half's DIFFERENT/FAILED impact would contradict it."""
    if half == "TallyVault":
        return HALF_CONFIRMED_IMPACT.format(label="TallyVault", action="entered the TallyVault password")
    return HALF_CONFIRMED_IMPACT.format(label=half[:1].upper() + half[1:], action="logged in")


RELINK_IMPACT = ("Export works once the company is open, but its identity/content changed: a new GUID with the same "
                 "name takes the re-link path (Q25), never a new company.")
RENAME_IMPACT = ("TallyVault renamed the company but kept its GUID: S1 keys a company by GUID only and updates its "
                 "stored name; the extractor sends the name TallyPrime lists now (Q25).")
REKEY_IMPACT = ("The company came back under a new name AND a new GUID with the same data: S1 can't key it by GUID, "
                "so it takes the re-link path (Q25) — a person confirms it is the same company — never a new company.")
FAILED_IMPACT = ("XML export of a company with {half} fails even with it open in TallyPrime: v1 can't sync such "
                 "companies — onboarding says so, and the gate reports it as its own condition (R26).")
UNDO_NOTE = ("Company C has security and/or TallyVault on (the throwaway credentials you chose). Nothing to undo in "
             "Tally: plan part 6 Task 11 archives company C's folder; to redo probe 24 restore "
             "s0probe-backups/<C number>-company-C-baseline-<date> first.")


def _parse(read: str, text: str | None) -> Any:
    if text is None:
        return None
    try:
        if read == "company_list":
            return parse_company_list(text)
        if read in ("active_company", "counters"):
            return [r for r in read_objects(text, "COMPANY", ["Name", "GUID"]) if r["Name"] or r["GUID"]]
        if read == "ledgers":
            return sorted(r["Name"] for r in read_objects(text, "LEDGER", ["Name"]) if r["Name"])
        return sorted(v["header"].get("NARRATION", "") for v in parse_vouchers(text))
    except ET.ParseError:
        return {"unparseable": text[:200]}


def _requests(ctx: ProbeContext) -> dict[str, str]:
    template = lambda name: ctx.store.confirmed(name)["xml_template"].replace(COMPANY_PLACEHOLDER, esc(C))
    return {"company_list": build_company_list(), "active_company": template("active_company"),
            "counters": template("company_counters"),
            "ledgers": master_request("S0P24Ledgers", "Ledger", ["Name", "Parent"], C),
            "vouchers": fill_month_request(ctx.store.confirmed("voucher_month")["xml_template"], C,
                                           COMPANY_C_BOOKS_FROM, COMPANY_C_BOOKS_TO)}


async def export(ctx: ProbeContext, stage: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    errors: dict[str, dict] = {}
    for read, xml in _requests(ctx).items():
        text, error = await ctx.try_send(f"{stage}_{read}", xml)
        if error:
            errors[read] = error
        elif detect_error(text):
            errors[read] = {"kind": "tally_error", "message": detect_error(text)}
        parsed[read] = _parse(read, text)
    active = parsed["active_company"] if isinstance(parsed["active_company"], list) else []
    counters = [r for r in (parsed["counters"] if isinstance(parsed["counters"], list) else []) if r["Name"] == C]
    out = {"stage": stage, "companies": parsed["company_list"], "errors": errors,
           "active_guid": active[0]["GUID"] if len(active) == 1 else "",
           "counters_guid": counters[0]["GUID"] if counters else "",
           "ledgers": parsed["ledgers"] if isinstance(parsed["ledgers"], list) else [],
           "narrations": parsed["vouchers"] if isinstance(parsed["vouchers"], list) else []}
    out["ok"] = (not errors and out["companies"] == [C] and bool(out["active_guid"])
                 and out["active_guid"] == out["counters_guid"] and COMPANY_C_LEDGER in out["ledgers"]
                 and COMPANY_C_VOUCHER_NARRATION in out["narrations"])
    return out


async def _gate_reads(ctx: ProbeContext, prefix: str) -> dict[str, Any]:
    """The gate's two cheap reads (company list, active company), captured as `<prefix>_<read>`."""
    shapes: dict[str, Any] = {}
    requests = _requests(ctx)
    for read in ("company_list", "active_company"):
        text, error = await ctx.try_send(f"{prefix}_{read}", requests[read], timeout=PENDING_TIMEOUT)
        if error:
            shapes[read] = {"transport": error["kind"], "body": None}
        else:
            body = _parse(read, text)
            shapes[read] = {"transport": "answered",
                            "body": {"companies": body} if read == "company_list" else {"rows": body}}
    return shapes


async def pending(ctx: ProbeContext, stage: str) -> dict[str, Any]:
    """The gate's two cheap reads while a login / TallyVault prompt is on screen (a new gate shape, spec §7)."""
    return await _gate_reads(ctx, stage)


async def _ready(ctx: ProbeContext, stage: str, prompt_shape: dict[str, Any]) -> dict[str, Any]:
    """Ruling S3 / review I1: after the person presses Enter at LOGIN / VAULT_OPEN, check the prompt is really gone
    before anything is judged. The same two reads as `pending` (captured as `<stage>_ready_*`) are compared with the
    prompt-state shape measured moments ago: if both answer exactly as they did while the prompt was open (same
    transport, same body) — whatever that shape is: a timeout, no company, or company C listed while named reads
    fail — Tally is still at the prompt, which is the person's timing -> BLOCKED, never a FAILED "export doesn't
    work". A timeout or an empty list is never an open company either. Which company is open is left to
    `_slip`/`_judge`, so a same-GUID rename is still recorded (Ruling S9)."""
    shape = await _gate_reads(ctx, f"{stage}_ready")
    listing = shape["company_list"]
    if shape == prompt_shape:
        why = "still answers exactly as it did while the prompt was open (indistinguishable from the prompt state)"
    elif listing["transport"] != "answered":
        why = f"didn't answer the company list ({listing['transport']})"
    elif not listing["body"]["companies"]:
        why = "lists no open company"
    else:
        return shape
    raise ProbeBlocked(PROMPT_STILL_OPEN.format(stage=stage, why=why))


def _renamed_and_rekeyed(stage: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """Review I2(b): one company listed, not under C's name, a GUID other than C's baseline one, but exactly C's
    baseline ledgers and vouchers — TallyVault renamed AND re-keyed company C (unmeasured), not another company."""
    names = stage["companies"] or []
    return (len(names) == 1 and names != [C] and stage["active_guid"] != baseline["active_guid"]
            and COMPANY_C_LEDGER in stage["ledgers"] and stage["ledgers"] == baseline["ledgers"]
            and stage["narrations"] == baseline["narrations"])


def _slip(stage: dict[str, Any], baseline: dict[str, Any]) -> None:
    names = stage["companies"] or []
    if len(names) > 1 or (names and names != [C] and stage["active_guid"] != baseline["active_guid"]
                          and not _renamed_and_rekeyed(stage, baseline)):
        raise ProbeBlocked(f"{stage['stage']}: Tally has {names} open, not only {C!r} — open only company C and re-run "
                           "probe 24 (restore the company-C baseline backup first if security/TallyVault is on). If "
                           f"only company C IS open, TallyVault may have renamed and re-keyed it without its data "
                           f"reading back as C's baseline — see observations.{stage['stage']}.")


def _judge(stage: dict[str, Any], baseline: dict[str, Any], half: str) -> tuple[str, str, str]:
    """(verdict, text, impact) for one half. A rename that keeps the GUID is checked BEFORE `ok` (Ruling S9): `ok`
    requires the listing to be exactly [C], so a renamed company would otherwise read as FAILED."""
    names = stage["companies"] or []
    if len(names) == 1 and names != [C] and stage["active_guid"] and stage["active_guid"] == baseline["active_guid"]:
        reads = f"failed: {', '.join(stage['errors'])}" if stage["errors"] else "worked"
        return "DIFFERENT", f"company renamed to {names[0]!r} with the same GUID (named reads {reads})", RENAME_IMPACT
    if _renamed_and_rekeyed(stage, baseline):
        who = "vault" if half == "TallyVault" else half
        return ("DIFFERENT", f"{who} renamed and re-keyed: listed as {names[0]!r} with a new GUID "
                f"{stage['active_guid'] or '(none read)'}, C's baseline ledgers and vouchers", REKEY_IMPACT)
    if not stage["ok"]:
        why = ", ".join(f"{k}: {v['kind']}" for k, v in stage["errors"].items()) or (
            f"listed as {stage['companies']}, ledgers {stage['ledgers'][:3]}, vouchers {stage['narrations'][:2]}")
        return "FAILED", f"export doesn't work with the company open ({why})", FAILED_IMPACT.format(half=half)
    changed = [k for k in ("active_guid", "ledgers", "narrations") if stage[k] != baseline[k]]
    if changed:
        return "DIFFERENT", f"export works but {', '.join(changed)} changed", RELINK_IMPACT
    return "CONFIRMED", "export unchanged", half_confirmed_impact(half)


def _shape_text(shapes: dict[str, Any]) -> str:
    listing = shapes["company_list"]
    if listing["transport"] != "answered":
        return f"a {listing['transport']} (the prompt blocks the XML server)"
    if listing["body"]["companies"]:
        active = shapes["active_company"]
        rows = active["body"]["rows"] if active["transport"] == "answered" else active["transport"]
        return (f"an answer listing {listing['body']['companies']} with the active-company read giving {rows} "
                "(listed, but not open for named reads)")
    return f"an answer listing {listing['body']['companies']} (as if no company were open)"


async def run_c(ctx: ProbeContext) -> PartResult:
    for name, probe_id in NEEDED:
        if ctx.store.confirmed(name) is None:
            raise ProbeBlocked(f"No confirmed {name} request: run probe {probe_id} first (S0-D7).")
    if ctx.run_mode == "auto":
        raise ProbeBlocked(AUTO_REFUSED)
    baseline = await export(ctx, "baseline")
    ctx.observe("baseline", baseline)
    if not baseline["ok"]:
        raise ProbeBlocked(BASELINE_DRIFT.format(ledgers=baseline["ledgers"], narrations=baseline["narrations"],
                                                 errors=baseline["errors"], company=C))
    ctx.on_abort(UNDO_NOTE)
    ctx.pause(SECURITY_ON)
    ctx.pause(SECURITY_RESELECT)
    login_pending = await pending(ctx, "security_login_pending")
    ctx.pause(LOGIN)
    ctx.observe("gate_shapes", {"security_login_pending": login_pending})
    ctx.observe("security_on_ready", await _ready(ctx, "security_on", login_pending))
    security = await export(ctx, "security_on")
    ctx.observe("security_on", security)                  # review I2(a): observed before `_slip` can BLOCK
    _slip(security, baseline)
    ctx.pause(VAULT_ON)
    ctx.pause(VAULT_RESELECT)
    vault_pending = await pending(ctx, "vault_prompt_pending")
    ctx.pause(VAULT_OPEN)
    ctx.observe("gate_shapes", {"security_login_pending": login_pending, "vault_prompt_pending": vault_pending})
    ctx.observe("vault_on_ready", await _ready(ctx, "vault_on", vault_pending))
    vault = await export(ctx, "vault_on")
    ctx.observe("vault_on", vault)
    _slip(vault, baseline)

    halves = {"security": _judge(security, baseline, "security"),
              "TallyVault": _judge(vault, baseline, "TallyVault")}
    shapes = {"security_login_pending": login_pending, "vault_prompt_pending": vault_pending}
    ctx.observe("gate_shapes", shapes)
    ctx.observe("sub_verdicts", {k: v[0] for k, v in halves.items()})
    outcome, summary, impacts = judge_halves({f"{k} on": v for k, v in halves.items()})
    if outcome is Outcome.CONFIRMED:                     # both halves: the whole-probe sentence, once (review I1)
        impacts = [CONFIRMED_IMPACT]
    login, vault_shape = _shape_text(login_pending), _shape_text(vault_pending)
    summary += f"; login prompt → {login}; TallyVault prompt → {vault_shape}"
    gate = (f"While a login prompt is open the gate sees {login}; while a TallyVault prompt is open, {vault_shape}. "
            "S2's gate treats both as 'company not open' (skip quietly, retry next cycle).")
    return PartResult(outcome, summary, spec_impact=" ".join([*dict.fromkeys(impacts), gate]))


PROBE = Probe(
    id=24,
    name="secured_company",
    question="Does XML export still work for a company with security and then TallyVault, once it is open, and what "
             "does the gate see while TallyPrime waits for the login / vault password?",
    feeds=("R2", "R26"),
    parts={"C": run_c},
    requires=(0, 1, 2, 5),
    mutating=True,
)
