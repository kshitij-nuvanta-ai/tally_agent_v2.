"""Tests for Tally import XML builder — vouchers, ledgers, groups.

IMPORTANT: XML formats verified against live Tally in docs/tally-write-exploration.md.
Key requirements: NAME.LIST for masters, TAGNAME/TAGVALUE for voucher delete/cancel.
"""
import xml.etree.ElementTree as ET

import pytest

from tally_bridge.import_builder import (
    build_create_group,
    build_create_ledger,
    build_create_payment_voucher,
    build_cancel_voucher,
    build_delete_ledger,
    build_delete_group,
    build_delete_voucher,
)


def _parse(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


class TestCreatePaymentVoucher:
    def test_basic_payment_structure(self):
        xml = build_create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=500.00,
            narration="Uber ride",
            company="Test Co",
        )
        root = _parse(xml)
        assert root.findtext(".//TALLYREQUEST") == "Import Data"
        assert root.findtext(".//REPORTNAME") == "Vouchers"
        assert root.findtext(".//SVCURRENTCOMPANY") == "Test Co"

        voucher = root.find(".//VOUCHER")
        assert voucher is not None
        assert voucher.get("VCHTYPE") == "Payment"
        assert voucher.get("ACTION") == "Create"
        assert voucher.findtext("DATE") == "20260404"
        assert voucher.findtext("NARRATION") == "Uber ride"

    def test_ledger_entries_balance(self):
        xml = build_create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=500.00,
            narration="Test",
            company="Test Co",
        )
        root = _parse(xml)
        entries = root.findall(".//ALLLEDGERENTRIES.LIST")
        assert len(entries) == 2

        # Debit entry (expense): negative amount, ISDEEMEDPOSITIVE=Yes
        debit = entries[0]
        assert debit.findtext("LEDGERNAME") == "Travel Expenses"
        assert debit.findtext("ISDEEMEDPOSITIVE") == "Yes"
        assert float(debit.findtext("AMOUNT")) == -500.00

        # Credit entry (cash): positive amount, ISDEEMEDPOSITIVE=No
        credit = entries[1]
        assert credit.findtext("LEDGERNAME") == "Cash"
        assert credit.findtext("ISDEEMEDPOSITIVE") == "No"
        assert float(credit.findtext("AMOUNT")) == 500.00

    def test_payment_with_gst_entries(self):
        xml = build_create_payment_voucher(
            date="20260404",
            debit_ledger="Office Supplies",
            credit_ledger="Cash",
            amount=1180.00,
            narration="Stationery with GST",
            company="Test Co",
            gst_entries=[
                {"ledger": "CGST Input 9%", "amount": 90.00},
                {"ledger": "SGST Input 9%", "amount": 90.00},
            ],
        )
        root = _parse(xml)
        entries = root.findall(".//ALLLEDGERENTRIES.LIST")
        # 1 debit (expense base: 1000) + 2 GST + 1 credit (cash: 1180)
        assert len(entries) == 4

        # Verify amounts balance: sum of all amounts should be 0
        total = sum(float(e.findtext("AMOUNT")) for e in entries)
        assert abs(total) < 0.01

        # Verify base amount is correct (total minus GST)
        debit = entries[0]
        assert debit.findtext("LEDGERNAME") == "Office Supplies"
        assert float(debit.findtext("AMOUNT")) == -1000.00

    def test_includes_required_tally_tags(self):
        """PERSISTEDVIEW and VOUCHERTYPENAME are required by Tally — verified during exploration."""
        xml = build_create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=500.00,
            narration="Test",
            company="Test Co",
        )
        root = _parse(xml)
        voucher = root.find(".//VOUCHER")
        # PERSISTEDVIEW required for proper voucher rendering in Tally
        persisted_view = voucher.findtext("PERSISTEDVIEW")
        assert persisted_view == "Accounting Voucher View", \
            f"Expected PERSISTEDVIEW='Accounting Voucher View', got {persisted_view!r}"
        # VOUCHERTYPENAME mirrors VCHTYPE attribute (required for Sales/Purchase compatibility)
        assert voucher.findtext("VOUCHERTYPENAME") == "Payment"


class TestXmlEscaping:
    def test_narration_with_special_chars_escaped(self):
        xml = build_create_payment_voucher(
            date="20260404",
            debit_ledger="Travel",
            credit_ledger="Cash",
            amount=100.00,
            narration='Refund for "X & Y" <urgent>',
            company="Test Co",
        )
        root = _parse(xml)  # must parse without error
        assert root.find(".//NARRATION").text == 'Refund for "X & Y" <urgent>'

    def test_ledger_name_with_ampersand(self):
        xml = build_create_ledger(
            name="Smith & Sons",
            parent="Sundry Creditors",
            company="Test Co",
        )
        root = _parse(xml)  # must not raise
        ledger = root.find(".//LEDGER")
        assert ledger.get("NAME") == "Smith & Sons"
        assert ledger.find("NAME.LIST").findtext("NAME") == "Smith & Sons"

    def test_company_name_with_special_chars(self):
        xml = build_create_ledger(
            name="Test Ledger",
            parent="Indirect Expenses",
            company="M/s. Smith & Co.",
        )
        root = _parse(xml)
        assert root.findtext(".//SVCURRENTCOMPANY") == "M/s. Smith & Co."

    def test_quote_in_attribute_escaped(self):
        xml = build_create_ledger(
            name='Foo "Bar" Inc',
            parent="Sundry Creditors",
            company="Test Co",
        )
        root = _parse(xml)  # must parse despite quotes in attribute
        assert root.find(".//LEDGER").get("NAME") == 'Foo "Bar" Inc'


