"""FakeBooks company B as the parity DB tests' Tally (S1 task 10c, controller ruling F9).

The real B-2023 data can't make a Sept-2022-only slice match a FY-end TB, so the 10c DB tests run on **FakeBooks B**:
`probes.setup.company_b.load_company_b` writes the whole company-B dataset (masters, openings, 960 vouchers incl.
the two USD export sales [S0-B:101]/[S0-B:102]) into a `FakeBooks` through the real `TallyWriter`, exactly as the
loader's own tests do. Every wire object and every snapshot's cells are then READ BACK from that fake with the
agent/probe request builders and turned into wire JSON by the same transcoder the real captures use
(`contract.transcode`) -- nothing is written by hand. A capture that FakeBooks can't answer raises (F1: fail,
never skip).

Knobs (see `FakeBooks.__init__`): `ledger_opening_scope="fy"`, `opening_stock_row`, `tb_fy_scoped` and
`unadjusted_forex_row` are the LIVE shapes of company B's FY 2025-26 captures; `educational=False` so a TB can be read
at any month-end (C43 is the agent's concern, §10.9).

"Current" (ruling F25) is Tally's current period, `FakeBooks.current_period` = FY 2025-26 -- not the IST FY.
"""
from __future__ import annotations

import copy
import functools
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tally_bridge.envelopes import wrap_report
from contract import transcode
from probes.companies import COMPANIES
from probes.reads import TB_EXPLODE_VARS, VOUCHER_MONTH_FIELDS, master_request, voucher_request
from probes.setup.company_b import load_company_b
from probes.setup.company_b_data import generate
from probes.setup.writes import TallyWriter
from tests.sync import realdata
from tests.probes.fake_books import USD_CURRENCY_ROW, FakeBooks, sync_client
from tests.probes.fakes import ScriptedIO

B_NAME = COMPANIES["B"]
KNOBS = dict(name=B_NAME, educational=False, ledger_opening_scope="fy", opening_stock_row=True, tb_fy_scoped=True,
             unadjusted_forex_row=True)
BOOKS_FROM = date(2022, 4, 1)
USD_PARTY = "Gulf Office Supplies LLC (USD)"
LW_FLAGS = {"ISLEDGERWISE": "Yes"}
GROUP_FLAGS = dict(TB_EXPLODE_VARS)
_FLAG_PAUSE = re.compile(r"^\[S0-B:(\d+)\].*?(ISCANCELLED|ISOPTIONAL) did not stick")
_OPENING_BILL = {"Op/2022-001": {"party": "Pune Digital Solutions", "amount": "-62500.00"}}


def _operator(books: FakeBooks):
    """The operator at the loader's pauses (as `test_company_b.py` models it): enters the opening bill in the UI and
    sets a cancelled/optional flag by hand when the XML flag didn't stick (F10: the fake never simulates the UI)."""
    def on_wait(instruction: str) -> None:
        if "Opening bill 'Op/2022-001'" in instruction:
            books.edit_state(lambda s: s.setdefault("bills", {}).update(copy.deepcopy(_OPENING_BILL)))
        match = _FLAG_PAUSE.match(instruction)
        if match:
            tag, flag = match.group(1), "cancelled" if match.group(2) == "ISCANCELLED" else "optional"

            def fix(s):
                for v in s["vouchers"].values():
                    if v["narration"].startswith(f"[S0-B:{tag}]"):
                        v[flag] = "Yes"
            books.edit_state(fix)
    return on_wait


