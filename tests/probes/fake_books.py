"""A stateful fake TallyPrime for the operator and write-helper tests: one company, imports change it.

With a folder the company data lives in `<folder>/fake_company.json`, so file-copy backup / restore / reset behave
the way they do on the real s0probe folder. Nothing here touches Wine or the real Tally.
"""
from __future__ import annotations

import copy
import html
import json
import re
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable

import httpx

from agent.tally.envelopes import esc
from probes.companies import SEED_COMPANY
from probes.operator.config import OperatorConfig
from probes.operator.tally_control import TallyProcess
from probes.reads import ForexAmount, forex_base, parse_forex_amount
from tests.probes.fakes import bills_xml, company_list_xml, objects_xml, stock_summary_xml, tb_xml, vouchers_xml

# Tally's own fixed reserved-group hierarchy (not dataset-specific — just enough of it to build a believable
# Trial Balance fixture for whatever masters a test created). A bucket not listed here is already a primary
# group (e.g. "Capital Account", "Sales Accounts") and gets no separate child row.
# ASSUMPTION, not a verified read (coordinator review round 2, "noted, no action"): this hierarchy is hand-built
# Tally domain knowledge, not derived from anything the loader itself reads. It's acceptable for now because the
# group-level TB comparison is the deliberately-scoped check (ledger-level is probe 17's job, S0-D7) — but it
# must be scrutinised, not trusted, against a live Tally read the first time this loader actually runs live.
RESERVED_GROUP_PARENTS = {
    "Sundry Debtors": "Current Assets", "Bank Accounts": "Current Assets", "Cash-in-Hand": "Current Assets",
    "Sundry Creditors": "Current Liabilities", "Duties & Taxes": "Current Liabilities",
}

# Every Tally company's reserved groups (name -> parent, "" = primary), for probes that walk Parent to a primary group.
PRIMARY_GROUPS = ("Capital Account", "Loans (Liability)", "Current Liabilities", "Fixed Assets", "Investments",
                  "Current Assets", "Branch / Divisions", "Misc. Expenses (ASSET)", "Suspense A/c", "Sales Accounts",
                  "Purchase Accounts", "Direct Incomes", "Direct Expenses", "Indirect Incomes", "Indirect Expenses")
RESERVED_GROUPS = {**{g: "" for g in PRIMARY_GROUPS}, **RESERVED_GROUP_PARENTS, "Stock-in-Hand": "Current Assets"}
NOMINAL_PRIMARIES = frozenset({"Sales Accounts", "Purchase Accounts", "Direct Incomes", "Direct Expenses",
                               "Indirect Incomes", "Indirect Expenses"})
# The fake's answer to request XML that isn't well-formed (probe 14's deliberately unescaped `&`). The live shape is
# what probe 14 records; this is only a Tally-style LINEERROR so `detect_error` sees a failure.
MALFORMED_ANSWER = ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA>"
                    "<LINEERROR>fake: request XML is not well-formed</LINEERROR></DATA></BODY></ENVELOPE>")
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)")
_COMPANY_VAR = re.compile(r"<SVCurrentCompany>([^<]*)</SVCurrentCompany>")
# Collection-name prefixes answered by the generic probe master routes below (probes 3 B, 11, 14, 15, 16 B, 18 B, 24,
# 25 B; S1 task 0's s1_capture).
B_PROBE_COLLECTIONS = ("S0P03B", "S0P11", "S0P14", "S0P15", "S0P16B", "S0P18B", "S0P22", "S0P24", "S0P25B",
                       "S1Cap")
# The fake keeps no stock valuation: a current-period opening (C46 knob) is priced at this placeholder rate. Probe 11
# records, never judges, the rate/value of a current-period opening.
FAKE_STOCK_RATE = Decimal("100.00")
_CREDIT_DAYS = re.compile(r"^\s*(\d+)\s*Days?\s*$", re.IGNORECASE)


def _short_date(day) -> str:
    """Tally's report date text: 1-Feb-23."""
    return f"{day.day}-{day:%b}-{day:%y}"


def _bill_row(ref: str, party: str, amount: str, bill_date: str = "1-Apr-25", due: str = "1-Apr-25",
              overdue: str = "10") -> str:
    """One Bills Receivable/Payable row — byte-identical to fakes.bills_xml when no date is known."""
    return (f"<BILLFIXED><BILLDATE>{bill_date}</BILLDATE><BILLREF>{esc(ref)}</BILLREF><BILLPARTY>{esc(party)}"
            f"</BILLPARTY></BILLFIXED><BILLCL>{amount}</BILLCL><BILLDUE>{due}</BILLDUE><BILLOVERDUE>{overdue}"
            "</BILLOVERDUE>")


def _voucher_header(state: dict, mid: str, v: dict) -> dict[str, str]:
    lines = [] if v.get("cancelled") == "Yes" else v.get("lines", [])     # Ruling S2: live has an empty party name
    return {"DATE": v["date"], "GUID": f"{state['guid']}-{int(mid):08x}", "MASTERID": mid, "ALTERID": mid,
            "VOUCHERTYPENAME": v.get("vch_type", ""), "VOUCHERNUMBER": v.get("number", mid), "REFERENCE": "",
            "PARTYLEDGERNAME": lines[0]["ledger"] if lines else "", "NARRATION": v["narration"],
            "ISCANCELLED": v["cancelled"], "ISOPTIONAL": v["optional"], "ISPOSTDATED": v["post_dated"]}


def _amount_text(value: Decimal) -> str:
    """Tally exports a zero balance as an empty tag (probe 16 A: `empty_closing_for_zero`)."""
    return "" if value == 0 else f"{value:.2f}"


def _fy_start(yyyymmdd: str) -> str:
    year, month = int(yyyymmdd[:4]), int(yyyymmdd[4:6])
    return f"{year if month >= 4 else year - 1}0401"


def _dr_cr(value: Decimal) -> tuple[str, str]:
    """Real Tally XML: a debit-natured closing balance is negative under DSPCLDRAMTA, credit-natured is
    positive under DSPCLCRAMTA (tests/fixtures/tally_samples/trial_balance_live.xml)."""
    return (f"{value:.2f}", "") if value < 0 else ("", f"{value:.2f}")

# C33 (live 2026-09-24): Tally honours SVFROMDATE/SVTODATE only with TYPE="Date" (formats 01-04-2022, 20220401 and
# 1-Apr-2022 all work typed). Untyped, it silently answers for the company's CURRENT period — live that was
# 1-Apr-2025..31-Mar-2026, the fake's default. A period variable the fake can't read also falls back to it.
# C43 (live 2026-09-24): Educational TallyPrime also ignores a TYPED date variable whose day is not the 1st, 2nd or 31st
# (the same rule it applies to voucher dates) and falls back to the current period for that variable alone — typed
# 01-06-2023..30-06-2023 answered 2023-06-01..2026-03-31. A licensed fake honours any valid date.
CURRENT_PERIOD = ("20250401", "20260331")
EDUCATIONAL_DATE_VAR_DAYS = (1, 2, 31)
_TYPED_DATE_VAR = r'<{name}\s+TYPE="Date">([^<]*)</{name}>'


def _yyyymmdd(text: str) -> str | None:
    from datetime import datetime
    for fmt in ("%d-%m-%Y", "%Y%m%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def requested_period(body: str, current: tuple[str, str] = CURRENT_PERIOD, *,
                     educational: bool = False) -> tuple[str, str]:
    """The (from, to) window Tally would use for a request: the typed SVFROMDATE/SVTODATE, each falling back to
    the current period when absent or UNTYPED (C33) — exactly the silent substitution live Tally makes — or, on an
    educational Tally, when its day is not 1/2/31 (C43)."""
    period = []
    for name, fallback in (("SVFROMDATE", current[0]), ("SVTODATE", current[1])):
        match = re.search(_TYPED_DATE_VAR.format(name=name), body)
        parsed = _yyyymmdd(html.unescape(match.group(1))) if match else None
        if parsed and educational and int(parsed[6:]) not in EDUCATIONAL_DATE_VAR_DAYS:
            parsed = None
        period.append(parsed or fallback)
    return period[0], period[1]


GUID = "710de34a-3661-4a7b-8148-c2206c3b3e17"
STATE_FILE = "fake_company.json"
COMPANY_LIST_MARKER = "<ID>List of Companies</ID>"


# plan part 7 Task 3.9 — live (forex_shape_2026-09-25_run2/currencies_after.xml): the `$` Currency master as company B
# lists it. It was created in the UI: the XML create is refused (run1, EXCEPTIONS=1). No DecimalSymbol is exported.
USD_CURRENCY_ROW = {"MailingName": "USD", "OriginalName": "$", "ExpandedSymbol": "USD", "IsSuffix": "No",
                    "HasSpace": "No", "DecimalPlaces": "2"}


# The pre-live candidate answers Task 1's writer and shape-runner tests were written against (plan part 7 Task 3.9
# moved the defaults to the live ones): an XML Currency create that succeeds, and a rate with a base symbol accepted.
# Those tests pass these explicitly, so they keep exercising the branches the live run did not take.
CANDIDATE_FOREX_KNOBS = {"forex_currency_create": "ok", "forex_rate_symbols_refused": ()}


def seed_state(name: str = SEED_COMPANY) -> dict:
    return {
        "name": name, "guid": GUID, "alt_vch": 50, "alt_mst": 266, "last_voucher_date": "20260301",
        "next_master_id": 51,
        "ledgers": {
            "Cash": {"parent": "Cash-in-Hand", "email": "", "alter_id": 10, "guid": f"{GUID}-0000000a"},
            "Electricity": {"parent": "Indirect Expenses", "email": "", "alter_id": 243, "guid": f"{GUID}-000000f1"},
            "Rajesh Computers": {"parent": "South Zone Debtors", "email": "", "alter_id": 30, "guid": f"{GUID}-0000001e"},
        },
        "stock_groups": ["Electronics"],
        "vouchers": {},
        "bills": {},                          # C34: bill name -> {"party", "amount" (signed: − = Dr = receivable)}
        "groups": {},
        "units": {},
        "items": {},
        "voucherTypes": ["Sales", "Purchase", "Receipt", "Payment", "Contra", "Journal"],
        # plan part 7: the base currency — live (R-SYM, logs/p7-forex-discovery-2026-09-25.log): company B's base
        # currency is NAMEd a literal "?" (its symbol was lost at UI creation under Wine), MAILINGNAME/EXPANDEDSYMBOL
        # "INR", and every ledger's CurrencyName reads "?".
        "currencies": {"?": {"MailingName": "INR", "OriginalName": "?", "ExpandedSymbol": "INR",
                             "DecimalSymbol": "paise", "IsSuffix": "No", "HasSpace": "Yes", "DecimalPlaces": "2"}},
    }


def write_company_folder(folder: Path, name: str = SEED_COMPANY) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / STATE_FILE).write_text(json.dumps(seed_state(name)), encoding="utf-8")


def deemed_positive_matches(flag: str, amount: Decimal) -> bool:
    """Op 6/7/8 (docs/tally-write-exploration-v4.md): ISDEEMEDPOSITIVE=Yes always carries a NEGATIVE AMOUNT and
    No a POSITIVE one — "AMOUNT is positive on the side that grows". Every other permutation the live
    exploration tried came back EXCEPTIONS=1. A zero amount pins nothing, so it is accepted either way."""
    if amount == 0:
        return True
    return (flag == "Yes") == (amount < 0)


