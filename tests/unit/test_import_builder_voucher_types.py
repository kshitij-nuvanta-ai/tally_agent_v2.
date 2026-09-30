"""Unit tests for new Stage 1 builders (Unit, StockGroup, StockItem, GST ledger,
Sales/Purchase/Receipt/Journal vouchers). Each test asserts XML structure only —
no live Tally calls."""
import xml.etree.ElementTree as ET

import pytest

from tally_bridge.import_builder import build_create_unit


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def test_build_create_unit_has_no_name_list():
    xml = build_create_unit("Nos", formal_name="Numbers", company="Bharat Traders Private Limited")
    root = _root(xml)
    unit = root.find(".//UNIT")
    assert unit is not None
    assert unit.get("ACTION") == "Create"
    # NAME.LIST is forbidden on UNIT (causes "BAD UNIT NAME" per v4 doc)
    assert unit.find("NAME.LIST") is None
    assert unit.findtext("NAME") == "Nos"
    assert unit.findtext("ISSIMPLEUNIT") == "Yes"


def test_build_create_unit_uses_all_masters_report():
    xml = build_create_unit("Nos", formal_name="Numbers", company="Bharat Traders Private Limited")
    root = _root(xml)
    assert root.findtext(".//REPORTNAME") == "All Masters"
    assert root.findtext(".//SVCURRENTCOMPANY") == "Bharat Traders Private Limited"

def test_build_create_stock_group_has_name_list():
    from tally_bridge.import_builder import build_create_stock_group
    xml = build_create_stock_group("Electronics", parent="", company="Bharat Traders Private Limited")
    root = _root(xml)
    sg = root.find(".//STOCKGROUP")
    assert sg.get("NAME") == "Electronics"
    assert sg.get("ACTION") == "Create"
    name_list = sg.find("NAME.LIST")
    assert name_list is not None
    assert name_list.findtext("NAME") == "Electronics"
    parent = sg.find("PARENT")
    assert parent is not None
    assert (parent.text or "") == ""
    assert sg.findtext("ISADDABLE") == "No"


def test_build_create_stock_group_with_parent():
    from tally_bridge.import_builder import build_create_stock_group
    xml = build_create_stock_group("Sub Group", parent="Electronics", company="X")
    root = _root(xml)
    assert root.find(".//STOCKGROUP/PARENT").text == "Electronics"

def test_build_create_stock_item_18pct_rate():
    from tally_bridge.import_builder import build_create_stock_item
    xml = build_create_stock_item(
        name="Samsung 24 inch Monitor",
        group="Electronics",
        uom="Nos",
        opening_qty=20,
        opening_rate=11000,
        hsn_code="8528",
        gst_rate=18,
        company="Bharat Traders Private Limited",
    )
    root = _root(xml)
    si = root.find(".//STOCKITEM")
    assert si.get("NAME") == "Samsung 24 inch Monitor"
    assert si.find("NAME.LIST/NAME").text == "Samsung 24 inch Monitor"
    assert si.findtext("PARENT") == "Electronics"
    assert si.findtext("BASEUNITS") == "Nos"
    assert si.findtext("HSNCODE") == "8528"
    assert si.findtext("HSN") == "8528"
    assert si.findtext("GSTAPPLICABLE") == "Applicable"
    assert si.findtext("GSTTYPEOFSUPPLY") == "Goods"

    # GSTDETAILS rates: 18% IGST, 9% CGST, 9% SGST
    gd = si.find("GSTDETAILS.LIST")
    assert gd.findtext("IGSTRATE") == "18"
    assert gd.findtext("CGSTRATE") == "9"
    assert gd.findtext("SGSTRATE") == "9"
    assert gd.findtext("TAXABILITY") == "Taxable"

    # Opening balance
    assert si.findtext("OPENINGBALANCE") == "20 Nos"
    assert si.findtext("OPENINGRATE") == "11000.00/Nos"
    assert si.findtext("OPENINGVALUE") == "220000.00"


def test_build_create_stock_item_12pct_rate_splits_correctly():
    from tally_bridge.import_builder import build_create_stock_item
    xml = build_create_stock_item(
        name="A4 Paper Ream",
        group="Office Supplies",
        uom="Pcs",
        opening_qty=200,
        opening_rate=280,
        hsn_code="4802",
        gst_rate=12,
        company="X",
    )
    root = _root(xml)
    gd = root.find(".//GSTDETAILS.LIST")
    assert gd.findtext("IGSTRATE") == "12"
    assert gd.findtext("CGSTRATE") == "6"
    assert gd.findtext("SGSTRATE") == "6"


