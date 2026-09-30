"""Tests for Tally import response parsing."""
from tally_bridge.response_parser import parse_import_response


class TestParseImportResponse:
    def test_successful_create(self):
        xml = """<RESPONSE>
<CREATED>1</CREATED>
<ALTERED>0</ALTERED>
<DELETED>0</DELETED>
<LASTVCHID>12345</LASTVCHID>
<LASTMID>67890</LASTMID>
<COMBINED>0</COMBINED>
<IGNORED>0</IGNORED>
<ERRORS>0</ERRORS>
</RESPONSE>"""
        result = parse_import_response(xml)
        assert result["success"] is True
        assert result["created"] == 1
        assert result["errors"] == 0
        assert result["last_vch_id"] == "12345"

    def test_error_response(self):
        xml = """<RESPONSE>
<CREATED>0</CREATED>
<ALTERED>0</ALTERED>
<DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID>
<LASTMID>0</LASTMID>
<COMBINED>0</COMBINED>
<IGNORED>0</IGNORED>
<ERRORS>1</ERRORS>
<LINEERROR>Ledger "Nonexistent" not found</LINEERROR>
</RESPONSE>"""
        result = parse_import_response(xml)
        assert result["success"] is False
        assert result["errors"] == 1
        assert "not found" in result["error_message"]

    def test_successful_delete(self):
        xml = """<RESPONSE>
<CREATED>0</CREATED>
<ALTERED>0</ALTERED>
<DELETED>1</DELETED>
<LASTVCHID>0</LASTVCHID>
<LASTMID>0</LASTMID>
<COMBINED>0</COMBINED>
<IGNORED>0</IGNORED>
<ERRORS>0</ERRORS>
</RESPONSE>"""
        result = parse_import_response(xml)
        assert result["success"] is True
        assert result["deleted"] == 1

    def test_malformed_xml(self):
        result = parse_import_response("<not valid xml>>>")
        assert result["success"] is False
        assert "parse" in result["error_message"].lower() or "invalid" in result["error_message"].lower()

    def test_empty_response(self):
        result = parse_import_response("")
        assert result["success"] is False

    def test_exceptions_response(self):
        """EXCEPTIONS=1 means silent failure (e.g., missing NAME.LIST in master XML)."""
        xml = """<RESPONSE>
<CREATED>0</CREATED>
<ALTERED>0</ALTERED>
<DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID>
<LASTMID>0</LASTMID>
<COMBINED>0</COMBINED>
<IGNORED>0</IGNORED>
<ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED>
<EXCEPTIONS>1</EXCEPTIONS>
</RESPONSE>"""
        result = parse_import_response(xml)
        assert result["success"] is False
        assert result["exceptions"] == 1
        assert "exception" in result["error_message"].lower()

    def test_translates_voucher_date_missing_error(self):
        """Tally's 'Voucher date is missing' is misleading — translate to a clear hint."""
        xml = """<RESPONSE>
<LINEERROR>Voucher date is missing for: 'Payment' voucher 1.  Verify the data, resolve errors (if any) and retry Split.</LINEERROR>
<CREATED>0</CREATED>
<ALTERED>0</ALTERED>
<DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID>
<LASTMID>0</LASTMID>
<COMBINED>0</COMBINED>
<IGNORED>0</IGNORED>
<ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED>
<EXCEPTIONS>1</EXCEPTIONS>
</RESPONSE>"""
        result = parse_import_response(xml)
        assert result["success"] is False
        # The original misleading message should be replaced
        assert "F2" in result["error_message"] or "current date" in result["error_message"]
        # Make sure the hint mentions the actionable fix
        assert "tally" in result["error_message"].lower()
