"""Tally XML -> wire objects (S1 spec §5.1, D1): tag names lower-cased, text unchanged except entity decoding.

A missing tag gives no key; an empty tag gives "". Nothing here parses a value: that is the server's job
(contract.parse). Posting rule (LESSONS rule 18): `ledger_entries` come from ALLLEDGERENTRIES.LIST only; the
LEDGERENTRIES.LIST copy is never emitted.

`sanitize_xml` and `detect_error` are the bridge's own (tally_bridge.xml_utils). On top of them: `&#4;` is swapped
for a placeholder before sanitize_xml (which would delete it) and mapped back to U+0004 in every text and attribute
(pre-flight F16, spec §5.4 / D31: the wire keeps "\u0004 Primary", parse.name strips it server-side); an <ERRORMSG>
body is an error envelope too (probe 10 body_kind).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from tally_bridge.xml_utils import detect_error, sanitize_xml


class TallyErrorEnvelope(ValueError):
    """Tally answered with an error (LINEERROR / ERRORS / ERRORMSG), or with a shape that is not the one asked for
    (a master collection to a voucher read, a collection to a report read: what a closed company answers, probe 10)."""


MASTER_TAGS: dict[str, str] = {"currency": "CURRENCY", "group": "GROUP", "voucher_type": "VOUCHERTYPE",
                               "ledger": "LEDGER", "stock_group": "STOCKGROUP", "unit": "UNIT",
                               "stock_item": "STOCKITEM"}
BALANCE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ledger_balance": ("LEDGER", ("guid", "name", "closingbalance", "openingbalance")),
    "stock_balance": ("STOCKITEM", ("guid", "name", "closingvalue", "closingbalance")),
}
COUNTER_KEYS = ("guid", "name", "altvchid", "altmstid", "booksfrom")
BILL_FIELDS = {"NAME": "name", "BILLTYPE": "billtype", "AMOUNT": "amount", "BILLCREDITPERIOD": "billcreditperiod",
               "BILLDATE": "billdate"}
REPORT_ROW_START: dict[str, str] = {
    "trial_balance": "DSPACCNAME", "trial_balance_ledgerwise": "DSPACCNAME", "stock_summary": "DSPACCNAME",
    "bills_receivable": "BILLFIXED", "bills_payable": "BILLFIXED", "balance_sheet": "BSNAME",
    "profit_and_loss": "DSPACCNAME",
}
_TB_KEYS = frozenset({"dspdispname", "dspcldramta", "dspclcramta"})
_BILL_KEYS = frozenset({"billdate", "billref", "billparty", "billcl", "billdue", "billoverdue"})
REPORT_CELL_KEYS: dict[str, frozenset[str]] = {                 # spec §7.12
    "trial_balance": _TB_KEYS, "trial_balance_ledgerwise": _TB_KEYS,
    "stock_summary": frozenset({"dspdispname", "dspclqty", "dspclrate", "dspclamta"}),
    "bills_receivable": _BILL_KEYS, "bills_payable": _BILL_KEYS,
    "balance_sheet": frozenset({"dspdispname", "bssubamt", "bsmainamt"}),
    "profit_and_loss": frozenset({"dspdispname", "plsubamt", "bsmainamt"}),
}

_PLACEHOLDER = ""                                         # private-use: never in Tally text (checked below)
_RESERVED_REF = re.compile(r"&#(?:0*4|[xX]0*4);")


def _parse(raw_xml: str) -> ET.Element:
    if _PLACEHOLDER in raw_xml:
        raise ValueError("input already holds the U+0004 placeholder character")
    text = _RESERVED_REF.sub(_PLACEHOLDER, raw_xml)
    error = detect_error(text)
    if error is not None:
        raise TallyErrorEnvelope(error)
    root = ET.fromstring(sanitize_xml(text))
    message = root if root.tag == "ERRORMSG" else root.find(".//ERRORMSG")
    if message is not None:
        raise TallyErrorEnvelope((message.text or "").strip() or "ERRORMSG")
    return root


def _text(value: str | None) -> str:
    return "" if value is None else value.replace(_PLACEHOLDER, "\u0004")


def _is_list(element: ET.Element) -> bool:
    return element.tag.endswith(".LIST")


def _leaves(element: ET.Element) -> dict[str, str]:
    """Direct leaf children -> {tag.lower(): text}; list placeholders (`*.LIST`, even empty) are never fields."""
    out: dict[str, str] = {}
    for child in element:
        if len(child) or _is_list(child):
            continue
        key = child.tag.lower()
        if key in out:
            raise ValueError(f"repeated leaf <{child.tag}> under <{element.tag}>")
        out[key] = _text(child.text)
    return out


def _lists(element: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in element if child.tag == tag and len(child)]      # an empty placeholder is skipped


def _bill_allocation(element: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag, key in BILL_FIELDS.items():
        child = element.find(tag)
        if child is not None:
            out[key] = _text(child.text)
    return out


def _ledger_entry(element: ET.Element) -> dict:
    entry: dict = _leaves(element)
    bills = [_bill_allocation(b) for b in _lists(element, "BILLALLOCATIONS.LIST")]
    if bills:
        entry["bill_allocations"] = bills
    return entry


def _inventory_entry(element: ET.Element) -> dict:
    entry: dict = _leaves(element)
    for tag, key in (("BATCHALLOCATIONS.LIST", "batch_allocations"),
                     ("ACCOUNTINGALLOCATIONS.LIST", "accounting_allocations")):
        allocations = [_leaves(a) for a in _lists(element, tag)]         # raw only (D26)
        if allocations:
            entry[key] = allocations
    return entry


def vouchers_from_xml(raw_xml: str) -> list[dict]:
    root = _parse(raw_xml)
    if any(c.get("ISMSTDEPTYPE") == "Yes" for c in root.iter("COLLECTION")):
        raise TallyErrorEnvelope("a master collection answered a voucher read")
    out: list[dict] = []
    for voucher in root.iter("VOUCHER"):
        if not len(voucher):                                          # CMPINFO's <VOUCHER>0</VOUCHER>
            continue
        data: dict = _leaves(voucher)
        data["ledger_entries"] = [_ledger_entry(e) for e in _lists(voucher, "ALLLEDGERENTRIES.LIST")]
        inventory = _lists(voucher, "ALLINVENTORYENTRIES.LIST") or _lists(voucher, "INVENTORYENTRIES.LIST")
        if inventory:
            data["inventory_entries"] = [_inventory_entry(e) for e in inventory]
        out.append({"kind": "voucher", "data": data})
    return out


def _master_elements(root: ET.Element, tag: str) -> list[ET.Element]:
    return [element for element in root.iter(tag) if len(element)]       # skips CMPINFO's bare counts


def _master_data(element: ET.Element) -> dict[str, str]:
    data = _leaves(element)
    if "name" not in data and element.get("NAME") is not None:
        data["name"] = _text(element.get("NAME"))
    return data


def masters_from_xml(raw_xml: str, kind: str) -> list[dict]:
    if kind not in MASTER_TAGS:
        raise ValueError(f"unknown master kind {kind!r}")
    root = _parse(raw_xml)
    return [{"kind": kind, "data": _master_data(e)} for e in _master_elements(root, MASTER_TAGS[kind])]


def balances_from_xml(raw_xml: str, captured_at: str, kind: str = "ledger_balance") -> list[dict]:
    """A mirrored balance re-read (G5 shape: a Ledger collection) -> `ledger_balance` / `stock_balance` objects.
    `captured_at` is the capture's own time (the sidecar's `sent_at`); it is not in Tally's answer (pre-flight F23)."""
    if kind not in BALANCE_FIELDS:
        raise ValueError(f"unknown balance kind {kind!r}")
    tag, fields = BALANCE_FIELDS[kind]
    root = _parse(raw_xml)
    out: list[dict] = []
    for element in _master_elements(root, tag):
        full = _master_data(element)
        data = {key: full[key] for key in fields if key in full}
        data["captured_at"] = captured_at
        out.append({"kind": kind, "data": data})
    return out


