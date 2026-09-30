"""Tests for mock handler Group B additions: company list + party voucher lookup.

Exercises the mock against the *verified probe envelopes* produced by
build_company_list (E7) and build_party_vouchers (E8).
"""
import xml.etree.ElementTree as ET

from tally_bridge.mock_handler import mock_tally_request
from tally_bridge.request_builder import (
    build_company_list,
    build_party_vouchers,
)


class TestCompanyListMock:
    def test_company_list_returns_fixture_company(self):
        result = mock_tally_request(build_company_list())
        assert "Bharat Traders Pvt Ltd" in result
        # parseable + at least one COMPANY element
        root = ET.fromstring(result)
        assert len(root.findall(".//COMPANY")) >= 1


class TestPartyVoucherLookupMock:
    def test_known_party_returns_vouchers(self):
        xml = build_party_vouchers(
            "Apex Technologies Pvt Ltd", ["Sales"], "01-04-2025", "31-03-2026"
        )
        result = mock_tally_request(xml)
        root = ET.fromstring(result)
        vouchers = root.findall(".//VOUCHER")
        assert len(vouchers) >= 1
        # all returned vouchers belong to the requested party + type
        for v in vouchers:
            assert v.findtext("PARTYLEDGERNAME") == "Apex Technologies Pvt Ltd"
            assert v.findtext("VOUCHERTYPENAME") == "Sales"

    def test_type_filter_excludes_other_types(self):
        # Apex has only Sales in the mock; asking for Purchase yields nothing.
        xml = build_party_vouchers(
            "Apex Technologies Pvt Ltd", ["Purchase"], "01-04-2025", "31-03-2026"
        )
        root = ET.fromstring(mock_tally_request(xml))
        assert root.findall(".//VOUCHER") == []

    def test_unknown_party_returns_empty(self):
        xml = build_party_vouchers(
            "Nonexistent Party", ["Sales"], "01-04-2025", "31-03-2026"
        )
        root = ET.fromstring(mock_tally_request(xml))
        assert root.findall(".//VOUCHER") == []

    def test_voucher_carries_reference_and_amount(self):
        xml = build_party_vouchers(
            "Apex Technologies Pvt Ltd", ["Sales"], "01-04-2025", "31-03-2026"
        )
        root = ET.fromstring(mock_tally_request(xml))
        v = root.find(".//VOUCHER")
        assert v.findtext("REFERENCE")
        assert v.findtext("AMOUNT")
