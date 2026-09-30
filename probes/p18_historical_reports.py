"""Probe 18 — do TB, Bills and Stock Summary as-on a past date return correct history? (S0 spec §7 "Probe 18", A part)

Feeds decision 11, R30 and Parts 2 + 3. Two sub-verdicts (spec §5.1): TB, and bills/stock; the part outcome is the
worse of the two. The B part is `run_b` (plan part 5).

Changed 2026-09-24 (plan part 5): dates audited against C33/C43 — bills/stock as-on moved 30-09-2025 → 31-10-2025;
vouchers read for the whole FY.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from agent.tally.envelopes import wrap_report
from agent.tally.reports import parse_bills, parse_ledger_list
from agent.tally.xml_utils import read_objects
from probes.context import ProbeContext
from probes.company_b_view import B_BOOKS_FROM, ledger_balances_at, loaded_licence
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import (A_FY_FROM, A_FY_TO, TB_EXPLODE_VARS, ZERO, amount, ancestors,
                             dmy, exploded_tb_rows, is_countable, ledger_movements, master_request, opening_stock_row,
                             parse_parents, parse_vouchers, primary_group_rows, primary_lines, qty_number,
                             stock_bearing_groups, stock_rows_any_depth, tally_date, top_group, voucher_request)

TB_AS_ON = "31-10-2025"
# C43 (LESSONS §15 rule 22): this was 30-09-2025 — a day Educational Tally silently ignores, answering for the current
# period's end instead. That is exactly what the 2026-09-23 run recorded as "Bills/Stock ignore the as-on date"
# (snapshot: tests/fixtures/sync/c33_untyped_2026-09-23/). 31-10-2025 is honoured under either licence and still
# splits company A's year (its vouchers run to 01-03-2026), so bills and stock still differ from the full period.
BILLS_AS_ON = "31-10-2025"
VOUCHER_FIELDS = ["Date", "VoucherTypeName", "MasterId", "IsCancelled", "IsOptional", "IsPostDated",
                  "AllLedgerEntries", "LedgerEntries", "AllInventoryEntries"]
STOCK_FIELDS = ["Name", "Parent", "BaseUnits", "OpeningBalance"]
STOCK_EXPLODE_CANDIDATE = {"EXPLODEFLAG": "Yes"}      # probe 17's first candidate: items under their stock groups
TB_IMPACT = ("A TB as-on a past date isn't history: there is no valid parity anchor while the backfill is incomplete, "
             "so parity is suspended during the backfill (R30, decision 11).")
TB_OK_IMPACT = (
    "A Trial Balance as-on a past date IS history: Tally honours SVFROMDATE/SVTODATE on TYPE=Data reports, and every "
    "primary group reconciles to the vouchers once (a) the nominal ledger is taken from ALLLEDGERENTRIES.LIST only — "
    "adding the inventory ACCOUNTINGALLOCATIONS.LIST double-counts Sales/Purchase exactly 2x "
    "(reads.PROBE_POSTING_RULE) — and (b) the stock-bearing group's static `Opening Stock` row (₹18,55,800 for "
    "company A) is added to the ledger rollup. The as-on TB IS a valid parity anchor during the backfill; R30 needs "
    "no suspension on this ground (decision 11).")
BILLS_STOCK_IMPACT = (
    "Bills Receivable, Bills Payable and Stock Summary IGNORE the as-on date and return the books' current position: "
    "bills dated after the as-on date still appear and the totals equal the period-end anchors, and stock quantities "
    "equal opening plus the FULL period's movements. Those tiles have no month-end comparison and must be computed "
    "from vouchers, not from a dated report (Part 1 probe 18, Part 3).")
# D3 / Ruling Q3: a dated report that matches NEITHER the as-on nor the full-period position is not evidence that the
# date was ignored — the verdict says only what the evidence shows.
BILLS_STOCK_WRONG_IMPACT = (
    "A dated Bills Receivable / Payable or Stock Summary returned neither the as-on nor the full-period position: "
    "those tiles have no month-end comparison and must be computed from vouchers, not from a dated report (Part 1 "
    "probe 18, Part 3).")


def compare_group_rows(tb_rows: dict[str, Decimal | None], computed: dict[str, Decimal], groups: dict[str, str],
                       opening_stock: Decimal | None = None) -> dict:
    """Each primary group's TB row vs `computed`; the stock-bearing group reconciled against the TB's own `Opening
    Stock` row (LESSONS §15 rule 19) and reported separately."""
    stock_groups = stock_bearing_groups(groups)
    compared: dict[str, dict] = {}
    stock_side: dict[str, dict] = {}
    for group in sorted(set(tb_rows) | {g for g, v in computed.items() if v != ZERO}):
        tb_value, mine = tb_rows.get(group), computed.get(group, ZERO)
        entry = {"tb": tb_value, "computed": mine, "match": (tb_value if tb_value is not None else ZERO) == mine}
        if group in stock_groups:
            entry["opening_stock"] = opening_stock
            entry["gap"] = (tb_value if tb_value is not None else ZERO) - mine
            entry["reconciled"] = entry["match"] or (opening_stock is not None and entry["gap"] == opening_stock)
            stock_side[group] = entry
        else:
            compared[group] = entry
    mismatched = sorted(group for group, entry in compared.items() if not entry["match"])
    return {"verdict": "FAILED" if mismatched or not compared else "CONFIRMED", "groups": compared,
            "stock_bearing": stock_side, "mismatched": mismatched}


def tb_verdict(tb_rows: dict[str, Decimal | None], ledgers: dict[str, dict], groups: dict[str, str],
               vouchers: list[dict], as_on: date, opening_stock: Decimal | None = None) -> dict:
    """Every primary group's as-on TB row vs opening + the vouchers up to that date.

    The stock-bearing group is reported separately (no ledger carries stock), and reconciled against the TB's own
    `Opening Stock` row when this response carries one — the same rule probe 16 uses at the period end.
    """
    moves = ledger_movements(vouchers, up_to=as_on)
    computed: dict[str, Decimal] = {}
    for name, row in ledgers.items():
        parent = row["parent_group"]
        if parent in ("", "Primary"):
            continue
        top = top_group(parent, groups)
        computed[top] = computed.get(top, ZERO) + (row["opening_balance"] or ZERO) + moves.get(name, ZERO)
    return compare_group_rows(tb_rows, computed, groups, opening_stock)


def pending_bills(vouchers: list[dict], ledgers: dict[str, dict], groups: dict[str, str], as_on: date,
                  side: str) -> dict[str, Decimal]:
    """'party|bill' → pending on `as_on`, from the bill allocations on party lines (receivable: debtors, payable: creditors)."""
    wanted = "Sundry Debtors" if side == "receivable" else "Sundry Creditors"
    totals: dict[str, Decimal] = {}
    for voucher in vouchers:
        when = tally_date(voucher["header"].get("DATE", ""))
        if not is_countable(voucher) or when is None or when > as_on:
            continue
        for item in primary_lines(voucher):
            party = item["fields"].get("LEDGERNAME", "")
            row = ledgers.get(party)
            if row is None or wanted not in ancestors(row["parent_group"], groups):
                continue
            for bill in item["bills"]:
                value = amount(bill.get("AMOUNT"))
                if value is not None:
                    key = f"{party}|{bill.get('NAME', '')}"
                    totals[key] = totals.get(key, ZERO) + value
    sign = Decimal("-1") if side == "receivable" else Decimal("1")
    return {key: sign * value for key, value in totals.items() if value != ZERO}


def _bills_side(report_text: str, expected: dict[str, Decimal], full_period: dict[str, Decimal]) -> dict:
    """What the dated report returned, vs the as-on position, vs the FULL-period position.

    The third comparison is the evidence for the finding: live 2026-09-23 both sides matched the full period and
    neither matched the as-on date, i.e. the report ignored the date it was given.
    """
    reported = {f"{b['party_name']}|{b['bill_number']}": b["amount"] for b in parse_bills(report_text)}
    return {"reported": reported, "expected": expected, "match": reported == expected,
            "matches_full_period": reported == full_period, "full_period_bills": len(full_period),
            "weak": not reported and not expected}


def stock_verdict(rows: list[dict], items: list[dict[str, str]], vouchers: list[dict], as_on: date) -> dict:
    expected = {item["Name"]: qty_number(item["OpeningBalance"]) or ZERO for item in items}
    parent = {item["Name"]: item["Parent"] for item in items}
    for voucher in vouchers:
        when = tally_date(voucher["header"].get("DATE", ""))
        if not is_countable(voucher) or when is None or when > as_on:
            continue
        for inv in voucher["inventory"]:
            name = inv["fields"].get("STOCKITEMNAME", "")
            qty = qty_number(inv["fields"].get("ACTUALQTY", ""))
            if name and qty is not None:
                inward = inv["fields"].get("ISDEEMEDPOSITIVE", "") == "Yes"
                expected[name] = expected.get(name, ZERO) + (abs(qty) if inward else -abs(qty))
    by_group: dict[str, Decimal] = {}
    for name, qty in expected.items():
        by_group[parent.get(name, "")] = by_group.get(parent.get(name, ""), ZERO) + qty
    compared: dict[str, dict] = {}
    for row in rows:
        if row["qty"] is None:
            continue
        if row["name"] in expected:
            want = expected[row["name"]]
        elif row["name"] in by_group:
            want = by_group[row["name"]]
        else:
            continue
        compared[row["name"]] = {"reported": row["qty"], "expected": want, "value": row["value"],
                                 "match": row["qty"] == want}
    mismatches = sorted(name for name, entry in compared.items() if not entry["match"])
    return {"verdict": "CONFIRMED" if compared and not mismatches else "DIFFERENT", "compared": compared,
            "mismatches": mismatches, "item_rows": sum(1 for row in rows if row["name"] in expected),
            "direction_rule": "ISDEEMEDPOSITIVE=Yes is inward"}


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    tb_date, bills_date, period_end = dmy(TB_AS_ON), dmy(BILLS_AS_ON), dmy(A_FY_TO)
    # Exploded, like probe 16: the primary-group rows are unchanged by EXPLODEFLAG and the synthetic `Opening Stock`
    # row that explains the stock-bearing group then comes from this very response.
    tb_all_rows = exploded_tb_rows(await ctx.send(
        "tb_asof_2025-10-31", wrap_report("Trial Balance", A_FY_FROM, TB_AS_ON, company, extra_vars=TB_EXPLODE_VARS)))
    tb_rows = {name: row["closing_balance"] for name, row in primary_group_rows(tb_all_rows).items()}
    stock_row = opening_stock_row(tb_all_rows)
    opening_stock = stock_row["closing_balance"] if stock_row else None
    # C33: the as-on figures filter these in Python (ledger_movements / pending_bills / stock_verdict take `as_on`);
    # the FULL-period comparison needs the whole FY, which the old untyped `vouchers_to_…` read only got by accident.
    vouchers = parse_vouchers(await ctx.send("vouchers_fy", voucher_request("S0P18Vouchers", VOUCHER_FIELDS, company)))
    ledgers = {row["name"]: row for row in parse_ledger_list(await ctx.send(
        "ledger_list", master_request("S0P18Ledgers", "Ledger", ["Name", "Parent", "OpeningBalance"], company)))}
    groups = parse_parents(await ctx.send("group_list", master_request("S0P18Groups", "Group", ["Name", "Parent"], company)))
    tb = tb_verdict(tb_rows, ledgers, groups, vouchers, tb_date, opening_stock)
    ctx.observe("tb_opening_stock_row", {"present": stock_row is not None, "closing_balance": opening_stock,
                                         "tb_rows": len(tb_all_rows)})

    receivable = await ctx.send("bills_receivable_asof_2025-10-31",
                                wrap_report("Bills Receivable", BILLS_AS_ON, BILLS_AS_ON, company))
    payable = await ctx.send("bills_payable_asof_2025-10-31",
                             wrap_report("Bills Payable", BILLS_AS_ON, BILLS_AS_ON, company))
    bills = {side: _bills_side(text, pending_bills(vouchers, ledgers, groups, bills_date, side),
                               pending_bills(vouchers, ledgers, groups, period_end, side))
             for side, text in (("receivable", receivable), ("payable", payable))}
    stock_text = await ctx.send("stock_summary_asof_2025-10-31",
                                wrap_report("Stock Summary", A_FY_FROM, BILLS_AS_ON, company,
                                            extra_vars=STOCK_EXPLODE_CANDIDATE))
    items = read_objects(await ctx.send("stock_item_openings",
                                        master_request("S0P18Stock", "StockItem", STOCK_FIELDS, company)),
                         "STOCKITEM", STOCK_FIELDS)
    stock_rows = stock_rows_any_depth(stock_text)
    stock = stock_verdict(stock_rows, items, vouchers, bills_date)
    stock["matches_full_period"] = stock_verdict(stock_rows, items, vouchers, period_end)["verdict"] == "CONFIRMED"
    bills_ok = bills["receivable"]["match"] and bills["payable"]["match"]
    # Two INDEPENDENT sub-verdicts (spec §5.1). The TB is a pass/fail question — history or not. Bills and Stock
    # Summary ignoring their as-on date is a real, reproducible Tally behaviour, so it is a DIFFERENT with evidence,
    # never a FAILED and never a silent pass.
    sub = {"tb": tb["verdict"],
           "bills_stock": "CONFIRMED" if bills_ok and stock["verdict"] == "CONFIRMED" else "DIFFERENT"}
    ctx.observe("tb", tb)
    ctx.observe("bills", bills)
    ctx.observe("stock", stock)
    ctx.observe("sub_verdicts", sub)

    wrong = [side for side in ("receivable", "payable") if not bills[side]["match"]]
    stock_wrong = stock["verdict"] != "CONFIRMED"
    what = [f"bills {side}" for side in wrong] + ([f"stock ({', '.join(stock['mismatches']) or 'nothing comparable'})"]
                                                  if stock_wrong else [])
    full = [name for name, ok in [*((f"bills {side}", bills[side]["matches_full_period"]) for side in wrong),
                                  ("stock", stock_wrong and stock["matches_full_period"])] if ok]
    # "Ignored the date" only where the report equals the FULL period; anything else is just wrong (D3, Ruling Q3).
    bills_stock_impacts = (([BILLS_STOCK_IMPACT] if full else [])
                           + ([BILLS_STOCK_WRONG_IMPACT] if len(full) < len(what) else []))

    if sub["tb"] == "FAILED":
        problem = f"TB as-on {TB_AS_ON} ≠ opening + lines for {', '.join(tb['mismatched']) or 'every group'}"
        return PartResult(Outcome.FAILED, problem, spec_impact=" ".join([TB_IMPACT, *bills_stock_impacts]))

    if sub["bills_stock"] == "DIFFERENT":
        evidence = (f" — {', '.join(full)} match the FULL period ({A_FY_TO}) instead, i.e. the report ignored the date "
                    f"it was given") if full else ""
        return PartResult(Outcome.DIFFERENT,
                          f"TB as-on {TB_AS_ON} is correct history; as-on {BILLS_AS_ON}: {'; '.join(what)}{evidence}",
                          spec_impact=" ".join([TB_OK_IMPACT, *bills_stock_impacts]))

    vacuous_note = " (Bills Receivable check is vacuous: nothing pending on either side)" \
        if bills["receivable"]["weak"] else ""
    return PartResult(Outcome.CONFIRMED, f"TB as-on {TB_AS_ON}, Bills and Stock Summary as-on {BILLS_AS_ON} equal what the "
                                         f"vouchers give (stock values recorded, not compared){vacuous_note}",
                      spec_impact=TB_OK_IMPACT)


B_TB_AS_ON = "31-03-2023"            # FY 2022-23's end — a closed year three FYs back; day 31 (C43)
B_TB_OK_IMPACT = ("A TB as-on a closed year's end is history on company B too — every primary group equals the "
                  "dataset, the stock-bearing one via its own `Opening Stock` row: parity has a valid anchor during "
                  "the backfill, and R30 needs no suspension (decision 11).")
# I2: when every non-stock primary group reconciles and only the stock-bearing group fails to reconcile via its
# `Opening Stock` row, that is NOT the same claim as TB_IMPACT ("a TB as-on isn't history, parity is suspended") —
# the non-stock groups ARE history. This is its own, narrower finding.
B_STOCK_UNRECONCILED_IMPACT = (
    "The stock-bearing group doesn't reconcile via its `Opening Stock` row on this TB as-on (a valuation or row-shape "
    "question, LESSONS §15 rule 19) — every non-stock primary group is history and R30 needs no suspension for them "
    "(decision 11); stock parity on this report is revisited.")


def dataset_rollup(ledger_parents: dict[str, str], groups: dict[str, str],
                   balances: dict[str, Decimal]) -> tuple[dict[str, Decimal], list[str]]:
    """Per-primary-group sums of the dataset's balances, keyed by Tally's own ledger → group tree; ledgers the dataset
    doesn't know are returned, not summed (drift)."""
    computed: dict[str, Decimal] = {}
    unknown: list[str] = []
    for name, parent in ledger_parents.items():
        if parent in ("", "Primary"):
            continue
        if name not in balances:
            unknown.append(name)
            continue
        top = top_group(parent, groups)
        computed[top] = computed.get(top, ZERO) + balances[name]
    return computed, sorted(unknown)


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    company = ctx.company_name
    tb_all = exploded_tb_rows(await ctx.send("tb_asof_2023-03-31", wrap_report(
        "Trial Balance", B_BOOKS_FROM, B_TB_AS_ON, company, extra_vars=TB_EXPLODE_VARS)))
    tb_rows = {name: row["closing_balance"] for name, row in primary_group_rows(tb_all).items()}
    stock_row = opening_stock_row(tb_all)
    opening_stock = stock_row["closing_balance"] if stock_row else None
    ledger_parents = parse_parents(await ctx.send(
        "ledger_list", master_request("S0P18BLedgers", "Ledger", ["Name", "Parent"], company)), "LEDGER")
    groups = parse_parents(await ctx.send("group_list",
                                          master_request("S0P18BGroups", "Group", ["Name", "Parent"], company)))
    balances = ledger_balances_at(licence, dmy(B_TB_AS_ON))
    computed, unknown = dataset_rollup(ledger_parents, groups, balances)
    missing = sorted(set(balances) - set(ledger_parents))
    if unknown or missing:
        raise ProbeBlocked(f"Company B's ledgers differ from the dataset (unknown {unknown[:5]}, missing "
                           f"{missing[:5]}) — re-run `setup-b` verify or restore the backup before trusting this probe.")
    tb = compare_group_rows(tb_rows, computed, groups, opening_stock)
    unreconciled_stock = sorted(g for g, entry in tb["stock_bearing"].items() if not entry["reconciled"])
    ctx.observe("tb", tb)
    ctx.observe("tb_opening_stock_row", {"present": stock_row is not None, "closing_balance": opening_stock,
                                         "tb_rows": len(tb_all)})
    bad = tb["mismatched"] + unreconciled_stock
    ctx.observe("sub_verdicts", {"tb": "FAILED" if bad or not tb["groups"] else "CONFIRMED"})
    if bad or not tb["groups"]:
        # I2: a stock-only reconciliation miss (no non-stock group mismatched) gets its own, narrower impact — not
        # TB_IMPACT's "a TB as-on isn't history, parity is suspended", which only applies when a real group mismatch
        # makes the anchor itself unusable.
        stock_only = not tb["mismatched"] and bool(unreconciled_stock)
        impact = B_STOCK_UNRECONCILED_IMPACT if stock_only else TB_IMPACT
        return PartResult(Outcome.FAILED, f"TB as-on {B_TB_AS_ON} ≠ the dataset for {', '.join(bad) or 'every group'}",
                          spec_impact=impact)
    return PartResult(Outcome.CONFIRMED, f"TB as-on {B_TB_AS_ON} equals the dataset for all {len(tb['groups'])} "
                                         "primary groups, and the stock-bearing group reconciles via its Opening "
                                         "Stock row",
                      spec_impact=B_TB_OK_IMPACT)


PROBE = Probe(
    id=18,
    name="historical_reports",
    question="Do TB, Bills Receivable/Payable and Stock Summary as-on a past date return correct history?",
    feeds=("decision 11", "R30", "Parts 2 + 3"),
    parts={"A": run_a, "B": run_b},
    planned_parts=("A", "B"),
    requires=(0, 1),
    educational_sensitive=True,
)
