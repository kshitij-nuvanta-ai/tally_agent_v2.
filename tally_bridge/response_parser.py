"""
Parse TallyPrime XML responses into normalized Python dicts.
Handles Tally's inconsistent casing, empty tags, comma-formatted amounts.
"""
import xml.etree.ElementTree as ET
from datetime import datetime

# The one definition of each lives in xml_utils; the names stay importable from here.
from tally_bridge.xml_utils import detect_error, get_text as _get_text, sanitize_xml  # noqa: F401


def parse_amount(text: str | None) -> float:
    if not text or not text.strip():
        return 0.0
    cleaned = text.strip().replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_overdue_days(text: str) -> int | None:
    """Parse overdue days from Tally — handles '45', '-10', '45 Days', etc."""
    if not text or not text.strip():
        return None
    num_str = text.strip().split()[0]
    try:
        return int(num_str)
    except ValueError:
        return None


def parse_trial_balance(raw_xml: str) -> list[dict]:
    """Parse Trial Balance report from Tally's TYPE=Data XML.

    Real Tally structure uses alternating sibling pairs:
      DSPACCNAME (contains DSPDISPNAME) followed by
      DSPACCINFO (contains DSPCLDRAMT>DSPCLDRAMTA and DSPCLCRAMT>DSPCLCRAMTA)
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows = []
    # Iterate direct children of root, pairing DSPACCNAME with following DSPACCINFO
    children = list(root)
    current_name = None
    for child in children:
        if child.tag == "DSPACCNAME":
            current_name = _get_text(child, "DSPDISPNAME")
        elif child.tag == "DSPACCINFO" and current_name:
            # Debit: DSPCLDRAMT > DSPCLDRAMTA
            debit_el = child.find("DSPCLDRAMT")
            debit = 0.0
            if debit_el is not None:
                debit = parse_amount(_get_text(debit_el, "DSPCLDRAMTA"))
            # Credit: DSPCLCRAMT > DSPCLCRAMTA
            credit_el = child.find("DSPCLCRAMT")
            credit = 0.0
            if credit_el is not None:
                credit = parse_amount(_get_text(credit_el, "DSPCLCRAMTA"))
            rows.append({
                "account_name": current_name,
                "debit_amount": debit,
                "credit_amount": credit,
                "closing_balance": debit + credit,
            })
            current_name = None
    return rows


def parse_ledger_list(raw_xml: str) -> list[dict]:
    root = ET.fromstring(sanitize_xml(raw_xml))
    ledgers = []
    for ledger in root.iter("LEDGER"):
        name = _get_text(ledger, "NAME") or ledger.get("NAME", "")
        if not name:
            continue
        ledgers.append({
            "name": name,
            "parent_group": _get_text(ledger, "PARENT"),
            "closing_balance": parse_amount(_get_text(ledger, "CLOSINGBALANCE")),
            "opening_balance": parse_amount(_get_text(ledger, "OPENINGBALANCE")),
        })
    return ledgers


def parse_profit_and_loss(raw_xml: str) -> list[dict]:
    """Parse Profit & Loss report from Tally's TYPE=Data XML.

    Real Tally structure uses alternating sibling pairs:
      DSPACCNAME (contains DSPDISPNAME) followed by
      PLAMT (contains BSMAINAMT for main amounts, PLSUBAMT for sub-items)
    Amount: prefer BSMAINAMT, fall back to PLSUBAMT.
    Sign convention: negative = debit (expense), positive = credit (income).
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows = []
    children = list(root)
    current_name = None
    for child in children:
        if child.tag == "DSPACCNAME":
            current_name = _get_text(child, "DSPDISPNAME")
        elif child.tag == "PLAMT" and current_name:
            main_amt = parse_amount(_get_text(child, "BSMAINAMT"))
            sub_amt = parse_amount(_get_text(child, "PLSUBAMT"))
            amount = main_amt if main_amt != 0.0 else sub_amt
            debit = amount if amount < 0 else 0.0
            credit = amount if amount > 0 else 0.0
            rows.append({
                "account_name": current_name,
                "debit_amount": debit,
                "credit_amount": credit,
                "closing_balance": amount,
            })
            current_name = None
    return rows


