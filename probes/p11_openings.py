"""Probe 11: ledger opening balances, the opening bill, and stock openings (S0 spec §7 "Probe 11", B). Feeds R5.

All three are checked against what setup-b wrote (the dataset, via company_b_view), never against another Tally read.
Ledger OpeningBalance is judged against the books-start opening; if it instead equals the current FY's opening, that
is recorded as DIFFERENT and the scope question is left to probe 16 B (S0-D7). The opening-bill candidate is the ledger
master's own BillAllocations; the fallback is Bills Receivable as-on the books start (01-04-2022, day 1, C43).
C46 (live 2026-09-24): StockItem opening fields are the current period's opening. The stock half judges that scope the
way the ledger half judges OpeningBalance's, and every half is named in the summary (the 2026-09-24 run's FAILED stock
half hid a DIFFERENT ledger half).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

from agent.tally.envelopes import formula_string, wrap_report
from agent.tally.reports import parse_bills, parse_ledger_list
from agent.tally.xml_utils import read_objects, sanitize_xml
from probes.company_b_view import (B_BOOKS_FROM, B_CURRENT_PERIOD, item_specs, ledger_openings_at, ledger_specs,
                                      loaded_licence, qty_unit, stock_opening_at)
from probes.context import ProbeContext
from probes.core import PartResult, Probe, ProbeBlocked, judge_halves
from probes.reads import ZERO, amount, master_request, qty_number

LEDGER_FIELDS = ["Name", "Parent", "OpeningBalance"]
BILL_FIELDS = ["Name", "OpeningBalance", "BillAllocations"]       # candidate: the ledger's own opening bills
STOCK_FIELDS = ["Name", "Parent", "BaseUnits", "OpeningBalance", "OpeningRate", "OpeningValue"]
OPENING_BILLS_AS_ON = B_BOOKS_FROM

CONFIRMED_IMPACT = ("S1's opening anchors (R5) come straight from the masters: Ledger OpeningBalance, the ledger's own "
                    "opening BillAllocations, and StockItem OpeningBalance / OpeningRate / OpeningValue (debit negative).")
LEDGER_FY_IMPACT = ("Ledger OpeningBalance reads as the current FY's opening, not the books-start one: R5's books-start "
                    "anchor is computed or read with the period in the first FY — probe 16 B settles the scope.")
LEDGER_FAILED_IMPACT = ("Ledger OpeningBalance doesn't export the openings setup wrote: R5's opening anchor comes from an "
                        "as-on TB at the books start (probe 18) instead of the ledger master.")
BILL_REPORT_IMPACT = ("Opening bills aren't on the ledger master's export: S1 reads them from Bills Receivable / Payable "
                      "as-on the books start (R5).")
BILL_FAILED_IMPACT = ("The opening bill can't be read over XML: S1 treats a party's opening balance as on-account (no "
                      "bill-wise ageing before the books start) (R5).")
STOCK_FAILED_IMPACT = ("Stock openings don't export setup's quantity / rate / value: stock tiles start from the first "
                       "Stock Summary snapshot, not an opening anchor (R5).")
STOCK_SIGN_IMPACT = ("StockItem OpeningValue exports positive for a debit: S1 negates it on ingest to keep debit "
                     "negative (Part 1 §6).")
STOCK_CURRENT_IMPACT = ("C46: StockItem OpeningBalance / OpeningRate / OpeningValue are the CURRENT period's opening, "
                        "not the books-start one — S1 may use them only as the current FY's stock opening; a "
                        "books-start stock opening comes from a Stock Summary as-on the books start (probe 18's route), "
                        "never the StockItem master (R5).")
STOCK_UNDECIDED_IMPACT = ("No stock item moved before the current period, so books-start and current-period openings "
                          "can't be told apart here: the StockItem opening's scope stays unmeasured (R5).")


def ledger_opening_check(rows: dict[str, dict], specs: dict, current_fy: dict[str, Decimal]) -> dict:
    mismatched: dict[str, dict] = {}
    as_current_fy: list[str] = []
    for name, spec in specs.items():
        got = rows[name]["opening_balance"]
        want = spec.opening if spec.opening is not None else ZERO
        if (got if got is not None else ZERO) == want:
            continue
        mismatched[name] = {"tally": got, "setup": want, "current_fy_opening": current_fy.get(name)}
        if (got if got is not None else ZERO) == current_fy.get(name):
            as_current_fy.append(name)
    return {"compared": len(specs), "mismatched": mismatched, "as_current_fy": sorted(as_current_fy)}


def ledger_bills(raw_xml: str, party: str) -> list[dict]:
    """Opening bills nested under the party's LEDGER element (BILLALLOCATIONS.LIST: NAME, BILLDATE, OPENINGBALANCE)."""
    found = []
    for ledger in ET.fromstring(sanitize_xml(raw_xml)).iter("LEDGER"):
        if (ledger.findtext("NAME") or ledger.get("NAME", "")).strip() != party:
            continue
        for bill in ledger.iter("BILLALLOCATIONS.LIST"):
            name = (bill.findtext("NAME") or "").strip()
            if name:
                found.append({"name": name, "amount": amount(bill.findtext("OPENINGBALANCE") or bill.findtext("AMOUNT")),
                              "date": (bill.findtext("BILLDATE") or "").strip()})
    return found


