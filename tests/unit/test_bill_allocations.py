"""Unit tests for BILLALLOCATIONS.LIST support in voucher builders.

Covers sales/purchase/receipt/payment vouchers — verifies:
- New Ref / Agst Ref / On Account billtype handling
- Multiple bills on a single party line
- Sign-mirroring with the party LEDGERENTRY amount
- Backwards-compat: bill_allocations=None ⇒ no BILLALLOCATIONS.LIST emitted
- Optional credit_period field

XML envelope contract from docs/tally-write-exploration-v4.md. See task brief for shape.
"""
import xml.etree.ElementTree as ET

from tally_bridge.import_builder import (
    build_create_payment_voucher,
    build_create_purchase_voucher,
    build_create_receipt_voucher,
    build_create_sales_voucher,
)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _party_entry(voucher: ET.Element, tag: str = "LEDGERENTRIES.LIST") -> ET.Element:
    """Find the party LEDGERENTRY block (the one with ISPARTYLEDGER=Yes,
    or the second ALLLEDGERENTRIES.LIST for receipt/payment)."""
    for entry in voucher.findall(tag):
        if entry.findtext("ISPARTYLEDGER") == "Yes":
            return entry
    raise AssertionError(f"No party ledger entry found under <{tag}>")


# ---------- Sales: New Ref ----------

def test_sales_voucher_with_new_ref_bill_allocation():
    xml = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_01",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 2, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Test sale with bill ref",
        gst_mode="intra",
        company="Bharat Traders Private Limited",
        bill_allocations=[
            {"name": "S_TEST_01", "type": "New Ref", "amount": 106200.00,
             "credit_period": "30 Days"},
        ],
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    party = _party_entry(v)
    # Party amount on sales: -tot (Dr-deemed-positive). Bill alloc must mirror sign.
    assert float(party.findtext("AMOUNT")) == -106200.00
    allocs = party.findall("BILLALLOCATIONS.LIST")
    assert len(allocs) == 1
    a = allocs[0]
    assert a.findtext("NAME") == "S_TEST_01"
    assert a.findtext("BILLTYPE") == "New Ref"
    # Sign mirrors the party AMOUNT (-106200 is the party-line value)
    assert float(a.findtext("AMOUNT")) == -106200.00
    assert a.findtext("BILLCREDITPERIOD") == "30 Days"


def test_sales_voucher_credit_period_omitted_when_not_provided():
    xml = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_02",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="No credit period",
        gst_mode="intra",
        company="X",
        bill_allocations=[
            {"name": "S_TEST_02", "type": "New Ref", "amount": 53100.00},
        ],
    )
    root = _root(xml)
    a = root.find(".//VOUCHER/LEDGERENTRIES.LIST/BILLALLOCATIONS.LIST")
    assert a is not None
    assert a.find("BILLCREDITPERIOD") is None


def test_sales_voucher_multiple_bills_on_party_line():
    xml = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_03",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 2, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Split allocation",
        gst_mode="intra",
        company="X",
        bill_allocations=[
            {"name": "BILL_A", "type": "New Ref", "amount": 60000.00},
            {"name": "BILL_B", "type": "New Ref", "amount": 46200.00},
        ],
    )
    root = _root(xml)
    party = _party_entry(root.find(".//VOUCHER"))
    allocs = party.findall("BILLALLOCATIONS.LIST")
    assert len(allocs) == 2
    assert allocs[0].findtext("NAME") == "BILL_A"
    assert allocs[1].findtext("NAME") == "BILL_B"
    # Both signs mirror party line (negative on sales)
    assert float(allocs[0].findtext("AMOUNT")) == -60000.00
    assert float(allocs[1].findtext("AMOUNT")) == -46200.00


def test_sales_voucher_no_bill_allocations_baseline_unchanged():
    """When bill_allocations is None, output must NOT contain BILLALLOCATIONS.LIST."""
    xml_with_none = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_04",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Baseline",
        gst_mode="intra",
        company="X",
        bill_allocations=None,
    )
    xml_without_arg = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_04",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Baseline",
        gst_mode="intra",
        company="X",
    )
    assert xml_with_none == xml_without_arg
    assert "BILLALLOCATIONS.LIST" not in xml_with_none


def test_sales_voucher_empty_bill_allocations_baseline_unchanged():
    xml = build_create_sales_voucher(
        date="20260302",
        voucher_number="S_TEST_05",
        party="Apex Technologies Pvt Ltd",
        items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
        narration="Baseline",
        gst_mode="intra",
        company="X",
        bill_allocations=[],
    )
    assert "BILLALLOCATIONS.LIST" not in xml


# ---------- Purchase: New Ref ----------

