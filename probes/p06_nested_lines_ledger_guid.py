"""Probe 6 — nested lines, the double-entry and sign rules, and a ledger GUID on lines (S0 spec §7 "Probe 6").

Feeds the S1 ingest rules, R5 and R9. List methods are fetched whole (AllLedgerEntries, LedgerEntries,
AllInventoryEntries), the form that returns nested lists (tests/fixtures/live_tally_debug.md).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from agent.tally.xml_utils import get_text, read_objects, sanitize_xml
from probes.context import POPUP_HINT, ProbeContext
from probes.core import Outcome, PartResult, Probe
from probes.reads import POSTING_RULES, ZERO, master_request, parse_vouchers, postings, voucher_request

VOUCHER_FIELDS = ["Date", "VoucherTypeName", "VoucherNumber", "MasterId", "PartyLedgerName", "Narration",
                  "AllLedgerEntries", "LedgerEntries", "AllInventoryEntries"]
LINE_FIELDS = ("LEDGERNAME", "AMOUNT", "ISDEEMEDPOSITIVE")
BILL_FIELDS = ("NAME", "BILLTYPE", "AMOUNT", "BILLCREDITPERIOD")
INVENTORY_FIELDS = ("STOCKITEMNAME", "ACTUALQTY", "BILLEDQTY", "RATE", "AMOUNT")
BATCH_FIELDS = ("GODOWNNAME", "BATCHNAME", "AMOUNT")
# Candidate ways to get a ledger GUID onto a voucher line (none verified; the probe records which work).
LINE_GUID_METHOD_CANDIDATES = ["AllLedgerEntries.LedgerGUID", "AllLedgerEntries.GUID", "AllLedgerEntries.LedgerMasterId"]
LINE_GUID_TAG_CANDIDATES = ("LEDGERGUID", "GUID", "LEDGERMASTERID", "MASTERID")
LINE_GUID_TDL_FIELD = "S0LedgerGuid"
EXPECTED_PARTY_PAYMENTS = 4                     # seed: PMT001/002/005/009 carry Agst Ref (docs/seed-data-setup.md)
EXPECTED_EXPENSE_PAYMENTS = 12


def _presence(vouchers: list[dict]) -> dict[str, dict[str, int]]:
    lines = [item for v in vouchers for item in v["ledger_lines"]]
    bills = [bill for item in lines for bill in item["bills"]]
    inventory = [inv for v in vouchers for inv in v["inventory"]]
    batches = [batch for inv in inventory for batch in inv["batches"]]
    return {
        "line": {f: sum(1 for item in lines if f in item["fields"]) for f in LINE_FIELDS} | {"total": len(lines)},
        "bill": {f: sum(1 for bill in bills if f in bill) for f in BILL_FIELDS} | {"total": len(bills)},
        "inventory": {f: sum(1 for inv in inventory if f in inv["fields"]) for f in INVENTORY_FIELDS}
                     | {"total": len(inventory)},
        "batch": {f: sum(1 for batch in batches if f in batch) for f in BATCH_FIELDS} | {"total": len(batches)},
    }


def _bill_types(voucher: dict) -> set[str]:
    return {bill.get("BILLTYPE", "") for item in voucher["ledger_lines"] for bill in item["bills"]}


def _structure(vouchers: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = {}
    for voucher in vouchers:
        by_type.setdefault(voucher["header"].get("VOUCHERTYPENAME", ""), []).append(voucher)
    payments = by_type.get("Payment", [])
    party_payments = [v for v in payments if "Agst Ref" in _bill_types(v)]
    expense_payments = [v for v in payments if not _bill_types(v)]
    return {
        "counts": {kind: len(items) for kind, items in sorted(by_type.items())},
        "sales_new_ref": all("New Ref" in _bill_types(v) for v in by_type.get("Sales", [])),
        "purchase_new_ref": all("New Ref" in _bill_types(v) for v in by_type.get("Purchase", [])),
        "receipts_agst_ref": all("Agst Ref" in _bill_types(v) for v in by_type.get("Receipt", [])),
        "party_payments_agst_ref": len(party_payments) == EXPECTED_PARTY_PAYMENTS,
        "expense_payments_no_bills": len(expense_payments) == EXPECTED_EXPENSE_PAYMENTS,
        "sales_have_inventory": all(v["inventory"] for v in by_type.get("Sales", [])),
    }


def _balance(vouchers: list[dict]) -> dict:
    """Which posting rule makes every voucher balance. This probe is the one that formally settles the rule, so it
    still measures all four candidates and nothing here is pre-judged.

    Live evidence recorded elsewhere on 2026-09-23 already points at 'all_only': on company A the 24 inventory
    vouchers (16 Sales + 8 Purchase) carry the nominal ledger in BOTH ALLLEDGERENTRIES.LIST and each inventory
    entry's ACCOUNTINGALLOCATIONS.LIST, so 'default' counts Sales/Purchase at exactly 2x and leaves 24 of 50
    vouchers unbalanced, while 'all_only' leaves 0 and reproduces Tally's own as-on TB rows to the paisa
    (reads.PROBE_POSTING_RULE). This probe's own run on a licensed Tally is what confirms it across voucher types.
    """
    per_rule = {}
    for rule in POSTING_RULES:
        balanced = 0
        for voucher in vouchers:
            values = [value for _, value in postings(voucher, rule)]
            if values and None not in values and sum(values, ZERO) == ZERO:
                balanced += 1
        per_rule[rule] = balanced
    chosen = next((rule for rule in POSTING_RULES if vouchers and per_rule[rule] == len(vouchers)), None)
    sign_violations = [
        {"voucher": v["header"].get("MASTERID", ""), "ledger": item["fields"].get("LEDGERNAME", "")}
        for v in vouchers for item in v["ledger_lines"] + [a for inv in v["inventory"] for a in inv["accounting"]]
        if item["amount"] not in (None, ZERO)
        and (item["fields"].get("ISDEEMEDPOSITIVE") == "Yes") != (item["amount"] < ZERO)
    ]
    return {"balanced_per_rule": per_rule, "rule": chosen, "sign_violations": sign_violations}


def _line_guid_fetch_request(company: str) -> str:
    return voucher_request("S0P06LineFetch", ["Date", "MasterId", "AllLedgerEntries", *LINE_GUID_METHOD_CANDIDATES],
                           company, filters=[("S0P06Payments", '$VoucherTypeName = "Payment"')])


def _line_guid_tdl_request(company: str) -> str:
    walk = (f"\n<WALK>AllLedgerEntries</WALK>\n"
            f"<COMPUTE>{LINE_GUID_TDL_FIELD} : $GUID:Ledger:$LedgerName</COMPUTE>")
    return voucher_request("S0P06LineTdl", ["LedgerName", "Amount"], company, extra_collection_xml=walk)


def _fetched_line_guids(text: str) -> dict[str, int]:
    """How many ALLLEDGERENTRIES.LIST elements carry each candidate tag with a value."""
    root = ET.fromstring(sanitize_xml(text))
    found = {tag: 0 for tag in LINE_GUID_TAG_CANDIDATES}
    for item in root.iter("ALLLEDGERENTRIES.LIST"):
        for tag in LINE_GUID_TAG_CANDIDATES:
            if get_text(item, tag):
                found[tag] += 1
    return found


def _tdl_pairs(text: str) -> list[tuple[str, str]]:
    root = ET.fromstring(sanitize_xml(text))
    return [(get_text(el, "LEDGERNAME"), get_text(el, LINE_GUID_TDL_FIELD.upper()))
            for el in root.iter() if get_text(el, LINE_GUID_TDL_FIELD.upper())]


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    vouchers = parse_vouchers(await ctx.send("vouchers_nested", voucher_request("S0P06Vouchers", VOUCHER_FIELDS, company)))
    ledger_guids = {row["Name"]: row["GUID"] for row in read_objects(await ctx.send(
        "ledger_guids", master_request("S0P06LedgerGuids", "Ledger", ["Name", "GUID"], company)), "LEDGER", ["Name", "GUID"])}
    presence, structure, balance = _presence(vouchers), _structure(vouchers), _balance(vouchers)
    fetch_text, fetch_error = await ctx.try_send("line_guid_fetch", _line_guid_fetch_request(company))
    tdl_text, tdl_error = await ctx.try_send("line_guid_tdl", _line_guid_tdl_request(company))
    fetched = _fetched_line_guids(fetch_text) if fetch_text is not None else {}
    pairs = _tdl_pairs(tdl_text) if tdl_text is not None else []
    tdl_ok = bool(pairs) and all(ledger_guids.get(name) == guid for name, guid in pairs)
    fetch_ok = any(count > 0 for count in fetched.values())
    route = "fetch" if fetch_ok else "tdl" if tdl_ok else None
    ctx.observe("voucher_count", len(vouchers))
    ctx.observe("presence", presence)
    ctx.observe("structure", structure)
    ctx.observe("balance", balance)
    ctx.observe("balancing_rule", balance["rule"])
    ctx.observe("line_guid", {"route": route, "fetch_tags": fetched, "fetch_error": fetch_error,
                              "tdl_pairs": len(pairs), "tdl_matches_ledgers": tdl_ok, "tdl_error": tdl_error})

    if route is None:   # M8: after the observations above, so a BLOCKED part still carries the evidence it collected
        for error in (fetch_error, tdl_error):
            if error is not None and error["kind"] in ("timeout", "refused"):
                return PartResult(Outcome.BLOCKED, f"{error['kind']} on a line-GUID candidate: {error['message']}. "
                                                   f"{POPUP_HINT}")

    missing = [name for name, ok in (("ledger lines", presence["line"]["total"] > 0),
                                     ("bill allocations", presence["bill"]["total"] > 0),
                                     ("Sales inventory lines", structure["sales_have_inventory"])) if not ok]
    if missing:
        return PartResult(Outcome.FAILED, f"Nested list(s) missing: {', '.join(missing)}",
                          spec_impact="A nested list S1 ingests doesn't export: S1 schema change before S1 "
                                      "(Part 1 §5 'Cloud' ingest rules, R5).")
    if balance["rule"] is None or balance["sign_violations"]:
        return PartResult(Outcome.FAILED, f"Lines don't balance under any posting rule ({balance['balanced_per_rule']}) "
                                          f"or a debit isn't negative ({len(balance['sign_violations'])} line(s))",
                          spec_impact="Rung 0 (Σ lines = 0.00) and the debit-negative rule can't be applied to the "
                                      "export as-is: the S1 ingest rules are revisited (Part 1 §6).")
    differences, impacts = [], []
    wrong_structure = [key for key, ok in structure.items() if key != "counts" and ok is False]
    if wrong_structure:
        differences.append(f"bill/inventory structure differs from the seed: {', '.join(wrong_structure)}")
        impacts.append("Bill allocations don't export the way S1's ingest assumes (New Ref / Agst Ref per voucher "
                       "kind); the recorded structure goes into the S1 spec.")
    if route is None:
        differences.append("no route gives a ledger GUID on voucher lines")
        impacts.append("Ledger GUID isn't available on lines: server-side name resolution stays (already designed, R9).")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences), spec_impact=" ".join(impacts))
    how = "a plain fetch field" if route == "fetch" else "inline TDL ($GUID:Ledger:$LedgerName)"
    return PartResult(Outcome.CONFIRMED, f"Nested lines, bills and inventory export; every voucher balances under the "
                                         f"'{balance['rule']}' rule with debit negative; ledger GUID on lines via {how}",
                      spec_impact=f"S1 ingest: posting rule '{balance['rule']}'; ledger GUID on lines comes from {how}.")


PROBE = Probe(
    id=6,
    name="nested_lines_ledger_guid",
    question="Do nested lines / bills / inventory export, balance with debit negative, and can a line carry the ledger GUID?",
    feeds=("S1 ingest rules", "R5", "R9"),
    parts={"A": run_a},
    requires=(0,),
)
