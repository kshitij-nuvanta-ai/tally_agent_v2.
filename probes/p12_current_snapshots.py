"""Probe 12 — current report snapshots via SVCurrentCompany (S0 spec §7 "Probe 12"). Feeds R5.

These six fixtures become the S1 snapshot-ingest fixtures.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

from tally_bridge.amounts import AmountParseError
from tally_bridge.envelopes import wrap_report
from tally_bridge.sync_reports import parse_bills, parse_stock_summary, parse_trial_balance
from tally_bridge.xml_utils import detect_error
from probes.anchors import ANCHOR_DATE, SEED_PAYABLE, SEED_RECEIVABLE
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe
from probes.reads import A_FY_FROM, A_FY_TO, ZERO

SNAPSHOT_DATE = ANCHOR_DATE     # 31-03-2026: company A's period end (Tally's Educational 'today' is 1-Mar-2026)
REPORTS: list[tuple[str, str, str, str]] = [
    ("tb_today", "Trial Balance", A_FY_FROM, SNAPSHOT_DATE),
    ("bs_today", "Balance Sheet", SNAPSHOT_DATE, SNAPSHOT_DATE),
    ("pl_fy2025", "Profit and Loss", A_FY_FROM, A_FY_TO),
    ("stock_summary_today", "Stock Summary", A_FY_FROM, SNAPSHOT_DATE),
    ("bills_receivable_today", "Bills Receivable", SNAPSHOT_DATE, SNAPSHOT_DATE),
    ("bills_payable_today", "Bills Payable", SNAPSHOT_DATE, SNAPSHOT_DATE),
]
DATA_TAG_PREFIXES = ("DSP", "BS", "PL", "BILL")


def _data_elements(text: str) -> int:
    return sum(1 for el in ET.fromstring(text).iter()
               if el.tag.startswith(DATA_TAG_PREFIXES) and (el.text or "").strip())


def _bills_total(text: str) -> Decimal | None:
    amounts = [bill["amount"] for bill in parse_bills(text)]
    return None if not amounts or any(a is None for a in amounts) else sum(amounts, ZERO)


async def run_a(ctx: ProbeContext) -> PartResult:
    texts: dict[str, str] = {}
    shapes: dict[str, dict] = {}
    unusable: list[str] = []
    for step, report, from_date, to_date in REPORTS:
        text = await ctx.send(step, wrap_report(report, from_date, to_date, ctx.company_name))
        texts[step] = text
        error = detect_error(text)
        try:
            count = _data_elements(text)
        except ET.ParseError:
            count, error = 0, error or "not XML"
        shapes[step] = {"report": report, "bytes": ctx.last_response.response_bytes, "data_elements": count,
                        "tally_error": error}
        if error or count == 0:
            unusable.append(report)
    ctx.observe("reports", shapes)
    if unusable:
        return PartResult(Outcome.FAILED, f"Unusable report response(s): {', '.join(unusable)}",
                          spec_impact=f"S1 snapshot ingest can't take {', '.join(unusable)} from TYPE=Data as it is; the "
                                      "request or parser is redone before S1 (R5).")
    try:
        tb_rows = parse_trial_balance(texts["tb_today"])
        stock_rows = parse_stock_summary(texts["stock_summary_today"])
    except AmountParseError as exc:
        return PartResult(Outcome.FAILED, f"Report amounts aren't plain numbers ({exc.raw!r})",
                          spec_impact="The S1 snapshot parsers must read Tally's amount text (e.g. Dr/Cr suffixes) "
                                      "before S1 (Part 1 §13, R5).")
    receivable = _bills_total(texts["bills_receivable_today"])
    payable = _bills_total(texts["bills_payable_today"])
    baseline = ctx.store.environment.get("company_a_tb_baseline") or {}
    tb_now = {row["account_name"]: str(row["closing_balance"]) for row in tb_rows}
    ctx.observe("tb_rows", len(tb_rows))
    ctx.observe("tb_equals_baseline", tb_now == baseline)
    ctx.observe("stock_rows", len(stock_rows))
    ctx.observe("bills_totals", {"receivable": receivable, "payable": payable})
    if receivable != SEED_RECEIVABLE or payable != SEED_PAYABLE:
        return PartResult(Outcome.BLOCKED, f"Bills totals {receivable} / {payable} ≠ the anchors {SEED_RECEIVABLE} / "
                                           f"{SEED_PAYABLE}: company A has drifted. Run `uv run python -m "
                                           "probes reset-a`, then re-run.")
    return PartResult(Outcome.CONFIRMED, f"All six reports parse ({len(tb_rows)} TB rows with Decimal amounts, "
                                         f"{len(stock_rows)} stock rows); bills totals match the anchors")


PROBE = Probe(
    id=12,
    name="current_snapshots",
    question="Do TB, BS, full-FY P&L, Stock Summary and Bills Receivable / Payable export and parse at today's date?",
    feeds=("R5", "S1 snapshot fixtures"),
    parts={"A": run_a},
    requires=(0,),
)