def import_result(created: int = 0, altered: int = 0, deleted: int = 0, errors: int = 0, last_vch_id: str = "0",
                  line_error: str = "", exceptions: int = 0) -> str:
    """The IMPORTRESULT shape TallyPrime 7 answers an import with (live 2026-09-22)."""
    err = f"<LINEERROR>{line_error}</LINEERROR>" if line_error else ""
    return ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA><IMPORTRESULT>"
            f"<CREATED>{created}</CREATED><ALTERED>{altered}</ALTERED><DELETED>{deleted}</DELETED>"
            f"<LASTVCHID>{last_vch_id}</LASTVCHID><LASTMID>0</LASTMID><COMBINED>0</COMBINED><IGNORED>0</IGNORED>"
            f"<ERRORS>{errors}</ERRORS><CANCELLED>0</CANCELLED><EXCEPTIONS>{exceptions}</EXCEPTIONS>{err}"
            "</IMPORTRESULT></DATA></BODY></ENVELOPE>")


def sync_client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(base_url="http://localhost:9000", transport=transport, trust_env=False)


def _flagged(voucher: dict) -> bool:
    """C42 (live run 4, 2026-09-24): a cancelled or optional voucher moves no balance, stock or bill in Tally —
    whether the flag came over the wire or the operator set it by hand in the UI after the create."""
    return voucher.get("cancelled") == "Yes" or voucher.get("optional") == "Yes"


def _effective_bills(state: dict) -> dict[str, dict[str, str]]:
    """The bills as Tally shows them: `state["bills"]` holds every posting ever made (and any bill a test or the
    operator entered by hand); each flagged voucher's own postings are backed out of it here (C42)."""
    bills = {name: dict(bill) for name, bill in state.get("bills", {}).items()}
    for v in state["vouchers"].values():
        if not _flagged(v):
            continue
        for posted in v.get("bills", []):
            if posted["type"] == "Agst Ref":
                bills[posted["name"]]["amount"] = f"{Decimal(bills[posted['name']]['amount']) - Decimal(posted['amount']):.2f}"
            else:
                bills.pop(posted["name"], None)
    return bills


def _export_voucher(state: dict, mid: str, v: dict, *, credit_periods: bool = True) -> str:
    """One stored voucher the way probe 5's month request gets it back: header, ledger lines (the voucher's bill
    postings on its first line, the party line), and inventory rows. It is shaped for the probes' parsers (reads.
    parse_vouchers), not a byte-for-byte copy of live Tally. Probe 21's live byte counts come from live Tally only.
    A posting that knows its bill date / credit period (seed_company_b(bills=True)) exports them as BILLDATE and
    BILLCREDITPERIOD (live shape: p21_B_fy2022_month_02.xml); without them the bytes are exactly as before.
    Known gap (pre-flight F9, kept as ruled): live Agst Ref allocations ALSO carry BILLCREDITPERIOD and the ORIGINAL
    bill's BILLDATE (p21 217 → Inv/45: "30 Days", 20220601); the fake stamps them on New Ref postings only. Probe 23 B
    reads New Ref bills only, so no verdict depends on it — model it before any probe reads Agst Ref terms.
    A cancelled voucher exports the recorded live shape (Ruling S2, p21_B_fy2022_month_02.xml 201/202): header only,
    an empty PARTYLEDGERNAME and empty placeholder lists -- no ledger or inventory lines, no amounts."""
    header = _voucher_header(state, mid, v)
    body = "".join(f"<{k}>{esc(str(value))}</{k}>" for k, value in header.items())
    if v.get("cancelled") == "Yes":
        body += ("<ALLINVENTORYENTRIES.LIST>      </ALLINVENTORYENTRIES.LIST><LEDGERENTRIES.LIST>      "
                 "</LEDGERENTRIES.LIST><ALLLEDGERENTRIES.LIST>      </ALLLEDGERENTRIES.LIST>")
        return f'<VOUCHER VCHTYPE="{esc(header["VOUCHERTYPENAME"])}">{body}</VOUCHER>'
    lines = v.get("lines", [])
    for i, line in enumerate(lines):
        bills = "".join(
            "<BILLALLOCATIONS.LIST>"
            + (f"<BILLDATE>{b['date']}</BILLDATE>" if b.get("date") else "")
            + f"<NAME>{esc(b['name'])}</NAME>"
            + (f"<BILLCREDITPERIOD>{esc(b['credit_period'])}</BILLCREDITPERIOD>"
               if credit_periods and b.get("credit_period") else "")
            + f"<BILLTYPE>{esc(b['type'])}</BILLTYPE><AMOUNT>{b['amount']}</AMOUNT></BILLALLOCATIONS.LIST>"
            for b in (v.get("bills", []) if i == 0 else []))
        body += (f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{esc(line['ledger'])}</LEDGERNAME>"
                 f"<ISDEEMEDPOSITIVE>{line['deemed_positive']}</ISDEEMEDPOSITIVE>"
                 # plan part 7: a forex line exports its knob-shaped text (FakeBooks._forex_line_text); a plain line's
                 # bytes are exactly as before.
                 f"<AMOUNT>{esc(line['amount_text']) if line.get('amount_text') else line['amount']}</AMOUNT>"
                 + "".join(f"<{k}>{esc(v)}</{k}>" for k, v in line.get("extra", {}).items())
                 + f"{bills or '<BILLALLOCATIONS.LIST>  </BILLALLOCATIONS.LIST>'}</ALLLEDGERENTRIES.LIST>")
    for inv in v.get("inventory", []):
        unit = state.get("items", {}).get(inv["item"], {}).get("qty_unit", "")
        qty = inv["qty"].lstrip("-") + (f" {unit}" if unit else "")
        body += (f"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{esc(inv['item'])}</STOCKITEMNAME>"
                 f"<ACTUALQTY> {qty}</ACTUALQTY></ALLINVENTORYENTRIES.LIST>")
    return f'<VOUCHER VCHTYPE="{esc(header["VOUCHERTYPENAME"])}">{body}</VOUCHER>'


def seed_company_b(books: "FakeBooks", licence="educational", *, masters=False, bills=False) -> None:
    """Company B as a clean `setup-b` leaves it: every written voucher on record with its flags, BooksFrom
    1-Apr-2022. It goes straight into state, NOT through the loader (read-probe tests only; the loader has its own
    tests). Skipped vouchers (C36) are absent. A cancelled voucher keeps no bill postings, as `_voucher` does.
    With `masters=True` it also puts company B's masters in state the way the loader leaves
    them: custom groups, units, stock items with signed opening values (C39) in their first unit (C40), ledgers with
    signed openings (C30), the opening bill Op/2022-001, and Tally's own Profit & Loss A/c. Company A's seed ledgers
    are replaced.
    With `bills=True` (probe 23 B) every bill is on record the way Tally
     keeps it: each New Ref opened by a written, unflagged voucher with its signed amount (the party line's sign;
     C34: − = receivable), its bill date and credit period; each Agst Ref added to its bill; the opening bill. The
     vouchers' postings then carry the bill date and credit period too."""
    from probes.setup.company_b_data import (FOREX_BASE_SYMBOL, FOREX_FORM, OPENING_BILL_DATE,
                                                SALES_GST_VOUCHER_TYPE, generate, quantity_unit)
    from probes.setup.writes import ForexLine, forex_amount_text
    data = generate(licence)

    def line_of(state, v, l) -> dict[str, str]:
        """A seeded line as the loader's import leaves it — a forex voucher's line through the same
        `_forex_line_text` an import uses (plan part 7 Task 3.8), so the export knobs apply to seeded vouchers too."""
        line = {"ledger": l.ledger, "amount": f"{l.amount:.2f}", "deemed_positive": "Yes" if l.deemed_positive else "No"}
        if v.currency != "INR":
            sent = forex_amount_text(l.amount, ForexLine(v.currency_symbol, v.fx_amount, v.fx_rate, form=FOREX_FORM,
                                                         base_symbol=FOREX_BASE_SYMBOL))
            line.update(books._forex_line_text(state, l.amount, parse_forex_amount(sent)))
        return line

    def posting(v, b) -> dict[str, str]:
        entry = {"name": b.name, "type": b.bill_type, "amount": f"{b.amount:.2f}"}
        if bills and b.credit_period:
            entry.update(date=v.date.strftime("%Y%m%d"), credit_period=b.credit_period)
        return entry

    def fill(state: dict) -> None:
        state["books_from"] = "20220401"
        if masters:                                   # plan part 7: `$` made in the UI before setup-b (live B has it)
            for c in data.currencies:
                state.setdefault("currencies", {})[c.symbol] = dict(USD_CURRENCY_ROW)
        for v in data.vouchers:
            if v.skip_reason:
                continue
            mid = str(state["next_master_id"])
            state["next_master_id"] += 1
            state["vouchers"][mid] = {
                "narration": v.narration, "date": v.date.strftime("%Y%m%d"), "post_dated": "No",
                "cancelled": "Yes" if v.cancelled else "No", "optional": "Yes" if v.optional else "No",
                "vch_type": v.vch_type,
                "lines": [line_of(state, v, l) for l in v.lines],
                "inventory": [{"item": i.item, "qty": f"{i.qty if v.kind == 'purchase' else -i.qty}"}
                              for i in v.inventory],
                "bills": [] if v.cancelled else [posting(v, b) for b in v.bills]}
        if masters:
            state["groups"] = {g.name: {"parent": g.parent} for g in data.groups}
            state["units"] = {u.name: {"base": u.first_unit, "additional": u.second_unit,
                                       "conversion": str(u.conversion) if u.conversion else None} for u in data.units}
            state["items"] = {}
            for item in data.items:
                unit = quantity_unit(data.units, item.unit)
                has = item.opening_qty is not None and item.opening_rate is not None
                state["items"][item.name] = {
                    "parent": "", "base_units": item.unit, "qty_unit": unit,
                    "opening_qty": f"{item.opening_qty} {unit}" if has else "",
                    "opening_rate": f"{item.opening_rate:.2f}/{unit}" if has else "",
                    "opening_value": f"{-(item.opening_qty * item.opening_rate):.2f}" if has else "0.00"}
            state["ledgers"] = {"Profit & Loss A/c": {"parent": "Primary", "email": "", "alter_id": 100,
                                                      "guid": f"{state['guid']}-b0000000", "opening": "0.00"}}
            for n, led in enumerate(data.ledgers, start=1):
                state["ledgers"][led.name] = {"parent": led.parent, "email": "", "alter_id": 100 + n,
                                              "guid": f"{state['guid']}-b{n:07x}",
                                              "opening": f"{led.opening:.2f}" if led.opening is not None else "0.00"}
                if led.currency:                                                                   # plan part 7
                    state["ledgers"][led.name]["currency"] = led.currency
                if led.opening_bill and led.opening is not None:
                    state.setdefault("bills", {})[led.opening_bill] = {
                        "party": led.name, "amount": f"{led.opening:.2f}", "opening": True,
                        "date": OPENING_BILL_DATE.strftime("%Y%m%d")}
            if SALES_GST_VOUCHER_TYPE not in state["voucherTypes"]:
                state["voucherTypes"].append(SALES_GST_VOUCHER_TYPE)
            state["voucher_type_parents"] = {SALES_GST_VOUCHER_TYPE: "Sales"}
        if bills:
            book = state.setdefault("bills", {})
            for led in data.ledgers:
                if led.opening_bill and led.opening is not None:
                    book.setdefault(led.opening_bill, {"party": led.name, "amount": f"{led.opening:.2f}",
                                                       "opening": True,
                                                       "date": OPENING_BILL_DATE.strftime("%Y%m%d")})
            for v in sorted(data.vouchers, key=lambda v: (v.date, v.tag)):
                if v.skip_reason or v.cancelled or v.optional:           # C42: flagged vouchers post no bill
                    continue
                party = next((line for line in v.lines if line.ledger == v.party), None)
                sign = Decimal("1") if party is None or party.amount > 0 else Decimal("-1")
                for b in v.bills:
                    if b.bill_type == "New Ref":
                        book[b.name] = {"party": v.party, "amount": f"{sign * b.amount:.2f}",
                                        "date": v.date.strftime("%Y%m%d"), "credit_period": b.credit_period or ""}
                    elif b.bill_type == "Agst Ref":
                        target = book.setdefault(b.name, {"party": v.party, "amount": "0.00"})
                        target["amount"] = f"{Decimal(target['amount']) + sign * b.amount:.2f}"
    books.edit_state(fill)


