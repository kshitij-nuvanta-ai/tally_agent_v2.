"""Test doubles: a fake Tally XML server and a scripted console. No real Tally."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from agent.tally.client import TallyClient
from agent.tally.envelopes import esc
from probes.actions import Action
from probes.capture import Capture
from probes.core import Outcome, PartResult
from probes.results import ResultsStore

Handler = Callable[[str], "str | bytes | type[Exception]"]
COMPANY_LIST_MARKER = "<ID>List of Companies</ID>"
RUNNING = b"<RESPONSE>TallyPrime Server is Running</RESPONSE>"


def company_list_xml(names: list[str]) -> str:
    companies = "".join(f'<COMPANY NAME="{esc(n)}"><NAME>{esc(n)}</NAME></COMPANY>' for n in names)
    return ("<ENVELOPE><BODY><DESC><CMPINFO><COMPANY>0</COMPANY></CMPINFO></DESC>"
            f"<DATA><COLLECTION>{companies}</COLLECTION></DATA></BODY></ENVELOPE>")


def objects_xml(tag: str, rows: list[dict[str, str]]) -> str:
    body = "".join(
        f"<{tag}>" + "".join(f"<{k.upper()}>{esc(v)}</{k.upper()}>" for k, v in row.items()) + f"</{tag}>"
        for row in rows
    )
    return f"<ENVELOPE><BODY><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>"


def bills_xml(bills: list[tuple[str, str, str]]) -> str:
    """bills: (ref, party, BILLCL text)."""
    return "<ENVELOPE>" + "".join(
        f"<BILLFIXED><BILLDATE>1-Apr-25</BILLDATE><BILLREF>{esc(ref)}</BILLREF><BILLPARTY>{esc(party)}</BILLPARTY>"
        f"</BILLFIXED><BILLCL>{amount}</BILLCL><BILLDUE>1-Apr-25</BILLDUE><BILLOVERDUE>10</BILLOVERDUE>"
        for ref, party, amount in bills
    ) + "</ENVELOPE>"


def tb_xml(rows: list[tuple[str, str, str]]) -> str:
    """rows: (group, debit text, credit text)."""
    return "<ENVELOPE>" + "".join(
        f"<DSPACCNAME><DSPDISPNAME>{esc(name)}</DSPDISPNAME></DSPACCNAME><DSPACCINFO>"
        f"<DSPCLDRAMT><DSPCLDRAMTA>{debit}</DSPCLDRAMTA></DSPCLDRAMT>"
        f"<DSPCLCRAMT><DSPCLCRAMTA>{credit}</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>"
        for name, debit, credit in rows
    ) + "</ENVELOPE>"


class FakeTally:
    """Answers each request from the first route whose marker appears in the request XML."""

    def __init__(self, companies: list[str] | None = None):
        self.companies = list(companies or [])
        self.down = False
        self.routes: list[tuple[str, Handler]] = []
        self.requests: list[str] = []

    def route(self, marker: str, handler: Handler) -> None:
        self.routes.append((marker, handler))

    def probe_requests(self) -> list[str]:
        """Requests other than the guard's company list."""
        return [r for r in self.requests if COMPANY_LIST_MARKER not in r]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8")
            self.requests.append(body)
            if self.down:
                raise httpx.ConnectError("connection refused", request=request)
            if request.method == "GET":
                return httpx.Response(200, content=RUNNING)
            for marker, handler in self.routes:
                if marker in body:
                    result = handler(body)
                    if isinstance(result, type) and issubclass(result, Exception):
                        raise result("fake failure", request=request)
                    return httpx.Response(200, content=result.encode("utf-8") if isinstance(result, str) else result)
            if COMPANY_LIST_MARKER in body:
                return httpx.Response(200, content=company_list_xml(self.companies).encode("utf-8"))
            return httpx.Response(200, content=b"<ENVELOPE></ENVELOPE>")

        return httpx.MockTransport(handle)


class ScriptedIO:
    def __init__(self, answers: list[str] | None = None, interactive: bool = True,
                 on_wait: Callable[[str], None] | None = None, on_action: Callable[[Action], None] | None = None,
                 answers_by_kind: dict[str, str] | None = None, run_mode: str = "manual"):
        self.interactive = interactive
        self.run_mode = run_mode
        self.answers = list(answers or [])
        self.answers_by_kind = dict(answers_by_kind or {})
        self.on_wait = on_wait
        self.on_action = on_action
        self.waits: list[str] = []
        self.actions: list[Action | None] = []
        self.asks: list[str] = []
        self.ask_actions: list[Action | None] = []
        self.said: list[str] = []

    def wait(self, instruction: str, action: Action | None = None) -> None:
        self.waits.append(instruction)
        self.actions.append(action)
        if self.on_wait:
            self.on_wait(instruction)
        if self.on_action and action is not None:
            self.on_action(action)

    def ask(self, prompt: str, action: Action | None = None) -> str:
        self.asks.append(prompt)
        self.ask_actions.append(action)
        if action is not None and action.kind in self.answers_by_kind:
            return self.answers_by_kind[action.kind]
        if not self.answers:
            raise AssertionError(f"Unexpected question: {prompt}")
        return self.answers.pop(0)

    def say(self, message: str) -> None:
        self.said.append(message)


def make_harness(tmp_path: Path, fake: FakeTally) -> tuple[TallyClient, ResultsStore, Capture]:
    return (TallyClient(transport=fake.transport()),
            ResultsStore(tmp_path / "results.json"),
            Capture(tmp_path / "fixtures"))


