"""Probe 23 — GST classification on ledger masters (S0 spec §7 "Probe 23", A part). Feeds the Part 3 GST tile.

The B part (plan part 6): BillCreditPeriod on the vouchers' New Ref bills, and Bills Receivable's due-date column, both
against the dataset. The due column's rule is measured, not assumed (Ruling S4): every compared bill's (due − bill
date) offset in days is recorded, and due dates that follow another rule get their own impact.
"""
from __future__ import annotations

from tally_bridge.envelopes import wrap_report
from tally_bridge.sync_reports import parse_bills
from tally_bridge.xml_utils import read_objects
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_TO, BillTerm, bill_terms, credit_days, drift_message,
                                      fetch_window, loaded_licence, month_window, tag_of)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import master_request, parse_vouchers, tally_date

GST_CANDIDATES = ["TaxType", "GSTDutyHead", "TypeOfDutyTax"]
FIELDS = ["Name", "Parent", *GST_CANDIDATES]
TAX_PARENT = "Duties & Taxes"
DUTY_HEADS = {"Central Tax", "State Tax", "Integrated Tax"}


async def run_a(ctx: ProbeContext) -> PartResult:
    rows = read_objects(await ctx.send("gst_ledgers", master_request("S0P23Ledgers", "Ledger", FIELDS, ctx.company_name)),
                        "LEDGER", FIELDS)
    tax = [row for row in rows if row["Parent"] == TAX_PARENT]
    other = [row for row in rows if row["Parent"] != TAX_PARENT]
    per_field = {field: {"tax_filled": sum(1 for row in tax if row[field]),
                         "other_filled": sum(1 for row in other if row[field]),
                         "tax_values": sorted({row[field] for row in tax if row[field]}),
                         "other_values": sorted({row[field] for row in other if row[field]})}
                 for field in GST_CANDIDATES}
    rule_field = None
    for field in GST_CANDIDATES:
        tax_values = {row[field] for row in tax}
        if tax and "" not in tax_values and not tax_values & {row[field] for row in other}:
            rule_field = field
            break
    duty_heads = sorted({row["GSTDutyHead"] for row in tax})
    duty_head_ok = bool(tax) and set(duty_heads) <= DUTY_HEADS
    ctx.observe("tax_ledgers", len(tax))
    ctx.observe("fields", per_field)
    ctx.observe("rule_field", rule_field)
    ctx.observe("duty_heads", duty_heads)
    ctx.observe("duty_head_ok", duty_head_ok)
    if rule_field is None:
        return PartResult(Outcome.FAILED, f"No candidate field ({', '.join(GST_CANDIDATES)}) identifies the "
                                          f"{len(tax)} Duties & Taxes ledgers",
                          spec_impact="Tax ledgers can't be identified without name matching: the GST position tile is "
                                      "dropped from v1, never approximated (Part 1 probe 23, Part 3).")
    head = "GSTDutyHead gives Central / State / Integrated Tax" if duty_head_ok else "GSTDutyHead doesn't give the duty head"
    return PartResult(Outcome.CONFIRMED, f"{rule_field} identifies the {len(tax)} GST ledgers "
                                         f"({per_field[rule_field]['tax_values']}); {head}",
                      spec_impact=f"S1 classifies GST ledgers by {rule_field}; "
                                  + ("duty head from GSTDutyHead." if duty_head_ok else "no duty head field (tile shows "
                                                                                        "GST in total only)."))


CREDIT_MONTH = (2023, 6)             # probe 5's own month: 30-day sales and 45-day purchase New Ref bills
DUE_AS_ON = B_BOOKS_TO               # 31-03-2026, company B's current position (day 31, C43-safe)
IGNORED_BILL_REFS = frozenset({"On Account"})
BOTH_IMPACT = ("Part 3's overdue split has two agreeing sources: each New Ref bill's BILLCREDITPERIOD on the voucher, "
               "and Bills Receivable's due-date column (BILLDUE = bill date + credit period). S1 stores the credit "
               "period on the bill row and reads due dates from the Bills snapshot.")