def test_build_create_stock_item_empty_hsn_is_gst_not_applicable_no_gstdetails():
    """Vision often extracts no HSN. GST-applicable-without-HSN triggers Tally's
    blocking "HSN/SAC required" modal, locking the API. So when hsn_code is empty
    the item must be plain name+unit+group: GSTAPPLICABLE=Not Applicable, and NO
    GSTDETAILS.LIST / HSN elements at all (GST is posted via voucher tax lines)."""
    from tally_bridge.import_builder import build_create_stock_item
    xml = build_create_stock_item(
        name="Generic Widget",
        group="Electronics",
        uom="Nos",
        opening_qty=0,
        opening_rate=0,
        hsn_code="",
        gst_rate=18,
        company="X",
    )
    root = _root(xml)
    si = root.find(".//STOCKITEM")
    # Required fields still present
    assert si.get("NAME") == "Generic Widget"
    assert si.find("NAME.LIST/NAME").text == "Generic Widget"
    assert si.findtext("PARENT") == "Electronics"
    assert si.findtext("BASEUNITS") == "Nos"
    # GST disabled at master level — avoids the HSN-mandatory modal
    assert si.findtext("GSTAPPLICABLE") == "Not Applicable"
    # No HSN of any form
    assert not (si.findtext("HSNCODE") or "")
    assert not (si.findtext("HSN") or "")
    assert si.find("HSNDETAILS.LIST") is None
    # No GST rate block at all
    assert si.find("GSTDETAILS.LIST") is None


def test_build_create_stock_item_with_hsn_still_emits_hsn():
    """Regression: a non-empty HSN still emits HSNCODE/HSN and HSNDETAILS."""
    from tally_bridge.import_builder import build_create_stock_item
    xml = build_create_stock_item(
        name="Bond Paper",
        group="Office Supplies",
        uom="Pcs",
        opening_qty=0,
        opening_rate=0,
        hsn_code="4802",
        gst_rate=12,
        company="X",
    )
    root = _root(xml)
    si = root.find(".//STOCKITEM")
    assert si.findtext("HSNCODE") == "4802"
    assert si.findtext("HSN") == "4802"
    assert si.find("HSNDETAILS.LIST") is not None


def test_build_create_ledger_with_opening_state_gstin():
    from tally_bridge.import_builder import build_create_ledger
    xml = build_create_ledger(
        name="Apex Technologies Pvt Ltd",
        parent="North Zone Debtors",
        company="X",
        gstin="27AAACA0000A1Z5",
        state="Maharashtra",
        gst_reg_type="Regular",
        opening_balance=0,
        is_billwise=True,
    )
    root = _root(xml)
    led = root.find(".//LEDGER")
    assert led.findtext("PARTYGSTIN") == "27AAACA0000A1Z5"
    assert led.findtext("LEDSTATENAME") == "Maharashtra"
    assert led.findtext("GSTREGISTRATIONTYPE") == "Regular"
    assert led.findtext("ISBILLWISEON") == "Yes"


def test_build_create_ledger_with_opening_balance_only():
    from tally_bridge.import_builder import build_create_ledger
    xml = build_create_ledger(
        name="Capital Account",
        parent="Capital Account",
        company="X",
        opening_balance=750000,
    )
    root = _root(xml)
    assert root.find(".//LEDGER").findtext("OPENINGBALANCE") == "750000.00"


def test_build_create_ledger_back_compat():
    """Existing call signature (positional + gstin only) still works."""
    from tally_bridge.import_builder import build_create_ledger
    xml = build_create_ledger("Travel", "Indirect Expenses", "X")
    root = _root(xml)
    led = root.find(".//LEDGER")
    assert led.findtext("PARENT") == "Indirect Expenses"
    assert led.find("OPENINGBALANCE") is None  # not emitted when 0/None


def test_build_create_gst_ledger():
    from tally_bridge.import_builder import build_create_gst_ledger
    xml = build_create_gst_ledger(
        name="CGST Output",
        duty_head="Central Tax",
        company="X",
    )
    root = _root(xml)
    led = root.find(".//LEDGER")
    assert led.get("NAME") == "CGST Output"
    assert led.findtext("PARENT") == "Duties & Taxes"
    assert led.findtext("TAXTYPE") == "GST"
    assert led.findtext("GSTDUTYHEAD") == "Central Tax"
    assert led.findtext("ISBILLWISEON") == "No"
    assert led.findtext("AFFECTSSTOCK") == "No"

