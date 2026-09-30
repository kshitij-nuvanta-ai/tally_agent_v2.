"""Tests for Debit Note / Credit Note XML builders (Group B Task 3).

Sales/Purchase invoice builders already exist (inventory-based) and are covered
by ``tests/unit/test_import_builder.py``. This file covers only the NEW
ledger-only Debit Note / Credit Note builders.

CORRECTNESS NOTE (2026-06-09 live manual test, logs/manual_test_group_b_live.log):
A return posts in the INVERSE direction of the original invoice — it must REDUCE
the outstanding bill, not increase it. The earlier "DN mirrors Purchase / CN
mirrors Sales" convention was a BUG (DN increased the payable, CN increased the
receivable). Correct convention:

  - Debit Note (purchase return) REDUCES the payable: party (supplier) on the
    DEBIT side (ISDEEMEDPOSITIVE=Yes / -amount); contra (purchase-returns) + the
    reversed Input GST on the CREDIT side (ISDEEMEDPOSITIVE=No / +amount).
  - Credit Note (sales return) REDUCES the receivable: party (customer) on the
    CREDIT side (ISDEEMEDPOSITIVE=No / +amount); contra (sales-returns) + the
    reversed Output GST on the DEBIT side (ISDEEMEDPOSITIVE=Yes / -amount).
  - The Agst Ref BILLALLOCATIONS amount mirrors the party-leg sign so it reduces
    the original bill.
  - Both use LEDGERENTRIES.LIST + Invoice Voucher View + ISPARTYLEDGER + Agst Ref.
"""
import xml.etree.ElementTree as ET

import pytest

from tally_bridge.import_builder import (
    build_create_credit_note,
    build_create_debit_note,
)