def mark_done(store: ResultsStore, probe_id: int, *, part: str = "A", all_parts: list[str] | None = None,
              observations: dict | None = None) -> None:
    """Seed `store` with a CONFIRMED part (for tests of a probe that `requires` this one)."""
    store.record_part(probe_id, part, PartResult(Outcome.CONFIRMED, "seeded for test", observations or {}),
                      all_parts=all_parts or [part], fixtures=[], manual_steps=[],
                      ran_at=datetime.now().astimezone())


COMPANY_A = "Bharat Traders Probe Copy"


def _leafs(fields: dict) -> str:
    return "".join(f"<{k}>{esc(str(v))}</{k}>" for k, v in fields.items()
                   if not k.startswith("_") and not isinstance(v, (list, tuple)))


def line(ledger: str, amount: str, **extra) -> dict:
    """A ledger line; ISDEEMEDPOSITIVE follows the sign (debit negative, Part 1 §6). extra: bills=[…], _list=tag."""
    return {"LEDGERNAME": ledger, "ISDEEMEDPOSITIVE": "Yes" if amount.startswith("-") else "No", "AMOUNT": amount,
            **extra}


def vch(header: dict[str, str], lines=(), inventory=(), *, list_tag: str = "ALLLEDGERENTRIES.LIST") -> str:
    """One <VOUCHER> the way Tally exports it, including the empty placeholder lists it always adds."""
    body = _leafs(header)
    for item in lines:
        tag = item.get("_list", list_tag)
        bills = "".join(f"<BILLALLOCATIONS.LIST>{_leafs(b)}</BILLALLOCATIONS.LIST>" for b in item.get("bills", ()))
        body += f"<{tag}>{_leafs(item)}{bills or '<BILLALLOCATIONS.LIST>  </BILLALLOCATIONS.LIST>'}</{tag}>"
    for inv in inventory:
        accounting = "".join(f"<ACCOUNTINGALLOCATIONS.LIST>{_leafs(a)}</ACCOUNTINGALLOCATIONS.LIST>"
                             for a in inv.get("accounting", ()))
        batches = "".join(f"<BATCHALLOCATIONS.LIST>{_leafs(b)}</BATCHALLOCATIONS.LIST>" for b in inv.get("batches", ()))
        body += f"<ALLINVENTORYENTRIES.LIST>{_leafs(inv)}{accounting}{batches}</ALLINVENTORYENTRIES.LIST>"
    kind = esc(header.get("VOUCHERTYPENAME", "Payment"))
    return f'<VOUCHER VCHTYPE="{kind}">{body}<INVOICEORDERLIST.LIST>  </INVOICEORDERLIST.LIST></VOUCHER>'


def vouchers_xml(vouchers: list[str]) -> str:
    """A voucher collection response, with CMPINFO's <VOUCHER>0</VOUCHER> counter that parsers must skip."""
    return ("<ENVELOPE><BODY><DESC><CMPINFO><VOUCHER>0</VOUCHER></CMPINFO></DESC><DATA><COLLECTION>"
            + "".join(vouchers) + "</COLLECTION></DATA></BODY></ENVELOPE>")


def stock_summary_xml(items: list[tuple[str, str, str, str]]) -> str:
    """items: (name, closing qty text e.g. '45 Nos', rate text, value text)."""
    return "<ENVELOPE>" + "".join(
        f"<DSPACCNAME><DSPDISPNAME>{esc(name)}</DSPDISPNAME></DSPACCNAME><DSPSTKINFO><DSPSTKCL>"
        f"<DSPCLQTY>{qty}</DSPCLQTY><DSPCLRATE>{rate}</DSPCLRATE><DSPCLAMTA>{value}</DSPCLAMTA></DSPSTKCL></DSPSTKINFO>"
        for name, qty, rate, value in items) + "</ENVELOPE>"


def a_tally(state: dict | None = None) -> tuple["FakeTally", dict]:
    """FakeTally with company A open and a counters route driven by `state` (AltVchId, AltMstId, optional GUID)."""
    state = state if state is not None else {"AltVchId": 50, "AltMstId": 266}
    fake = FakeTally([COMPANY_A])
    fake.route("S0CompanyCounters", lambda body: objects_xml("COMPANY", [{
        "Name": COMPANY_A, "GUID": state.get("GUID", "g-1"), "AltVchId": str(state["AltVchId"]),
        "AltMstId": str(state["AltMstId"]), "BooksFrom": "20250401", "LastVoucherDate": "20260301",
        "AlterID": str(state["AltMstId"])}]))
    return fake, state


def ready_store(store: ResultsStore, *, baseline: dict[str, str] | None = None, licence: str = "licensed",
                p01_last_voucher_date: str | None = None) -> None:
    """Probes 0, 1, 2 done, counters confirmed with probe 1's request, environment as probe 0 leaves it.

    `p01_last_voucher_date`: probe 1's recorded pre-throwaway LastVoucherDate baseline ("" = probe 1 ran but the
    company exported none). None = probe 1 recorded no baseline at all.
    """
    from probes.p01_company_counters import CANDIDATE_FIELDS, COUNTERS_REQUEST, LAST_VOUCHER_DATE_BASELINE
    for probe_id in (0, 1, 2):
        observations = None
        if probe_id == 1 and p01_last_voucher_date is not None:
            observations = {LAST_VOUCHER_DATE_BASELINE: {"value": p01_last_voucher_date,
                                                         "available": bool(p01_last_voucher_date), "note": ""}}
        mark_done(store, probe_id, observations=observations)
    store.confirm_request("company_counters", 1, COUNTERS_REQUEST, fields=list(CANDIDATE_FIELDS))
    store.update_environment(licence=licence, company_a_tb_baseline=baseline or {})
