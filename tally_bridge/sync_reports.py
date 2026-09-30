"""Decimal parsers for Tally report and ledger-list responses: the sync path (merge decision M11).

``response_parser.py`` holds the chat path's parsers of the same names; they return float and stay as they are.
What is different here:
- amounts are ``Decimal`` (``amounts.parse_decimal``) and a missing value is ``None``, not ``0.0``; a value that is
  present but unreadable raises ``AmountParseError`` instead of becoming ``0.0``;
- ``parse_stock_summary`` reads a stock rate like "1250.00/NOS" as its number (the float parser gives 0.0), and its
  rows carry no ``parent_group``;
- ``parse_ledger_list`` reads a forex expression balance at its stated INR base (``amounts.parse_amount``, C47) and
  adds ``closing_forex`` / ``opening_forex``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

from tally_bridge.amounts import parse_amount, parse_decimal
from tally_bridge.response_parser import _parse_overdue_days
from tally_bridge.xml_utils import get_text, sanitize_xml


def _sum_optional(*values: Decimal | None) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal("0")) if present else None


def parse_trial_balance(raw_xml: str) -> list[dict]:
    """TB rows from TYPE=Data: DSPACCNAME/DSPDISPNAME followed by DSPACCINFO (DSPCLDRAMTA debit, DSPCLCRAMTA credit)."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows: list[dict] = []
    current_name = None
    for child in list(root):
        if child.tag == "DSPACCNAME":
            current_name = get_text(child, "DSPDISPNAME")
        elif child.tag == "DSPACCINFO" and current_name:
            debit_el = child.find("DSPCLDRAMT")
            credit_el = child.find("DSPCLCRAMT")
            debit = parse_decimal(get_text(debit_el, "DSPCLDRAMTA")) if debit_el is not None else None
            credit = parse_decimal(get_text(credit_el, "DSPCLCRAMTA")) if credit_el is not None else None
            rows.append({
                "account_name": current_name,
                "debit_amount": debit,
                "credit_amount": credit,
                "closing_balance": _sum_optional(debit, credit),
            })
            current_name = None
    return rows


def parse_ledger_list(raw_xml: str) -> list[dict]:
    """Ledger rows. A forex ledger's balance (C47, live `-$1609.71 @ ? 82.58/$ = -? 132929.85`) reads as its stated
    INR base — Tally's own figure, revalued at the latest voucher rate — with the parts in `closing_forex` /
    `opening_forex` (amounts.ForexAmount, None for a plain balance). An unreadable amount raises AmountParseError."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    ledgers: list[dict] = []
    for ledger in root.iter("LEDGER"):
        name = get_text(ledger, "NAME") or ledger.get("NAME", "")
        if not name:
            continue
        closing, closing_forex = parse_amount(get_text(ledger, "CLOSINGBALANCE"))
        opening, opening_forex = parse_amount(get_text(ledger, "OPENINGBALANCE"))
        ledgers.append({
            "name": name,
            "parent_group": get_text(ledger, "PARENT"),
            "closing_balance": closing,
            "opening_balance": opening,
            "closing_forex": closing_forex,
            "opening_forex": opening_forex,
        })
    return ledgers


def parse_bills(raw_xml: str) -> list[dict]:
    """Bills Receivable/Payable: BILLFIXED (ref, party, date) followed by sibling BILLCL/BILLDUE/BILLOVERDUE."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    bills: list[dict] = []
    elements = list(root.iter())
    for i, el in enumerate(elements):
        if el.tag != "BILLFIXED":
            continue
        bill_ref = get_text(el, "BILLREF")
        if not bill_ref:
            continue
        amount: Decimal | None = None
        due_date = ""
        overdue = ""
        for sib in elements[i + 1: i + 10]:
            if sib.tag == "BILLFIXED":
                break
            if sib.tag == "BILLCL" and sib.text:
                parsed = parse_decimal(sib.text)
                amount = abs(parsed) if parsed is not None else None
            elif sib.tag == "BILLDUE" and sib.text:
                due_date = sib.text.strip()
            elif sib.tag == "BILLOVERDUE" and sib.text:
                overdue = sib.text.strip()
        bills.append({
            "bill_number": bill_ref,
            "party_name": get_text(el, "BILLPARTY"),
            "bill_date": get_text(el, "BILLDATE"),
            "amount": amount,
            "pending_amount": amount,
            "due_date": due_date,
            "overdue_days": _parse_overdue_days(overdue),
        })
    return bills


def _number_before(text: str, separator: str) -> Decimal | None:
    """'1,250.00/NOS' → Decimal('1250.00') with separator '/'."""
    if not text.strip():
        return None
    return parse_decimal(text.split(separator)[0])


def parse_stock_summary(raw_xml: str) -> list[dict]:
    """Stock Summary: DSPACCNAME/DSPDISPNAME followed by DSPSTKINFO/DSPSTKCL (DSPCLQTY '-2.0000 NOS', DSPCLRATE, DSPCLAMTA)."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    items: list[dict] = []
    current_name = None
    for child in list(root):
        if child.tag == "DSPACCNAME":
            current_name = get_text(child, "DSPDISPNAME")
        elif child.tag == "DSPSTKINFO" and current_name:
            stk_cl = child.find("DSPSTKCL")
            qty: Decimal | None = None
            unit = ""
            rate: Decimal | None = None
            value: Decimal | None = None
            if stk_cl is not None:
                qty_parts = get_text(stk_cl, "DSPCLQTY").split()
                if qty_parts:
                    qty = parse_decimal(qty_parts[0])
                    unit = " ".join(qty_parts[1:])
                rate = _number_before(get_text(stk_cl, "DSPCLRATE"), "/")
                value = parse_decimal(get_text(stk_cl, "DSPCLAMTA"))
            items.append({
                "name": current_name,
                "base_units": unit,
                "closing_quantity": qty,
                "closing_rate": rate,
                "closing_value": value,
            })
            current_name = None
    return items
