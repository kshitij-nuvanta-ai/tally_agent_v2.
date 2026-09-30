"""Unit tests for Group B Task 7 primitives: company-list + party-voucher
request builders and response parsers.

Builders must match the verified probe envelopes (probe_group_b.py E7/E8):
  - E7: Collection TYPE=Company "List of Companies".
  - E8: Collection of Voucher CHILDOF $$VchType<Type> filtered by $PartyLedgerName.
"""
import xml.etree.ElementTree as ET

from backend.tally_bridge.request_builder import (
    build_company_list,
    build_party_vouchers,
)
from backend.tally_bridge.response_parser import (
    parse_company_list,
    parse_party_vouchers,
)


# ---------------------------------------------------------------------------
# build_company_list — verified probe E7 envelope
# ---------------------------------------------------------------------------
class TestBuildCompanyList:
    def test_is_well_formed_collection(self):
        xml = build_company_list()
        ET.fromstring(xml)  # must not raise
        assert "<TYPE>Collection</TYPE>" in xml

    def test_targets_company_object_type(self):
        xml = build_company_list()
        # TDL collection of Company objects (matches probe E7)
        assert 'NAME="List of Companies"' in xml
        assert "<TYPE>Company</TYPE>" in xml
        assert "<NATIVEMETHOD>Name</NATIVEMETHOD>" in xml

    def test_matches_mock_dispatch_token(self):
        # Mock handler routes on the "List of Companies" string.
        assert "List of Companies" in build_company_list()


# ---------------------------------------------------------------------------
# build_party_vouchers — verified probe E8 envelope
# ---------------------------------------------------------------------------
class TestBuildPartyVouchers:
    def test_is_well_formed(self):
        xml = build_party_vouchers("Apex Technologies Pvt Ltd", ["Sales"], "01-04-2025", "31-03-2026")
        ET.fromstring(xml)

    def test_filters_by_party_ledger_name(self):
        xml = build_party_vouchers("Apex Technologies Pvt Ltd", ["Sales"], "01-04-2025", "31-03-2026")
        assert '$PartyLedgerName = "Apex Technologies Pvt Ltd"' in xml
        assert "<FILTER>PartyVchFilter</FILTER>" in xml

    def test_childof_vchtype_per_type(self):
        xml = build_party_vouchers("Croma Electronics", ["Sales", "Purchase"], "01-04-2025", "31-03-2026")
        assert "<CHILDOF>$$VchTypeSales</CHILDOF>" in xml
        assert "<CHILDOF>$$VchTypePurchase</CHILDOF>" in xml

    def test_voucher_type_is_title_cased(self):
        xml = build_party_vouchers("X", ["sales"], "01-04-2025", "31-03-2026")
        assert "<CHILDOF>$$VchTypeSales</CHILDOF>" in xml

    def test_includes_required_native_methods(self):
        xml = build_party_vouchers("X", ["Sales"], "01-04-2025", "31-03-2026")
        for field in ("Date", "VoucherNumber", "VoucherTypeName", "PartyLedgerName", "Reference", "Amount"):
            assert f"<NATIVEMETHOD>{field}</NATIVEMETHOD>" in xml

    def test_carries_date_range(self):
        xml = build_party_vouchers("X", ["Sales"], "01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml

    def test_company_optional(self):
        without = build_party_vouchers("X", ["Sales"], "01-04-2025", "31-03-2026")
        assert "SVCurrentCompany" not in without
        with_co = build_party_vouchers("X", ["Sales"], "01-04-2025", "31-03-2026", company="Bharat Traders")
        assert "<SVCurrentCompany>Bharat Traders</SVCurrentCompany>" in with_co

    def test_quote_in_party_name_escaped(self):
        xml = build_party_vouchers('A"B Ltd', ["Sales"], "01-04-2025", "31-03-2026")
        ET.fromstring(xml)  # still well-formed
        assert "&quot;" in xml


# ---------------------------------------------------------------------------
# parse_company_list
# ---------------------------------------------------------------------------
class TestParseCompanyList:
    def test_parses_child_name_element(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <COMPANY><NAME>Bharat Traders Pvt Ltd</NAME></COMPANY>
        <COMPANY><NAME>Demo Co</NAME></COMPANY>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        assert parse_company_list(xml) == ["Bharat Traders Pvt Ltd", "Demo Co"]

    def test_parses_name_attribute(self):
        xml = '<ENVELOPE><BODY><COMPANY NAME="Attr Co"></COMPANY></BODY></ENVELOPE>'
        assert parse_company_list(xml) == ["Attr Co"]

    def test_skips_empty(self):
        xml = "<ENVELOPE><BODY><COMPANY><NAME></NAME></COMPANY></BODY></ENVELOPE>"
        assert parse_company_list(xml) == []

    def test_empty_collection(self):
        xml = "<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"
        assert parse_company_list(xml) == []


# ---------------------------------------------------------------------------
# parse_party_vouchers
# ---------------------------------------------------------------------------
class TestParsePartyVouchers:
    SAMPLE = """<ENVELOPE><BODY><DATA><COLLECTION>
    <VOUCHER>
      <DATE>20250520</DATE>
      <VOUCHERNUMBER>1</VOUCHERNUMBER>
      <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
      <PARTYLEDGERNAME>Apex Technologies Pvt Ltd</PARTYLEDGERNAME>
      <REFERENCE>INV-APX-001</REFERENCE>
      <AMOUNT>118000.00</AMOUNT>
    </VOUCHER>
    <VOUCHER>
      <DATE>20250812</DATE>
      <VOUCHERNUMBER>7</VOUCHERNUMBER>
      <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
      <PARTYLEDGERNAME>Apex Technologies Pvt Ltd</PARTYLEDGERNAME>
      <REFERENCE>INV-APX-002</REFERENCE>
      <AMOUNT>88500.00</AMOUNT>
    </VOUCHER>
    </COLLECTION></DATA></BODY></ENVELOPE>"""

    def test_parses_all_fields(self):
        rows = parse_party_vouchers(self.SAMPLE)
        assert len(rows) == 2
        first = rows[0]
        assert first == {
            "date": "20250520",
            "voucher_number": "1",
            "voucher_type": "Sales",
            "party": "Apex Technologies Pvt Ltd",
            "reference": "INV-APX-001",
            "amount": 118000.00,
        }

    def test_amount_is_float(self):
        rows = parse_party_vouchers(self.SAMPLE)
        assert isinstance(rows[1]["amount"], float)
        assert rows[1]["amount"] == 88500.00

    def test_skips_ghost_vouchers(self):
        xml = """<ENVELOPE><BODY><COLLECTION>
        <VOUCHER></VOUCHER>
        <VOUCHER><DATE>20250520</DATE><VOUCHERNUMBER>1</VOUCHERNUMBER>
        <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME></VOUCHER>
        </COLLECTION></BODY></ENVELOPE>"""
        rows = parse_party_vouchers(xml)
        assert len(rows) == 1

    def test_empty_collection(self):
        xml = "<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"
        assert parse_party_vouchers(xml) == []
