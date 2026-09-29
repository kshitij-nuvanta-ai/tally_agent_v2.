"""FakeBooks company B as the agent would read it (S1 task 12; test-only -- ``v2.cloud`` never imports it, the
isolation test covers that).

The company is loaded ONCE per session through ``parity_fakebooks`` -- which runs the real
``v2.probes.setup.company_b.load_company_b`` against a ``FakeBooks`` with the ``_empty_b`` / operator pattern of
``v2/tests/probes/test_company_b.py`` (the operator enters the opening bill and sets a cancelled/optional flag by hand
when the XML flag didn't stick) -- and every read goes through ``FakeBooks.transport()`` with the agent's / probes'
own request builders:

- ``month_xml``: probe 5's confirmed month request (``v2.probes.reads.fill_month_request`` over the typed-period
  template with ``VOUCHER_MONTH_FIELDS``), i.e. what S2's extractor sends for one month;
- ``tb_xml``: the Trial Balance report (group level with ``EXPLODEFLAG``, or ledger level with ``ISLEDGERWISE``);
- ``ledgers_xml``: the mirrored ledger re-read (GUID, name, opening, closing);
- ``counters``: probe 1's company-counters request.

Every wire object is then made by the same transcoder the real captures use (``v2.contract.transcode``); nothing is
written by hand. FakeBooks' "current period" (ruling F25) is FY 2025-26 -- Tally's, not the IST FY.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from v2.contract import transcode
from v2.probes.p01_company_counters import COUNTERS_REQUEST
from v2.probes.p05_voucher_month_bounds import svdates_template
from v2.probes.reads import fill_month_request
from v2.tests.cloud import parity_fakebooks as pf
from v2.tests.cloud import realdata
from v2.tests.probes.fake_books import FakeBooks

B_NAME = pf.B_NAME
BOOKS_FROM = pf.BOOKS_FROM
USD_PARTY = pf.USD_PARTY


def month_bounds(month: str) -> tuple[date, date]:
    """``"YYYY-MM"`` -> (first day, last day)."""
    y, m = (int(p) for p in month.split("-"))
    first = date(y, m, 1)
    return first, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def fy_months(fy_start: date, books_from: date = BOOKS_FROM, until: date | None = None) -> list[str]:
    """Every ``YYYY-MM`` of the FY from ``max(fy_start, books_from)``, up to ``until`` (default: the FY end)."""
    start = max(fy_start, books_from)
    end = until or date(fy_start.year + 1, 3, 31)
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass
class FakeB:
    books: FakeBooks

    def __post_init__(self) -> None:
        self.cap = pf.Capture(self.books)

    # --- raw reads (XML as Tally answers) ---------------------------------------------------------------------------
    def _post(self, xml: str) -> str:
        return self.cap.client.post("/", content=xml.encode("utf-8")).text

    def month_xml(self, fy_start: date, month: str) -> str:
        first, last = month_bounds(month)
        if pf.fy_start(first) != fy_start:
            raise ValueError(f"{month} is not in FY {fy_start}")
        return self._post(fill_month_request(svdates_template(), B_NAME, pf.dmy(first), pf.dmy(last)))

    def tb_xml(self, as_on: date, ledgerwise: bool) -> str:
        from v2.agent.tally.envelopes import wrap_report
        extra = pf.LW_FLAGS if ledgerwise else pf.GROUP_FLAGS
        return self._post(wrap_report("Trial Balance", pf.dmy(pf.fy_start(as_on)), pf.dmy(as_on), B_NAME,
                                      extra_vars=extra))

    def ledgers_xml(self) -> str:
        from v2.probes.reads import master_request
        return self._post(master_request("S1CapLedgerBalances", "Ledger",
                                         ["GUID", "Name", "OpeningBalance", "ClosingBalance"], B_NAME))

    def counters(self) -> dict:
        c = transcode.counters_from_xml(self._post(COUNTERS_REQUEST))
        return {"alt_vch_id": int(c["altvchid"]), "alt_mst_id": int(c["altmstid"])}

    # --- wire objects -----------------------------------------------------------------------------------------------
    @property
    def guid(self) -> str:
        return self.cap.guid

    def month_vouchers(self, fy_start: date, month: str) -> list[dict]:
        objs = transcode.vouchers_from_xml(self.month_xml(fy_start, month))
        for o in objs:
            # FakeBooks keeps no stock valuation, so its inventory rows carry no AMOUNT (live ones do, §5.3); parity
            # reads ledger lines only, so the rows are dropped rather than given an invented amount (as in 10c).
            o["data"].pop("inventory_entries", None)
        return objs

    def masters(self) -> list[dict]:
        return self.cap.masters()

    def balances(self, captured_at: datetime) -> list[dict]:
        return transcode.balances_from_xml(self.ledgers_xml(), captured_at.isoformat())

    def tb_snapshot(self, as_on: date, ledgerwise: bool) -> realdata.Snapshot:
        flags = pf.LW_FLAGS if ledgerwise else pf.GROUP_FLAGS
        return realdata.Snapshot(transcode.report_cells(self.tb_xml(as_on, ledgerwise), "trial_balance"),
                                 dict(flags), f"FakeBooks B TB as-on {as_on} ledgerwise={ledgerwise}")

    def snapshot_body(self, report_type: str, as_on: date, captured_at: datetime, *, purpose: str = "parity") -> dict:
        """A ``POST /snapshots`` body read from this fake (never a dataset-built "Tally" figure -- ruling F9)."""
        body = self.cap.snapshot(report_type, as_on, captured_at, purpose=purpose)
        body["counters"] = self.counters()
        return body

    def assembled(self, start: date, end: date, captured_at: datetime) -> realdata.Assembled:
        """Masters (ledgers joined by name with the mirrored re-read's opening/closing -- keys copied, never made up)
        + every voucher dated in [start, end], as ``realdata.Assembled`` for ``realdata.pure_parity``."""
        balances = {o["data"]["name"]: o["data"] for o in self.balances(captured_at)}
        ms = []
        synthetic: dict[tuple[str, str], str] = {}
        for m in self.masters():
            data = dict(m["data"])
            if "-fx-" in data["guid"]:
                synthetic[(m["kind"], data["name"])] = "G4"
            if m["kind"] == "ledger":
                for k in ("openingbalance", "closingbalance"):
                    if k in balances.get(data["name"], {}) and k not in data:
                        data[k] = balances[data["name"]][k]
            ms.append({"kind": m["kind"], "data": data})
        vs = []
        for fy in sorted({pf.fy_start(d) for d in (start, end)} | _fys_between(start, end)):
            for month in fy_months(fy):
                first, last = month_bounds(month)
                if last < start or first > end:
                    continue
                vs += [v for v in self.month_vouchers(fy, month)
                       if start <= realdata.tally_date(v["data"]["date"]) <= end]
        return realdata.Assembled(self.guid, BOOKS_FROM, ms, vs, synthetic, {})

    def fresh(self) -> "FakeB":
        """A private copy (for a test that mutates the books)."""
        return FakeB(pf.books())


def _fys_between(start: date, end: date) -> set[date]:
    out, fy = set(), pf.fy_start(start)
    while fy <= end:
        out.add(fy)
        fy = date(fy.year + 1, 4, 1)
    return out


@functools.cache
def _session_fake() -> FakeB:
    return FakeB(pf.books())


def fakeb_company_b(tmp_path_factory=None) -> FakeB:
    """Session-scoped and cached: the (slow) company-B load runs once per session (``parity_fakebooks`` caches the
    loaded state; this caches the FakeB over it). ``tmp_path_factory`` is accepted for the fixture signature the plan
    names; the fake is in-memory, so nothing is written there. Tests that mutate the books take ``.fresh()``."""
    return _session_fake()


__all__ = ["FakeB", "fakeb_company_b", "fy_months", "month_bounds"]
