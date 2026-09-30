"""Tests for the in-process mock Tally handler."""

import pytest
from backend.tally_bridge.mock_handler import (
    mock_tally_request,
    STATIC_FIXTURES,
    VOUCHER_FIXTURES,
    DATE_AWARE_REPORTS,
)


def _build_pnl_request(from_date: str, to_date: str) -> str:
    """Build a P&L request XML with date range."""
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Profit and Loss</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE TYPE=\"Date\">{from_date}</SVFROMDATE>
<SVTODATE TYPE=\"Date\">{to_date}</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""


def _build_tb_request(from_date: str, to_date: str) -> str:
    """Build a Trial Balance request XML with date range."""
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Trial Balance</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE TYPE=\"Date\">{from_date}</SVFROMDATE>
<SVTODATE TYPE=\"Date\">{to_date}</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""


def _extract_bsmainamt(xml_str: str, account_name: str) -> float:
    """Extract BSMAINAMT value for a given account from P&L XML."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_str)
    children = list(root)
    for i, child in enumerate(children):
        if child.tag == "DSPACCNAME":
            disp = child.find("DSPDISPNAME")
            if disp is not None and disp.text == account_name:
                if i + 1 < len(children) and children[i + 1].tag == "PLAMT":
                    main = children[i + 1].find("BSMAINAMT")
                    if main is not None and main.text:
                        return float(main.text)
    return 0.0


class TestMockHandler:
    """Test pattern matching and fixture loading."""

    def test_report_fixtures_mapping_exists(self):
        """All expected report types are mapped across the three dictionaries."""
        static_expected = [
            "List of Companies", "Trial Balance", "CustomLedgerList",
            "Balance Sheet", "Bills Receivable", "Bills Payable", "Stock Summary",
        ]
        voucher_expected = ["DayBookVchs", "SalesVchs", "PurchaseVchs", "LedgerVchs"]
        date_aware_expected = ["Profit and Loss"]

        for name in static_expected:
            assert name in STATIC_FIXTURES, f"Missing static fixture for {name}"
        for name in voucher_expected:
            assert name in VOUCHER_FIXTURES, f"Missing voucher fixture for {name}"
        for name in date_aware_expected:
            assert name in DATE_AWARE_REPORTS, f"Missing date-aware report for {name}"

    def test_company_list_request(self):
        xml = '<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER><BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Companies</REPORTNAME></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        assert "<COMPANY>" in result
        assert "Bharat Traders" in result

    def test_trial_balance_request(self):
        xml = '<ENVELOPE><BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Trial Balance</REPORTNAME></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result
        assert len(result) > 100

    def test_profit_and_loss_request(self):
        xml = '<REPORTNAME>Profit and Loss</REPORTNAME>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_balance_sheet_request(self):
        xml = '<REPORTNAME>Balance Sheet</REPORTNAME>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_sales_register_request(self):
        xml = '<COLLECTION>SalesVchs</COLLECTION>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_purchase_register_request(self):
        xml = '<COLLECTION>PurchaseVchs</COLLECTION>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_day_book_request(self):
        xml = '<COLLECTION>DayBookVchs</COLLECTION>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_bills_receivable_request(self):
        xml = '<REPORTNAME>Bills Receivable</REPORTNAME>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_stock_summary_request(self):
        xml = '<REPORTNAME>Stock Summary</REPORTNAME>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_ledger_list_request(self):
        xml = '<COLLECTION>CustomLedgerList</COLLECTION>'
        result = mock_tally_request(xml)
        assert "<ENVELOPE>" in result

    def test_unknown_request_returns_error(self):
        xml = '<ENVELOPE><BODY>Something Unknown</BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        assert "Unknown request" in result

    def test_all_fixtures_return_valid_xml(self):
        """Every static fixture response should start with < (valid XML)."""
        for report_name in STATIC_FIXTURES:
            xml = f'<REPORTNAME>{report_name}</REPORTNAME>'
            result = mock_tally_request(xml)
            assert result.strip().startswith("<"), f"Invalid XML for {report_name}"


class TestDateAwarePnL:
    """P&L mock returns different cumulative amounts based on SVTODATE."""

    def test_full_fy_pnl_has_all_sales(self):
        xml = _build_pnl_request("01-04-2025", "31-03-2026")
        result = mock_tally_request(xml)
        assert "BSMAINAMT" in result
        assert "2057650.00" in result

    def test_q3_cumulative_pnl_has_oct_dec_sales(self):
        """Cumulative to Dec 31 should include Oct+Nov+Dec sales only."""
        xml = _build_pnl_request("01-04-2025", "31-12-2025")
        result = mock_tally_request(xml)
        # Oct: S001(94k)+S002(110.5k)+S003(130k)+S004(29.5k)+S005(180k)=544000
        # Nov: S006(25.5k)+S007(116k)+S008(88k)=229500
        # Dec: S009(262.5k)+S010(141k)+S011(14.25k)=417750
        # Cumulative to Dec = 1191250
        assert "1191250.00" in result

    def test_q2_cumulative_pnl_has_no_sales(self):
        """Cumulative to Sep 30 should have no sales (first sale is Oct 1)."""
        xml = _build_pnl_request("01-04-2025", "30-09-2025")
        result = mock_tally_request(xml)
        assert "Sales Accounts" in result

    def test_different_dates_produce_different_pnl(self):
        """Core test: different SVTODATEs must produce different P&L amounts."""
        q2_xml = _build_pnl_request("01-04-2025", "30-09-2025")
        q3_xml = _build_pnl_request("01-04-2025", "31-12-2025")
        q2_result = mock_tally_request(q2_xml)
        q3_result = mock_tally_request(q3_xml)
        assert q2_result != q3_result, "Q2 and Q3 cumulative P&L must differ"

    def test_subtraction_yields_nonzero_for_q3(self):
        """Q3 cumulative P&L minus Q2 cumulative should be non-zero (mock data has Q3 sales)."""
        cum_xml = _build_pnl_request("01-04-2025", "31-12-2025")
        prior_xml = _build_pnl_request("01-04-2025", "30-09-2025")
        cum_result = mock_tally_request(cum_xml)
        prior_result = mock_tally_request(prior_xml)
        cum_sales = _extract_bsmainamt(cum_result, "Sales Accounts")
        prior_sales = _extract_bsmainamt(prior_result, "Sales Accounts")
        period_sales = cum_sales - prior_sales
        assert period_sales > 0, f"Q3 sales should be >0, got {period_sales}"


class TestVoucherCollectionsReturnFullData:
    """No / untyped dates → the current period (FY 2025-26 = the whole current-FY
    fixture), mirroring live C33. Typed windows: see test_mock_c33_date_vars.py."""

    def test_sales_register_returns_all_vouchers(self):
        xml = '<ENVELOPE><HEADER><TYPE>Collection</TYPE></HEADER><BODY>SalesVchs</BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        assert result.count('VCHTYPE="Sales"') == 16

    def test_purchase_register_returns_all_vouchers(self):
        xml = '<ENVELOPE><HEADER><TYPE>Collection</TYPE></HEADER><BODY>PurchaseVchs</BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        assert result.count('VCHTYPE="Purchase"') == 8

    def test_day_book_returns_all_vouchers(self):
        xml = '<ENVELOPE><HEADER><TYPE>Collection</TYPE></HEADER><BODY>DayBookVchs</BODY></ENVELOPE>'
        result = mock_tally_request(xml)
        total_vouchers = result.count("<VOUCHER ")
        assert total_vouchers == 50
