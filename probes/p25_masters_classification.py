"""Probe 25 — group nature / revenue flags and voucher-type base type (S0 spec §7 "Probe 25", A part).

Feeds the S1 schema, R5 and R16. The B part (plan part 6): the custom voucher type 'Sales - GST' must resolve to
Sales, plus the R9 duplicate-ledger-name UI attempt moved here from the company-B loader (spec §4.3 note,
2026-09-24). A base type found by ReservedName is CONFIRMED; found only by walking the voucher-type Parent chain it is
DIFFERENT (spec §7 probe 25 "Missing → DIFFERENT … base type by the parent chain", as 25 A's live verdict; Ruling S6).
"""
from __future__ import annotations

from tally_bridge.xml_utils import read_objects
from probes.company_b_view import B_CUSTOM_VOUCHER_TYPE, B_CUSTOM_VOUCHER_TYPE_BASE, loaded_licence, r9_candidate
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked, judge_halves
from probes.reads import PL_PRIMARY_GROUPS, PRIMARY_NATURE, ancestors, master_request

GROUP_CANDIDATES = ["PrimaryGroup", "_PrimaryGroup", "Nature", "IsRevenue", "AffectsGrossProfit", "IsDeemedPositive",
                    "ReservedName"]
GROUP_FIELDS = ["Name", "Parent", *GROUP_CANDIDATES]
VOUCHER_TYPE_CANDIDATES = ["Parent", "ReservedName"]
VOUCHER_TYPE_FIELDS = ["Name", *VOUCHER_TYPE_CANDIDATES]
CHECK_GROUP = "National Creditors"


def nature_from_fields(row: dict[str, str]) -> tuple[str | None, str | None]:
    """(nature, the field that gave it) from the candidate fields alone."""
    for field in ("PrimaryGroup", "_PrimaryGroup"):
        if row.get(field) in PRIMARY_NATURE:
            return PRIMARY_NATURE[row[field]], field
    nature = row.get("Nature", "").lower()
    for word, value in (("liabilit", "liabilities"), ("asset", "assets"), ("income", "income"), ("expense", "expenses")):
        if word in nature:
            return value, "Nature"
    return None, None


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    groups = read_objects(await ctx.send("groups", master_request("S0P25Groups", "Group", GROUP_FIELDS, company)),
                          "GROUP", GROUP_FIELDS)
    vtypes = read_objects(await ctx.send("voucher_types", master_request("S0P25VoucherTypes", "VoucherType",
                                                                         VOUCHER_TYPE_FIELDS, company)),
                          "VOUCHERTYPE", VOUCHER_TYPE_FIELDS)
    parents = {row["Name"]: row["Parent"] for row in groups if row["Name"]}
    present = {field: sum(1 for row in groups if row[field]) for field in ["Parent", *GROUP_CANDIDATES]}
    check = next((row for row in groups if row["Name"] == CHECK_GROUP), None)
    walk = ancestors(CHECK_GROUP, parents) if check else []
    walk_nature = PRIMARY_NATURE.get(walk[-1]) if walk else None
    field_nature, nature_field = nature_from_fields(check) if check else (None, None)
    revenue = {row["Name"]: row["IsRevenue"] for row in groups if row["Name"] in PRIMARY_NATURE and row["IsRevenue"]}
    revenue_ok = bool(revenue) and all((value == "Yes") == (name in PL_PRIMARY_GROUPS) for name, value in revenue.items())
    vparents = {row["Name"]: row["Parent"] for row in vtypes if row["Name"]}
    base_types = {row["Name"]: row["ReservedName"] or ancestors(row["Name"], vparents)[-1] for row in vtypes}
    vtypes_ok = bool(vtypes) and all(row["Parent"] or row["ReservedName"] for row in vtypes)
    ctx.observe("present", present)
    ctx.observe("walk", walk)
    ctx.observe("walk_nature", walk_nature)
    ctx.observe("field_nature", field_nature)
    ctx.observe("nature_field", nature_field)
    ctx.observe("is_revenue_consistent", revenue_ok)
    ctx.observe("voucher_type_fields", {field: sum(1 for row in vtypes if row[field]) for field in VOUCHER_TYPE_CANDIDATES})
    ctx.observe("base_types", base_types)

    if not present["Parent"] or check is None or walk_nature != "liabilities":
        return PartResult(Outcome.FAILED, f"Group Parent doesn't classify {CHECK_GROUP!r} (walk: {walk or 'none'})",
                          spec_impact="S1 can't tell balance-sheet from P&L groups from the export: the S1 schema's "
                                      "group classification is revisited before S1 (R5, R16).")
    if field_nature == "liabilities" and revenue_ok and vtypes_ok:
        return PartResult(Outcome.CONFIRMED, f"{nature_field} gives {CHECK_GROUP!r} = liabilities; IsRevenue is "
                                             "consistent; voucher types export Parent / ReservedName",
                          spec_impact=f"S1: group nature from {nature_field}, P&L flag from IsRevenue; voucher base type "
                                      "from ReservedName, walking Parent for custom types.")
    return PartResult(Outcome.DIFFERENT, "Nature / revenue / base-type fields don't all export; walking Parent gives "
                                         f"{CHECK_GROUP!r} → {walk[-1]} ({walk_nature})",
                      spec_impact="Nature is derived by walking Parent to a reserved primary group (PRIMARY_NATURE map) "
                                  "and base type by the voucher-type Parent chain; the mapping goes into the S1 spec.")


