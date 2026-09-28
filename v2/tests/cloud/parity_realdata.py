"""Real-capture parity inputs (Task 10a; CLAUDE.md "test reality"): company A FY 2025-26 and company B as-on
31-03-2023, assembled from S0 / S1 captures through the same transcoder + parsers the cloud uses. Pure: no DB.

Every figure comes from a capture, except B's books-start anchor: G6 (B's ledger-level TB as-on `books_from`) was
not captured, so -- plan ambiguity A5 -- B's anchor is the ledger openings of `company_b_data.generate()`
(`anchor_source="dataset"`, named in every test that uses it).

"Current" (controller ruling F25) is the FY-end of TALLY's current period, i.e. the FY of the mirrored ledger
masters -- 31-03-2026 for both A and B (FY 2025-26). It is NOT the IST-current FY (2026-27). It is passed to
`forex.scope_face` as `tally_current_fy_end` (ruling F3).

A missing capture raises `FileNotFoundError` via `realdata.read_capture` (F1: fail, never skip).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from v2.cloud.ingest.derive import is_base_currency, is_forex_ledger, nature_walk
from v2.cloud.ingest.resolve import NameIndex
from v2.cloud.ingest.snapshots import parse_cells
from v2.cloud.ingest.validate import parse_objects
from v2.cloud.parity import forex as fx
from v2.cloud.parity.anchors import AnchorPlan, anchor_amounts, plan, resolve_rows
from v2.cloud.parity.model import LedgerIn, LineFact, Sums, TbRow, build_sums, synthetic_amount, tb_rows
from v2.cloud.parity.rung1 import rung1
from v2.cloud.parity.rung2 import primary_group_rows, rung2
from v2.contract import transcode
from v2.contract.parse import amount as parse_amount
from v2.contract.parse import name as parse_name
from v2.contract.tally_rules import OPENING_STOCK_ROW, PRIMARY_PARENT, UNADJUSTED_FOREX_ROW
from v2.probes.setup import company_b_data
from v2.tests.cloud import realdata

TOL = Decimal("1.00")
STOCK_BEARING_PRIMARY = "Current Assets"
# F25: Tally's current period (the mirrored masters' FY) ends 31-03-2026 for both S0 companies.
TALLY_CURRENT_FY_END = date(2026, 3, 31)


@dataclass(frozen=True)
class Case:
    label: str
    as_on: date
    anchor_plan: AnchorPlan
    ledgers: list[LedgerIn]
    anchors: dict[str, Decimal]
    anchor_unresolved: list[str]
    sums: Sums
    ledgerwise_tb: dict[str, Decimal]
    ledgerwise_unresolved: list[str]
    group_rows: dict[str, Decimal]
    opening_stock: Decimal | None
    unadjusted: Decimal | None
    ledgerwise_flags: dict[str, str]


@dataclass(frozen=True)
class Result:
    rung1: list
    forex: list
    forex_unrealised_total: Decimal
    rung2: list

    @property
    def all_lines(self) -> list:
        return [*self.rung1, *self.forex, *self.rung2]


def tb(file_name: str) -> list[TbRow]:
    return tb_rows(parse_cells("trial_balance", transcode.report_cells(realdata.read_capture(file_name),
                                                                         "trial_balance")).rows)


def request_flags(file_name: str) -> dict[str, str]:
    """The snapshot's `request_flags` as the agent would post them (§7.12): the report-shaping flags from the
    capture's own request envelope (sidecar `request_xml`)."""
    import json
    import re
    side = json.loads((realdata.SYNC / f"{file_name}.json").read_text(encoding="utf-8"))
    return {k: v for k, v in re.findall(r"<(EXPLODEFLAG|ISLEDGERWISE|EXPLODEALLLEVELS)[^>]*>([^<]*)<",
                                        side["request_xml"])}


def _index_and_ledgers(masters: list[dict]):
    groups = [m["data"] for m in masters if m["kind"] == "group"]
    ledgers = [m["data"] for m in masters if m["kind"] == "ledger"]
    currencies = [m["data"] for m in masters if m["kind"] == "currency"]
    base = next((c["name"] for c in currencies if is_base_currency(c.get("expandedsymbol", ""))), None)
    parents = {parse_name(g["name"]): parse_name(g.get("parent")) or PRIMARY_PARENT for g in groups}
    index = NameIndex.from_rows([("ledger", l["guid"], l["name"], False) for l in ledgers])
    return index, ledgers, parents, base


def _ledger_in(l: dict, parents: dict[str, str], base: str | None, mirrored: Decimal | None,
               balance_source: str, as_on: date) -> LedgerIn:
    parent = parse_name(l.get("parent"))
    derived = nature_walk(parent, parents) if parent and parent != PRIMARY_PARENT else None
    closing = parse_amount(l.get("closingbalance"))
    opening = parse_amount(l.get("openingbalance"))
    ever_expression = any(a is not None and a.fx_amount is not None for a in (closing, opening))
    raw = LedgerIn(
        guid=l["guid"], name=parse_name(l["name"]), group_guid=parent or None,
        primary_group=derived.primary_group if derived else None, nature=derived.nature if derived else None,
        is_forex=is_forex_ledger(l.get("currencyname"), base, ever_expression),
        mirrored_closing=mirrored,
        closing_fx=closing.fx_amount if closing is not None else None,
        closing_fx_rate=closing.fx_rate if closing is not None else None,
        # a plain opening is a known face only when it is zero (no foreign amount yet); else unknowable
        opening_fx=(opening.fx_amount if opening is not None and opening.fx_amount is not None
                    else (Decimal("0") if opening is not None and opening.inr == 0 else None)),
        balance_source=balance_source, in_capture=True)
    return fx.scope_face(raw, as_on, TALLY_CURRENT_FY_END)