def test_build_create_sales_voucher_intra_state_18pct():
    from tally_bridge.import_builder import build_create_sales_voucher
    xml = build_create_sales_voucher(
        date="20251001",
        voucher_number="S001",
        party="Apex Technologies Pvt Ltd",
        items=[
            # (item_name, qty, rate, sales_ledger, uom, gst_rate)
            ("HP Laptop 15s", 2, 45000, "Sales - Electronics", "Nos", 18),
            ("Logitech Wireless Mouse", 5, 800, "Sales - Electronics", "Nos", 18),
        ],
        narration="Invoice #S001 - Laptops and peripherals",
        gst_mode="intra",
        company="Bharat Traders Private Limited",
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    assert v.get("VCHTYPE") == "Sales"
    assert v.get("ACTION") == "Create"
    assert v.findtext("DATE") == "20251001"
    assert v.findtext("VOUCHERNUMBER") == "S001"
    assert v.findtext("PARTYLEDGERNAME") == "Apex Technologies Pvt Ltd"
    assert v.findtext("PERSISTEDVIEW") == "Invoice Voucher View"
    assert v.findtext("ISINVOICE") == "Yes"

    ledger_entries = v.findall("LEDGERENTRIES.LIST")
    # Expected: 1 party + 1 CGST + 1 SGST = 3
    assert len(ledger_entries) == 3

    # Party: ISDEEMEDPOSITIVE=Yes, AMOUNT = -94000.00 - 18% = -110920.00
    party = ledger_entries[0]
    assert party.findtext("LEDGERNAME") == "Apex Technologies Pvt Ltd"
    assert party.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert party.findtext("ISPARTYLEDGER") == "Yes"
    assert float(party.findtext("AMOUNT")) == -110920.00

    # CGST: +8460 (94000 * 9% = 8460)
    cgst = ledger_entries[1]
    assert cgst.findtext("LEDGERNAME") == "CGST Output"
    assert cgst.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(cgst.findtext("AMOUNT")) == 8460.00

    sgst = ledger_entries[2]
    assert sgst.findtext("LEDGERNAME") == "SGST Output"
    assert float(sgst.findtext("AMOUNT")) == 8460.00

    inv_entries = v.findall("ALLINVENTORYENTRIES.LIST")
    assert len(inv_entries) == 2
    laptop = inv_entries[0]
    assert laptop.findtext("STOCKITEMNAME") == "HP Laptop 15s"
    assert laptop.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(laptop.findtext("AMOUNT")) == 90000.00  # 2 * 45000
    assert laptop.findtext("ACTUALQTY") == "2 Nos"
    alloc = laptop.find("ACCOUNTINGALLOCATIONS.LIST")
    assert alloc.findtext("LEDGERNAME") == "Sales - Electronics"
    assert float(alloc.findtext("AMOUNT")) == 90000.00


def test_build_create_sales_voucher_mixed_rate_18_and_12():
    from tally_bridge.import_builder import build_create_sales_voucher
    xml = build_create_sales_voucher(
        date="20251001",
        voucher_number="S004",
        party="Sharma & Sons Traders",
        items=[
            ("A4 Paper Ream 500 sheets", 50, 350, "Sales - Office Supplies", "Pcs", 12),  # 17500 @ 12% → 2100 GST
            ("Box File Pack of 10",       20, 600, "Sales - Office Supplies", "Pcs", 12),  # 12000 @ 12% → 1440 GST
        ],
        narration="Invoice #S004",
        gst_mode="intra",
        company="X",
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    ledger_entries = v.findall("LEDGERENTRIES.LIST")
    # Both lines at 12% → grouped into ONE bucket → 1 party + 1 CGST + 1 SGST
    assert len(ledger_entries) == 3
    cgst = ledger_entries[1]
    sgst = ledger_entries[2]
    # (17500+12000) * 6% = 1770 each
    assert float(cgst.findtext("AMOUNT")) == 1770.00
    assert float(sgst.findtext("AMOUNT")) == 1770.00
    # Party = -(29500 + 2*1770) = -33040
    assert float(ledger_entries[0].findtext("AMOUNT")) == -33040.00


def test_build_create_sales_voucher_inter_state_uses_igst():
    from tally_bridge.import_builder import build_create_sales_voucher
    xml = build_create_sales_voucher(
        date="20251001",
        voucher_number="S100",
        party="Out of State Buyer",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Inter-state",
        gst_mode="inter",
        company="X",
    )
    root = _root(xml)
    ledger_entries = root.find(".//VOUCHER").findall("LEDGERENTRIES.LIST")
    # 1 party + 1 IGST (no CGST/SGST)
    assert len(ledger_entries) == 2
    assert ledger_entries[1].findtext("LEDGERNAME") == "IGST Output"
    assert float(ledger_entries[1].findtext("AMOUNT")) == 8100.00  # 45000 * 18%

def test_build_create_purchase_voucher_intra_state_signs():
    from tally_bridge.import_builder import build_create_purchase_voucher
    xml = build_create_purchase_voucher(
        date="20250928",
        voucher_number="P001",
        party="Samsung India Electronics",
        items=[
            ("Samsung 24 inch Monitor", 25, 11000, "Purchase - Electronics", "Nos", 18),
        ],
        narration="P001",
        gst_mode="intra",
        company="X",
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    assert v.get("VCHTYPE") == "Purchase"
    assert v.findtext("PERSISTEDVIEW") == "Invoice Voucher View"
    assert v.findtext("ISINVOICE") == "Yes"

    ledger_entries = v.findall("LEDGERENTRIES.LIST")
    # 1 party + 1 CGST + 1 SGST
    assert len(ledger_entries) == 3

    # Party: ISDEEMEDPOSITIVE=No, AMOUNT = +275000 + 18% = +324500
    party = ledger_entries[0]
    assert party.findtext("ISDEEMEDPOSITIVE") == "No"
    assert party.findtext("ISPARTYLEDGER") == "Yes"
    assert float(party.findtext("AMOUNT")) == 324500.00

    # GST input: ISDEEMEDPOSITIVE=Yes, AMOUNT NEGATIVE
    cgst = ledger_entries[1]
    assert cgst.findtext("LEDGERNAME") == "CGST Input"
    assert cgst.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(cgst.findtext("AMOUNT")) == -24750.00  # -(275000 * 9%)

    # Inventory: ISDEEMEDPOSITIVE=Yes, AMOUNT NEGATIVE
    inv = v.find("ALLINVENTORYENTRIES.LIST")
    assert inv.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(inv.findtext("AMOUNT")) == -275000.00
    alloc = inv.find("ACCOUNTINGALLOCATIONS.LIST")
    assert alloc.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(alloc.findtext("AMOUNT")) == -275000.00

def test_build_create_receipt_voucher():
    from tally_bridge.import_builder import build_create_receipt_voucher
    xml = build_create_receipt_voucher(
        date="20251020",
        voucher_number="RCT001",
        party="Apex Technologies Pvt Ltd",
        bank_ledger="HDFC Bank - Current A/c",
        amount=94000,
        narration="Receipt against S001",
        company="X",
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    assert v.get("VCHTYPE") == "Receipt"
    assert v.findtext("PERSISTEDVIEW") == "Accounting Voucher View"
    assert v.findtext("VOUCHERNUMBER") == "RCT001"

    entries = v.findall("ALLLEDGERENTRIES.LIST")
    assert len(entries) == 2
    bank, party = entries[0], entries[1]
    assert bank.findtext("LEDGERNAME") == "HDFC Bank - Current A/c"
    assert bank.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(bank.findtext("AMOUNT")) == -94000.00
    assert party.findtext("LEDGERNAME") == "Apex Technologies Pvt Ltd"
    assert party.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(party.findtext("AMOUNT")) == 94000.00


def test_build_create_journal_voucher():
    from tally_bridge.import_builder import build_create_journal_voucher
    xml = build_create_journal_voucher(
        date="20251031",
        voucher_number="J001",
        debit_ledger="Rent",
        credit_ledger="HDFC Bank - Current A/c",
        amount=75000,
        narration="Adjusting entry",
        company="X",
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    assert v.get("VCHTYPE") == "Journal"
    assert v.findtext("PERSISTEDVIEW") == "Accounting Voucher View"
    entries = v.findall("ALLLEDGERENTRIES.LIST")
    assert len(entries) == 2
    debit, credit = entries[0], entries[1]
    assert debit.findtext("LEDGERNAME") == "Rent"
    assert debit.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert float(debit.findtext("AMOUNT")) == -75000.00
    assert credit.findtext("LEDGERNAME") == "HDFC Bank - Current A/c"
    assert credit.findtext("ISDEEMEDPOSITIVE") == "No"
    assert float(credit.findtext("AMOUNT")) == 75000.00


# ---------------------------------------------------------------------------
# REFERENCE / REFERENCEDATE (supplier invoice no + date) — LESSONS §14
# ---------------------------------------------------------------------------

def _sales_xml(**kwargs):
    from tally_bridge.import_builder import build_create_sales_voucher
    base = dict(
        date="20251001",
        voucher_number="S001",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Invoice S001",
        gst_mode="intra",
        company="X",
    )
    base.update(kwargs)
    return build_create_sales_voucher(**base)


def _purchase_xml(**kwargs):
    from tally_bridge.import_builder import build_create_purchase_voucher
    base = dict(
        date="20250928",
        voucher_number="P001",
        party="Samsung India Electronics",
        items=[("Samsung 24 inch Monitor", 1, 11000, "Purchase - Electronics", "Nos", 18)],
        narration="P001",
        gst_mode="intra",
        company="X",
    )
    base.update(kwargs)
    return build_create_purchase_voucher(**base)


# Sales

def test_sales_reference_and_referencedate_both_present():
    xml = _sales_xml(reference="S001", reference_date="20251001")
    v = _root(xml).find(".//VOUCHER")
    refs = v.findall("REFERENCE")
    rdates = v.findall("REFERENCEDATE")
    assert len(refs) == 1 and refs[0].text == "S001"
    assert len(rdates) == 1 and rdates[0].text == "20251001"
    # sibling of DATE
    children = [c.tag for c in v]
    assert "DATE" in children and "REFERENCE" in children and "REFERENCEDATE" in children


def test_sales_reference_omitted_emits_nothing():
    xml = _sales_xml()
    v = _root(xml).find(".//VOUCHER")
    assert v.find("REFERENCE") is None
    assert v.find("REFERENCEDATE") is None


def test_sales_reference_only_no_date():
    xml = _sales_xml(reference="S001")
    v = _root(xml).find(".//VOUCHER")
    assert v.findtext("REFERENCE") == "S001"
    assert v.find("REFERENCEDATE") is None


def test_sales_reference_date_only_no_reference():
    xml = _sales_xml(reference_date="20251001")
    v = _root(xml).find(".//VOUCHER")
    assert v.find("REFERENCE") is None
    assert v.findtext("REFERENCEDATE") == "20251001"


def test_sales_reference_date_invalid_format_raises():
    with pytest.raises(ValueError, match="reference_date"):
        _sales_xml(reference="S001", reference_date="2025-10-01")
    with pytest.raises(ValueError, match="reference_date"):
        _sales_xml(reference="S001", reference_date="01-10-2025")
    with pytest.raises(ValueError, match="reference_date"):
        _sales_xml(reference="S001", reference_date="2025101")  # 7 digits


# Purchase

def test_purchase_reference_and_referencedate_both_present():
    xml = _purchase_xml(reference="P001", reference_date="20250926")
    v = _root(xml).find(".//VOUCHER")
    refs = v.findall("REFERENCE")
    rdates = v.findall("REFERENCEDATE")
    assert len(refs) == 1 and refs[0].text == "P001"
    assert len(rdates) == 1 and rdates[0].text == "20250926"


def test_purchase_reference_omitted_emits_nothing():
    xml = _purchase_xml()
    v = _root(xml).find(".//VOUCHER")
    assert v.find("REFERENCE") is None
    assert v.find("REFERENCEDATE") is None


def test_purchase_reference_only_no_date():
    xml = _purchase_xml(reference="P001")
    v = _root(xml).find(".//VOUCHER")
    assert v.findtext("REFERENCE") == "P001"
    assert v.find("REFERENCEDATE") is None


def test_purchase_reference_date_only_no_reference():
    xml = _purchase_xml(reference_date="20250926")
    v = _root(xml).find(".//VOUCHER")
    assert v.find("REFERENCE") is None
    assert v.findtext("REFERENCEDATE") == "20250926"


def test_purchase_reference_date_invalid_format_raises():
    with pytest.raises(ValueError, match="reference_date"):
        _purchase_xml(reference="P001", reference_date="20250-926")
    with pytest.raises(ValueError, match="reference_date"):
        _purchase_xml(reference_date="abcdefgh")


# Receipt / Payment must NOT accept these kwargs (signature check)

def test_receipt_does_not_accept_reference_kwargs():
    import inspect
    from tally_bridge.import_builder import build_create_receipt_voucher
    params = inspect.signature(build_create_receipt_voucher).parameters
    assert "reference" not in params
    assert "reference_date" not in params


def test_payment_accepts_reference_kwargs():
    # Phase 1 Part A: payment vouchers now carry the supplier invoice no.
    import inspect
    from tally_bridge.import_builder import build_create_payment_voucher
    params = inspect.signature(build_create_payment_voucher).parameters
    assert "reference" in params
    assert "reference_date" in params
