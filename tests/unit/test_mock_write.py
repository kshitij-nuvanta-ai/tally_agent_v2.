"""Tests for mock handler write operations (IMPORTDATA requests)."""
from tally_bridge.mock_handler import mock_tally_request, reset_mock_state


class TestMockImport:
    def setup_method(self):
        reset_mock_state()

    def test_create_voucher_returns_success(self):
        xml = """<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
<REQUESTDATA><TALLYMESSAGE><VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>20260404</DATE><NARRATION>Test</NARRATION>
</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""
        resp = mock_tally_request(xml)
        assert "<CREATED>1</CREATED>" in resp
        assert "<ERRORS>0</ERRORS>" in resp
        assert "<EXCEPTIONS>0</EXCEPTIONS>" in resp

    def test_create_ledger_returns_success(self):
        xml = """<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
<REQUESTDATA><TALLYMESSAGE><LEDGER NAME="Test" ACTION="Create">
<NAME.LIST><NAME>Test</NAME></NAME.LIST>
<PARENT>Indirect Expenses</PARENT></LEDGER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""
        resp = mock_tally_request(xml)
        assert "<CREATED>1</CREATED>" in resp

    def test_delete_voucher_returns_success(self):
        xml = """<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
<REQUESTDATA><TALLYMESSAGE>
<VOUCHER DATE="20260404" TAGNAME="Master ID" TAGVALUE="1" VCHTYPE="Payment" ACTION="Delete">
</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""
        resp = mock_tally_request(xml)
        assert "<DELETED>1</DELETED>" in resp

    def test_cancel_voucher_returns_altered(self):
        """Cancel is internally an alter — returns ALTERED=1."""
        xml = """<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
<REQUESTDATA><TALLYMESSAGE>
<VOUCHER DATE="20260404" TAGNAME="Master ID" TAGVALUE="1" VCHTYPE="Payment" ACTION="Cancel">
</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""
        resp = mock_tally_request(xml)
        assert "<ALTERED>1</ALTERED>" in resp

    def test_counter_increments_across_creates(self):
        xml = """<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
<REQUESTDATA><TALLYMESSAGE><VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>20260404</DATE><NARRATION>Test</NARRATION>
</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""
        resp1 = mock_tally_request(xml)
        resp2 = mock_tally_request(xml)
        # LASTVCHID should increment
        assert "<LASTVCHID>1</LASTVCHID>" in resp1
        assert "<LASTVCHID>2</LASTVCHID>" in resp2

    def test_read_requests_still_work(self):
        """Regression: write support must not break read handling."""
        from tally_bridge.request_builder import build_list_companies
        xml = build_list_companies()
        resp = mock_tally_request(xml)
        # Should return company fixture, not an error
        assert "ERROR" not in resp.upper() or "COMPANY" in resp.upper()