VOUCHER_ONLY_IMPACT = ("Bills Receivable's due column doesn't carry the credit period: S1 computes due = bill date + "
                       "BILLCREDITPERIOD from the voucher's bill allocation (Part 3 overdue split).")
REPORT_ONLY_IMPACT = ("Voucher bill allocations don't export the credit period: the overdue split reads due dates from "
                      "the Bills Receivable snapshot only (no due date for bills settled before a snapshot).")
RULE_DIFFERS_IMPACT = ("Bills Receivable's due column is present, but the rule differs from bill date + credit period "
                       "(observed due − bill date offsets by credit period: {offsets}): S1 reads due dates from the Bills "
                       "snapshot as Tally computes them and never recomputes them from BILLCREDITPERIOD.")
NEITHER_IMPACT = ("Neither a credit period nor a due date is readable: the overdue split is dropped from v1, never "
                  "approximated (Part 1 probe 23, Part 3).")


def voucher_credit_periods(raw: str) -> dict[str, dict]:
    """New Ref bill name → {tag, credit_period, bill_date} from a full voucher export (probe 5's month request)."""
    out: dict[str, dict] = {}
    for v in parse_vouchers(raw):
        tag = tag_of(v["header"].get("NARRATION", ""))
        for line in v["ledger_lines"]:
            for bill in line["bills"]:
                if bill.get("BILLTYPE") == "New Ref" and bill.get("NAME"):
                    out[bill["NAME"]] = {"tag": tag, "credit_period": bill.get("BILLCREDITPERIOD", ""),
                                         "bill_date": bill.get("BILLDATE", "")}
    return out


def credit_period_check(found: dict[str, dict], expected: dict[str, BillTerm]) -> dict:
    names = sorted(expected)
    missing = [n for n in names if not found.get(n, {}).get("credit_period")]
    wrong = {n: {"tally": found[n]["credit_period"], "setup": expected[n].credit_period} for n in names
             if n not in missing and credit_days(found[n]["credit_period"]) != expected[n].credit_days}
    values = sorted({found[n]["credit_period"] for n in names if n not in missing})
    ok = bool(names) and not missing and not wrong
    return {"compared": len(names), "missing": missing, "wrong": wrong, "values": values,
            "verdict": "CONFIRMED" if ok else "FAILED"}


