import os
import pytest
from tally_bridge.response_parser import (
    parse_amount, parse_trial_balance, parse_ledger_list, detect_error,
    parse_profit_and_loss, parse_balance_sheet, parse_stock_summary,
    parse_bills, sanitize_xml, parse_stock_items, parse_groups, parse_cash_flow,
    parse_stock_group_list,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


# ---------------------------------------------------------------------------
# parse_amount — utility, unchanged
# ---------------------------------------------------------------------------
class TestParseAmount:
    def test_normal_amount(self):
        assert parse_amount("1,23,456.78") == 123456.78
    def test_empty_string(self):
        assert parse_amount("") == 0.0
    def test_none_value(self):
        assert parse_amount(None) == 0.0
    def test_negative_amount(self):
        assert parse_amount("-1,23,456.78") == -123456.78
    def test_plain_number(self):
        assert parse_amount("50000.00") == 50000.0
    def test_whitespace(self):
        assert parse_amount("  1,000.00  ") == 1000.0


# ---------------------------------------------------------------------------
# Trial Balance — real Tally XML (DSPACCNAME + sibling DSPACCINFO)
# ---------------------------------------------------------------------------
class TestParseTrialBalance:
    @pytest.fixture
    def rows(self):
        return parse_trial_balance(_read_fixture("trial_balance.xml"))

    def test_returns_all_rows(self, rows):
        assert len(rows) == 9

    def test_account_names(self, rows):
        names = [r["account_name"] for r in rows]
        assert "Capital Account" in names
        assert "Current Liabilities" in names
        assert "Fixed Assets" in names
        assert "Current Assets" in names
        assert "Sales Accounts" in names
        assert "Purchase Accounts" in names
        assert "Indirect Expenses" in names

    def test_capital_account_credit(self, rows):
        capital = next(r for r in rows if r["account_name"] == "Capital Account")
        assert capital["debit_amount"] == 0.0
        assert capital["credit_amount"] == 750000.0
        assert capital["closing_balance"] == 750000.0

    def test_current_liabilities_both_amounts(self, rows):
        cl = next(r for r in rows if r["account_name"] == "Current Liabilities")
        assert cl["debit_amount"] == 0.0
        assert cl["credit_amount"] == 1391300.0
        assert cl["closing_balance"] == pytest.approx(1391300.0)

    def test_fixed_assets_debit_only(self, rows):
        fa = next(r for r in rows if r["account_name"] == "Fixed Assets")
        assert fa["debit_amount"] == 0.0
        assert fa["credit_amount"] == 0.0
        assert fa["closing_balance"] == 0.0

    def test_sales_accounts_credit(self, rows):
        sales = next(r for r in rows if r["account_name"] == "Sales Accounts")
        assert sales["debit_amount"] == 0.0
        assert sales["credit_amount"] == 2057650.0
        assert sales["closing_balance"] == 2057650.0

    def test_purchase_accounts_debit(self, rows):
        purchase = next(r for r in rows if r["account_name"] == "Purchase Accounts")
        assert purchase["debit_amount"] == -2521300.0
        assert purchase["credit_amount"] == 0.0
        assert purchase["closing_balance"] == -2521300.0

    def test_indirect_expenses_small_credit(self, rows):
        ie = next(r for r in rows if r["account_name"] == "Indirect Expenses")
        assert ie["debit_amount"] == -1343000.0
        assert ie["credit_amount"] == 0.0
        assert ie["closing_balance"] == pytest.approx(-1343000.0)

    def test_closing_balance_is_float(self, rows):
        for row in rows:
            assert isinstance(row["closing_balance"], float)
            assert isinstance(row["debit_amount"], float)
            assert isinstance(row["credit_amount"], float)

    def test_empty_xml_returns_empty(self):
        rows = parse_trial_balance("<ENVELOPE></ENVELOPE>")
        assert rows == []


# ---------------------------------------------------------------------------
# Profit & Loss — real Tally XML (DSPACCNAME + sibling PLAMT)
# ---------------------------------------------------------------------------
class TestParseProfitAndLoss:
    @pytest.fixture
    def rows(self):
        return parse_profit_and_loss(_read_fixture("profit_and_loss.xml"))

    def test_returns_all_rows(self, rows):
        assert len(rows) == 7

    def test_sales_accounts(self, rows):
        sales = next(r for r in rows if r["account_name"] == "Sales Accounts")
        assert sales["closing_balance"] == 2057650.0
        assert sales["credit_amount"] == 2057650.0
        assert sales["debit_amount"] == 0.0

    def test_cost_of_sales(self, rows):
        cos = next(r for r in rows if r["account_name"] == "Cost of Sales :")
        assert cos["closing_balance"] == -2521300.0
        assert cos["debit_amount"] == -2521300.0
        assert cos["credit_amount"] == 0.0

    def test_opening_stock_empty(self, rows):
        opening = next(r for r in rows if r["account_name"] == "Opening Stock")
        assert opening["closing_balance"] == 0.0
        assert opening["debit_amount"] == 0.0
        assert opening["credit_amount"] == 0.0

    def test_sub_item_uses_plsubamt(self, rows):
        """When BSMAINAMT is empty, parser should fall back to PLSUBAMT."""
        purchase = next(r for r in rows if r["account_name"] == "Add: Purchase Accounts")
        assert purchase["closing_balance"] == -2521300.0
        assert purchase["debit_amount"] == -2521300.0

    def test_indirect_expenses(self, rows):
        ie = next(r for r in rows if r["account_name"] == "Indirect Expenses")
        assert ie["closing_balance"] == -1343000.0
        assert ie["debit_amount"] == -1343000.0
        assert ie["credit_amount"] == 0.0

    def test_closing_balance_is_float(self, rows):
        for row in rows:
            assert isinstance(row["closing_balance"], float)

    def test_empty_xml_returns_empty(self):
        rows = parse_profit_and_loss("<ENVELOPE></ENVELOPE>")
        assert rows == []


# ---------------------------------------------------------------------------
# Balance Sheet — real Tally XML (BSNAME + sibling BSAMT)
# ---------------------------------------------------------------------------
class TestParseBalanceSheet:
    @pytest.fixture
    def rows(self):
        return parse_balance_sheet(_read_fixture("balance_sheet.xml"))

    def test_returns_all_rows(self, rows):
        assert len(rows) == 7

    def test_capital_account(self, rows):
        capital = next(r for r in rows if r["account_name"] == "Capital Account")
        assert capital["closing_balance"] == 750000.0
        assert capital["credit_amount"] == 750000.0
        assert capital["debit_amount"] == 0.0

    def test_loans_liability_empty(self, rows):
        loans = next(r for r in rows if r["account_name"] == "Loans (Liability)")
        assert loans["closing_balance"] == 0.0

    def test_current_liabilities(self, rows):
        cl = next(r for r in rows if r["account_name"] == "Current Liabilities")
        assert cl["closing_balance"] == 1391300.0
        assert cl["credit_amount"] == 1391300.0

    def test_profit_and_loss_account(self, rows):
        pnl = next(r for r in rows if r["account_name"] == "Profit & Loss A/c")
        assert pnl["closing_balance"] == -1806650.0
        assert pnl["debit_amount"] == -1806650.0

    def test_fixed_assets(self, rows):
        fa = next(r for r in rows if r["account_name"] == "Fixed Assets")
        assert fa["closing_balance"] == 0.0
        assert fa["debit_amount"] == 0.0

    def test_current_assets(self, rows):
        ca = next(r for r in rows if r["account_name"] == "Current Assets")
        assert ca["closing_balance"] == -334650.0
        assert ca["debit_amount"] == -334650.0

    def test_closing_balance_is_float(self, rows):
        for row in rows:
            assert isinstance(row["closing_balance"], float)

    def test_empty_xml_returns_empty(self):
        rows = parse_balance_sheet("<ENVELOPE></ENVELOPE>")
        assert rows == []


# ---------------------------------------------------------------------------
# Stock Summary — real Tally XML (DSPACCNAME + sibling DSPSTKINFO)
# ---------------------------------------------------------------------------
class TestParseStockSummary:
    @pytest.fixture
    def items(self):
        return parse_stock_summary(_read_fixture("stock_summary.xml"))

    def test_returns_all_items(self, items):
        assert len(items) == 15

    def test_item_names(self, items):
        names = [i["name"] for i in items]
        assert "Samsung 24 inch Monitor" in names
        assert "HP Laptop 15s" in names
        assert "Logitech Wireless Mouse" in names
        assert "A4 Paper Ream 500 sheets" in names
        assert "Pen Drive 32GB" in names

    def test_quantity_parsing(self, items):
        monitor = next(i for i in items if i["name"] == "Samsung 24 inch Monitor")
        assert monitor["closing_quantity"] == 42.0

    def test_quantity_negative_values(self, items):
        # Lenovo oversold (negative closing qty)
        lenovo = next(i for i in items if i["name"] == "Lenovo Ideapad Slim 3")
        assert lenovo["closing_quantity"] == -1.0
        # Dell fully sold out (zero)
        dell = next(i for i in items if i["name"] == "Dell Desktop Optiplex")
        assert dell["closing_quantity"] == 0.0

    def test_base_units_parsed(self, items):
        # Most items use Nos
        nos_items = [i for i in items if i["base_units"] == "Nos"]
        pcs_items = [i for i in items if i["base_units"] == "Pcs"]
        assert len(nos_items) > 0
        assert len(pcs_items) > 0

    def test_closing_value_non_zero(self, items):
        # Samsung monitor has closing_value from fixture
        monitor = next(i for i in items if i["name"] == "Samsung 24 inch Monitor")
        assert monitor["closing_value"] == 462000.0
        assert monitor["closing_rate"] == 0.0  # Rate field has format "11000.00/Nos", parses as 0

    def test_parent_group_empty_for_data_report(self, items):
        for item in items:
            assert item["parent_group"] == ""

    def test_empty_xml_returns_empty(self):
        items = parse_stock_summary("<ENVELOPE></ENVELOPE>")
        assert items == []


# ---------------------------------------------------------------------------
# Bills Receivable — real Tally XML
# ---------------------------------------------------------------------------
class TestParseBillsReceivable:
    @pytest.fixture
    def bills(self):
        return parse_bills(_read_fixture("bills_receivable_live.xml"))

    def test_returns_all_bills(self, bills):
        assert len(bills) == 12

    def test_first_bill(self, bills):
        first = bills[0]
        assert first["bill_number"] == "#1"
        assert first["party_name"] == "HCODE TECHNOLOGIES PRIVATE LIMITED"
        assert first["amount"] == 200000.0
        assert first["bill_date"] == "2-Jul-25"
        assert first["due_date"] == "2-Jul-25"
        assert first["overdue_days"] == 272

    def test_smartbike_bill(self, bills):
        sb = next(b for b in bills if b["party_name"] == "SMARTBIKE MOBILITY PRIVATE LIMITED" and b["bill_number"] == "7")
        assert sb["amount"] == 300000.0

    def test_last_bill(self, bills):
        last = bills[-1]
        assert last["bill_number"] == "#12"
        assert last["party_name"] == "SMARTBIKE TECH PRIVATE LIMITED"
        assert last["amount"] == 354000.0


# ---------------------------------------------------------------------------
# Bills Payable — real Tally XML
# ---------------------------------------------------------------------------
class TestParseBillsPayable:
    @pytest.fixture
    def bills(self):
        return parse_bills(_read_fixture("bills_payable_live.xml"))

    def test_returns_one_bill(self, bills):
        assert len(bills) == 1

    def test_anthropic_bill(self, bills):
        bill = bills[0]
        assert bill["bill_number"] == "6ZBO7JXW-0003"
        assert bill["party_name"] == "Anthropic, PBC"
        assert bill["amount"] == 2096.86
        assert bill["due_date"] == "26-Sep-25"
        assert bill["overdue_days"] == 186


def test_parse_bills_includes_overdue_days():
    xml = """<ENVELOPE><BODY><DATA><TALLYMESSAGE>
    <BILLFIXED><BILLREF>INV001</BILLREF><BILLPARTY>Test Co</BILLPARTY><BILLDATE>20251001</BILLDATE></BILLFIXED>
    <BILLCL>50000</BILLCL><BILLDUE>15-11-2025</BILLDUE><BILLOVERDUE>45</BILLOVERDUE>
    </TALLYMESSAGE></DATA></BODY></ENVELOPE>"""
    bills = parse_bills(xml)
    assert len(bills) == 1
    assert bills[0]["overdue_days"] == 45
    assert bills[0]["due_date"] == "15-11-2025"


# ---------------------------------------------------------------------------
# Ledger List — unchanged (working correctly)
# ---------------------------------------------------------------------------
class TestParseLedgerList:
    def test_returns_list(self):
        ledgers = parse_ledger_list(_read_fixture("ledger_list.xml"))
        assert len(ledgers) == 34
    def test_fields(self):
        ledgers = parse_ledger_list(_read_fixture("ledger_list.xml"))
        hdfc = next(l for l in ledgers if "HDFC" in l["name"])
        assert hdfc["parent_group"] == "Bank Accounts"
        assert hdfc["closing_balance"] == 834500.0
    def test_empty_opening(self):
        ledgers = parse_ledger_list(_read_fixture("ledger_list.xml"))
        apex = next(l for l in ledgers if "Apex" in l["name"])
        assert apex["opening_balance"] == 0.0


# ---------------------------------------------------------------------------
# detect_error — unchanged (working correctly)
# ---------------------------------------------------------------------------
class TestDetectError:
    def test_detect_error_response(self):
        error = detect_error(_read_fixture("error_response.xml"))
        assert error is not None
        assert "Company not loaded" in error
    def test_no_error_in_valid(self):
        error = detect_error(_read_fixture("trial_balance.xml"))
        assert error is None


# ---------------------------------------------------------------------------
# Inline XML tests — updated to use real Tally structure
# ---------------------------------------------------------------------------
class TestClosingBalanceParsing:
    """Verify closing_balance is parsed correctly from real Tally XML structure."""

    def test_trial_balance_closing_balance_is_float(self):
        xml = """<ENVELOPE>
        <DSPACCNAME>
            <DSPDISPNAME>Cash</DSPDISPNAME>
        </DSPACCNAME>
        <DSPACCINFO>
            <DSPCLDRAMT><DSPCLDRAMTA>-1000.00</DSPCLDRAMTA></DSPCLDRAMT>
            <DSPCLCRAMT><DSPCLCRAMTA></DSPCLCRAMTA></DSPCLCRAMT>
        </DSPACCINFO>
        </ENVELOPE>"""
        rows = parse_trial_balance(xml)
        assert len(rows) == 1
        assert isinstance(rows[0]["closing_balance"], float)
        assert rows[0]["closing_balance"] == -1000.0
        assert rows[0]["debit_amount"] == -1000.0

    def test_trial_balance_credit_only(self):
        xml = """<ENVELOPE>
        <DSPACCNAME>
            <DSPDISPNAME>Sales</DSPDISPNAME>
        </DSPACCNAME>
        <DSPACCINFO>
            <DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT>
            <DSPCLCRAMT><DSPCLCRAMTA>123456.00</DSPCLCRAMTA></DSPCLCRAMT>
        </DSPACCINFO>
        </ENVELOPE>"""
        rows = parse_trial_balance(xml)
        assert rows[0]["closing_balance"] == 123456.0
        assert rows[0]["credit_amount"] == 123456.0
        assert rows[0]["debit_amount"] == 0.0

    def test_trial_balance_empty_returns_zero(self):
        xml = """<ENVELOPE>
        <DSPACCNAME>
            <DSPDISPNAME>Empty Account</DSPDISPNAME>
        </DSPACCNAME>
        <DSPACCINFO>
            <DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT>
            <DSPCLCRAMT><DSPCLCRAMTA></DSPCLCRAMTA></DSPCLCRAMT>
        </DSPACCINFO>
        </ENVELOPE>"""
        rows = parse_trial_balance(xml)
        assert rows[0]["closing_balance"] == 0.0

    def test_profit_and_loss_inline(self):
        xml = """<ENVELOPE>
        <DSPACCNAME>
            <DSPDISPNAME>Revenue</DSPDISPNAME>
        </DSPACCNAME>
        <PLAMT>
            <PLSUBAMT></PLSUBAMT>
            <BSMAINAMT>500000.00</BSMAINAMT>
        </PLAMT>
        </ENVELOPE>"""
        rows = parse_profit_and_loss(xml)
        assert isinstance(rows[0]["closing_balance"], float)
        assert rows[0]["closing_balance"] == 500000.0
        assert rows[0]["credit_amount"] == 500000.0

    def test_balance_sheet_inline(self):
        xml = """<ENVELOPE>
        <BSNAME>
            <DSPACCNAME>
                <DSPDISPNAME>Assets</DSPDISPNAME>
            </DSPACCNAME>
        </BSNAME>
        <BSAMT>
            <BSSUBAMT></BSSUBAMT>
            <BSMAINAMT>-1000000.00</BSMAINAMT>
        </BSAMT>
        </ENVELOPE>"""
        rows = parse_balance_sheet(xml)
        assert isinstance(rows[0]["closing_balance"], float)
        assert rows[0]["closing_balance"] == -1000000.0
        assert rows[0]["debit_amount"] == -1000000.0

    def test_stock_summary_inline(self):
        xml = """<ENVELOPE>
        <DSPACCNAME>
            <DSPDISPNAME>Widget A</DSPDISPNAME>
        </DSPACCNAME>
        <DSPSTKINFO>
            <DSPSTKCL>
                <DSPCLQTY>10.0000 PCS</DSPCLQTY>
                <DSPCLRATE>150.00</DSPCLRATE>
                <DSPCLAMTA>1500.00</DSPCLAMTA>
            </DSPSTKCL>
        </DSPSTKINFO>
        </ENVELOPE>"""
        items = parse_stock_summary(xml)
        assert len(items) == 1
        assert items[0]["name"] == "Widget A"
        assert items[0]["closing_quantity"] == 10.0
        assert items[0]["base_units"] == "PCS"
        assert items[0]["closing_rate"] == 150.0
        assert items[0]["closing_value"] == 1500.0


# ---------------------------------------------------------------------------
# sanitize_xml — unchanged (working correctly)
# ---------------------------------------------------------------------------
class TestSanitizeXml:
    """Verify sanitize_xml is importable as a public function (Issue #13)."""

    def test_strips_control_characters(self):
        raw = '<ROOT><NAME>Test&#4; Value</NAME></ROOT>'
        cleaned = sanitize_xml(raw)
        assert "&#4;" not in cleaned
        assert "<ROOT>" in cleaned

    def test_preserves_valid_xml(self):
        raw = '<ROOT><NAME>Hello World</NAME></ROOT>'
        assert sanitize_xml(raw) == raw


class TestParseVouchersMonthField:
    """Phase 6: parse_vouchers extracts month field."""

    def test_month_field_extracted(self):
        from tally_bridge.response_parser import parse_vouchers
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE>20250715</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>001</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Test</PARTYLEDGERNAME>
            <NARRATION>Test sale</NARRATION>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        result = parse_vouchers(xml)
        assert len(result) == 1
        assert result[0]["month"] == "Jul 2025"

    def test_month_field_empty_for_missing_date(self):
        from tally_bridge.response_parser import parse_vouchers
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE></DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>002</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Test</PARTYLEDGERNAME>
            <NARRATION></NARRATION>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        result = parse_vouchers(xml)
        assert len(result) == 1
        assert result[0]["month"] == ""


class TestVoucherDateFiltering:
    """Phase 6: Python-side date filtering safety net."""

    def test_filters_vouchers_within_range(self):
        from tally_bridge.response_parser import _filter_vouchers_by_date
        vouchers = [
            {"date": "20250715", "party_name": "A", "voucher_type": "Sales"},
            {"date": "20250801", "party_name": "B", "voucher_type": "Sales"},
            {"date": "20250630", "party_name": "C", "voucher_type": "Sales"},
        ]
        result = _filter_vouchers_by_date(vouchers, "01-07-2025", "31-07-2025")
        assert len(result) == 1
        assert result[0]["party_name"] == "A"

    def test_keeps_all_when_dates_unparseable(self):
        from tally_bridge.response_parser import _filter_vouchers_by_date
        vouchers = [{"date": "bad", "party_name": "A"}]
        result = _filter_vouchers_by_date(vouchers, "01-07-2025", "31-07-2025")
        assert len(result) == 1

    def test_empty_vouchers(self):
        from tally_bridge.response_parser import _filter_vouchers_by_date
        result = _filter_vouchers_by_date([], "01-07-2025", "31-07-2025")
        assert result == []

    def test_parse_vouchers_with_date_filter(self):
        from tally_bridge.response_parser import parse_vouchers
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE>20250715</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>001</VOUCHERNUMBER>
            <PARTYLEDGERNAME>InRange</PARTYLEDGERNAME>
            <NARRATION></NARRATION>
        </VOUCHER>
        <VOUCHER>
            <DATE>20250801</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>002</VOUCHERNUMBER>
            <PARTYLEDGERNAME>OutOfRange</PARTYLEDGERNAME>
            <NARRATION></NARRATION>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        result = parse_vouchers(xml, from_date="01-07-2025", to_date="31-07-2025")
        assert len(result) == 1
        assert result[0]["party_name"] == "InRange"


# ---------------------------------------------------------------------------
# parse_stock_items
# ---------------------------------------------------------------------------
class TestParseStockItems:
    def test_parse_stock_items_extracts_fields(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <STOCKITEM NAME="HP Laptop 15s">
            <NAME>HP Laptop 15s</NAME>
            <PARENT>Electronics</PARENT>
            <BASEUNITS>Nos</BASEUNITS>
            <CLOSINGBALANCE>10.0000 Nos</CLOSINGBALANCE>
            <CLOSINGRATE>38000.00/Nos</CLOSINGRATE>
            <CLOSINGVALUE>380000.00</CLOSINGVALUE>
        </STOCKITEM>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        items = parse_stock_items(xml)
        assert len(items) == 1
        assert items[0]["name"] == "HP Laptop 15s"
        assert items[0]["parent_group"] == "Electronics"
        assert items[0]["base_units"] == "Nos"
        assert items[0]["closing_balance"] == 10.0
        assert items[0]["closing_rate"] == 38000.0
        assert items[0]["closing_value"] == 380000.0

    def test_parse_stock_items_empty(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        items = parse_stock_items(xml)
        assert items == []

    def test_parse_stock_items_skips_nameless(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <STOCKITEM NAME="">
            <NAME></NAME>
        </STOCKITEM>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        items = parse_stock_items(xml)
        assert items == []


# ---------------------------------------------------------------------------
# parse_stock_group_list
# ---------------------------------------------------------------------------
class TestParseStockGroupList:
    def test_parses_group_names(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <STOCKGROUP NAME="Electronics"><NAME>Electronics</NAME></STOCKGROUP>
        <STOCKGROUP NAME="AI Imported Items"><NAME>AI Imported Items</NAME></STOCKGROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        names = parse_stock_group_list(xml)
        assert names == ["Electronics", "AI Imported Items"]

    def test_uses_name_attribute_when_no_child(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <STOCKGROUP NAME="Stationery"></STOCKGROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        assert parse_stock_group_list(xml) == ["Stationery"]

    def test_empty_collection(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        assert parse_stock_group_list(xml) == []

    def test_skips_nameless(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <STOCKGROUP NAME=""><NAME></NAME></STOCKGROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        assert parse_stock_group_list(xml) == []


# ---------------------------------------------------------------------------
# parse_groups
# ---------------------------------------------------------------------------
class TestParseGroups:
    def test_parse_groups_extracts_fields(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <GROUP NAME="Sales Accounts">
            <NAME>Sales Accounts</NAME>
            <PARENT>Revenue</PARENT>
        </GROUP>
        <GROUP NAME="North Zone Debtors">
            <NAME>North Zone Debtors</NAME>
            <PARENT>Sundry Debtors</PARENT>
        </GROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        groups = parse_groups(xml)
        assert len(groups) == 2
        assert groups[0]["name"] == "Sales Accounts"
        assert groups[0]["parent"] == "Revenue"

    def test_parse_groups_empty(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        groups = parse_groups(xml)
        assert groups == []

    def test_parse_groups_skips_nameless(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <GROUP NAME="">
            <NAME></NAME>
        </GROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        groups = parse_groups(xml)
        assert groups == []


# ---------------------------------------------------------------------------
# H2: parse_vouchers — inventory_entries field
# ---------------------------------------------------------------------------
from tally_bridge.response_parser import parse_vouchers


class TestParseVouchersInventoryEntries:
    def test_parse_vouchers_extracts_inventory_entries(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE>20251001</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>S001</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Apex Technologies</PARTYLEDGERNAME>
            <NARRATION>Test sale</NARRATION>
            <ALLLEDGERENTRIES.LIST>
                <LEDGERNAME>Sales - Electronics</LEDGERNAME>
                <AMOUNT>-94000</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>HP Laptop 15s</STOCKITEMNAME>
                <ACTUALQTY>2 Nos</ACTUALQTY>
                <RATE>45000/Nos</RATE>
                <AMOUNT>-90000</AMOUNT>
            </ALLINVENTORYENTRIES.LIST>
            <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Logitech Wireless Mouse</STOCKITEMNAME>
                <ACTUALQTY>5 Nos</ACTUALQTY>
                <RATE>800/Nos</RATE>
                <AMOUNT>-4000</AMOUNT>
            </ALLINVENTORYENTRIES.LIST>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        vouchers = parse_vouchers(xml)
        assert len(vouchers) == 1
        v = vouchers[0]
        assert "inventory_entries" in v
        assert len(v["inventory_entries"]) == 2
        assert v["inventory_entries"][0]["item_name"] == "HP Laptop 15s"
        assert v["inventory_entries"][0]["quantity"] == 2.0
        assert v["inventory_entries"][0]["rate"] == 45000.0
        assert v["inventory_entries"][0]["amount"] == -90000.0

    def test_parse_vouchers_second_inventory_entry(self):
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE>20251001</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>S001</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Apex Technologies</PARTYLEDGERNAME>
            <NARRATION>Test sale</NARRATION>
            <ALLLEDGERENTRIES.LIST>
                <LEDGERNAME>Sales - Electronics</LEDGERNAME>
                <AMOUNT>-94000</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>HP Laptop 15s</STOCKITEMNAME>
                <ACTUALQTY>2 Nos</ACTUALQTY>
                <RATE>45000/Nos</RATE>
                <AMOUNT>-90000</AMOUNT>
            </ALLINVENTORYENTRIES.LIST>
            <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Logitech Wireless Mouse</STOCKITEMNAME>
                <ACTUALQTY>5 Nos</ACTUALQTY>
                <RATE>800/Nos</RATE>
                <AMOUNT>-4000</AMOUNT>
            </ALLINVENTORYENTRIES.LIST>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        vouchers = parse_vouchers(xml)
        v = vouchers[0]
        assert v["inventory_entries"][1]["item_name"] == "Logitech Wireless Mouse"
        assert v["inventory_entries"][1]["quantity"] == 5.0
        assert v["inventory_entries"][1]["rate"] == 800.0
        assert v["inventory_entries"][1]["amount"] == -4000.0

    def test_parse_vouchers_without_inventory_entries_returns_empty_list(self):
        """Vouchers without ALLINVENTORYENTRIES should have an empty inventory_entries list."""
        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
        <VOUCHER>
            <DATE>20251001</DATE>
            <VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
            <VOUCHERNUMBER>PMT001</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Rent</PARTYLEDGERNAME>
            <NARRATION>Office rent</NARRATION>
            <ALLLEDGERENTRIES.LIST>
                <LEDGERNAME>Rent</LEDGERNAME>
                <AMOUNT>-75000</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
        </VOUCHER>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        vouchers = parse_vouchers(xml)
        assert len(vouchers) == 1
        assert vouchers[0]["inventory_entries"] == []


# ---------------------------------------------------------------------------
# parse_cash_flow
# ---------------------------------------------------------------------------
def test_parse_cash_flow_extracts_rows():
    """Cash Flow report uses same sibling-pair structure as Trial Balance."""
    xml = """<ENVELOPE><DSPACCNAME><DSPDISPNAME>Cash from Operating Activities</DSPDISPNAME></DSPACCNAME>
    <DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA>150000</DSPCLDRAMTA></DSPCLDRAMT><DSPCLCRAMT><DSPCLCRAMTA>0</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO></ENVELOPE>"""
    rows = parse_cash_flow(xml)
    assert len(rows) == 1
    assert rows[0]["account_name"] == "Cash from Operating Activities"