def seed_company_c(books: "FakeBooks") -> None:
    """Company C as `setup-c` leaves it (plan part 6): Tally's own Cash and Profit & Loss A/c, the one expense
    ledger and the one Payment voucher, books from 1-Apr-2025. Straight into state (read-probe tests only)."""
    from probes.companies import (COMPANY_C_LEDGER, COMPANY_C_LEDGER_PARENT, COMPANY_C_VOUCHER_AMOUNT,
                                     COMPANY_C_VOUCHER_DATE, COMPANY_C_VOUCHER_NARRATION)

    def fill(state: dict) -> None:
        guid = state["guid"]
        state["books_from"] = COMPANY_C_VOUCHER_DATE
        state["last_voucher_date"] = COMPANY_C_VOUCHER_DATE
        state["ledgers"] = {
            "Cash": {"parent": "Cash-in-Hand", "email": "", "alter_id": 10, "guid": f"{guid}-c000000a", "opening": "0.00"},
            "Profit & Loss A/c": {"parent": "Primary", "email": "", "alter_id": 11, "guid": f"{guid}-c000000b",
                                  "opening": "0.00"},
            COMPANY_C_LEDGER: {"parent": COMPANY_C_LEDGER_PARENT, "email": "", "alter_id": 12,
                               "guid": f"{guid}-c000000c", "opening": "0.00"}}
        state["vouchers"] = {"1": {
            "narration": COMPANY_C_VOUCHER_NARRATION, "date": COMPANY_C_VOUCHER_DATE, "post_dated": "No",
            "cancelled": "No", "optional": "No", "vch_type": "Payment",
            "lines": [{"ledger": COMPANY_C_LEDGER, "amount": f"-{COMPANY_C_VOUCHER_AMOUNT}", "deemed_positive": "Yes"},
                      {"ledger": "Cash", "amount": COMPANY_C_VOUCHER_AMOUNT, "deemed_positive": "No"}],
            "inventory": []}}
    books.edit_state(fill)