def counters_from_xml(raw_xml: str) -> dict[str, str]:
    """The open company's GUID, name and counters, verbatim. No company in the answer -> {} (a closed company or a
    pending login/vault prompt reads as an empty list, LESSONS rule 26)."""
    companies = _master_elements(_parse(raw_xml), "COMPANY")
    if not companies:
        return {}
    if len(companies) > 1:
        raise ValueError(f"{len(companies)} companies in a counters read")
    leaves = _leaves(companies[0])
    if "name" not in leaves and "basiccompanyname" in leaves:
        leaves["name"] = leaves["basiccompanyname"]
    return {key: leaves[key] for key in COUNTER_KEYS if key in leaves}


def _collect(element: ET.Element, row: dict[str, str]) -> None:
    for leaf in element.iter():
        if len(leaf):
            continue
        key = leaf.tag.lower()
        if key in row:
            raise ValueError(f"repeated report cell <{leaf.tag}> in one row")
        row[key] = _text(leaf.text)


def report_cells(raw_xml: str, report_type: str) -> list[dict[str, str]]:
    """A TYPE=Data report -> one dict per row, in document order: the row-name element starts a row and the elements
    after it (up to the next row name) fill it. Keys outside spec §7.12's set for the type raise ValueError."""
    if report_type not in REPORT_ROW_START:
        raise ValueError(f"unknown report type {report_type!r}")
    root = _parse(raw_xml)
    if root.find("HEADER") is not None or root.find(".//COLLECTION") is not None:
        raise TallyErrorEnvelope("a collection answered a report read")
    start, allowed = REPORT_ROW_START[report_type], REPORT_CELL_KEYS[report_type]
    rows: list[dict[str, str]] = []
    for child in root:
        if child.tag == start:
            rows.append({})
        elif not rows:
            raise ValueError(f"<{child.tag}> before the first <{start}>")
        _collect(child, rows[-1])
    for row in rows:
        if not set(row) <= allowed:
            raise ValueError(f"{report_type} cells {sorted(set(row) - allowed)} are not in spec §7.12")
    return rows