def _parse(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


def _voucher(xml_str: str) -> ET.Element:
    root = _parse(xml_str)
    return root.find(".//VOUCHER")


def _party_entry(v: ET.Element) -> ET.Element:
    return [
        e for e in v.findall("LEDGERENTRIES.LIST")
        if e.find("ISPARTYLEDGER") is not None
        and e.find("ISPARTYLEDGER").text == "Yes"
    ][0]


class TestCreateDebitNote:
    def test_basic_structure(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Croma",
            purchase_ledger="Purchases", amount=4718.0,
            narration="Return", company="Test Co",
        )
        v = _voucher(xml)
        assert v.get("VCHTYPE") == "Debit Note"
        assert v.get("ACTION") == "Create"
        assert v.find("VOUCHERTYPENAME").text == "Debit Note"
        assert v.find("DATE").text == "20260305"
        assert v.find("PERSISTEDVIEW").text == "Invoice Voucher View"
        assert v.find("ISINVOICE").text == "Yes"

    def test_party_ledger_has_ispartyledger(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Croma",
            purchase_ledger="Purchases", amount=1000.0,
            narration="Return", company="Test Co",
        )
        v = _voucher(xml)
        party = _party_entry(v)
        assert party.find("LEDGERNAME").text == "Croma"

    def test_debit_note_reduces_payable_party_on_debit(self):
        """DN (purchase return) reduces the payable: party = DEBIT side
        (Yes / -amount); contra (purchase-returns) = credit (No / +amount)."""
        xml = build_create_debit_note(
            date="20260305", party_ledger="Croma",
            purchase_ledger="Purchases", amount=1000.0,
            narration="Return", company="Test Co",
        )
        v = _voucher(xml)
        party = _party_entry(v)
        assert party.find("ISDEEMEDPOSITIVE").text == "Yes"
        assert float(party.find("AMOUNT").text) < 0
        contra = [
            e for e in v.findall("LEDGERENTRIES.LIST")
            if e.find("ISPARTYLEDGER") is None
            and "Purchases" in (e.find("LEDGERNAME").text or "")
        ][0]
        assert contra.find("ISDEEMEDPOSITIVE").text == "No"
        assert float(contra.find("AMOUNT").text) > 0

    def test_debit_note_agst_ref(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Croma",
            purchase_ledger="Purchases", amount=1000.0,
            narration="Return", company="Test Co",
            bill_ref="CRO-2026-5678",
        )
        v = _voucher(xml)
        bill = _party_entry(v).find("BILLALLOCATIONS.LIST")
        assert bill is not None
        assert bill.find("BILLTYPE").text == "Agst Ref"
        assert bill.find("NAME").text == "CRO-2026-5678"
        # Bill alloc amount mirrors the party-line sign (NEGATIVE for DN — the
        # party is on the debit side so the Agst Ref reduces the payable bill).
        assert float(bill.find("AMOUNT").text) < 0

    def test_debit_note_gst_input(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Supplier",
            purchase_ledger="Purchases", amount=11800.0,
            narration="Return", company="Test Co",
            gst_entries=[
                {"ledger": "INPUT CGST", "amount": 900.0},
                {"ledger": "INPUT SGST", "amount": 900.0},
            ],
        )
        v = _voucher(xml)
        gst = [
            e for e in v.findall("LEDGERENTRIES.LIST")
            if "INPUT" in (e.find("LEDGERNAME").text or "")
        ]
        assert len(gst) == 2
        for e in gst:
            # Reversed Input GST on a DN sits on the CREDIT side (No / positive)
            # — same side as the contra returns ledger.
            assert e.find("ISDEEMEDPOSITIVE").text == "No"
            assert float(e.find("AMOUNT").text) > 0

    def test_debit_note_entries_balance(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Supplier",
            purchase_ledger="Purchases", amount=11800.0,
            narration="Return", company="Test Co",
            gst_entries=[
                {"ledger": "INPUT CGST", "amount": 900.0},
                {"ledger": "INPUT SGST", "amount": 900.0},
            ],
        )
        v = _voucher(xml)
        amounts = [float(e.find("AMOUNT").text) for e in v.findall("LEDGERENTRIES.LIST")]
        assert abs(sum(amounts)) < 0.01

    def test_debit_note_agst_ref_sign_reduces_payable(self):
        """Regression (live test 2026-06-09): the Agst Ref allocation amount on a
        DN must be NEGATIVE — same sign as the party DEBIT leg — so it reduces
        the outstanding payable bill rather than increasing it."""
        xml = build_create_debit_note(
            date="20260305", party_ledger="Croma",
            purchase_ledger="Purchases", amount=2000.0,
            narration="Return", company="Test Co",
            bill_ref="CRO-2026-5678",
        )
        v = _voucher(xml)
        party = _party_entry(v)
        bill = party.find("BILLALLOCATIONS.LIST")
        party_amt = float(party.find("AMOUNT").text)
        bill_amt = float(bill.find("AMOUNT").text)
        # Both negative and equal magnitude.
        assert party_amt < 0
        assert bill_amt < 0
        assert abs(party_amt - bill_amt) < 0.01

    def test_required_fields_validation(self):
        with pytest.raises(ValueError, match="date"):
            build_create_debit_note(
                date="", party_ledger="S", purchase_ledger="P",
                amount=100.0, narration="t", company="C",
            )

    def test_amount_must_be_positive(self):
        with pytest.raises(ValueError, match="amount"):
            build_create_debit_note(
                date="20260305", party_ledger="S", purchase_ledger="P",
                amount=0.0, narration="t", company="C",
            )

    def test_xml_escaping(self):
        xml = build_create_debit_note(
            date="20260305", party_ledger="Smith & Sons",
            purchase_ledger="Office <Supplies>",
            amount=100.0, narration='Test "quote"', company="Test Co",
        )
        assert "Smith &amp; Sons" in xml
        assert "Office &lt;Supplies&gt;" in xml


class TestCreateCreditNote:
    def test_basic_structure(self):
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=11800.0,
            narration="Discount", company="Test Co",
        )
        v = _voucher(xml)
        assert v.get("VCHTYPE") == "Credit Note"
        assert v.find("VOUCHERTYPENAME").text == "Credit Note"
        assert v.find("PERSISTEDVIEW").text == "Invoice Voucher View"
        assert v.find("ISINVOICE").text == "Yes"

    def test_credit_note_reduces_receivable_party_on_credit(self):
        """CN (sales return) reduces the receivable: party = CREDIT side
        (No / +amount); contra (sales-returns) = debit (Yes / -amount)."""
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=1000.0,
            narration="Discount", company="Test Co",
        )
        v = _voucher(xml)
        party = _party_entry(v)
        assert party.find("ISDEEMEDPOSITIVE").text == "No"
        assert float(party.find("AMOUNT").text) > 0
        contra = [
            e for e in v.findall("LEDGERENTRIES.LIST")
            if e.find("ISPARTYLEDGER") is None
            and "Sales" in (e.find("LEDGERNAME").text or "")
        ][0]
        assert contra.find("ISDEEMEDPOSITIVE").text == "Yes"
        assert float(contra.find("AMOUNT").text) < 0

    def test_credit_note_agst_ref(self):
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=1000.0,
            narration="Discount", company="Test Co",
            bill_ref="INV-2026-FEB-001",
        )
        v = _voucher(xml)
        bill = _party_entry(v).find("BILLALLOCATIONS.LIST")
        assert bill.find("BILLTYPE").text == "Agst Ref"
        assert bill.find("NAME").text == "INV-2026-FEB-001"
        # Bill alloc amount mirrors the party-line sign (POSITIVE for CN — the
        # party is on the credit side so the Agst Ref reduces the receivable bill).
        assert float(bill.find("AMOUNT").text) > 0

    def test_credit_note_gst_output(self):
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=11800.0,
            narration="Discount", company="Test Co",
            gst_entries=[
                {"ledger": "OUTPUT CGST", "amount": 900.0},
                {"ledger": "OUTPUT SGST", "amount": 900.0},
            ],
        )
        v = _voucher(xml)
        gst = [
            e for e in v.findall("LEDGERENTRIES.LIST")
            if "OUTPUT" in (e.find("LEDGERNAME").text or "")
        ]
        assert len(gst) == 2
        for e in gst:
            # Reversed Output GST on a CN sits on the DEBIT side (Yes / negative)
            # — same side as the contra returns ledger.
            assert e.find("ISDEEMEDPOSITIVE").text == "Yes"
            assert float(e.find("AMOUNT").text) < 0

    def test_credit_note_entries_balance(self):
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=11800.0,
            narration="Discount", company="Test Co",
            gst_entries=[
                {"ledger": "OUTPUT CGST", "amount": 900.0},
                {"ledger": "OUTPUT SGST", "amount": 900.0},
            ],
        )
        v = _voucher(xml)
        amounts = [float(e.find("AMOUNT").text) for e in v.findall("LEDGERENTRIES.LIST")]
        assert abs(sum(amounts)) < 0.01

    def test_credit_note_agst_ref_sign_reduces_receivable(self):
        """Regression (live test 2026-06-09): the Agst Ref allocation amount on a
        CN must be POSITIVE — same sign as the party CREDIT leg — so it reduces
        the outstanding receivable bill rather than increasing it."""
        xml = build_create_credit_note(
            date="20260315", party_ledger="Infosys",
            sales_ledger="Sales", amount=3000.0,
            narration="Return", company="Test Co",
            bill_ref="INV-2026-FEB-001",
        )
        v = _voucher(xml)
        party = _party_entry(v)
        bill = party.find("BILLALLOCATIONS.LIST")
        party_amt = float(party.find("AMOUNT").text)
        bill_amt = float(bill.find("AMOUNT").text)
        assert party_amt > 0
        assert bill_amt > 0
        assert abs(party_amt - bill_amt) < 0.01