class FakeBooks:
    """Tally running or not, a loaded company, a licence box (`click_polls`), a busy load (`busy_polls`), a modal."""

    def __init__(self, folder: Path | None = None, *, name: str = SEED_COMPANY, running: bool = True,
                 loaded: bool = True, educational: bool = True, click_polls_on_load: int = 0,
                 busy_polls_on_load: int = 0, drop_flags: bool = False, fail_imports: bool = False,
                 current_period: tuple[str, str] = CURRENT_PERIOD, ledger_svtodate_honoured: bool = False,
                 ledger_opening_scope: str = "books", ledger_svfromdate_wedges: bool = True,
                 ledger_opening_bills_exported: bool = True, opening_stock_row: bool = False,
                 honour_company_var: bool = False, tolerate_raw_ampersand: bool = False,
                 hindi_ledger_filter_matches: bool = True,
                 cancelled_vouchers_listed: bool = True, optional_vouchers_listed: bool = True,
                 bill_credit_period_exported: bool = True, bill_due_from_credit_period: bool = True,
                 voucher_type_parent_exported: bool = True, stock_opening_scope: str = "current",
                 bill_due_offset_days: int = 0, header_lists_flagged: bool = True,
                 forex_currency_create: str = "refuse", forex_storage: str = "expression",
                 forex_forms_accepted: tuple[str, ...] = ("full", "no_base"), forex_on_base_party: str = "same",
                 forex_export_form: str = "full", forex_ledger_closing: str = "expression", deletes_stick: bool = True,
                 refuse_narrations: tuple[str, ...] = (), forex_currency_listed: bool = True,
                 ledger_currency_sticks: bool = True, forex_rate_symbols_refused: tuple[str, ...] = ("?",),
                 forex_ledger_revaluation: str = "latest_rate", forex_ledger_opening: str = "expression",
                 forex_tb_row: str = "plain", tb_fy_scoped: bool = False, unadjusted_forex_row: bool = False):
        self.folder = folder
        # plan part 7 (probe 22). Task 3.9 pinned the defaults to the live read-back on company B (2026-09-25:
        # forex_shape_2026-09-25_run2/, run1_currency_refused/; test_fake_books_forex.py compares them with the
        # capture). Each knob says "live" or "candidate"; the candidate values stay only so every probe-22 branch has a
        # test (the part-6 pattern).
        # live — run1: "refuse" (EXCEPTIONS=1, no LINEERROR; `$` then had to be created in the UI). "ok" | "popup" (a
        # modal: the create times out) are candidates, kept for the shape runner's own tests.
        self.forex_currency_create = forex_currency_create
        # live — "expression" (V1b/V3 kept the forex text). "plain" (accepted, but stored as plain INR — the C36
        # failure) | "refuse" (EXCEPTIONS=1) are candidates.
        self.forex_storage = forex_storage
        # which AMOUNT forms are accepted: "full" (`… = base`, F1) is live (V1b); "no_base" (F2) is a candidate (V2
        # never ran, because V1b stored). Others get EXCEPTIONS=1.
        self.forex_forms_accepted = tuple(forex_forms_accepted)
        # a forex voucher whose PARTY ledger has NO currency: live — "same" (V3 kept like any forex voucher).
        # "refuse" (EXCEPTIONS=1) | "plain" (accepted, every line stored as plain INR) are candidates.
        self.forex_on_base_party = forex_on_base_party
        # how a kept forex line exports: live — "full" in the live layout (`_forex_line_text`). "no_base" |
        # "plain_plus_field" (plain INR AMOUNT + a hypothesis FOREXAMOUNT field, only to cover probe 22's "field"
        # route) are candidates.
        self.forex_export_form = forex_export_form
        # a currency ledger's ClosingBalance: live — "expression" (C47, 2026-09-25: `-$1609.71 @ ? 82.58/$ =
        # -? 132929.85` after setup-b). "plain" (a number) is a candidate.
        self.forex_ledger_closing = forex_ledger_closing
        # a currency ledger's OpeningBalance: live — "expression" (C47 review I2, p22_B_usd_ledger.xml:52: the
        # FY-scoped opening `-$1609.71 @ ? 82.58/$ = -? 132929.85`, the same form as the closing). It only shows when
        # the opening carries forex lines, i.e. under `ledger_opening_scope="fy"` (live); the books-scope opening is
        # the master's own INR opening. "plain" (a number) is a candidate.
        self.forex_ledger_opening = forex_ledger_opening
        # C47, live 2026-09-25: "latest_rate" — a currency ledger's balance is its face total × the rate of its
        # latest-dated forex voucher, not the sum of the INR bases (Export Sales keeps the bases, so the TB is out by
        # the unrealised difference). "bases" (the plain sum) is a candidate.
        self.forex_ledger_revaluation = forex_ledger_revaluation
        # False = a voucher delete answers DELETED=1 but the voucher stays (plan part 7 fact 3 / Review Focus 2).
        self.deletes_stick = deletes_stick
        # A test seam: a voucher whose NARRATION contains one of these gets EXCEPTIONS=1 ("this shape is refused").
        self.refuse_narrations = tuple(refuse_narrations)
        # candidate (review I5): False = a Currency create answers CREATED=1 but the master never shows in the list.
        self.forex_currency_listed = forex_currency_listed
        # candidate (review I3): False = a ledger create answers CREATED=1 but CURRENCYNAME is silently dropped.
        # Live: True (ledgers_after_create.xml, CURRENCYNAME "$").
        self.ledger_currency_sticks = ledger_currency_sticks
        # live — V1: a rate written WITH the base symbol "?" (`@ ?82.99/$`) got EXCEPTIONS=1, no LINEERROR; V1b's bare
        # rate stored. () is the candidate "every symbol accepted".
        self.forex_rate_symbols_refused = tuple(forex_rate_symbols_refused)
        # plan part 6. Recorded live: cancelled vouchers are listed with ISCANCELLED=Yes and New Ref bills export
        # BILLCREDITPERIOD (p21_B_fy2022_month_02.xml). Hypotheses measured live by probes 3 B / 23 B / 25 B:
        # optional vouchers listed, BILLDUE = bill date + credit period, a custom voucher type exports its Parent.
        self.cancelled_vouchers_listed = cancelled_vouchers_listed
        self.optional_vouchers_listed = optional_vouchers_listed
        # probe 3 B / review I3: False = 3 B's plain Voucher header collection leaves out cancelled and optional
        # vouchers while probe 5's month request still returns them (a Day Book-like default, unmeasured).
        self.header_lists_flagged = header_lists_flagged
        self.bill_credit_period_exported = bill_credit_period_exported
        self.bill_due_from_credit_period = bill_due_from_credit_period
        # probe 23 B / Ruling S4: a due rule other than bill date + credit days (e.g. -1 = one day short).
        self.bill_due_offset_days = bill_due_offset_days
        self.voucher_type_parent_exported = voucher_type_parent_exported
        # probe 11 / C46 (live 2026-09-24): "current" (the default, review M4) = StockItem opening fields are the
        # current period's opening; "books" = the books-beginning opening (the pre-C46 hypothesis, opt-in).
        self.stock_opening_scope = stock_opening_scope

        # probe 16: the 2026-09-23 untyped evidence; the typed form is re-measured live
        self.ledger_svtodate_honoured = ledger_svtodate_honoured
        self.ledger_opening_scope = ledger_opening_scope              # probe 16 B
        self.ledger_svfromdate_wedges = ledger_svfromdate_wedges      # LESSONS §15 rule 17
        self.ledger_opening_bills_exported = ledger_opening_bills_exported   # probe 11
        self.opening_stock_row = opening_stock_row    # LESSONS §15 rule 19; off so the loader's tests keep their TB
        self.honour_company_var = honour_company_var                  # probe 14
        self.tolerate_raw_ampersand = tolerate_raw_ampersand          # probe 14
        # I1: a hypothesis that a `$Name = "<non-ASCII>"` TDL formula filter can fail to match on live Tally (never
        # measured) — False models the filter returning nothing for a non-ASCII `wanted`, so probe 15 must fall back
        # to an unfiltered read rather than record a false text FAILED.
        self.hindi_ledger_filter_matches = hindi_ledger_filter_matches
        self.current_period = current_period  # C33: what an untyped (ignored) period variable reads instead
        # S1 task 0 (fixture gap G2, UNMEASURED): how a currency ledger's row reads in an ISLEDGERWISE Trial Balance —
        # "plain" (the revalued INR number) or "expression" (the C47 ClosingBalance form). The capture decides.
        self.forex_tb_row = forex_tb_row
        # S1 task 10c (parity on FakeBooks B, pre-flight F9). Both knobs model what the LIVE FY 2025-26 company-B
        # Trial Balances show (s1_B_tb_ledger_2025-04-01_2026-03-31.xml, s1_B_tb_group_asof_2026-03-31.xml) and stay
        # off by default so every earlier test keeps its TB bytes:
        # - tb_fy_scoped: nominal (P&L) ledgers restart each FY -- their rows are the FY's own movement, and the
        #   earlier FYs' profit sits on a top-level `Profit & Loss A/c` row (live: Domestic Sales 10,84,724.72 for
        #   the FY only; `Profit & Loss A/c` Cr 14,40,883.90).
        # - unadjusted_forex_row: the synthetic top-level `Unadjusted Forex Gain/Loss` row = minus the unrealised
        #   revaluation of the currency ledgers (live: -183.87 on both TBs; p18_B_tb_asof_2023-03-31.xml too).
        self.tb_fy_scoped = tb_fy_scoped
        self.unadjusted_forex_row = unadjusted_forex_row
        self._memory = seed_state(name)
        self.running = running
        self.loaded = loaded
        self.educational = educational
        self.click_polls_on_load = click_polls_on_load
        self.busy_polls_on_load = busy_polls_on_load
        self.click_polls = 0
        self.busy_polls = 0
        self.popup = False
        # probe 24 / review I1: a login / TallyVault prompt that still LISTS the company while every named read fails
        # (unmeasured; the prompt shape probe 24 must not mistake for "export doesn't work").
        self.popup_listed = False
        self.requests: list[str] = []
        self.drop_flags = drop_flags          # a voucher import "succeeds" but ISCANCELLED/ISOPTIONAL don't stick
        # A test seam for "something changes at request N", e.g. Tally's modal appearing partway through a run:
        # called with each request body before it is answered.
        self.before_request: Callable[[str], None] | None = None
        self.fail_imports = fail_imports      # every import fails, modelling a report/company-level write refusal

    # --- company data ---------------------------------------------------------------------------------------------
    @property
    def state(self) -> dict:
        """A read-only snapshot (a copy, in both modes): callers that want to read must not accidentally mutate
        the live state by holding onto what `.state` returns. Use `edit_state` to mutate."""
        if self.folder is None:
            return copy.deepcopy(self._memory)
        return json.loads((self.folder / STATE_FILE).read_text(encoding="utf-8"))

    def edit_state(self, mutate: Callable[[dict], None]) -> None:
        """Mutate the live state and persist it — in both memory and folder-backed modes. `.state` returns a
        copy on purpose (Ruling C11/M5: a snapshot that silently discards `books.state[...] = ...` is a trap —
        it bit test setup code once already), so test setup and 'something vanished behind our back' scenarios
        go through this instead: `books.edit_state(lambda s: s["groups"].__setitem__(name, {...}))`."""
        state = self.state
        mutate(state)
        self._save(state)

    def _save(self, state: dict) -> None:
        if self.folder is None:
            self._memory = state
        else:
            (self.folder / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    # --- process lifecycle (driven by FakeRunner) ---------------------------------------------------------------------
    def start(self, load: bool) -> None:
        self.running, self.loaded, self.popup, self.popup_listed = True, load, False, False
        self.click_polls = self.click_polls_on_load if load else 0
        self.busy_polls = self.busy_polls_on_load if load else 0

    def stop(self) -> None:
        self.running = self.loaded = self.popup = self.popup_listed = False

    def companies(self) -> list[str]:
        """What a company-list request sees; while the licence box is up the list is empty."""
        if not self.loaded:
            return []
        if self.click_polls > 0:
            self.click_polls -= 1
            return []
        return [self.state["name"]]

    # --- the XML server -------------------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8")
            self.requests.append(body)
            if self.before_request is not None:
                self.before_request(body)
            if not self.running:
                raise httpx.ConnectError("connection refused", request=request)
            if self.popup:
                raise httpx.ReadTimeout("a modal is open", request=request)
            if request.method == "GET":
                return httpx.Response(200, content=b"<RESPONSE>TallyPrime Server is Running</RESPONSE>")
            if self.busy_polls > 0 and COMPANY_LIST_MARKER in body:
                self.busy_polls -= 1
                raise httpx.ReadTimeout("loading the company", request=request)
            return httpx.Response(200, content=self._answer(body, request).encode("utf-8"))

        return httpx.MockTransport(handle)

    def _answer(self, body: str, request: httpx.Request) -> str:
        if "<TYPE>Function</TYPE>" in body:
            return ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS><PRODMAJORREL>7</PRODMAJORREL>"
                    f"<PRODMINORREL>0</PRODMINORREL></HEADER><BODY><DATA><RESULT>{'Yes' if self.educational else 'No'}"
                    "</RESULT></DATA></BODY></ENVELOPE>")
        if COMPANY_LIST_MARKER in body:
            return company_list_xml(self.companies())
        if self.popup_listed:
            return ("<ENVELOPE><BODY><DATA><LINEERROR>Could not find Company "
                    f"'{esc(self.state['name'])}'</LINEERROR></DATA></BODY></ENVELOPE>")
        if not self.loaded or self.click_polls > 0:
            return "<ENVELOPE></ENVELOPE>"
        if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in body:
            return self._import(body, request)
        if "<TALLYREQUEST>Export</TALLYREQUEST>" in body:
            try:
                ET.fromstring(body)
            except ET.ParseError:
                if not self.tolerate_raw_ampersand:
                    return MALFORMED_ANSWER
                body = _BARE_AMP.sub("&amp;", body)
            company = _COMPANY_VAR.search(body)
            wanted_company = html.unescape(company.group(1)) if company else ""
            if self.honour_company_var and wanted_company and wanted_company != self.state["name"]:
                return ("<ENVELOPE><BODY><DATA><LINEERROR>Could not find Company "
                        f"'{esc(wanted_company)}'</LINEERROR></DATA></BODY></ENVELOPE>")
        state = self.state
        if "S0CompanyCounters" in body:
            return objects_xml("COMPANY", [{
                "Name": state["name"], "GUID": state["guid"], "AltVchId": str(state["alt_vch"]),
                "AltMstId": str(state["alt_mst"]), "BooksFrom": state.get("books_from", "20250401"),
                "LastVoucherDate": state["last_voucher_date"], "AlterID": str(state["alt_mst"])}])
        if "S0ActiveCompany" in body:
            # Probe 2's confirmed active-company read (candidate a): Company collection filtered to ##SVCurrentCompany.
            return objects_xml("COMPANY", [{"Name": state["name"], "GUID": state["guid"]}])
        period = requested_period(body, self.current_period, educational=self.educational)
        in_period = {mid: v for mid, v in state["vouchers"].items()
                     if period[0] <= (_yyyymmdd(v["date"]) or v["date"]) <= period[1]}
        if "<TYPE>Voucher</TYPE>" in body and ("S0VoucherMonth" in body or "S0P05MonthFormula" in body
                                               or "S0FxDay" in body):
            # Probe 5's month request and its formula candidate: full exports, bounded ONLY by the typed period
            # (C33: an untyped one reads current_period). The fake does not evaluate the formula; typed dates always
            # bound here, so probe 5 never needs it against FakeBooks (its formula path is tested with FakeTally).
            return vouchers_xml([_export_voucher(state, mid, v, credit_periods=self.bill_credit_period_exported)
                                 for mid, v in sorted(in_period.items(), key=lambda kv: int(kv[0]))
                                 if self._listed(v)])
        if "S0BCurrencies" in body or "S0P22Currencies" in body:          # plan part 7 (candidate fields)
            return objects_xml("CURRENCY", [{"Name": n, **{k: v for k, v in c.items() if k != "hidden"}}
                                            for n, c in state.get("currencies", {}).items() if not c.get("hidden")])
        if "S0BLedgerDetail" in body:                                          # plan part 7
            match = re.search(r'\$Name = "([^"]*)"', body)
            wanted = html.unescape(match.group(1)) if match else ""
            led = state["ledgers"].get(wanted)
            if led is None:
                return objects_xml("LEDGER", [])
            closing = self._ledger_balances(state, up_to=self.current_period[1]).get(wanted, Decimal("0.00"))
            return objects_xml("LEDGER", [{
                "Name": wanted, "Parent": led["parent"], "CurrencyName": led.get("currency", ""),
                "IsBillWiseOn": led.get("bill_wise", ""), "OpeningBalance": led.get("opening", "0.00"),
                "ClosingBalance": self._closing_text(state, wanted, closing, up_to=self.current_period[1])}])
        if "S0FxNumbers" in body:                         # plan part 7 review M6: header fields only
            return objects_xml("VOUCHER", [{"MasterID": mid, "VoucherNumber": v.get("number", mid), "AlterID": mid,
                                            "Date": v["date"], "VoucherTypeName": v.get("vch_type", ""),
                                            "Narration": v["narration"]}
                                           for mid, v in sorted(in_period.items(), key=lambda kv: int(kv[0]))])
        if "S0OpVouchers" in body:
            return objects_xml("VOUCHER", [{"MasterId": mid, "Narration": v["narration"], "Date": v["date"],
                                            "IsPostDated": v["post_dated"]} for mid, v in in_period.items()])
        if "S0LedgerList" in body:
            return objects_xml("LEDGER", [{"Name": n, "Parent": led["parent"]} for n, led in state["ledgers"].items()])
        if "S0OpLedger" in body or "S0OneLedger" in body or "S0SignCheckOpening" in body:
            match = re.search(r'\$Name = "([^"]*)"', body)
            wanted = html.unescape(match.group(1)) if match else ""
            led = state["ledgers"].get(wanted)
            rows = [] if led is None else [{"Name": wanted, "Parent": led["parent"], "Email": led["email"],
                                            "GUID": led["guid"], "AlterID": str(led["alter_id"]),
                                            "OpeningBalance": led.get("opening", "0.00")}]
            return objects_xml("LEDGER", rows)
        if "S0BGroups" in body:
            return objects_xml("GROUP", [{"Name": n, "Parent": g["parent"]} for n, g in state["groups"].items()])
        if "S0BUnits" in body:
            return objects_xml("UNIT", [{"Name": n, "Base": u["base"] or "", "Conversion": u["conversion"] or ""}
                                        for n, u in state["units"].items()])
        if "S0BItems" in body:
            return objects_xml("STOCKITEM", [{"Name": n, "Parent": i["parent"], "BaseUnits": i["base_units"]}
                                             for n, i in state["items"].items()])
        if "S0BLedgers" in body:
            return objects_xml("LEDGER", [{"Name": n, "Parent": led["parent"]} for n, led in state["ledgers"].items()])
        if "S0BVouchers" in body:
            return objects_xml("VOUCHER", [{"MasterId": mid, "Narration": v["narration"], "Date": v["date"],
                                            "IsPostDated": v["post_dated"], "IsCancelled": v["cancelled"],
                                            "IsOptional": v["optional"]} for mid, v in in_period.items()])
        if "S0BVoucherTypes" in body:
            return objects_xml("VOUCHERTYPE", [{"Name": n} for n in state["voucherTypes"]])
        if "<ID>Trial Balance</ID>" in body:
            if "<ISLEDGERWISE>Yes</ISLEDGERWISE>" in body:
                return tb_xml(self._ledger_level_tb_rows(state, as_on=period[1]))
            return tb_xml(self._trial_balance_rows(state, as_on=period[1]))
        if "<ID>Bills Receivable</ID>" in body:
            return self._bills_report(state, receivable=True, as_on=period[1])
        if "<ID>Bills Payable</ID>" in body:
            return self._bills_report(state, receivable=False, as_on=period[1])
        if "<ID>Stock Summary</ID>" in body:
            return self._stock_summary(state, period[1])
        generic = self._b_collection(state, body, period, request)
        if generic is not None:
            return generic
        return "<ENVELOPE></ENVELOPE>"

    def _bucket_of(self, state: dict, ledger_parent: str) -> str:
        """One hop through a custom group this fake was asked to create (e.g. National Creditors -> Sundry
        Creditors); anything else is already at the reserved-group granularity."""
        group = state["groups"].get(ledger_parent)
        if group and group["parent"] in ("", "Primary"):     # a custom TOP-LEVEL group is its own primary (10c)
            return ledger_parent
        return group["parent"] if group else ledger_parent

    def _trial_balance_rows(self, state: dict, as_on: str = "99991231") -> list[tuple[str, str, str]]:
        """An exploded-to-two-levels Trial Balance (EXPLODEFLAG=Yes shape, probe 17), computed from whatever
        ledgers/groups/vouchers this fake actually has on record — not from any dataset's idea of what should
        be there. Real Tally XML signs a debit-natured closing balance negative and a credit-natured one
        positive (verified against tests/fixtures/tally_samples/trial_balance_live.xml); `_dr_cr` mirrors that.

        C30 (overturns F11/C21): a ledger's `OPENINGBALANCE` on record is SIGNED exactly as it came over the wire,
        and real Tally reads that sign — negative = Dr, positive = Cr — whatever the parent group (company A's
        abs()'d bank openings landed as credits). So this fake takes the sign as given and never re-derives it
        from the group's nature.
        """
        # C42: flagged vouchers post nothing; C33: closing as on the (typed, or current-period) SVTODATE; C47: a
        # currency ledger at its latest voucher rate — all in `_ledger_balances`.
        balances = self._tb_balances(state, as_on)
        buckets: dict[str, Decimal] = {}
        top_ledgers: list[tuple[str, Decimal]] = []
        for name, led in state["ledgers"].items():
            if self.tb_fy_scoped and led["parent"] in ("", "Primary"):    # live: its own top-level ledger row
                if balances.get(name, Decimal("0.00")):
                    top_ledgers.append((name, balances[name]))
                continue
            bucket = self._bucket_of(state, led["parent"])
            buckets[bucket] = buckets.get(bucket, Decimal("0.00")) + balances.get(name, Decimal("0.00"))
        primaries: dict[str, Decimal] = {}
        # C39 (live 2026-09-24): an item's OPENINGVALUE sits in Current Assets by its WIRE sign — +10200 landed Cr,
        # −10200 Dr and the TB closed. Stock movements from vouchers are not valued here (the fake keeps no stock).
        stock = sum((Decimal(i.get("opening_value") or "0.00") for i in state["items"].values()), Decimal("0.00"))
        if stock:
            primaries["Current Assets"] = stock
        for bucket, value in buckets.items():
            primary = RESERVED_GROUP_PARENTS.get(bucket, bucket)
            primaries[primary] = primaries.get(primary, Decimal("0.00")) + value
        rows = list(primaries.items())
        rows += [(bucket, value) for bucket, value in buckets.items()
                if RESERVED_GROUP_PARENTS.get(bucket, bucket) != bucket]
        rows += top_ledgers
        if stock and self.opening_stock_row:
            rows.append(("Opening Stock", stock))
        rows += self._unadjusted_rows(state, as_on)
        return [(name, *_dr_cr(value)) for name, value in rows]

    def _ledger_level_tb_rows(self, state: dict, as_on: str) -> list[tuple[str, str, str]]:
        """The ISLEDGERWISE Trial Balance (probe 17's confirmed request): one flat row per ledger with a balance as on
        `as_on` (a currency ledger per the `forex_tb_row` knob), plus Opening Stock when the company holds stock."""
        balances = self._tb_balances(state, as_on)
        rows: list[tuple[str, str, str]] = []
        stock = sum((Decimal(i.get("opening_value") or "0.00") for i in state["items"].values()), Decimal("0.00"))
        if stock:
            rows.append(("Opening Stock", *_dr_cr(stock)))
        for name in sorted(state["ledgers"]):
            value = balances.get(name, Decimal("0.00"))
            if value == 0:
                continue
            debit, credit = _dr_cr(value)
            if self.forex_tb_row == "expression" and state["ledgers"][name].get("currency"):
                text = self._forex_balance_text(state, name, value, up_to=as_on)
                debit, credit = (text, "") if value < 0 else ("", text)
            rows.append((name, debit, credit))
        rows += [(name, *_dr_cr(value)) for name, value in self._unadjusted_rows(state, as_on)]
        return rows

    def _tb_balances(self, state: dict, as_on: str) -> dict[str, Decimal]:
        """The ledger balances a Trial Balance as on `as_on` shows: `_ledger_balances`, and -- under `tb_fy_scoped`
        (live) -- each nominal ledger reduced to its own FY's movement, the earlier FYs' total moved onto the
        top-level `Profit & Loss A/c`."""
        balances = self._ledger_balances(state, up_to=as_on)
        if not self.tb_fy_scoped:
            return balances
        prior = self._ledger_balances(state, up_to="99991231", before=_fy_start(as_on))
        moved = Decimal("0.00")
        for name, led in state["ledgers"].items():
            if self._primary_of(state, led["parent"]) in NOMINAL_PRIMARIES:
                balances[name] = balances.get(name, Decimal("0.00")) - prior.get(name, Decimal("0.00"))
                moved += prior.get(name, Decimal("0.00"))
        pl = next((n for n, led in state["ledgers"].items() if n == "Profit & Loss A/c"), None)
        if pl is not None:
            balances[pl] = balances.get(pl, Decimal("0.00")) + moved
        return balances

    def _unadjusted_rows(self, state: dict, as_on: str) -> list[tuple[str, Decimal]]:
        """Under `unadjusted_forex_row` (live): the synthetic `Unadjusted Forex Gain/Loss` row -- minus Σ over the
        currency ledgers of (revalued balance − Σ INR bases of their forex lines), dated ≤ `as_on`; none when 0."""
        if not self.unadjusted_forex_row or self.forex_ledger_revaluation != "latest_rate":
            return []
        per: dict[str, list[tuple[str, int, Decimal, Decimal, Decimal]]] = {}
        for mid, v in state["vouchers"].items():
            day = _yyyymmdd(v["date"]) or v["date"]
            if _flagged(v) or day > as_on:
                continue
            for line in v.get("lines", []):
                if line.get("fx") and line.get("rate") and state["ledgers"].get(line["ledger"], {}).get("currency"):
                    per.setdefault(line["ledger"], []).append(
                        (day, int(mid), Decimal(line["fx"]), Decimal(line["rate"]), Decimal(line["amount"])))
        unrealised = Decimal("0.00")
        for lines in per.values():
            face = sum((fx for _, _, fx, _, _ in lines), Decimal("0.00"))
            revalued = (face * max(lines)[3]).quantize(Decimal("0.01"), ROUND_HALF_UP)
            unrealised += revalued - sum((base for *_, base in lines), Decimal("0.00"))
        return [("Unadjusted Forex Gain/Loss", -unrealised)] if unrealised else []

    def _all_groups(self, state: dict) -> dict[str, str]:
        return {**RESERVED_GROUPS, **{n: g["parent"] for n, g in state["groups"].items()}}

    def _primary_of(self, state: dict, group: str) -> str:
        parents, seen = self._all_groups(state), []
        while parents.get(group, "") not in ("", "Primary") and group not in seen:
            seen.append(group)
            group = parents[group]
        return group

    def _ledger_balances(self, state: dict, *, up_to: str, before: str | None = None) -> dict[str, Decimal]:
        """Opening + unflagged lines dated ≤ up_to (and < before, when given). C42: flagged vouchers post nothing.
        C47 (live 2026-09-25): under `forex_ledger_revaluation="latest_rate"` a currency ledger's forex lines count as
        their face total × the rate of the latest-dated one (HALF_UP to paise), not as their INR bases."""
        balances = {n: Decimal(l.get("opening") or "0.00") for n, l in state["ledgers"].items()}
        forex: dict[str, list[tuple[str, int, Decimal, Decimal]]] = {}
        for mid, v in state["vouchers"].items():
            day = _yyyymmdd(v["date"]) or v["date"]
            if _flagged(v) or day > up_to or (before is not None and day >= before):
                continue
            for line in v.get("lines", []):
                name = line["ledger"]
                if (self.forex_ledger_revaluation == "latest_rate" and line.get("fx") and line.get("rate")
                        and state["ledgers"].get(name, {}).get("currency")):
                    forex.setdefault(name, []).append((day, int(mid), Decimal(line["fx"]), Decimal(line["rate"])))
                    continue
                balances[name] = balances.get(name, Decimal("0.00")) + Decimal(line["amount"] or "0")
        for name, lines in forex.items():
            face = sum((fx for _, _, fx, _ in lines), Decimal("0.00"))
            rate = max(lines)[3]                                     # the latest-dated forex voucher's rate
            balances[name] = balances.get(name, Decimal("0.00")) + (face * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return balances

    def _b_collection(self, state: dict, body: str, period: tuple[str, str], request: httpx.Request) -> str | None:
        """Probes 11, 14, 15, 16 B, 18 B: Ledger / Group / StockItem collections, fields by NATIVEMETHOD, a
        `$Name = "…"` filter honoured. None = not one of these collections."""
        name = re.search(r"<ID>([^<]+)</ID>", body)
        if name is None or not name.group(1).startswith(B_PROBE_COLLECTIONS):
            return None
        kind = re.search(r"<COLLECTION [^>]*>\s*<TYPE>([^<]+)</TYPE>", body)
        kind = kind.group(1) if kind else ""
        fields = {f.lower() for f in re.findall(r"<NATIVEMETHOD>([^<]+)</NATIVEMETHOD>", body)}
        names = [html.unescape(n) for n in re.findall(r'\$Name = "([^"]*)"', body)]
        # One `$Name = "…"` filter → that name; several OR-ed (S1 task 0's touched-ledgers re-read) → the set.
        wanted: str | frozenset[str] | None = (None if not names else names[0] if len(names) == 1
                                               else frozenset(names))
        if kind == "Voucher":                              # probe 3 B: header fields only, typed period, knobs
            rows = [_voucher_header(state, mid, v)
                    for mid, v in sorted(state["vouchers"].items(), key=lambda kv: int(kv[0]))
                    if period[0] <= (_yyyymmdd(v["date"]) or v["date"]) <= period[1] and self._listed(v)
                    and (self.header_lists_flagged or not _flagged(v))]
            return objects_xml("VOUCHER", rows)
        if kind == "VoucherType":                          # probe 25 B
            parents = state.get("voucher_type_parents", {})
            reserved = state.get("voucher_type_reserved", {})    # a custom type exporting its base (unseen live)
            rows = []
            for vtype in state["voucherTypes"]:
                if vtype in parents:                       # a custom type: Parent = its base type, no ReservedName
                    rows.append({"Name": vtype, "Parent": parents[vtype] if self.voucher_type_parent_exported else "",
                                 "ReservedName": reserved.get(vtype, "")})
                else:                                      # live p25 A: a reserved type is its own Parent and ReservedName
                    rows.append({"Name": vtype, "Parent": vtype, "ReservedName": vtype})
            return objects_xml("VOUCHERTYPE", self._with_ids(state, "vt", rows, fields))
        if kind == "Currency":                             # S1 task 0 (G4)
            rows = [{"Name": n, **{k: v for k, v in c.items() if k != "hidden"}}
                    for n, c in state.get("currencies", {}).items() if not c.get("hidden")]
            return objects_xml("CURRENCY", self._with_ids(state, "cu", rows, fields))
        if kind == "StockGroup":                           # S1 task 0 (G4)
            rows = [{"Name": n, "Parent": ""} for n in state.get("stock_groups", [])]
            return objects_xml("STOCKGROUP", self._with_ids(state, "sg", rows, fields))
        if kind == "Unit":                                 # S1 task 0 (G4)
            rows = [{"Name": n, "IsSimpleUnit": "No" if u.get("additional") else "Yes", "BaseUnits": u.get("base") or "",
                     "AdditionalUnits": u.get("additional") or "", "Conversion": u.get("conversion") or ""}
                    for n, u in state.get("units", {}).items()]
            return objects_xml("UNIT", self._with_ids(state, "un", rows, fields))
        if kind == "Ledger":
            if self.ledger_svfromdate_wedges and "<SVFROMDATE" in body:
                self.popup = True                     # LESSONS §15 rule 17 (measured untyped, 2026-09-23)
                raise httpx.ReadTimeout("SVFROMDATE on a master collection", request=request)
            return self._ledger_export(state, fields, period, wanted)
        if kind == "Group":
            return objects_xml("GROUP", [{"Name": n, "Parent": p} for n, p in self._all_groups(state).items()])
        if kind == "StockItem":
            rows = []
            for n, i in state["items"].items():
                if wanted not in (None, n):
                    continue
                row = {"Name": n, "Parent": i.get("parent", ""), "BaseUnits": i.get("base_units", ""),
                       "OpeningBalance": i.get("opening_qty", ""), "OpeningRate": i.get("opening_rate", ""),
                       "OpeningValue": i.get("opening_value", "")}
                if self.stock_opening_scope == "current":          # C46: the current period's opening
                    qty, unit = self._stock_level(state, n, before=self.current_period[0]), i.get("qty_unit", "")
                    row.update(OpeningBalance=f"{qty} {unit}".strip(), OpeningRate=f"{FAKE_STOCK_RATE:.2f}/{unit}",
                               OpeningValue=f"{-(qty * FAKE_STOCK_RATE):.2f}")
                rows.append(row)
            return objects_xml("STOCKITEM", rows)
        return "<ENVELOPE></ENVELOPE>"

    @staticmethod
    def _with_ids(state: dict, prefix: str, rows: list[dict[str, str]], fields: set[str]) -> list[dict[str, str]]:
        """GUID / AlterID / MasterID on a master row, only when the request asks for them (S1 task 0, G4): synthetic
        but stable per (kind, position) — the live values are what the capture records."""
        out = []
        for n, row in enumerate(rows, start=1):
            ids = {"GUID": f"{state['guid']}-{prefix}{n:05x}", "AlterID": str(500 + n), "MasterID": str(900 + n)}
            out.append({**row, **{k: v for k, v in ids.items() if k.lower() in fields}})
        return out

    def _ledger_export(self, state: dict, fields: set[str], period: tuple[str, str],
                       wanted: str | frozenset[str] | None) -> str:
        closing_to = period[1] if self.ledger_svtodate_honoured else self.current_period[1]
        closing = self._ledger_balances(state, up_to=closing_to)
        before_fy = self._ledger_balances(state, up_to="99991231", before=_fy_start(closing_to))
        out = []
        for name, led in state["ledgers"].items():
            if isinstance(wanted, frozenset):
                if name not in wanted:
                    continue
            elif wanted is not None:
                if not self.hindi_ledger_filter_matches and not wanted.isascii():
                    continue        # I1 knob: the formula filter never matches a non-ASCII `wanted`
                if name != wanted:
                    continue
            opening_text = None
            if self.ledger_opening_scope == "fy":
                nominal = self._primary_of(state, led["parent"]) in NOMINAL_PRIMARIES
                opening = Decimal("0.00") if nominal else before_fy.get(name, Decimal("0.00"))
                if self.forex_ledger_opening == "expression":          # C47 review I2: the live opening form
                    opening_text = self._forex_balance_text(state, name, opening, up_to="99991231",
                                                            before=_fy_start(closing_to))
            else:
                opening = Decimal(led.get("opening") or "0.00")
            parts = [f"<NAME>{esc(name)}</NAME>"]
            if "guid" in fields:                                               # S1 task 0 (G4 / G5)
                parts.append(f"<GUID>{esc(led.get('guid', ''))}</GUID>")
            if "alterid" in fields:
                parts.append(f"<ALTERID>{led.get('alter_id', '')}</ALTERID>")
            if "masterid" in fields:
                parts.append(f"<MASTERID>{led.get('alter_id', '')}</MASTERID>")
            if "parent" in fields:
                parts.append(f"<PARENT>{esc(led['parent'])}</PARENT>")
            if "openingbalance" in fields:
                parts.append(f"<OPENINGBALANCE>{esc(opening_text or _amount_text(opening))}</OPENINGBALANCE>")
            if "currencyname" in fields:                   # plan part 7 (candidate: "" for a base-currency ledger)
                parts.append(f"<CURRENCYNAME>{esc(led.get('currency', ''))}</CURRENCYNAME>")
            if "closingbalance" in fields:
                parts.append(f"<CLOSINGBALANCE>"
                             f"{esc(self._closing_text(state, name, closing.get(name, Decimal('0.00')), up_to=closing_to))}"
                             "</CLOSINGBALANCE>")
            if "billallocations" in fields and self.ledger_opening_bills_exported:
                for bill_name, bill in state.get("bills", {}).items():
                    if bill.get("opening") and bill["party"] == name:
                        parts.append(f"<BILLALLOCATIONS.LIST><NAME>{esc(bill_name)}</NAME><BILLDATE>{bill['date']}"
                                     f"</BILLDATE><OPENINGBALANCE>{bill['amount']}</OPENINGBALANCE>"
                                     "</BILLALLOCATIONS.LIST>")
            out.append(f'<LEDGER NAME="{esc(name)}">{"".join(parts)}</LEDGER>')
        for dup in state.get("duplicate_ledgers", []):     # probe 25 B / R9: a second ledger with the same name
            if wanted is None or dup["name"] == wanted or (isinstance(wanted, frozenset) and dup["name"] in wanted):
                parent = f"<PARENT>{esc(dup['parent'])}</PARENT>" if "parent" in fields else ""
                out.append(f'<LEDGER NAME="{esc(dup["name"])}"><NAME>{esc(dup["name"])}</NAME>{parent}</LEDGER>')
        return f"<ENVELOPE><BODY><DATA><COLLECTION>{''.join(out)}</COLLECTION></DATA></BODY></ENVELOPE>"

    def _closing_text(self, state: dict, name: str, closing: Decimal, *, up_to: str) -> str:
        """A ledger's ClosingBalance text. Plain (`_amount_text`) unless the ledger has a currency and forex lines and
        `forex_ledger_closing == "expression"` — then the live C47 form (2026-09-25, ledger_details after setup-b):
        `-$1609.71 @ ? 82.58/$ = -? 132929.85` — face total @ the latest voucher rate = `closing` (the revalued
        balance from `_ledger_balances`), in the base currency's own prefix."""
        if self.forex_ledger_closing != "expression":
            return _amount_text(closing)
        return self._forex_balance_text(state, name, closing, up_to=up_to)

    def _forex_balance_text(self, state: dict, name: str, amount: Decimal, *, up_to: str,
                            before: str | None = None) -> str:
        """The live C47 expression for a currency ledger's balance `amount` over its forex lines dated ≤ up_to (and
        < before, when given — an opening): face total @ the latest voucher rate = amount. Plain when the ledger has
        no currency, no such lines, or a zero balance. Shared by the ClosingBalance and (review I2) OpeningBalance."""
        currency = state["ledgers"].get(name, {}).get("currency", "")
        if not currency or amount == 0:
            return _amount_text(amount)
        lines = [(day, int(mid), Decimal(line["fx"]), Decimal(line.get("rate") or "0"))
                 for mid, v in state["vouchers"].items()
                 for day in [_yyyymmdd(v["date"]) or v["date"]]
                 if not _flagged(v) and day <= up_to and (before is None or day < before)
                 for line in v.get("lines", []) if line["ledger"] == name and line.get("fx")]
        if not lines:
            return _amount_text(amount)
        fx_total = sum((fx for _, _, fx, _ in lines), Decimal("0.00"))
        sign, prefix = ("-" if amount < 0 else ""), self._base_prefix(state)
        return (f"{sign}{currency}{abs(fx_total):.2f} @ {prefix}{max(lines)[3]:.2f}/{currency} = "
                f"{sign}{prefix}{abs(amount):.2f}")

    @staticmethod
    def _base_prefix(state: dict) -> str:
        """The base currency's NAME as a forex export writes it, followed by a space when its HasSpace is Yes — live
        company B: "? " (variant_V1b.xml `@ ? 82.99/$ = -? 37216.04`; currencies_after.xml: NAME "?", HasSpace Yes)."""
        name, row = next(((n, c) for n, c in state.get("currencies", {}).items() if c.get("MailingName") == "INR"),
                         ("", {}))
        return name + (" " if name and row.get("HasSpace") == "Yes" else "")

    def _forex_line_text(self, state: dict, base: Decimal, fa: ForexAmount) -> dict:
        """How one KEPT forex line is stored and exported, by `forex_export_form`. The ONE place this text is built —
        `_voucher` (imports) and `seed_company_b` both call it, so seeded and imported forex lines cannot drift
        apart. Live layout (Task 3.9, forex_shape_2026-09-25_run2/variant_V1b.xml): Tally rewrites the rate and the
        base with the base currency's own prefix (`_base_prefix`) whatever symbol was sent — V1b sent none and got
        `-$448.44 @ ? 82.99/$ = -? 37216.04`. `forex_storage="plain"` (candidate, the C36 failure) keeps nothing:
        {} — the line stays plain INR, seeded or imported alike (pre-flight F3). `fx` (signed like the base) is data
        for `_closing_text`, never exported."""
        if self.forex_storage == "plain":
            return {}
        sign = "-" if base < 0 else ""
        prefix = self._base_prefix(state)
        face = f"{sign}{fa.currency}{abs(fa.fx):.2f}"
        out = {"fx": f"{sign}{abs(fa.fx):.2f}", "rate": f"{fa.rate}"}           # data (C47), never exported
        if self.forex_export_form == "plain_plus_field":
            out["extra"] = {"FOREXAMOUNT": face}
        elif self.forex_export_form == "no_base":
            out["amount_text"] = f"{face} @ {prefix}{fa.rate:.2f}/{fa.currency}"
        else:
            out["amount_text"] = f"{face} @ {prefix}{fa.rate:.2f}/{fa.currency} = {sign}{prefix}{abs(base):.2f}"
        return out

    def _forex_entries(self, state: dict, element: ET.Element) -> dict[str, ForexAmount] | None:
        """Plan part 7 (candidate rules): every ledger line whose AMOUNT is a forex expression. Refused (None →
        EXCEPTIONS=1) by `forex_storage="refuse"`, a form not in `forex_forms_accepted`, or — under
        `forex_on_base_party="refuse"` — a voucher whose PARTY ledger has no currency. Otherwise the line's AMOUNT is
        rewritten IN PLACE to its INR base, so the sign and balance checks see the base, and the kept lines
        (ledger → parsed text) are returned. The base-party rule keys on the voucher's party, not on each line's own
        ledger: the nominal Export Sales line never has a currency, and it carries the forex text on every variant."""
        party = element.findtext("PARTYLEDGERNAME") or ""
        base_party = bool(party) and not state["ledgers"].get(party, {}).get("currency")
        kept: dict[str, ForexAmount] = {}
        for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
            for entry in element.findall(tag):
                amount = entry.find("AMOUNT")
                fa = parse_forex_amount(amount.text if amount is not None else None)
                if fa is None:
                    continue
                ledger = entry.findtext("LEDGERNAME", "")
                form = "full" if fa.base is not None else "no_base"
                if (self.forex_storage == "refuse" or form not in self.forex_forms_accepted
                        or fa.rate_symbol in self.forex_rate_symbols_refused):
                    return None
                if base_party and self.forex_on_base_party == "refuse":
                    return None
                amount.text = f"{forex_base(fa)[0]:.2f}"
                if self.forex_storage == "plain" or (base_party and self.forex_on_base_party == "plain"):
                    continue
                kept[ledger] = fa
        return kept

    def _listed(self, v: dict) -> bool:
        if v.get("cancelled") == "Yes" and not self.cancelled_vouchers_listed:
            return False
        return not (v.get("optional") == "Yes" and not self.optional_vouchers_listed)

    @staticmethod
    def _stock_level(state: dict, name: str, *, before: str) -> Decimal:
        """Opening quantity + unflagged inventory moves dated before `before` (YYYYMMDD). C42: flagged move nothing."""
        words = (state["items"][name].get("opening_qty") or "").split()
        qty = Decimal(words[0]) if words else Decimal("0")
        for v in state["vouchers"].values():
            if _flagged(v) or (_yyyymmdd(v["date"]) or v["date"]) >= before:
                continue
            qty += sum((Decimal(i["qty"]) for i in v.get("inventory", []) if i["item"] == name), Decimal("0"))
        return qty

    def _bills_report(self, state: dict, *, receivable: bool, as_on: str) -> str:
        """Bills Receivable/Payable (C34: the bill's sign files it). A bill that knows its date shows it; BILLDUE is
        date + credit period when `bill_due_from_credit_period`, else the bill date. Rows of bills without a date are
        byte-identical to the old fakes.bills_xml output."""
        from datetime import datetime, timedelta
        rows = [_bill_row(ref, party, amount) for ref, party, amount in
                (state.get("bills_receivable", []) if receivable else [])]
        as_on_day = datetime.strptime(as_on, "%Y%m%d").date()
        for name, bill in _effective_bills(state).items():
            amount = Decimal(bill["amount"])
            if amount == 0 or (amount < 0) != receivable:
                continue
            if not bill.get("date"):
                rows.append(_bill_row(name, bill["party"], bill["amount"]))
                continue
            start = datetime.strptime(bill["date"], "%Y%m%d").date()
            match = _CREDIT_DAYS.match(bill.get("credit_period") or "")
            days = int(match.group(1)) + self.bill_due_offset_days if match and self.bill_due_from_credit_period else 0
            due = start + timedelta(days=days)
            rows.append(_bill_row(name, bill["party"], bill["amount"], _short_date(start), _short_date(due),
                                  str(max((as_on_day - due).days, 0))))
        return "<ENVELOPE>" + "".join(rows) + "</ENVELOPE>"

    @staticmethod
    def _stock_summary(state: dict, as_on: str) -> str:
        """Closing quantity per item as on `as_on` (the fake keeps no valuation, so values are blank)."""
        rows = []
        for name, item in state["items"].items():
            words = (item.get("opening_qty") or "").split()
            qty = Decimal(words[0]) if words else Decimal("0")
            for v in state["vouchers"].values():
                if _flagged(v) or (_yyyymmdd(v["date"]) or v["date"]) > as_on:
                    continue
                qty += sum((Decimal(i["qty"]) for i in v.get("inventory", []) if i["item"] == name), Decimal("0"))
            rows.append((name, f"{qty} {item.get('qty_unit', '')}".strip(), "", ""))
        return stock_summary_xml(rows)

    def _import(self, body: str, request: httpx.Request) -> str:
        if self.fail_imports:
            return import_result(errors=1, line_error="fake import failure")
        state = self.state
        element = next(iter(ET.fromstring(body).find(".//TALLYMESSAGE")))
        action = element.get("ACTION", "")
        if element.tag == "COMPANY":
            new_name = element.findtext("NAME")      # the NAME.LIST variant answers ALTERED=1 and changes nothing
            if new_name and element.get("NAME") == state["name"]:
                state["name"] = new_name
                self._save(state)
            return import_result(altered=1)
        if element.tag == "STOCKGROUP" and action == "Create":
            if element.get("NAME") in state["stock_groups"]:
                self.popup = True                    # LESSONS §15 rule 10: a duplicate create raises a blocking modal
                raise httpx.ReadTimeout("duplicate master modal", request=request)
            state["stock_groups"].append(element.get("NAME"))
            state["alt_mst"] += 1
            self._save(state)
            return import_result(created=1)
        if element.tag == "CURRENCY" and action == "Create":                    # plan part 7 (candidate)
            if self.forex_currency_create == "refuse":
                return import_result(exceptions=1, line_error="fake: currency refused")
            if self.forex_currency_create == "popup":
                self.popup = True
                raise httpx.ReadTimeout("currency create modal", request=request)
            state.setdefault("currencies", {})
            return self._create_master(state, "currencies", element, request,
                                        lambda el: {"MailingName": el.findtext("MAILINGNAME", ""),
                                                    "ExpandedSymbol": el.findtext("EXPANDEDSYMBOL", ""),
                                                    "DecimalSymbol": el.findtext("DECIMALSYMBOL", ""),
                                                    "DecimalPlaces": el.findtext("DECIMALPLACES", ""),
                                                    **({} if self.forex_currency_listed else {"hidden": True})})
        if element.tag == "LEDGER":
            return self._ledger(state, element, action, request)
        if element.tag == "VOUCHER":
            return self._voucher(state, element, action)
        if element.tag == "GROUP" and action == "Create":
            return self._create_master(state, "groups", element, request,
                                        lambda el: {"parent": el.findtext("PARENT", "")})
        if element.tag == "UNIT" and action == "Create":
            first, second = element.findtext("BASEUNITS"), element.findtext("ADDITIONALUNITS")
            if first is not None and first == second:
                # C31: the live answer to BASEUNITS=ADDITIONALUNITS=Nos (logs/setup-b-live-2026-09-24.log).
                return import_result(exceptions=1, line_error="Next Unit already contains the First unit!")
            missing = [u for u in (first, second) if u is not None and u not in state["units"]]
            if missing:                              # the fake's own wording — no live answer recorded for this
                return import_result(exceptions=1, line_error=f"fake: unit {missing[0]!r} does not exist")
            return self._create_master(state, "units", element, request,
                                        lambda el: {"base": el.findtext("BASEUNITS"),
                                                    "additional": el.findtext("ADDITIONALUNITS"),
                                                    "conversion": el.findtext("CONVERSION")})
        if element.tag == "STOCKITEM" and action == "Create":
            return self._create_master(state, "items", element, request,
                                        lambda el: {"parent": el.findtext("PARENT", ""),
                                                    "base_units": el.findtext("BASEUNITS", ""),
                                                    **self._stock_opening(state, el)})
        return import_result(errors=1, line_error=f"fake: unsupported {element.tag}")

    @staticmethod
    def _stock_opening(state: dict, element: ET.Element) -> dict[str, str]:
        """C40 (live 2026-09-24): an opening quantity written in a compound unit's FULL name ("15 Box of 10 Nos") is
        accepted (created=1) but stores NO opening — quantity blank, value 0. Written in its first unit ("15 Box")
        it is kept. C39: the value is kept with its wire sign."""
        qty = (element.findtext("OPENINGBALANCE") or "").strip()
        compounds = {name for name, unit in state["units"].items() if unit.get("additional")}
        if any(qty.endswith(f" {name}") for name in compounds):
            return {"opening_qty": "", "opening_value": "0.00"}
        return {"opening_qty": qty, "opening_value": element.findtext("OPENINGVALUE") or "0.00"}

    def _create_master(self, state: dict, collection: str, element: ET.Element, request: httpx.Request,
                       fields: Callable[[ET.Element], dict]) -> str:
        """Shared CREATE handling for GROUP / UNIT / STOCKITEM: duplicate names raise the modal (LESSONS §15 rule 10).

        UNIT (Op 1, live-verified) carries no NAME attribute, only a <NAME> child — fall back to it, and to
        NAME.LIST/NAME for the NAME.LIST-wrapped shapes, so this generic handler keys on whatever the real
        TallyPrime import actually names the master by.
        """
        name = element.get("NAME") or element.findtext("NAME") or element.findtext("NAME.LIST/NAME") or ""
        store = state[collection]
        if name in store:
            self.popup = True
            raise httpx.ReadTimeout("duplicate master modal", request=request)
        store[name] = fields(element)
        state["alt_mst"] += 1
        self._save(state)
        return import_result(created=1)

    def _ledger(self, state: dict, element: ET.Element, action: str, request: httpx.Request) -> str:
        name = element.get("NAME", "")
        ledgers = state["ledgers"]
        if action == "Create":
            if name in ledgers:
                self.popup = True
                raise httpx.ReadTimeout("duplicate master modal", request=request)
            currency = element.findtext("CURRENCYNAME") or ""                        # plan part 7 (candidate tag)
            if currency and currency not in state.get("currencies", {}):
                return import_result(exceptions=1, line_error="fake: unknown currency")
            state["alt_mst"] += 1
            ledgers[name] = {"parent": element.findtext("PARENT", ""), "email": "", "alter_id": state["alt_mst"],
                             "guid": f"{state['guid']}-{state['alt_mst']:08x}",
                             "opening": element.findtext("OPENINGBALANCE", "0.00")}
            if currency and self.ledger_currency_sticks:
                ledgers[name]["currency"] = currency
            bill_wise = element.findtext("ISBILLWISEON")
            if bill_wise:
                ledgers[name]["bill_wise"] = bill_wise
            self._save(state)
            return import_result(created=1)
        if name not in ledgers:
            return import_result(errors=1, line_error=f"Could not find Ledger '{name}'")
        if action == "Delete":
            del ledgers[name]
            state["alt_mst"] += 1
            self._save(state)
            return import_result(deleted=1)
        changed = False
        new_name = element.findtext("NAME.LIST/NAME")
        if new_name and new_name != name:
            ledgers[new_name] = ledgers.pop(name)
            name, changed = new_name, True
        email = element.findtext("EMAIL")
        if email:                                     # an empty value is silently ignored (live 2026-09-22)
            ledgers[name]["email"] = email
            changed = True
        if changed:
            state["alt_mst"] += 1
            ledgers[name]["alter_id"] = state["alt_mst"]
        self._save(state)
        return import_result(altered=1)

    @staticmethod
    def _signed_entries(element: ET.Element):
        """Every element carrying an ISDEEMEDPOSITIVE/AMOUNT pair: the ledger lines, each inventory row, and
        each inventory row's ACCOUNTINGALLOCATIONS child (where the Task-6 I1 defect actually lived)."""
        for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
            for entry in element.findall(tag):
                yield entry.findtext("LEDGERNAME", ""), entry
        for inv in element.findall("ALLINVENTORYENTRIES.LIST"):
            name = inv.findtext("STOCKITEMNAME", "")
            yield name, inv
            for allocation in inv.findall("ACCOUNTINGALLOCATIONS.LIST"):
                yield f"{name} (accounting allocation)", allocation

    def _signs_agree(self, element: ET.Element) -> bool:
        for _name, entry in self._signed_entries(element):
            amount = entry.findtext("AMOUNT")
            if amount is None:
                continue
            if not deemed_positive_matches(entry.findtext("ISDEEMEDPOSITIVE", "No"), Decimal(amount or "0.00")):
                return False
        return True

    @staticmethod
    def _posted_lines(element: ET.Element) -> list[dict[str, str]]:
        """What a voucher posts to ledgers, as Tally computes it (C32): every ledger line plus every inventory
        row's ACCOUNTINGALLOCATIONS (the nominal Sales/Purchase ledger of an invoice lives only there). The
        Trial Balance is built from these, so the nominal ledger is booked exactly once either way."""
        def line(entry: ET.Element) -> dict[str, str]:
            return {"ledger": entry.findtext("LEDGERNAME", ""), "amount": entry.findtext("AMOUNT", "0.00"),
                    "deemed_positive": entry.findtext("ISDEEMEDPOSITIVE", "No")}
        lines = [line(entry) for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST")
                 for entry in element.findall(tag)]
        lines += [line(allocation) for inv in element.findall("ALLINVENTORYENTRIES.LIST")
                  for allocation in inv.findall("ACCOUNTINGALLOCATIONS.LIST")]
        return lines

    @staticmethod
    def _stock_moves(element: ET.Element) -> list[dict[str, str]]:
        """C41: each inventory row's signed quantity — ISDEEMEDPOSITIVE=Yes brings stock in (a purchase, Op 7), No
        takes it out (a sale, Op 6). The quantity is ACTUALQTY's leading number ("12 Nos", "10 Box")."""
        moves = []
        for inv in element.findall("ALLINVENTORYENTRIES.LIST"):
            qty = Decimal((inv.findtext("ACTUALQTY") or "0").split()[0])
            inward = inv.findtext("ISDEEMEDPOSITIVE", "No") == "Yes"
            moves.append({"item": inv.findtext("STOCKITEMNAME", ""), "qty": f"{qty if inward else -qty}"})
        return moves

    def posted_bills(self) -> dict[str, dict[str, str]]:
        """C42: every bill as Tally shows it — a flagged (cancelled/optional) voucher's postings backed out."""
        return _effective_bills(self.state)

    def stock_day_closes(self) -> list[tuple[str, dict[str, Decimal]]]:
        """C41: every item's quantity at the close of each voucher day (YYYYMMDD), from the items' opening quantities
        plus every inventory row on record. Cancelled/optional vouchers move no stock, as in Tally."""
        state = self.state
        level = {name: Decimal((item.get("opening_qty") or "0").split()[0] if item.get("opening_qty") else "0")
                 for name, item in state["items"].items()}
        by_day: dict[str, list[dict]] = {}
        for v in state["vouchers"].values():
            if _flagged(v):
                continue
            by_day.setdefault(v["date"], []).append(v)
        closes = []
        for day in sorted(by_day):
            for v in by_day[day]:
                for move in v.get("inventory", []):
                    level[move["item"]] = level.get(move["item"], Decimal("0")) + Decimal(move["qty"])
            closes.append((day, dict(level)))
        return closes

    @staticmethod
    def _open_bills(state: dict, *, receivable: bool) -> list[tuple[str, str, str]]:
        """C34 (live UI 2026-09-24): Tally files a bill by the SIGN of its amount, not by the voucher type or the
        party's group — [S0-B:1]'s sales bill sent +9861.74 landed in Bills PAYABLE. Negative (Dr) = receivable,
        positive (Cr) = payable; a settled bill (zero) is in neither. BILLCL keeps the sign, as live XML does."""
        return [(name, bill["party"], bill["amount"]) for name, bill in _effective_bills(state).items()
                if Decimal(bill["amount"]) != 0 and (Decimal(bill["amount"]) < 0) == receivable]

    @staticmethod
    def _bills_refused(state: dict, element: ET.Element) -> bool:
        """C35 (review 2026-09-24 #1/#5) — shapes the fake used to accept silently, each of which live Tally would
        reject or mis-book. Refused with EXCEPTIONS=1 (the fake's approximation; the exact live answer to each is
        unrecorded):
        - bills under a line whose total's magnitude differs from that line's AMOUNT (the bill-wise split would
          leave the ledger);
        - an Agst Ref to a bill that was never opened (Tally would open a fresh bill on the wrong side, or refuse);
        - an Agst Ref to another party's bill;
        - an Agst Ref that pushes its bill past zero (over-settles it)."""
        bills = _effective_bills(state)
        for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
            for entry in element.findall(tag):
                allocations = entry.findall("BILLALLOCATIONS.LIST")
                if not allocations:
                    continue
                party = entry.findtext("LEDGERNAME", "")
                amounts = [Decimal(b.findtext("AMOUNT") or "0.00") for b in allocations]
                # By MAGNITUDE only: live Tally accepted [S0-B:1]'s +9861.74 bill under a −9861.74 line (C34) and
                # filed it by its own sign, so a sign mismatch is recorded behaviour, not a refusal.
                if abs(sum(amounts, Decimal("0.00"))) != abs(Decimal(entry.findtext("AMOUNT") or "0.00")):
                    return True
                for bill, amount in zip(allocations, amounts):
                    if bill.findtext("BILLTYPE") != "Agst Ref":
                        continue
                    target = bills.get(bill.findtext("NAME", ""))
                    if target is None or target["party"] != party:
                        return True
                    before = Decimal(target["amount"])
                    after = before + amount
                    if before == 0 or (after != 0 and (after < 0) != (before < 0)):
                        return True
        return False

    @staticmethod
    def _post_bills(state: dict, element: ET.Element) -> list[dict[str, str]]:
        """New Ref opens a bill with its signed amount; Agst Ref adds its signed amount to that bill (a receipt's
        + knocks off a sale's −). On Account names no bill, so nothing is opened (C35). Refusals happen first,
        in `_bills_refused`. Returns what was posted, kept on the voucher so a later flag can back it out (C42)."""
        bills = state.setdefault("bills", {})
        posted: list[dict[str, str]] = []
        for tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"):
            for entry in element.findall(tag):
                party = entry.findtext("LEDGERNAME", "")
                for bill in entry.findall("BILLALLOCATIONS.LIST"):
                    name, amount = bill.findtext("NAME", ""), Decimal(bill.findtext("AMOUNT") or "0.00")
                    bill_type = bill.findtext("BILLTYPE")
                    if bill_type == "On Account":
                        continue
                    if bill_type == "Agst Ref":
                        bills[name]["amount"] = f"{Decimal(bills[name]['amount']) + amount:.2f}"
                    else:
                        bills[name] = {"party": party, "amount": f"{amount:.2f}"}
                    posted.append({"name": name, "type": bill_type or "New Ref", "amount": f"{amount:.2f}"})
        return posted

    def _voucher(self, state: dict, element: ET.Element, action: str) -> str:
        vouchers = state["vouchers"]
        if action == "Create":
            narration_text = element.findtext("NARRATION", "")
            if any(marker in narration_text for marker in self.refuse_narrations):
                return import_result(exceptions=1)                     # test seam: "this voucher shape is refused"
            forex = self._forex_entries(state, element)                # plan part 7: rewrites forex AMOUNTs to base
            if forex is None:
                return import_result(exceptions=1)
            if not self._signs_agree(element):
                # I5: the flag whose wrong value is the documented cause of EXCEPTIONS=1 used not to be stored
                # here at all, so no balance assertion could ever see it. The fake now approximates Tally's
                # ACCEPTANCE rule instead of echoing whatever it was given: EXCEPTIONS=1 and no LINEERROR, which
                # is exactly what the live exploration got for every wrong permutation (v4 doc, cross-cutting
                # finding 2 — "there's no helpful error message").
                return import_result(exceptions=1)
            lines = self._posted_lines(element)
            for line in lines:
                if line["ledger"] in forex:
                    line.update(self._forex_line_text(state, Decimal(line["amount"]), forex[line["ledger"]]))
            if sum((Decimal(line["amount"] or "0.00") for line in lines), Decimal("0.00")) != 0:
                # C32 (live 2026-09-24, logs/debug-vch1-*.log): Tally totals the ledger lines AND every inventory
                # row's ACCOUNTINGALLOCATIONS — a nominal Sales line sent as well counts the goods twice and the
                # answer is EXCEPTIONS=1 with no LINEERROR. Any unbalanced voucher gets the same answer here.
                return import_result(exceptions=1)
            if element.findtext("ISCANCELLED") != "Yes" and self._bills_refused(state, element):
                return import_result(exceptions=1)                                                        # C35
            mid = str(state["next_master_id"])
            state["next_master_id"] += 1
            date = element.findtext("DATE", "")
            cancelled = "No" if self.drop_flags else (element.findtext("ISCANCELLED") or "No")
            optional = "No" if self.drop_flags else (element.findtext("ISOPTIONAL") or "No")
            vouchers[mid] = {"narration": element.findtext("NARRATION", ""), "date": date,
                             "post_dated": element.findtext("ISPOSTDATED") or "No",
                             "cancelled": cancelled, "optional": optional, "lines": lines,
                             "inventory": self._stock_moves(element)}
            if cancelled != "Yes":
                vouchers[mid]["bills"] = self._post_bills(state, element)
            state["alt_vch"] += 1
            state["last_voucher_date"] = max(state["last_voucher_date"], date)
            self._save(state)
            return import_result(created=1, last_vch_id=mid)
        mid = element.get("TAGVALUE", "")
        if mid not in vouchers:
            return import_result(errors=1, line_error="Voucher not found")
        if action == "Alter":
            narration = element.findtext("NARRATION")
            if narration:
                vouchers[mid]["narration"] = narration
            state["alt_vch"] += 1
            self._save(state)
            return import_result(altered=1, last_vch_id=mid)
        if action == "Delete":
            if not self.deletes_stick:                 # plan part 7 fact 3: DELETED=1, but the voucher stays
                return import_result(deleted=1, last_vch_id=mid)
            del vouchers[mid]
            state["alt_vch"] += 3                     # live: a delete moved AltVchId by 3
            self._save(state)
            return import_result(deleted=1, last_vch_id=mid)
        return import_result(errors=1, line_error=f"fake: unsupported voucher action {action}")


OWN_COMMAND = r"C:\Program Files\TallyPrimeEditLog\tally.exe /DATA:C:\users\Public\TallyPrimeEditLog\s0probe /LOAD:100003"
FOREIGN_COMMAND = r"C:\Program Files\TallyPrimeEditLog\tally.exe"


def tmp_config(tmp_path: Path) -> OperatorConfig:
    """An operator config whose folders all live under `tmp_path` (the data folder is still named s0probe)."""
    return OperatorConfig(
        wine_bin=tmp_path / "wine" / "bin" / "wine",
        tally_dir=tmp_path / "Program Files" / "TallyPrimeEditLog",
        data_dir=tmp_path / "Public" / "TallyPrimeEditLog" / "s0probe",
        data_dir_windows=r"C:\users\Public\TallyPrimeEditLog\s0probe",
        seed_dir=tmp_path / "seed_data",
        backups_dir=tmp_path / "Public" / "TallyPrimeEditLog" / "s0probe-backups",
        wine_log=tmp_path / "logs" / "tally-wine.log",
        poll_s=1.0,
    )


class FakeRunner:
    """Stands in for ps / kill / Popen / sleep. Spawning 'Tally' starts FakeBooks; a virtual clock never sleeps."""

    def __init__(self, books: FakeBooks, procs: list[TallyProcess] | None = None, *, stubborn: bool = False,
                 hide_args: bool = False, appears_during_wait: TallyProcess | None = None):
        self.books = books
        self.procs = list(procs or [])
        self.stubborn = stubborn          # terminate() doesn't stop it; kill() does
        self.hide_args = hide_args        # `ps` shows only the exe path, not /DATA:…
        self.appears_during_wait = appears_during_wait   # a process that shows up the first time sleep() is called
        self.clock = 0.0
        self.spawned: list[list[str]] = []
        self.terminated: list[int] = []
        self.killed: list[int] = []
        self.commands: list[list[str]] = []

    def list_tally(self) -> list[TallyProcess]:
        return list(self.procs)

    def spawn(self, argv: list[str], cwd: Path, log_path: Path) -> None:
        self.spawned.append(list(argv))
        command = FOREIGN_COMMAND if self.hide_args else " ".join(argv[1:])
        self.procs = [TallyProcess(100 + len(self.spawned), command)]
        self.books.start(load=any(arg.startswith("/LOAD:") for arg in argv))

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        if not self.stubborn:
            self._gone(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        self._gone(pid)

    def _gone(self, pid: int) -> None:
        self.procs = [p for p in self.procs if p.pid != pid]
        if not self.procs:
            self.books.stop()

    def run(self, argv: list[str], timeout: float) -> str:
        self.commands.append(list(argv))
        return "wine-11.0"

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        if self.appears_during_wait is not None:
            self.procs.append(self.appears_during_wait)
            self.appears_during_wait = None

    def now(self) -> float:
        return self.clock