class TestValidation:
    def test_negative_amount_raises(self):
        with pytest.raises(ValueError, match="amount must be positive"):
            build_create_payment_voucher(
                date="20260404", debit_ledger="Travel", credit_ledger="Cash",
                amount=-100.00, narration="Test", company="Test Co",
            )

    def test_zero_amount_raises(self):
        with pytest.raises(ValueError, match="amount must be positive"):
            build_create_payment_voucher(
                date="20260404", debit_ledger="Travel", credit_ledger="Cash",
                amount=0, narration="Test", company="Test Co",
            )

    def test_gst_exceeds_amount_raises(self):
        with pytest.raises(ValueError, match="invalid gst"):
            build_create_payment_voucher(
                date="20260404", debit_ledger="Travel", credit_ledger="Cash",
                amount=100.00, narration="Test", company="Test Co",
                gst_entries=[{"ledger": "CGST", "amount": 200.00}],
            )

    def test_empty_narration_raises(self):
        with pytest.raises(ValueError, match="narration is required"):
            build_create_payment_voucher(
                date="20260404", debit_ledger="Travel", credit_ledger="Cash",
                amount=100.00, narration="", company="Test Co",
            )

    def test_empty_ledger_name_raises(self):
        with pytest.raises(ValueError, match="name is required"):
            build_create_ledger(name="", parent="Indirect Expenses", company="Test Co")

    def test_whitespace_only_name_raises(self):
        with pytest.raises(ValueError, match="name is required"):
            build_create_ledger(name="   ", parent="Indirect Expenses", company="Test Co")


class TestCreateLedger:
    def test_basic_ledger(self):
        xml = build_create_ledger(
            name="Uber",
            parent="Indirect Expenses",
            company="Test Co",
        )
        root = _parse(xml)
        assert root.findtext(".//REPORTNAME") == "All Masters"
        ledger = root.find(".//LEDGER")
        assert ledger is not None
        assert ledger.get("NAME") == "Uber"
        assert ledger.get("ACTION") == "Create"
        assert ledger.findtext("PARENT") == "Indirect Expenses"

    def test_ledger_has_name_list(self):
        """NAME.LIST is REQUIRED — without it Tally crashes with memory violation."""
        xml = build_create_ledger(name="Test", parent="Indirect Expenses", company="Test Co")
        root = _parse(xml)
        ledger = root.find(".//LEDGER")
        name_list = ledger.find("NAME.LIST")
        assert name_list is not None
        assert name_list.findtext("NAME") == "Test"

    def test_ledger_with_gstin(self):
        xml = build_create_ledger(
            name="Supplier ABC",
            parent="Sundry Creditors",
            company="Test Co",
            gstin="29XXXXX1234Z",
        )
        root = _parse(xml)
        ledger = root.find(".//LEDGER")
        assert ledger.findtext("PARTYGSTIN") == "29XXXXX1234Z"


class TestCreateGroup:
    def test_basic_group(self):
        xml = build_create_group(
            name="SaaS Subscriptions",
            parent="Indirect Expenses",
            company="Test Co",
        )
        root = _parse(xml)
        group = root.find(".//GROUP")
        assert group is not None
        assert group.get("NAME") == "SaaS Subscriptions"
        assert group.get("ACTION") == "Create"
        assert group.findtext("PARENT") == "Indirect Expenses"

    def test_group_has_name_list(self):
        """NAME.LIST is REQUIRED for group operations too."""
        xml = build_create_group(name="Test Group", parent="Indirect Expenses", company="Test Co")
        root = _parse(xml)
        group = root.find(".//GROUP")
        name_list = group.find("NAME.LIST")
        assert name_list is not None
        assert name_list.findtext("NAME") == "Test Group"


class TestDeleteOperations:
    def test_delete_voucher_uses_tagname(self):
        """Voucher delete uses TAGNAME='Master ID' + TAGVALUE (not VCHKEY)."""
        xml = build_delete_voucher(
            voucher_type="Payment",
            master_id="301",
            date="20260405",
            company="Test Co",
        )
        root = _parse(xml)
        voucher = root.find(".//VOUCHER")
        assert voucher.get("ACTION") == "Delete"
        assert voucher.get("VCHTYPE") == "Payment"
        assert voucher.get("TAGNAME") == "Master ID"
        assert voucher.get("TAGVALUE") == "301"
        assert voucher.get("DATE") == "20260405"

    def test_cancel_voucher(self):
        """Cancel sets ACTION='Cancel', returns ALTERED=1 from Tally."""
        xml = build_cancel_voucher(
            voucher_type="Payment",
            master_id="301",
            date="20260405",
            company="Test Co",
            narration="Cancelled by user",
        )
        root = _parse(xml)
        voucher = root.find(".//VOUCHER")
        assert voucher.get("ACTION") == "Cancel"
        assert voucher.get("TAGNAME") == "Master ID"
        assert voucher.findtext("NARRATION") == "Cancelled by user"

    def test_delete_ledger_has_name_list(self):
        """Ledger delete REQUIRES NAME.LIST — without it Tally crashes."""
        xml = build_delete_ledger(name="Old Ledger", company="Test Co")
        root = _parse(xml)
        ledger = root.find(".//LEDGER")
        assert ledger.get("ACTION") == "Delete"
        assert ledger.get("NAME") == "Old Ledger"
        name_list = ledger.find("NAME.LIST")
        assert name_list is not None

    def test_delete_group_has_name_list(self):
        xml = build_delete_group(name="Old Group", company="Test Co")
        root = _parse(xml)
        group = root.find(".//GROUP")
        assert group.get("ACTION") == "Delete"
        name_list = group.find("NAME.LIST")
        assert name_list is not None