@functools.cache
def _loaded_state() -> dict:
    books = FakeBooks(name=B_NAME, educational=False)       # the loader verifies all-time totals: default TB shape
    books.edit_state(lambda s: s.update(voucherTypes=["Sales", "Purchase", "Receipt", "Payment", "Sales - GST"]))
    books.edit_state(lambda s: s["currencies"].__setitem__("$", dict(USD_CURRENCY_ROW)))
    report = load_company_b(TallyWriter(sync_client(books.transport()), lambda _m: None),
                            ScriptedIO(on_wait=_operator(books)))
    assert report.problems == [], report.problems

    def as_company_b(s):
        # `seed_state` is company A's shell: drop its leftovers the B loader never touches (A's `Electricity` and
        # `Rajesh Computers` -- whose group `South Zone Debtors` B doesn't have -- and A's `Electronics` stock
        # group), and add Tally's own `Profit & Loss A/c` the way `seed_company_b(masters=True)` does (live B has
        # it: the FY 2025-26 TBs carry its row).
        for name in ("Electricity", "Rajesh Computers"):
            s["ledgers"].pop(name, None)
        s["stock_groups"] = []
        s["ledgers"]["Profit & Loss A/c"] = {"parent": "Primary", "email": "", "alter_id": 100,
                                             "guid": f"{s['guid']}-b0000000", "opening": "0.00"}
        # FakeBooks' import path doesn't record VOUCHERTYPENAME (live Tally does); give each voucher back the type
        # the loader sent, by its `[S0-B:n]` tag.
        by_tag = {v.tag: v.vch_type for v in generate("licensed").vouchers}
        for v in s["vouchers"].values():
            tag = re.match(r"^\[S0-B:(\d+)\]", v["narration"])
            if tag and not v.get("vch_type"):
                v["vch_type"] = by_tag[int(tag.group(1))]
    books.edit_state(as_company_b)
    return books.state


def books(**overrides) -> FakeBooks:
    """A fresh FakeBooks holding a private copy of the loaded company B (the load runs once per session)."""
    fresh = FakeBooks(**{**KNOBS, **overrides})
    state = copy.deepcopy(_loaded_state())
    fresh.edit_state(lambda s: (s.clear(), s.update(state)))
    return fresh


def dmy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def month_ends(start: date, end: date) -> list[date]:
    out, y, m = [], start.year, start.month
    while True:
        first_next = date(y + (m == 12), m % 12 + 1, 1)
        me = first_next - timedelta(days=1)
        if me > end:
            return out
        out.append(me)
        y, m = first_next.year, first_next.month