def parse_balance_sheet(raw_xml: str) -> list[dict]:
    """Parse Balance Sheet report from Tally's TYPE=Data XML.

    Real Tally structure uses alternating sibling pairs:
      BSNAME (contains DSPACCNAME > DSPDISPNAME) followed by
      BSAMT (contains BSMAINAMT for main amounts, BSSUBAMT for sub-items)
    Sign convention: negative = debit (asset), positive = credit (liability/equity).
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows = []
    children = list(root)
    current_name = None
    for child in children:
        if child.tag == "BSNAME":
            name_el = child.find(".//DSPDISPNAME")
            if name_el is not None and name_el.text:
                current_name = name_el.text.strip()
        elif child.tag == "BSAMT" and current_name:
            main_amt = parse_amount(_get_text(child, "BSMAINAMT"))
            sub_amt = parse_amount(_get_text(child, "BSSUBAMT"))
            amount = main_amt if main_amt != 0.0 else sub_amt
            debit = amount if amount < 0 else 0.0
            credit = amount if amount > 0 else 0.0
            rows.append({
                "account_name": current_name,
                "debit_amount": debit,
                "credit_amount": credit,
                "closing_balance": amount,
            })
            current_name = None
    return rows


def parse_bills(raw_xml: str) -> list[dict]:
    root = ET.fromstring(sanitize_xml(raw_xml))
    bills = []
    # Tally returns BILLFIXED (not BILLSFIXED) with sibling BILLCL/BILLDUE/BILLOVERDUE.
    # We iterate all children and pair BILLFIXED with the BILLCL that follows it.
    elements = list(root.iter())
    i = 0
    while i < len(elements):
        el = elements[i]
        if el.tag == "BILLFIXED":
            bill_ref = _get_text(el, "BILLREF")
            if not bill_ref:
                i += 1
                continue
            party = _get_text(el, "BILLPARTY")
            bill_date = _get_text(el, "BILLDATE")
            # Look for sibling BILLCL after this BILLFIXED
            amount = 0.0
            due_date = ""
            overdue_days = ""
            for j in range(i + 1, min(i + 10, len(elements))):
                sib = elements[j]
                if sib.tag == "BILLFIXED":
                    break
                if sib.tag == "BILLCL" and sib.text:
                    amount = abs(parse_amount(sib.text))
                elif sib.tag == "BILLDUE" and sib.text:
                    due_date = sib.text.strip()
                elif sib.tag == "BILLOVERDUE" and sib.text:
                    overdue_days = sib.text.strip()
            bills.append({
                "bill_number": bill_ref,
                "party_name": party,
                "bill_date": bill_date,
                "amount": amount,
                "pending_amount": amount,
                "due_date": due_date,
                "overdue_days": _parse_overdue_days(overdue_days),
            })
        i += 1
    return bills


def parse_stock_summary(raw_xml: str) -> list[dict]:
    """Parse Stock Summary report from Tally's TYPE=Data XML.

    Real Tally structure uses alternating sibling pairs:
      DSPACCNAME (contains DSPDISPNAME) followed by
      DSPSTKINFO (contains DSPSTKCL with DSPCLQTY, DSPCLRATE, DSPCLAMTA)

    DSPCLQTY format: "-2.0000 NOS" (number + space + unit).
    No parent group info in TYPE=Data reports.
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    items = []
    children = list(root)
    current_name = None
    for child in children:
        if child.tag == "DSPACCNAME":
            current_name = _get_text(child, "DSPDISPNAME")
        elif child.tag == "DSPSTKINFO" and current_name:
            stk_cl = child.find("DSPSTKCL")
            qty = 0.0
            unit = ""
            rate = 0.0
            value = 0.0
            if stk_cl is not None:
                qty_text = _get_text(stk_cl, "DSPCLQTY")
                if qty_text:
                    parts = qty_text.split()
                    if parts:
                        try:
                            qty = float(parts[0].replace(",", ""))
                        except ValueError:
                            pass
                        if len(parts) > 1:
                            unit = " ".join(parts[1:])
                rate = parse_amount(_get_text(stk_cl, "DSPCLRATE"))
                value = parse_amount(_get_text(stk_cl, "DSPCLAMTA"))
            items.append({
                "name": current_name,
                "parent_group": "",
                "base_units": unit,
                "closing_quantity": qty,
                "closing_rate": rate,
                "closing_value": value,
            })
            current_name = None
    return items


def _filter_vouchers_by_date(vouchers: list[dict], from_date: str, to_date: str) -> list[dict]:
    """Filter vouchers by date range (DD-MM-YYYY format). Safety net for TDL filter."""
    try:
        dt_from = datetime.strptime(from_date, "%d-%m-%Y")
        dt_to = datetime.strptime(to_date, "%d-%m-%Y")
    except ValueError:
        return vouchers
    filtered = []
    for v in vouchers:
        date_str = v.get("date", "")
        if not date_str:
            filtered.append(v)  # Keep vouchers without dates (can't verify range)
            continue
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            if dt_from <= dt <= dt_to:
                filtered.append(v)
        except ValueError:
            filtered.append(v)
    return filtered