def bill_check(found: list[dict], name: str, want: Decimal) -> dict:
    """By magnitude: the sign is recorded, not judged (Tally files a bill by its own sign — C34)."""
    hit = next((b for b in found if b["name"] == name), None)
    value = hit["amount"] if hit else None
    return {"found": hit is not None, "amount": value, "date": hit["date"] if hit else "",
            "magnitude_match": value is not None and abs(value) == abs(want),
            "sign": None if value is None else ("negative" if value < 0 else "positive"), "bills_seen": len(found)}


def stock_check(rows: list[dict[str, str]], specs: dict, units: dict[str, str],
                current_qty: dict[str, Decimal]) -> dict[str, dict]:
    by_name = {row["Name"]: row for row in rows}
    out: dict[str, dict] = {}
    for name, spec in specs.items():
        row = by_name.get(name)
        if row is None:
            out[name] = {"present": False}
            continue
        want_qty, want_rate = spec.opening_qty or ZERO, spec.opening_rate or ZERO
        want_value = -(want_qty * want_rate)
        qty = qty_number(row["OpeningBalance"]) or ZERO
        rate = amount(row["OpeningRate"].split("/")[0]) if row["OpeningRate"] else None
        value = amount(row["OpeningValue"])
        out[name] = {"present": True, "qty_text": row["OpeningBalance"], "rate_text": row["OpeningRate"],
                     "value": value, "base_units": row["BaseUnits"], "qty_unit": units[name],
                     "books_qty": want_qty, "current_period_qty": current_qty[name],
                     "qty_ok": qty == want_qty, "qty_current_ok": qty == current_qty[name],
                     "rate_ok": (rate or ZERO) == want_rate, "value_ok": (value or ZERO) == want_value,
                     "value_sign_flipped": want_value != ZERO and value == -want_value}
    return out


def stock_scope(stock: dict[str, dict]) -> dict:
    """Which opening StockItem exports, judged on the items whose books-start and current-period openings differ."""
    telling = sorted(n for n, e in stock.items() if e["books_qty"] != e["current_period_qty"])
    as_books = [n for n in telling if stock[n]["qty_ok"]]
    as_current = [n for n in telling if stock[n]["qty_current_ok"]]
    neither = sorted(n for n, e in stock.items() if not e["qty_ok"] and not e["qty_current_ok"])
    if neither:
        scope = "neither"
    elif not telling:
        scope = "undecided"
    elif len(as_books) == len(telling):
        scope = "books"
    elif len(as_current) == len(telling):
        scope = "current_period"
    else:
        scope = "mixed"
    return {"scope": scope, "telling": len(telling), "as_books": as_books, "as_current_period": as_current,
            "neither": neither}


def _ledger_half(ledgers: dict) -> tuple[str, str, str]:
    if not ledgers["mismatched"]:
        return "CONFIRMED", f"{ledgers['compared']} openings equal setup's books-start openings", ""
    if set(ledgers["as_current_fy"]) == set(ledgers["mismatched"]):
        return ("DIFFERENT", f"OpeningBalance equals the current FY's opening for {len(ledgers['mismatched'])} "
                             "ledger(s)", LEDGER_FY_IMPACT)
    return "FAILED", f"OpeningBalance ≠ setup for {', '.join(sorted(ledgers['mismatched'])[:5])}", LEDGER_FAILED_IMPACT


def _bill_half(opening_bill: dict) -> tuple[str, str, str]:
    bill = opening_bill["bill"]
    if opening_bill["candidate"]["magnitude_match"]:
        return "CONFIRMED", f"{bill!r} (₹{abs(opening_bill['expected'])}) on the ledger master", ""
    if opening_bill.get("report", {}).get("magnitude_match"):
        return "DIFFERENT", f"{bill!r} only in Bills Receivable as-on {OPENING_BILLS_AS_ON}", BILL_REPORT_IMPACT
    return "FAILED", f"{bill!r} found neither on the ledger nor in Bills Receivable", BILL_FAILED_IMPACT