LEDGER_CHECK_FIELDS = ["Name", "Parent"]
B_TYPE_IMPACT = ("S1 reads a custom voucher type's base type from ReservedName (R16): 'Sales - GST' → Sales.")
B_TYPE_WALK_IMPACT = ("A custom voucher type exports no base type of its own: S1 derives it by walking the voucher-type "
                      "Parent chain (R16): 'Sales - GST' → Sales — the same Parent-chain rule probe 25 A put into the "
                      "S1 spec.")
B_TYPE_FAILED_IMPACT = ("A custom voucher type's base type can't be read or derived from the export: S1 can't classify "
                        "custom types (R16) and needs another source before their vouchers reach typed tiles.")
R9_REFUSED_IMPACT = ("R9: ledger names are unique within a company (TallyPrime refused the duplicate), so S1's "
                     "name → GUID resolution on ingest is unambiguous.")
R9_ACCEPTED_IMPACT = ("R9: TallyPrime allows two ledgers with one name under different groups: S1 keys ledgers by GUID "
                      "only and resolves a line's ledger name with its parent, or by the GUID on lines (probe 6).")
R9_NOTE = ("Company B: if TallyPrime saved a second ledger {name!r} under {other!r}, delete it (Gateway of Tally → "
           "Alter → Ledger → the one under {other!r} → D: Delete) or restore company B's backup "
           "(s0probe-backups/100000-company-B-loaded-2026-09-24).")


R9_EMPTY_REASK = "No answer was typed (Enter alone). Asking once more — "


def r9_prompt(name: str, parent: str, other: str) -> str:
    return (f"Probe 25 B, duplicate ledger name (R9). In TallyPrime: Gateway of Tally → Create → Ledger. Name: {name}   "
            f"Under: {other}   (it already exists under {parent}). Try to save it (A: Accept on the right-hand "
            "button bar). Then press Esc until you are back at the Gateway of Tally, answering Yes if TallyPrime "
            "asks to quit without saving. Type TallyPrime's message word for word, or 'saved' if it saved the "
            "ledger, or 'skip' to leave R9 unmeasured.")