def parse_vouchers(raw_xml: str, from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    root = ET.fromstring(sanitize_xml(raw_xml))
    vouchers = []
    for v in root.iter("VOUCHER"):
        date_str = _get_text(v, "DATE")
        voucher_type = _get_text(v, "VOUCHERTYPENAME")
        voucher_number = _get_text(v, "VOUCHERNUMBER")
        party = _get_text(v, "PARTYLEDGERNAME")
        narration = _get_text(v, "NARRATION")
        # Extract month field for grouping
        month_str = ""
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                month_str = dt.strftime("%b %Y")
            except ValueError:
                month_str = ""
        ledger_entries = []
        for entry in v.findall("ALLLEDGERENTRIES.LIST"):
            ledger_entries.append({
                "ledger_name": _get_text(entry, "LEDGERNAME"),
                "amount": parse_amount(_get_text(entry, "AMOUNT")),
            })
        inventory_entries = []
        for inv_entry in v.findall("ALLINVENTORYENTRIES.LIST"):
            item_name = _get_text(inv_entry, "STOCKITEMNAME")
            if not item_name:
                continue
            # Parse quantity: "2 Nos" → 2.0
            qty_text = _get_text(inv_entry, "ACTUALQTY")
            qty = 0.0
            if qty_text:
                parts = qty_text.split()
                if parts:
                    try:
                        qty = abs(float(parts[0].replace(",", "")))
                    except ValueError:
                        pass
            # Parse rate: "45000/Nos" → 45000.0
            rate_text = _get_text(inv_entry, "RATE")
            rate = 0.0
            if rate_text:
                rate_num = rate_text.split("/")[0].strip()
                rate = parse_amount(rate_num)
            inventory_entries.append({
                "item_name": item_name,
                "quantity": qty,
                "rate": rate,
                "amount": parse_amount(_get_text(inv_entry, "AMOUNT")),
            })
        # Skip ghost/empty vouchers that Tally TDL sometimes emits
        if not date_str and not voucher_type and not voucher_number:
            continue
        vouchers.append({
            "date": date_str,
            "month": month_str,
            "voucher_type": voucher_type,
            "voucher_number": voucher_number,
            "party_name": party,
            "narration": narration,
            "ledger_entries": ledger_entries,
            "inventory_entries": inventory_entries,
        })
    if from_date and to_date:
        vouchers = _filter_vouchers_by_date(vouchers, from_date, to_date)
    return vouchers


def parse_stock_items(raw_xml: str) -> list[dict]:
    """Parse CustomStockItemList collection response.

    CLOSINGBALANCE format: "10.0000 Nos" (number + space + unit) in our fixtures.
    Note: Real Tally TYPE=Collection may return just the numeric value without unit.
    The BASEUNITS fallback handles this — if CLOSINGBALANCE has no unit suffix,
    the unit comes from the dedicated BASEUNITS field.
    CLOSINGRATE format: "38000.00/Nos" (number + slash + unit)
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    items = []
    for item in root.iter("STOCKITEM"):
        name = _get_text(item, "NAME") or item.get("NAME", "")
        if not name:
            continue
        cb_text = _get_text(item, "CLOSINGBALANCE")
        qty = 0.0
        unit = _get_text(item, "BASEUNITS")
        if cb_text:
            parts = cb_text.split()
            if parts:
                try:
                    qty = float(parts[0].replace(",", ""))
                except ValueError:
                    pass
                if len(parts) > 1 and not unit:
                    unit = " ".join(parts[1:])
        rate_text = _get_text(item, "CLOSINGRATE")
        rate = 0.0
        if rate_text:
            rate_num = rate_text.split("/")[0].strip()
            rate = parse_amount(rate_num)
        items.append({
            "name": name,
            "parent_group": _get_text(item, "PARENT"),
            "base_units": unit,
            "closing_balance": qty,
            "closing_rate": rate,
            "closing_value": parse_amount(_get_text(item, "CLOSINGVALUE")),
        })
    return items


def parse_groups(raw_xml: str) -> list[dict]:
    """Parse CustomGroupList collection response."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    groups = []
    for group in root.iter("GROUP"):
        name = _get_text(group, "NAME") or group.get("NAME", "")
        if not name:
            continue
        groups.append({
            "name": name,
            "parent": _get_text(group, "PARENT"),
        })
    return groups


def parse_stock_group_list(raw_xml: str) -> list[str]:
    """Parse a CustomStockGroupList collection response into group names.

    Each STOCKGROUP yields a name from a child ``<NAME>`` element, the ``NAME``
    attribute, or inline text. Nameless entries are skipped.
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    names: list[str] = []
    for grp in root.iter("STOCKGROUP"):
        name = (_get_text(grp, "NAME") or grp.get("NAME", "") or (grp.text or "")).strip()
        if name:
            names.append(name)
    return names


def parse_party_vouchers(raw_xml: str) -> list[dict]:
    """Parse a party-voucher collection response (verified probe E8).

    Each VOUCHER yields: date, voucher_number, voucher_type, party, reference,
    amount. Ghost/empty vouchers (no date, number, or type) are skipped.
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    results: list[dict] = []
    for v in root.iter("VOUCHER"):
        date_str = _get_text(v, "DATE")
        voucher_number = _get_text(v, "VOUCHERNUMBER")
        voucher_type = _get_text(v, "VOUCHERTYPENAME")
        party = _get_text(v, "PARTYLEDGERNAME")
        reference = _get_text(v, "REFERENCE")
        amount = parse_amount(_get_text(v, "AMOUNT"))
        if not date_str and not voucher_number and not voucher_type:
            continue
        results.append({
            "date": date_str,
            "voucher_number": voucher_number,
            "voucher_type": voucher_type,
            "party": party,
            "reference": reference,
            "amount": amount,
        })
    return results


def parse_cash_flow(raw_xml: str) -> list[dict]:
    """Parse Cash Flow report — assumed same sibling-pair structure as Trial Balance.

    TODO: Validate against live Tally instance. The actual Cash Flow XML structure
    may differ from Trial Balance. This is a separate function (not just an alias)
    so it can be adjusted independently after live testing.
    """
    return parse_trial_balance(raw_xml)


def parse_import_response(raw_xml: str) -> dict:
    """Parse Tally's IMPORTDATA response.

    Tally returns CREATED/ALTERED/DELETED counts, plus ERRORS and EXCEPTIONS.
    EXCEPTIONS=1 (without ERRORS) means silent failure — typically malformed XML
    (e.g., missing NAME.LIST in master operations).

    Returns:
        {
            "success": bool,
            "created": int,
            "altered": int,
            "deleted": int,
            "errors": int,
            "exceptions": int,
            "last_vch_id": str | None,
            "error_message": str | None,
        }
    """
    if not raw_xml or not raw_xml.strip():
        return {
            "success": False, "error_message": "Empty response from Tally",
            "created": 0, "altered": 0, "deleted": 0, "errors": 0,
            "exceptions": 0, "last_vch_id": None,
        }
    try:
        root = ET.fromstring(sanitize_xml(raw_xml))
    except ET.ParseError:
        return {
            "success": False, "error_message": "Invalid XML in Tally response",
            "created": 0, "altered": 0, "deleted": 0, "errors": 0,
            "exceptions": 0, "last_vch_id": None,
        }

    def _find_int(tag: str) -> int:
        el = root.find(tag)
        if el is None:
            el = root.find(f".//{tag}")
        return int(el.text.strip()) if el is not None and el.text else 0

    created = _find_int("CREATED")
    altered = _find_int("ALTERED")
    deleted = _find_int("DELETED")
    errors = _find_int("ERRORS")
    exceptions = _find_int("EXCEPTIONS")
    last_vch_id = root.findtext("LASTVCHID") or root.findtext(".//LASTVCHID")
    if last_vch_id == "0":
        last_vch_id = None

    error_message = None
    line_error = root.findtext("LINEERROR") or root.findtext(".//LINEERROR")
    if line_error:
        # Translate Tally's misleading "Voucher date is missing" error.
        # Despite the message, the date IS present in the XML. The real cause is
        # that the voucher date is after Tally's configured "current date".
        # The user must press F2 in Tally (Gateway of Tally) and update the
        # current date to a date >= the voucher date.
        if "voucher date is missing" in line_error.lower():
            error_message = (
                "Tally rejected the voucher date. Its configured 'current date' is "
                "earlier than the voucher date. In Tally: press F2 (at Gateway of "
                "Tally) and set the current date to today or later, then retry."
            )
        else:
            error_message = line_error
    elif errors > 0:
        error_message = f"Tally reported {errors} error(s)"
    elif exceptions > 0:
        error_message = f"Tally reported {exceptions} exception(s) — likely malformed XML"

    success = errors == 0 and exceptions == 0 and not line_error and (created > 0 or altered > 0 or deleted > 0)
    return {
        "success": success,
        "created": created,
        "altered": altered,
        "deleted": deleted,
        "errors": errors,
        "exceptions": exceptions,
        "last_vch_id": last_vch_id,
        "error_message": error_message,
    }