class Capture:
    """Reads company B back out of a FakeBooks, as wire objects / snapshot cells."""

    def __init__(self, fake: FakeBooks):
        self.fake = fake
        self.client = sync_client(fake.transport())

    def _get(self, xml: str) -> str:
        text = self.client.post("/", content=xml.encode("utf-8")).text
        if text.strip() in ("", "<ENVELOPE></ENVELOPE>"):
            raise AssertionError(f"FakeBooks gave no answer for {xml[:200]!r}")
        return text

    @property
    def guid(self) -> str:
        return self.fake.state["guid"]

    @property
    def counters(self) -> dict:
        return {"alt_vch_id": int(self.fake.state["alt_vch"]), "alt_mst_id": int(self.fake.state["alt_mst"])}

    # --- masters ---------------------------------------------------------------------------------------------------
    def _masters(self, collection: str, tdl: str, kind: str, fields: list[str]) -> list[dict]:
        objs = transcode.masters_from_xml(self._get(master_request(collection, tdl, fields, B_NAME)), kind)
        out = []
        for obj in objs:
            data = dict(obj["data"])
            if "guid" not in data or "alterid" not in data:          # A4 / gap G4: synthetic identity
                guid, alter = realdata.synthetic_identity(self.guid, kind, data["name"])
                data.setdefault("guid", guid)
                data.setdefault("alterid", alter)
            data.setdefault("parent", "")
            out.append({"kind": kind, "data": data})
        return out

    def masters(self) -> list[dict]:
        ids = ["GUID", "AlterID", "MasterID"]
        ledgers = self._masters("S1CapLedgers", "Ledger", "ledger", ["Name", "Parent", "CurrencyName", *ids])
        return [
            *self._masters("S1CapCurrencies", "Currency", "currency",
                           ["Name", "MailingName", "ExpandedSymbol", "OriginalName", "DecimalPlaces", *ids]),
            *self._masters("S1CapGroups", "Group", "group", ["Name", "Parent"]),
            *self._masters("S1CapVoucherTypes", "VoucherType", "voucher_type", ["Name", "Parent", "ReservedName", *ids]),
            *self._masters("S1CapUnits", "Unit", "unit",
                           ["Name", "IsSimpleUnit", "BaseUnits", "AdditionalUnits", "Conversion", *ids]),
            *self._masters("S1CapStockGroups", "StockGroup", "stock_group", ["Name", "Parent", *ids]),
            *ledgers,
            *self._masters("S1CapStockItems", "StockItem", "stock_item", ["Name", "Parent", "BaseUnits"]),
        ]

    def balances(self, captured_at: datetime, names: set[str] | None = None) -> list[dict]:
        """The mirrored ledger re-read (`ledger_balance` objects): Tally's current-period closing and FY opening."""
        xml = master_request("S1CapLedgerBalances", "Ledger",
                             ["GUID", "Name", "OpeningBalance", "ClosingBalance"], B_NAME)
        objs = transcode.balances_from_xml(self._get(xml), captured_at.isoformat())
        return [o for o in objs if names is None or o["data"]["name"] in names]

    # --- vouchers --------------------------------------------------------------------------------------------------
    def vouchers(self, start: date, end: date, *, omit: tuple[str, ...] = ()) -> list[dict]:
        """Every voucher dated in [start, end] (probe 5's month request, typed period), minus any whose narration
        starts with one of `omit` (e.g. "[S0-B:101]")."""
        xml = voucher_request("S0VoucherMonth", VOUCHER_MONTH_FIELDS, B_NAME, from_date=dmy(start), to_date=dmy(end))
        objs = transcode.vouchers_from_xml(self._get(xml))
        for o in objs:
            # FakeBooks keeps no stock valuation, so its inventory rows carry no AMOUNT (live ones do, §5.3). Parity
            # reads ledger lines only; the rows are dropped rather than given an invented amount.
            o["data"].pop("inventory_entries", None)
        return [o for o in objs if not any((o["data"].get("narration") or "").startswith(t) for t in omit)]

    # --- reports ---------------------------------------------------------------------------------------------------
    def tb_cells(self, report_type: str, as_on: date) -> list[dict]:
        extra = LW_FLAGS if report_type == "trial_balance_ledgerwise" else GROUP_FLAGS
        xml = wrap_report("Trial Balance", dmy(fy_start(as_on)), dmy(as_on), B_NAME, extra_vars=extra)
        return transcode.report_cells(self._get(xml), "trial_balance")

    def snapshot(self, report_type: str, as_on: date, captured_at: datetime, *, purpose: str = "parity",
                 flags: dict | None = None, cells: list[dict] | None = None) -> dict:
        default_flags = LW_FLAGS if report_type == "trial_balance_ledgerwise" else GROUP_FLAGS
        return {"report_type": report_type, "from_date": dmy(fy_start(as_on)), "as_on_date": dmy(as_on),
                "request_flags": dict(default_flags if flags is None else flags), "purpose": purpose,
                "captured_at": captured_at.isoformat(), "counters": self.counters,
                "cells": self.tb_cells(report_type, as_on) if cells is None else cells}


def add_current_fy_usd_sale(fake: FakeBooks, *, day: str = "20250902", fx: str = "500.00", rate: str = "84.00") -> str:
    """A current-FY (Tally's current period) USD export sale on the USD party, shaped exactly like the loader's
    [S0-B:101] (`FakeBooks._forex_line_text`, the one place a forex line's text is built). Returns its narration
    tag. Used to prove the §10.5(a) face check in the current FY (ruling F4)."""
    from probes.reads import parse_forex_amount
    tag = "[S1-10c:USD-FY25]"
    base = (Decimal(fx) * Decimal(rate)).quantize(Decimal("0.01"))

    def add(s):
        mid = str(s["next_master_id"])
        s["next_master_id"] += 1
        s["alt_vch"] += 1
        lines = []
        for ledger, sign, deemed in ((USD_PARTY, "-", "Yes"), ("Export Sales", "", "No")):
            amount = Decimal(f"{sign}{base}")
            text = f"{sign}$ {fx} @ ? {rate}/$ = {sign}? {base}".replace("$ ", "$")
            line = {"ledger": ledger, "amount": f"{amount:.2f}", "deemed_positive": deemed}
            line.update(fake._forex_line_text(s, amount, parse_forex_amount(text)))
            lines.append(line)
        s["vouchers"][mid] = {"narration": f"{tag} Export sale to Gulf Office Supplies LLC (USD)", "date": day,
                              "post_dated": "No", "cancelled": "No", "optional": "No", "vch_type": "Sales",
                              "lines": lines, "inventory": [], "bills": []}
    fake.edit_state(add)
    return tag


UTC = timezone.utc
