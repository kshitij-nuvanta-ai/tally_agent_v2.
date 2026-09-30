"""Read requests and parsers shared by the company-A probes (probe side only; S2 grows its own in agent/tally/)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from agent.tally.amounts import AmountParseError, parse_decimal
from agent.tally.envelopes import COMPANY_PLACEHOLDER, esc, wrap_collection
from agent.tally.xml_utils import read_objects, sanitize_xml

A_FY_FROM = "01-04-2025"
A_FY_TO = "31-03-2026"
VOUCHER_CHILDOF = "<CHILDOF>$$VchTypeAllVouchers</CHILDOF>"    # without it a Voucher collection has empty bodies (v4)
LEDGER_LISTS = ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST")
INVENTORY_LISTS = ("ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST")
POSTING_RULES = ("default", "all_only", "ledger_plus_alloc", "all_plus_alloc")
ZERO = Decimal("0")

# The rule these probes count a voucher's postings with (live 2026-09-23, company A, TallyPrime 7.0 Edit Log,
# Educational, under Wine 11.0). Company A's 24 inventory vouchers (16 Sales + 8 Purchase) carry the nominal ledger
# TWICE: once in ALLLEDGERENTRIES.LIST and again in each inventory entry's ACCOUNTINGALLOCATIONS.LIST. The "default"
# rule (primary lines + allocations) therefore counts Sales and Purchase at exactly 2x, and every one of those 24
# vouchers fails a double-entry sum; "all_only" leaves 0 of 50 unbalanced. Measured against the TB as-on 31-10-2025
# (p18_A fixtures, 9 countable vouchers to that date): all_only gives Purchase -1,157,000 and Sales +544,000, which
# are Tally's own as-on TB rows to the paisa, while "default" gives -2,314,000 and +1,088,000 — the 2x. All 50
# vouchers export ALLLEDGERENTRIES.LIST, so it alone is complete (party + nominal + GST). Probe 6 is the probe that
# formally settles the rule across voucher types; it still measures all four and is not affected by this default.
PROBE_POSTING_RULE = "all_only"

OPENING_STOCK_ROW = "Opening Stock"
# EXPLODEFLAG=Yes turns the TYPE=Data Trial Balance into a detailed one: the primary-group rows stay, byte-for-byte
# the same values as the plain TB, and their children are added -- including the synthetic "Opening Stock" row that
# no ledger carries (probe 17, live 2026-09-23). It stops at the second group level, so it is used here only to read
# the group rows plus that Opening Stock row, never to enumerate ledgers.
TB_EXPLODE_VARS = {"EXPLODEFLAG": "Yes"}

# Tally's reserved primary groups and the nature S1 gives each (Part 1 §6 rung 1 needs balance-sheet vs P&L).
PRIMARY_NATURE: dict[str, str] = {
    "Capital Account": "liabilities", "Loans (Liability)": "liabilities", "Current Liabilities": "liabilities",
    "Suspense A/c": "liabilities", "Branch / Divisions": "liabilities",
    "Fixed Assets": "assets", "Investments": "assets", "Current Assets": "assets", "Misc. Expenses (ASSET)": "assets",
    "Sales Accounts": "income", "Direct Incomes": "income", "Indirect Incomes": "income",
    "Purchase Accounts": "expenses", "Direct Expenses": "expenses", "Indirect Expenses": "expenses",
}
PL_PRIMARY_GROUPS = frozenset(group for group, nature in PRIMARY_NATURE.items() if nature in ("income", "expenses"))


# Period variables must never reach a master collection (live 2026-09-23, twice, on a freshly reset company A;
# TallyPrime 7.0 Edit Log, Educational, under Wine 11.0 — proven on the Ledger collection only). SVFROMDATE froze
# Tally's XML server behind a modal: the read timed out at 45 s and an unrelated counters read then timed out too, so
# the whole server was blocked until Tally was restarted. SVTODATE answered instantly, byte-identical to the control,
# with 0 of 35 closing balances changed — a healthy 200 carrying TODAY's balances, which is the more dangerous of the
# two. Reports (wrap_report, TYPE=Data) honour both and are the route to as-on figures. The guard is deliberately
# conservative — it covers every master collection, though only Ledger was tested — and the same rule will be needed
# in the S2 agent's own builders (agent/tally/). voucher_request and wrap_report are proven fine and untouched.
MASTER_PERIOD_VARS = ("SVFROMDATE", "SVTODATE")
MASTER_PERIOD_VARS_ERROR = (
    "period variables on a master collection freeze Tally (SVFROMDATE: the XML server stays blocked behind a modal "
    "until Tally is restarted) or are silently ignored (SVTODATE: a healthy 200 carrying today's balances). As-on "
    "figures come from wrap_report (TYPE=Data) instead. Pass allow_period_vars=True only from probe 16, the probe "
    "that measures this behaviour."
)


# --- requests -------------------------------------------------------------------------------------------------------
def voucher_request(name: str, fields: list[str], company: str, *, from_date: str = A_FY_FROM, to_date: str = A_FY_TO,
                    filters: list[tuple[str, str]] | None = None, extra_collection_xml: str = "") -> str:
    return wrap_collection(name, "Voucher", fields, company, static_vars={"SVFROMDATE": from_date, "SVTODATE": to_date},
                           filters=filters, extra_collection_xml=VOUCHER_CHILDOF + extra_collection_xml)


# --- the month request (probe 5 confirms it, probe 21 and S2's extractor reuse it) --------------------------------
FROM_PLACEHOLDER = "__FROM__"
TO_PLACEHOLDER = "__TO__"
# Everything S1 stores for a voucher (Part 1 §5 "Cloud" minimum columns) plus the nested lists (probe 6: fetched whole).
VOUCHER_MONTH_FIELDS = ["GUID", "MasterID", "AlterID", "Date", "VoucherTypeName", "VoucherNumber", "Reference",
                        "PartyLedgerName", "Narration", "IsCancelled", "IsOptional", "IsPostDated",
                        "AllLedgerEntries", "AllInventoryEntries"]
_TYPED_DATE_VAR = re.compile(r'<(SV[A-Z0-9]*DATE) TYPE="Date">', re.IGNORECASE)


def fill_month_request(template: str, company: str, from_date: str, to_date: str) -> str:
    """A confirmed month template (placeholders __COMPANY__ / __FROM__ / __TO__) for one company and window.

    The company is XML-escaped here; dates are DD-MM-YYYY. Works for both forms probe 5 can confirm: typed period
    variables, or a `$Date` formula whose bounds carry the placeholders.
    """
    return (template.replace(COMPANY_PLACEHOLDER, esc(company))
            .replace(FROM_PLACEHOLDER, from_date).replace(TO_PLACEHOLDER, to_date))


def untyped_period_vars(xml: str) -> str:
    """The same request with TYPE="Date" stripped from every SV*DATE variable. Evidence only (C33): probe 5 sends
    it once to record Tally's silent current-period fallback. Nothing may use it for data."""
    return _TYPED_DATE_VAR.sub(r"<\1>", xml)