def due_date_check(bills: list[dict], terms: dict[str, BillTerm]) -> dict:
    ok, as_bill_date, no_due, unknown, flagged_listed = [], [], [], [], []
    wrong: dict[str, dict] = {}
    opening: dict[str, dict] = {}
    offsets: dict[str, int] = {}
    report_offsets: dict[str, int] = {}                   # review M3: due − the report row's own BILLDATE
    bill_date_differs: dict[str, dict] = {}
    by_credit: dict[str, set[int]] = {}
    for bill in bills:
        name = bill["bill_number"]
        if name in IGNORED_BILL_REFS:
            continue
        term = terms.get(name)
        if term is None:
            unknown.append(name)
            continue
        if term.flagged:
            flagged_listed.append(name)
            continue
        if term.credit_days is None:                      # the opening bill: recorded, not judged
            opening[name] = {"due": bill["due_date"], "bill_date": bill["bill_date"]}
            continue
        due = tally_date(bill["due_date"])
        if due is not None:
            offsets[name] = (due - term.bill_date).days       # Ruling S4: the rule is measured, per bill
            by_credit.setdefault(str(term.credit_days), set()).add(offsets[name])
        row_date = tally_date(bill["bill_date"])
        if due is not None and row_date is not None:
            report_offsets[name] = (due - row_date).days
        if row_date != term.bill_date:
            bill_date_differs[name] = {"tally": bill["bill_date"], "setup": term.bill_date.isoformat()}
        if due is None:
            no_due.append(name)
        elif due == term.due:
            ok.append(name)
        elif due == term.bill_date:
            as_bill_date.append(name)
        else:
            wrong[name] = {"tally": bill["due_date"], "setup": term.due.isoformat()}
    compared = len(ok) + len(as_bill_date) + len(no_due) + len(wrong)
    if compared and len(ok) == compared:
        verdict = "CONFIRMED"
    elif wrong and not as_bill_date and not no_due:
        verdict = "OTHER_RULE"        # S4: every bill has a due date, but not bill date + credit days
    else:
        verdict = "FAILED"
    return {"compared": compared, "ok": len(ok), "as_bill_date": sorted(as_bill_date), "no_due": sorted(no_due),
            "wrong": wrong, "unknown": sorted(unknown), "flagged_listed": sorted(flagged_listed), "opening": opening,
            "offsets": offsets, "report_offsets": report_offsets, "bill_date_differs": bill_date_differs,
            "offsets_by_credit_days": {k: sorted(v) for k, v in sorted(by_credit.items())},
            "verdict": verdict}


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    confirmed = ctx.store.confirmed("voucher_month")
    if confirmed is None:
        raise ProbeBlocked("No confirmed month request: run probe 5 first (S0-D7 — 23 B reads bills with it).")
    terms = bill_terms(licence)
    start, end = month_window(*CREDIT_MONTH, licence)
    result, raw = await fetch_window(ctx, "bills_credit_period", confirmed["xml_template"], licence,
                                     start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y"))
    if result["drifted"]:
        raise ProbeBlocked(drift_message("bills_credit_period", result))
    if not result["reach_ok"]:
        raise ProbeBlocked(f"bills_credit_period: probe 5's request didn't return {start}..{end} exactly "
                           f"(missing {result['missing'][:5]}) — re-run probe 5 / `setup-b` verify.")
    expected = {n: t for n, t in terms.items() if t.credit_period and not t.flagged and start <= t.bill_date <= end}
    voucher = credit_period_check(voucher_credit_periods(raw), expected)
    report = await ctx.send("bills_receivable_due", wrap_report("Bills Receivable", B_BOOKS_FROM, DUE_AS_ON,
                                                                ctx.company_name))
    due = due_date_check(parse_bills(report), terms)
    if due["unknown"]:
        raise ProbeBlocked(f"Bills Receivable lists bill(s) {due['unknown'][:5]} the dataset never opened — company B "
                           "drifted; re-run `setup-b` verify or restore the backup.")
    if not due["compared"]:
        raise ProbeBlocked(f"Bills Receivable as-on {DUE_AS_ON} has no open bill with a credit period — nothing to "
                           "compare; re-run `setup-b` verify.")
    subs = {"voucher_credit_period": voucher["verdict"], "report_due_date": due["verdict"]}
    ctx.observe("credit_period", voucher)
    ctx.observe("due_dates", due)
    ctx.observe("sub_verdicts", subs)
    summary = (f"voucher credit periods: {voucher['compared']} New Ref bill(s) in {start:%b %Y} "
               f"({voucher['verdict']}); Bills Receivable due dates: {due['ok']}/{due['compared']} = bill date + "
               f"credit period ({due['verdict']})")
    if subs == {"voucher_credit_period": "CONFIRMED", "report_due_date": "CONFIRMED"}:
        return PartResult(Outcome.CONFIRMED, summary, spec_impact=BOTH_IMPACT)
    if due["verdict"] == "OTHER_RULE":
        offsets = "; ".join(f"{days} days → {', '.join(map(str, seen))}"
                            for days, seen in due["offsets_by_credit_days"].items())
        impact = RULE_DIFFERS_IMPACT.format(offsets=offsets)
        if voucher["verdict"] != "CONFIRMED":
            impact += " " + REPORT_ONLY_IMPACT
        return PartResult(Outcome.DIFFERENT, f"{summary}; due column present, rule differs ({offsets})",
                          spec_impact=impact)
    if "CONFIRMED" in subs.values():
        impact = VOUCHER_ONLY_IMPACT if voucher["verdict"] == "CONFIRMED" else REPORT_ONLY_IMPACT
        return PartResult(Outcome.DIFFERENT, summary, spec_impact=impact)
    return PartResult(Outcome.FAILED, summary, spec_impact=NEITHER_IMPACT)


PROBE = Probe(
    id=23,
    name="gst_due_dates",
    question="Do ledger masters expose GST classification (and bills a due date / credit period)?",
    feeds=("Part 3 tiles",),
    parts={"A": run_a, "B": run_b},
    planned_parts=("A", "B"),
    requires=(0,),
)