def test_purchase_voucher_with_new_ref_bill_allocation():
    xml = build_create_purchase_voucher(
        date="20260302",
        voucher_number="P_TEST_01",
        party="Samsung India Electronics",
        items=[("Samsung 24 inch Monitor", 5, 11000, "Purchase - Electronics", "Nos", 18)],
        narration="Purchase with bill ref",
        gst_mode="intra",
        company="X",
        bill_allocations=[
            {"name": "P_TEST_01", "type": "New Ref", "amount": 64900.00},
        ],
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    party = _party_entry(v)
    # Purchase: party AMOUNT is +tot (Cr-deemed-positive). Bill alloc must mirror sign.
    assert float(party.findtext("AMOUNT")) == 64900.00
    a = party.find("BILLALLOCATIONS.LIST")
    assert a is not None
    assert a.findtext("BILLTYPE") == "New Ref"
    assert float(a.findtext("AMOUNT")) == 64900.00


def test_purchase_voucher_no_bill_allocations_baseline_unchanged():
    xml = build_create_purchase_voucher(
        date="20260302",
        voucher_number="P_TEST_02",
        party="Samsung India Electronics",
        items=[("Samsung 24 inch Monitor", 1, 11000, "Purchase - Electronics", "Nos", 18)],
        narration="Baseline",
        gst_mode="intra",
        company="X",
    )
    assert "BILLALLOCATIONS.LIST" not in xml


# ---------- Receipt: Agst Ref ----------

def test_receipt_voucher_with_agst_ref_bill_allocation():
    """Receipt clearing an existing sales bill — Agst Ref."""
    xml = build_create_receipt_voucher(
        date="20260302",
        voucher_number="RCT_TEST_01",
        party="Apex Technologies Pvt Ltd",
        bank_ledger="HDFC Bank - Current A/c",
        amount=106200.00,
        narration="Receipt against S_TEST_01",
        company="X",
        bill_allocations=[
            {"name": "S_TEST_01", "type": "Agst Ref", "amount": 106200.00},
        ],
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    # Receipt party uses ALLLEDGERENTRIES.LIST (no ISPARTYLEDGER tag — second entry).
    entries = v.findall("ALLLEDGERENTRIES.LIST")
    assert len(entries) == 2
    party = entries[1]
    assert party.findtext("LEDGERNAME") == "Apex Technologies Pvt Ltd"
    # Receipt: party AMOUNT is +amount (Cr-deemed-positive)
    assert float(party.findtext("AMOUNT")) == 106200.00
    a = party.find("BILLALLOCATIONS.LIST")
    assert a is not None
    assert a.findtext("NAME") == "S_TEST_01"
    assert a.findtext("BILLTYPE") == "Agst Ref"
    # Sign mirrors party line (+ on receipt)
    assert float(a.findtext("AMOUNT")) == 106200.00


def test_receipt_voucher_on_account():
    xml = build_create_receipt_voucher(
        date="20260302",
        voucher_number="RCT_TEST_02",
        party="Apex Technologies Pvt Ltd",
        bank_ledger="HDFC Bank - Current A/c",
        amount=50000.00,
        narration="On-account receipt",
        company="X",
        bill_allocations=[
            {"name": "OnAccount", "type": "On Account", "amount": 50000.00},
        ],
    )
    root = _root(xml)
    party = root.findall(".//VOUCHER/ALLLEDGERENTRIES.LIST")[1]
    a = party.find("BILLALLOCATIONS.LIST")
    assert a.findtext("BILLTYPE") == "On Account"
    assert float(a.findtext("AMOUNT")) == 50000.00


def test_receipt_voucher_no_bill_allocations_baseline_unchanged():
    xml_with_none = build_create_receipt_voucher(
        date="20260302",
        voucher_number="RCT001",
        party="Apex Technologies Pvt Ltd",
        bank_ledger="HDFC Bank - Current A/c",
        amount=94000,
        narration="Receipt against S001",
        company="X",
        bill_allocations=None,
    )
    xml_without_arg = build_create_receipt_voucher(
        date="20260302",
        voucher_number="RCT001",
        party="Apex Technologies Pvt Ltd",
        bank_ledger="HDFC Bank - Current A/c",
        amount=94000,
        narration="Receipt against S001",
        company="X",
    )
    assert xml_with_none == xml_without_arg
    assert "BILLALLOCATIONS.LIST" not in xml_with_none


# ---------- Payment: Agst Ref ----------

def test_payment_voucher_with_agst_ref_bill_allocation():
    """Payment clearing an existing purchase bill — Agst Ref."""
    xml = build_create_payment_voucher(
        date="20260302",
        debit_ledger="Samsung India Electronics",
        credit_ledger="HDFC Bank - Current A/c",
        amount=64900.00,
        narration="Payment against P_TEST_01",
        company="X",
        bill_allocations=[
            {"name": "P_TEST_01", "type": "Agst Ref", "amount": 64900.00},
        ],
    )
    root = _root(xml)
    v = root.find(".//VOUCHER")
    entries = v.findall("ALLLEDGERENTRIES.LIST")
    # First entry is the debit (party here), with -amount per existing convention
    debit = entries[0]
    assert debit.findtext("LEDGERNAME") == "Samsung India Electronics"
    assert float(debit.findtext("AMOUNT")) == -64900.00
    a = debit.find("BILLALLOCATIONS.LIST")
    assert a is not None
    assert a.findtext("NAME") == "P_TEST_01"
    assert a.findtext("BILLTYPE") == "Agst Ref"
    # Sign mirrors the debit-ledger line
    assert float(a.findtext("AMOUNT")) == -64900.00


def test_payment_voucher_no_bill_allocations_baseline_unchanged():
    xml_with_none = build_create_payment_voucher(
        date="20260302",
        debit_ledger="Travel",
        credit_ledger="Cash",
        amount=1000.00,
        narration="Travel exp",
        company="X",
        bill_allocations=None,
    )
    xml_without_arg = build_create_payment_voucher(
        date="20260302",
        debit_ledger="Travel",
        credit_ledger="Cash",
        amount=1000.00,
        narration="Travel exp",
        company="X",
    )
    assert xml_with_none == xml_without_arg
    assert "BILLALLOCATIONS.LIST" not in xml_with_none