def _stock_half(stock: dict[str, dict], scope: dict) -> tuple[str, str, str]:
    if scope["scope"] == "books":
        bad = sorted(n for n, e in stock.items() if not (e["rate_ok"] and (e["value_ok"] or e["value_sign_flipped"])))
        flipped = sorted(n for n, e in stock.items() if e["value_sign_flipped"] and not e["value_ok"])
        if bad:
            return "FAILED", f"openings ≠ setup's rate/value for {', '.join(bad)}", STOCK_FAILED_IMPACT
        if flipped:
            return "DIFFERENT", f"OpeningValue exported positive for {', '.join(flipped)}", STOCK_SIGN_IMPACT
        return "CONFIRMED", f"{len(stock)} openings equal what setup-b wrote", ""
    if scope["scope"] == "current_period":
        return ("DIFFERENT", f"openings are the current period's (as at {B_CURRENT_PERIOD[0]}) for "
                             f"{len(scope['as_current_period'])} item(s), not the books-start ones (C46); "
                             "rate/value recorded, not judged",
                STOCK_CURRENT_IMPACT)
    if scope["scope"] == "undecided":
        return "DIFFERENT", "books-start and current-period openings are equal on every item", STOCK_UNDECIDED_IMPACT
    names = scope["neither"] or sorted(scope["as_books"] + scope["as_current_period"])
    return ("FAILED", f"openings ≠ setup for {', '.join(names)}" if scope["neither"]
            else f"openings mix books-start and current-period values ({', '.join(names)})", STOCK_FAILED_IMPACT)


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    company = ctx.company_name
    specs = ledger_specs(licence)
    rows = {row["name"]: row for row in parse_ledger_list(await ctx.send(
        "ledger_openings", master_request("S0P11Ledgers", "Ledger", LEDGER_FIELDS, company)))}
    missing = sorted(set(specs) - set(rows))
    if missing:
        raise ProbeBlocked(f"Company B is missing ledger(s) {missing[:5]} the dataset has — re-run `setup-b` verify or "
                           "restore the backup before trusting this probe.")
    ledgers = ledger_opening_check(rows, specs, ledger_openings_at(licence, B_CURRENT_PERIOD[0]))
    ctx.observe("ledgers", ledgers)

    party, bill, want = next((s.name, s.opening_bill, s.opening) for s in specs.values() if s.opening_bill)
    candidate = await ctx.send("opening_bills", master_request(
        "S0P11PartyBills", "Ledger", BILL_FIELDS, company, filters=[("S0P11IsParty", f"$Name = {formula_string(party)}")]))
    opening_bill = {"party": party, "bill": bill, "expected": want,
                    "candidate": bill_check(ledger_bills(candidate, party), bill, want)}
    if not opening_bill["candidate"]["magnitude_match"]:
        report = await ctx.send("opening_bills_report", wrap_report("Bills Receivable", OPENING_BILLS_AS_ON,
                                                                    OPENING_BILLS_AS_ON, company))
        found = [{"name": b["bill_number"], "amount": b["amount"], "date": b["bill_date"]}
                 for b in parse_bills(report) if b["party_name"] == party]
        opening_bill["report"] = bill_check(found, bill, want)
    ctx.observe("opening_bill", opening_bill)

    items = read_objects(await ctx.send("stock_openings", master_request("S0P11Stock", "StockItem", STOCK_FIELDS,
                                                                         company)), "STOCKITEM", STOCK_FIELDS)
    item_names = item_specs(licence)
    current = {n: stock_opening_at(licence, n, B_CURRENT_PERIOD[0]) for n in item_names}
    stock = stock_check(items, item_names, {n: qty_unit(licence, n) for n in item_names}, current)
    ctx.observe("stock", stock)
    absent = sorted(n for n, e in stock.items() if not e["present"])
    if absent:
        raise ProbeBlocked(f"Company B is missing stock item(s) {absent} — re-run `setup-b` verify or restore the backup.")
    scope = stock_scope(stock)
    ctx.observe("stock_scope", scope)

    halves = {"ledgers": _ledger_half(ledgers), "opening_bill": _bill_half(opening_bill),
              "stock": _stock_half(stock, scope)}
    ctx.observe("sub_verdicts", {k: v[0] for k, v in halves.items()})
    outcome, summary, impacts = judge_halves(halves)
    return PartResult(outcome, summary, spec_impact=" ".join(impacts) or CONFIRMED_IMPACT)


PROBE = Probe(
    id=11,
    name="openings",
    question="Do ledger openings, the opening bill and stock opening qty / rate / value export as setup wrote them?",
    feeds=("R5",),
    parts={"B": run_b},
    requires=(0,),
)