def master_request(name: str, object_type: str, fields: list[str], company: str, *,
                   static_vars: dict[str, str] | None = None, filters: list[tuple[str, str]] | None = None,
                   allow_period_vars: bool = False) -> str:
    """A master (Ledger, Group, StockItem, …) collection. Period variables are refused — see MASTER_PERIOD_VARS_ERROR.

    `allow_period_vars=True` is the single deliberate exception: probe 16 is the probe that MEASURES this behaviour
    (its SVTODATE read is the evidence; its SVFROMDATE read is opt-in and off by default). Nothing else may set it.
    """
    if not allow_period_vars:
        used = [name_ for name_ in MASTER_PERIOD_VARS if name_ in (static_vars or {})]
        if used:
            raise ValueError(f"{', '.join(used)} on a {object_type} collection ({name}): {MASTER_PERIOD_VARS_ERROR}")
    return wrap_collection(name, object_type, fields, company, static_vars=static_vars, filters=filters)


# --- values ---------------------------------------------------------------------------------------------------------
def amount(text: str | None) -> Decimal | None:
    """parse_decimal, except that a non-plain amount (e.g. a forex expression) reads as None instead of raising."""
    try:
        return parse_decimal(text)
    except AmountParseError:
        return None


def tally_date(text: str) -> date | None:
    """'20251001', '1-Oct-25', '01-Oct-2025' or '01-10-2025' → a date; anything else → None."""
    cleaned = (text or "").strip()
    for fmt in ("%Y%m%d", "%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def dmy(text: str) -> date:
    return datetime.strptime(text, "%d-%m-%Y").date()


def qty_number(text: str) -> Decimal | None:
    """' 25 Nos' → 25; '-2.0000 NOS' → -2; '' → None."""
    parts = (text or "").strip().split()
    return amount(parts[0]) if parts else None


def signed_ui_amount(text: str) -> Decimal | None:
    """A balance typed from the Tally UI: '62,800 Dr' → -62800 (debit negative); '1,000 Cr' → 1000; '' → None."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered.endswith(("dr", "cr")):
        value = amount(cleaned[:-2])
        if value is None:
            return None
        return -abs(value) if lowered.endswith("dr") else abs(value)
    return amount(cleaned)



# --- forex amounts (probe 22, plan part 7) --------------------------------------------------------------------------
# Candidate grammar (review 2026-09-24 #2): "<sign><sym><fx> @ <base sym><rate>/<sym> [= <sign><base sym><base>]".
# Widened for the unmeasured export form: optional spaces, a missing "= base" part, "Rs." or no base symbol, a
# symbol word ("USD"). Task 2 of plan part 7 measures Tally's own form; tests pin it to that capture.
_FX_NUM = r"\d[\d,]*(?:\.\d+)?"
_FOREX_RE = re.compile(
    rf"^\s*(?P<s1>-?)\s*(?P<sym>[^\d\s@=/-]+)\s*(?P<s2>-?)\s*(?P<fx>{_FX_NUM})"
    rf"\s*@\s*(?P<rsym>[^\d\s@=/-]*)\s*(?P<rate>{_FX_NUM})\s*/\s*(?P<per>[^\s=]+)"
    rf"(?:\s*=\s*(?P<b1>-?)\s*(?P<bsym>[^\d\s=-]*)\s*(?P<b2>-?)\s*(?P<base>{_FX_NUM}))?\s*$")
_PAISA = Decimal("0.01")


@dataclass(frozen=True)
class ForexAmount:
    currency: str            # the foreign symbol as written ("$")
    fx: Decimal              # signed foreign face value
    rate: Decimal            # base-currency units per foreign unit
    rate_symbol: str         # the base symbol as written ("₹", "Rs.", "")
    base: Decimal | None     # signed base amount when the text states it ("= -₹37216.04"), else None
    raw: str


def _fx_number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def parse_forex_amount(text: str | None) -> ForexAmount | None:
    """A Tally forex AMOUNT expression → its parts; anything else (a plain number, empty, junk) → None."""
    match = _FOREX_RE.match(text or "")
    if match is None:
        return None
    fx = _fx_number(match["fx"]) * (-1 if "-" in match["s1"] + match["s2"] else 1)
    base = None
    if match["base"] is not None:
        base = _fx_number(match["base"]) * (-1 if "-" in (match["b1"] or "") + (match["b2"] or "") else 1)
    return ForexAmount(currency=match["sym"], fx=fx, rate=_fx_number(match["rate"]),
                       rate_symbol=match["rsym"] or "", base=base, raw=text)


def forex_base(fa: ForexAmount) -> tuple[Decimal, str]:
    """The INR base of one forex line: the stated "= ₹…" part when present ("stated"), else face × rate rounded
    ROUND_HALF_UP to paise ("computed" — Tally's own rounding is unmeasured, so a computed base is a DIFFERENT)."""
    if fa.base is not None:
        return fa.base, "stated"
    return (fa.fx * fa.rate).quantize(_PAISA, rounding=ROUND_HALF_UP), "computed"

# --- vouchers -------------------------------------------------------------------------------------------------------
def _leaf_fields(element: ET.Element) -> dict[str, str]:
    return {child.tag: (child.text or "").strip() for child in element if len(child) == 0}


def _filled(element: ET.Element, tag: str) -> list[ET.Element]:
    """Direct children named `tag` that hold something (Tally also exports empty placeholder lists)."""
    return [child for child in element if child.tag == tag and len(child) > 0]


def _line(element: ET.Element, list_tag: str) -> dict:
    fields = _leaf_fields(element)
    return {"list": list_tag, "fields": fields, "amount": amount(fields.get("AMOUNT")),
            "amount_raw": fields.get("AMOUNT", ""),
            "bills": [_leaf_fields(bill) for bill in _filled(element, "BILLALLOCATIONS.LIST")]}


def parse_vouchers(raw_xml: str) -> list[dict]:
    root = ET.fromstring(sanitize_xml(raw_xml))
    vouchers: list[dict] = []
    for element in root.iter("VOUCHER"):
        if len(element) == 0:
            continue                                   # CMPINFO's <VOUCHER>0</VOUCHER> counter
        lines = [_line(child, tag) for tag in LEDGER_LISTS for child in _filled(element, tag)]
        inventory = [{"fields": _leaf_fields(inv),
                      "accounting": [_line(a, "ACCOUNTINGALLOCATIONS.LIST")
                                     for a in _filled(inv, "ACCOUNTINGALLOCATIONS.LIST")],
                      "batches": [_leaf_fields(b) for b in _filled(inv, "BATCHALLOCATIONS.LIST")]}
                     for tag in INVENTORY_LISTS for inv in _filled(element, tag)]
        vouchers.append({"attrs": dict(element.attrib), "header": _leaf_fields(element), "ledger_lines": lines,
                         "inventory": inventory})
    return vouchers


def primary_lines(voucher: dict) -> list[dict]:
    """The voucher's own ledger lines: ALLLEDGERENTRIES.LIST if it has any, else LEDGERENTRIES.LIST — never both."""
    all_lines = [item for item in voucher["ledger_lines"] if item["list"] == "ALLLEDGERENTRIES.LIST"]
    return all_lines or [item for item in voucher["ledger_lines"] if item["list"] == "LEDGERENTRIES.LIST"]


def postings(voucher: dict, rule: str = PROBE_POSTING_RULE) -> list[tuple[str, Decimal | None]]:
    """(ledger, amount) per posting, under `rule` (default PROBE_POSTING_RULE = 'all_only' -- see its comment above).

    The rule names are probe 6's candidates and all four stay available: 'default' = primary lines + inventory
    accounting allocations, which on company A double-counts the nominal ledger of every inventory voucher.
    """
    all_lines = [item for item in voucher["ledger_lines"] if item["list"] == "ALLLEDGERENTRIES.LIST"]
    ledger_lines = [item for item in voucher["ledger_lines"] if item["list"] == "LEDGERENTRIES.LIST"]
    allocations = [a for inv in voucher["inventory"] for a in inv["accounting"]]
    chosen = {"default": primary_lines(voucher) + allocations, "all_only": all_lines,
              "ledger_plus_alloc": ledger_lines + allocations, "all_plus_alloc": all_lines + allocations}[rule]
    return [(item["fields"].get("LEDGERNAME", ""), item["amount"]) for item in chosen]


def is_countable(voucher: dict) -> bool:
    """Affects balances: not cancelled, not optional (post-dated vouchers are the caller's choice)."""
    header = voucher["header"]
    return header.get("ISCANCELLED", "No") != "Yes" and header.get("ISOPTIONAL", "No") != "Yes"


def ledger_movements(vouchers: list[dict], *, up_to: date | None = None, before: date | None = None,
                     include_post_dated: bool = False, rule: str = PROBE_POSTING_RULE) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for voucher in vouchers:
        if not is_countable(voucher):
            continue
        if not include_post_dated and voucher["header"].get("ISPOSTDATED", "No") == "Yes":
            continue
        when = tally_date(voucher["header"].get("DATE", ""))
        if when is None or (up_to is not None and when > up_to) or (before is not None and when >= before):
            continue
        for ledger, value in postings(voucher, rule):
            if value is not None:
                totals[ledger] = totals.get(ledger, ZERO) + value
    return totals


# --- masters --------------------------------------------------------------------------------------------------------
def parse_parents(raw_xml: str, tag: str = "GROUP") -> dict[str, str]:
    """name → parent for every `tag` object (primary groups have parent '' or 'Primary')."""
    return {row["Name"]: row["Parent"] for row in read_objects(raw_xml, tag, ["Name", "Parent"]) if row["Name"]}


def _is_top(parent: str) -> bool:
    return parent in ("", "Primary")


def ancestors(name: str, parents: dict[str, str]) -> list[str]:
    """[name, parent, grandparent, …] up to the primary group (cycle-safe)."""
    chain = [name]
    while name in parents and not _is_top(parents[name]) and parents[name] not in chain:
        name = parents[name]
        chain.append(name)
    return chain


def top_group(group: str, parents: dict[str, str]) -> str:
    return ancestors(group, parents)[-1]


def stock_bearing_groups(parents: dict[str, str]) -> set[str]:
    """The primary group holding Stock-in-Hand, whose TB row carries a stock figure no ledger holds.

    On company A that figure is the TB's own synthetic `Opening Stock` row (OPENING_STOCK_ROW), NOT the Stock
    Summary's closing value -- the two are different quantities (live 2026-09-23: 18,55,800 vs -9,89,462.31).
    """
    return {top_group("Stock-in-Hand", parents)} if "Stock-in-Hand" in parents else {"Current Assets"}


# --- report rows at any depth (exploded reports) ---------------------------------------------------------------------
def tb_rows_any_depth(raw_xml: str) -> list[dict]:
    """name, debit, credit, closing for every DSPDISPNAME in document order — flat or nested (exploded TBs)."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows: list[dict] = []
    current: dict | None = None
    for element in root.iter():
        if element.tag == "DSPDISPNAME":
            current = {"name": (element.text or "").strip(), "debit": None, "credit": None}
            rows.append(current)
        elif current is not None and element.tag == "DSPCLDRAMTA":
            current["debit"] = amount(element.text)
        elif current is not None and element.tag == "DSPCLCRAMTA":
            current["credit"] = amount(element.text)
    for row in rows:
        present = [value for value in (row["debit"], row["credit"]) if value is not None]
        row["closing"] = sum(present, ZERO) if present else None
    return rows


def stock_rows_any_depth(raw_xml: str) -> list[dict]:
    """name, qty text, closing qty and closing value for every DSPDISPNAME in a Stock Summary — flat or exploded."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows: list[dict] = []
    current: dict | None = None
    for element in root.iter():
        if element.tag == "DSPDISPNAME":
            current = {"name": (element.text or "").strip(), "qty_text": "", "qty": None, "value": None}
            rows.append(current)
        elif current is not None and element.tag == "DSPCLQTY":
            current["qty_text"] = (element.text or "").strip()
            current["qty"] = qty_number(current["qty_text"])
        elif current is not None and element.tag == "DSPCLAMTA":
            current["value"] = amount(element.text)
    return rows


def exploded_tb_rows(raw_xml: str) -> list[dict]:
    """Every TB row at any depth, in parse_trial_balance's shape (account_name / debit_amount / credit_amount /
    closing_balance). Works on a plain TB too, where the rows are just the primary groups."""
    return [{"account_name": row["name"], "debit_amount": row["debit"], "credit_amount": row["credit"],
             "closing_balance": row["closing"]} for row in tb_rows_any_depth(raw_xml)]


def primary_group_rows(rows: list[dict]) -> dict[str, dict]:
    """The reserved primary groups' own rows, first occurrence wins.

    First occurrence matters: company A has a LEDGER called "Capital Account" as well as the group, and an exploded
    TB emits the group row before its children.
    """
    found: dict[str, dict] = {}
    for row in rows:
        name = row["account_name"]
        if name in PRIMARY_NATURE and name not in found:
            found[name] = row
    return found


def opening_stock_row(rows: list[dict]) -> dict | None:
    """The TB's synthetic `Opening Stock` row, if this response carries one (an exploded TB does; a plain one doesn't)."""
    return next((row for row in rows if row["account_name"] == OPENING_STOCK_ROW), None)