async def duplicate_name_attempt(ctx: ProbeContext, licence: str) -> dict:
    if not ctx.io.interactive:
        return {"status": "not attempted (non-interactive)"}
    if ctx.run_mode == "auto":
        return {"status": "not attempted (auto mode: a duplicate create is never sent over XML — it raises a "
                          "blocking modal, LESSONS §15 rule 10)"}
    name, parent, other = r9_candidate(licence)
    note = R9_NOTE.format(name=name, other=other)
    ctx.on_abort(note)
    answer = ctx.ask(r9_prompt(name, parent, other)).strip()
    if not answer:                                   # review M6: Enter alone is not TallyPrime's message
        answer = ctx.ask(R9_EMPTY_REASK + r9_prompt(name, parent, other)).strip()
    if answer.lower() == "skip":
        ctx.resolve_abort(note)
        return {"status": "skipped by the operator"}
    rows = read_objects(await ctx.send("ledgers_after_duplicate_attempt", master_request(
        "S0P25BLedgers", "Ledger", LEDGER_CHECK_FIELDS, ctx.company_name)), "LEDGER", LEDGER_CHECK_FIELDS)
    parents = sorted(row["Parent"] for row in rows if row["Name"] == name)
    out = {"status": "attempted", "ledger": name, "existing_parent": parent, "tried_parent": other,
           "tally_message": answer or None, "parents_after": parents}
    if not answer and parents == [parent]:
        # Review M6: no answer means no evidence an attempt was made, so one ledger read back isn't a refusal.
        ctx.resolve_abort(note)
        return {"status": "not measured (no answer typed, twice)", "ledger": name, "parents_after": parents}
    if parents == [parent]:
        out["verdict"] = "refused"
        ctx.resolve_abort(note)
    elif len(parents) > 1 and parent in parents:
        out["verdict"] = "accepted"
        out["cleanup_needed"] = note
    else:
        raise ProbeBlocked(f"Ledger {name!r} now reads under {parents or 'nothing'}, not {parent!r} — company B "
                           f"changed during the R9 attempt. {note}")
    out["answer_agrees"] = (None if not answer
                            else (answer.lower() == "saved") == (out["verdict"] == "accepted"))
    return out


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    vtypes = read_objects(await ctx.send("voucher_types", master_request(
        "S0P25BVoucherTypes", "VoucherType", VOUCHER_TYPE_FIELDS, ctx.company_name)), "VOUCHERTYPE", VOUCHER_TYPE_FIELDS)
    row = next((r for r in vtypes if r["Name"] == B_CUSTOM_VOUCHER_TYPE), None)
    if row is None:
        raise ProbeBlocked(f"Company B has no voucher type {B_CUSTOM_VOUCHER_TYPE!r} (made in the UI before setup-b, "
                           "spec §4.3) — company B drifted or isn't loaded.")
    walk = ancestors(B_CUSTOM_VOUCHER_TYPE, {r["Name"]: r["Parent"] for r in vtypes if r["Name"]})
    resolved_by = ("ReservedName" if row["ReservedName"] == B_CUSTOM_VOUCHER_TYPE_BASE
                   else "Parent walk" if len(walk) > 1 and walk[-1] == B_CUSTOM_VOUCHER_TYPE_BASE else None)
    ctx.observe("voucher_type", {"name": B_CUSTOM_VOUCHER_TYPE, "parent": row["Parent"],
                                 "reserved_name": row["ReservedName"], "walk": walk, "resolved_by": resolved_by})
    r9 = await duplicate_name_attempt(ctx, licence)
    ctx.observe("r9", r9)

    halves: dict[str, tuple[str, str, str]] = {}
    if resolved_by == "ReservedName":
        halves["voucher_type"] = ("CONFIRMED", f"{B_CUSTOM_VOUCHER_TYPE!r} resolves to {B_CUSTOM_VOUCHER_TYPE_BASE} "
                                               "by ReservedName", B_TYPE_IMPACT)
    elif resolved_by == "Parent walk":                 # Ruling S6: the spec's "base type by the parent chain" case
        halves["voucher_type"] = ("DIFFERENT", f"{B_CUSTOM_VOUCHER_TYPE!r} resolves to {B_CUSTOM_VOUCHER_TYPE_BASE} "
                                               f"only by the Parent walk ({' → '.join(walk)})", B_TYPE_WALK_IMPACT)
    else:
        halves["voucher_type"] = ("FAILED", f"{B_CUSTOM_VOUCHER_TYPE!r} doesn't resolve to {B_CUSTOM_VOUCHER_TYPE_BASE} "
                                            f"(Parent {row['Parent']!r}, ReservedName {row['ReservedName']!r})",
                                  B_TYPE_FAILED_IMPACT)
    if r9.get("verdict") == "refused":
        halves["duplicate_name"] = ("CONFIRMED", f"TallyPrime refused a second {r9['ledger']!r} "
                                                 f"({r9['tally_message']!r})", R9_REFUSED_IMPACT)
    elif r9.get("verdict") == "accepted":
        halves["duplicate_name"] = ("DIFFERENT", f"TallyPrime SAVED a second {r9['ledger']!r} under "
                                                 f"{r9['tried_parent']!r} — CLEANUP: {r9['cleanup_needed']}",
                                    R9_ACCEPTED_IMPACT)
    ctx.observe("sub_verdicts", {k: v[0] for k, v in halves.items()} | ({} if "duplicate_name" in halves
                                                                       else {"duplicate_name": r9["status"]}))
    outcome, summary, impacts = judge_halves(halves)
    if "duplicate_name" not in halves:
        summary += f"; duplicate name (R9): {r9['status']}"
    return PartResult(outcome, summary, spec_impact=" ".join(impacts))


PROBE = Probe(
    id=25,
    name="masters_classification",
    question="Do groups export nature / IsRevenue / AffectsGrossProfit, and voucher types their parent and base type?",
    feeds=("S1 schema", "R5", "R16"),
    parts={"A": run_a, "B": run_b},
    planned_parts=("A", "B"),
    requires=(0,),
    mutating=True,
)