def _line_facts(pvouchers, index: NameIndex) -> list[LineFact]:
    facts: list[LineFact] = []
    for v in pvouchers:
        if v.is_cancelled or v.is_optional:              # countable (§10.2)
            continue
        for line in v.lines:
            facts.append(LineFact(guid=index.resolve("ledger", line.ledger_name), voucher_date=v.date,
                                  amount=line.amount.inr, fx_amount=line.amount.fx_amount))
    return facts


def _parse_vouchers(objs: list[dict]):
    parsed, errors, _ = parse_objects(objs)
    assert not errors, errors
    return parsed


def case_a() -> Case:
    """Company A (`Bharat Traders Probe Copy`), FY 2025-26, books from 01-04-2025 = E (D9 books-start anchor)."""
    as_on, books_from = date(2026, 3, 31), date(2025, 4, 1)
    anchor_plan = plan(books_from, books_from)
    index, ledgers, parents, base = _index_and_ledgers(realdata.a_masters())
    facts = _line_facts(_parse_vouchers(realdata.a_vouchers("p16_A_vouchers_fy.xml")), index)
    day_one = build_sums([f for f in facts if f.voucher_date == anchor_plan.subtract_lines_dated],
                         verified_edge=books_from, as_on=books_from).total
    anchors, anchor_unresolved = anchor_amounts(tb("s1_A_tb_ledger_asof_2025-04-01.xml"), index, day_one,
                                                ledgerwise_flags=request_flags("s1_A_tb_ledger_asof_2025-04-01.xml"))
    ledgerwise, lw_unresolved = resolve_rows(tb("p17_A_tb_exploded_isledgerwise.xml"), index)
    ledger_ins = []
    for l in ledgers:
        closing = parse_amount(l.get("closingbalance"))
        source = "needs_tb" if closing is not None and not closing.stated else "tally"
        ledger_ins.append(_ledger_in(l, parents, base, closing.inr if closing is not None else None, source, as_on))
    group_tb = tb("p16_A_tb_fy_end.xml")
    return Case("A FY 2025-26", as_on, anchor_plan, ledger_ins, anchors, anchor_unresolved,
                build_sums(facts, verified_edge=books_from, as_on=as_on), ledgerwise, lw_unresolved,
                primary_group_rows(group_tb), synthetic_amount(group_tb, OPENING_STOCK_ROW),
                synthetic_amount(group_tb, UNADJUSTED_FOREX_ROW),
                request_flags("p17_A_tb_exploded_isledgerwise.xml"))


def b_dataset_anchor(index: NameIndex) -> dict[str, Decimal]:
    """A5: company B's books-start anchor from the dataset's ledger openings (G6 not captured)."""
    return {index.resolve("ledger", spec.name): spec.opening
            for spec in company_b_data.generate("educational").ledgers if spec.opening}


def b_fy2022_facts(index: NameIndex) -> list[LineFact]:
    """Every FY 2022-23 voucher of company B: probe 21's twelve month captures (incl. the two USD sales, 73/74)."""
    objs = [o for m in range(1, 13) for o in realdata.vouchers(f"p21_B_fy2022_month_{m:02d}.xml")]
    return _line_facts(_parse_vouchers(objs), index)


def case_b_2023_dataset_anchor() -> Case:
    """Company B (`Sharma & Sons' Probe Traders`) as-on 31-03-2023, E = books_from = 01-04-2022. Ruling F3: the
    rung-1 "mirrored" figures are the G2 ledger-level TB rows as-on 31-03-2023 (plain INR); the USD ledger's face
    fields come from the CURRENT-FY master (C46) and are scoped out by `scope_face`, so §10.5(b) carries it."""
    as_on, books_from = date(2023, 3, 31), company_b_data.COMPANY_B_BOOKS_FROM
    anchor_plan = plan(books_from, books_from)
    index, ledgers, parents, base = _index_and_ledgers(realdata.b_masters())
    facts = b_fy2022_facts(index)
    ledgerwise, lw_unresolved = resolve_rows(tb("s1_B_tb_ledger_asof_2023-03-31.xml"), index)
    ledger_ins = [_ledger_in(l, parents, base, ledgerwise.get(l["guid"]), "tally", as_on) for l in ledgers]
    group_tb = tb("p18_B_tb_asof_2023-03-31.xml")
    return Case("B as-on 2023-03-31 (anchor_source=dataset)", as_on, anchor_plan, ledger_ins,
                b_dataset_anchor(index), [], build_sums(facts, verified_edge=books_from, as_on=as_on),
                ledgerwise, lw_unresolved, primary_group_rows(group_tb),
                synthetic_amount(group_tb, OPENING_STOCK_ROW), synthetic_amount(group_tb, UNADJUSTED_FOREX_ROW),
                request_flags("s1_B_tb_ledger_asof_2023-03-31.xml"))


def run(case: Case, tol: Decimal = TOL) -> Result:
    r1 = rung1(case.ledgers, case.anchors, case.sums, case.ledgerwise_tb,
               [*case.anchor_unresolved, *case.ledgerwise_unresolved], tol, ledgerwise_flags=case.ledgerwise_flags)
    forex_ledgers = [l for l in case.ledgers if l.is_forex]
    fl, total = fx.forex_lines(forex_ledgers, case.anchors, case.sums, case.ledgerwise_tb, case.unadjusted, tol)
    r2 = rung2(case.ledgers, r1, fl, case.group_rows, case.opening_stock, STOCK_BEARING_PRIMARY,
               case.ledgerwise_tb, case.sums, [], tol, ledgerwise_flags=case.ledgerwise_flags)
    return Result(r1, fl, total, r2)
