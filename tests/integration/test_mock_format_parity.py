"""Format parity tests: verify mock handler responses parse identically
to what live Tally format parsers expect.

These tests call mock_tally_request() with realistic XML request strings,
then run the same parser functions used in production.  The goal is to
catch regressions where fixture XML drifts away from the parser's
expected structure.
"""
import pytest

from backend.tally_bridge.mock_handler import mock_tally_request
from backend.tally_bridge.response_parser import (
    parse_trial_balance,
    parse_profit_and_loss,
    parse_balance_sheet,
    parse_stock_summary,
    parse_vouchers,
    parse_ledger_list,
    parse_bills,
)


# ---------------------------------------------------------------------------
# Helper request builders (minimal but realistic XML matching real requests)
# ---------------------------------------------------------------------------

def _tb_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"Trial Balance\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _pl_request(to_date: str) -> str:
    """Build a P&L request with SVTODATE in DD-MM-YYYY format."""
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<STATICVARIABLES>"
        "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>"
        f"<SVTODATE TYPE=\"Date\">{to_date}</SVTODATE>"
        "</STATICVARIABLES>"
        "<TDLMESSAGE><TDL><TDLFILE>Profit and Loss</TDLFILE></TDLMESSAGE>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _bs_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<STATICVARIABLES>"
        "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>"
        "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>"
        "</STATICVARIABLES>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"Balance Sheet\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _stock_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"Stock Summary\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _ledger_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"CustomLedgerList\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _sales_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"SalesVchs\"><FORM NAME=\"SalesVchs\"></FORM>"
        "</REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _purchase_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"PurchaseVchs\"><FORM NAME=\"PurchaseVchs\"></FORM>"
        "</REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _daybook_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"DayBookVchs\"><FORM NAME=\"DayBookVchs\"></FORM>"
        "</REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _bills_receivable_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"Bills Receivable\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


def _bills_payable_request() -> str:
    return (
        "<ENVELOPE><BODY><REPORTREQUEST>"
        "<TDL><TDLMESSAGE><REPORT NAME=\"Bills Payable\"></REPORT></TDLMESSAGE></TDL>"
        "</REPORTREQUEST></BODY></ENVELOPE>"
    )


# ---------------------------------------------------------------------------
# Trial Balance
# ---------------------------------------------------------------------------

class TestTrialBalanceParity:
    def test_tb_parses_without_error(self):
        """Mock TB response must not raise during parsing."""
        resp = mock_tally_request(_tb_request())
        rows = parse_trial_balance(resp)
        assert isinstance(rows, list)

    def test_tb_row_count(self):
        """TB fixture has 9 account groups."""
        rows = parse_trial_balance(mock_tally_request(_tb_request()))
        assert len(rows) == 9

    def test_tb_required_fields(self):
        """Every TB row must have the four canonical fields."""
        rows = parse_trial_balance(mock_tally_request(_tb_request()))
        required = {"account_name", "debit_amount", "credit_amount", "closing_balance"}
        for row in rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_tb_amounts_are_numeric(self):
        """All numeric fields must be floats (not strings)."""
        rows = parse_trial_balance(mock_tally_request(_tb_request()))
        for row in rows:
            assert isinstance(row["debit_amount"], float), row
            assert isinstance(row["credit_amount"], float), row
            assert isinstance(row["closing_balance"], float), row

    def test_tb_contains_known_accounts(self):
        """TB must contain Capital Account and Sales Accounts."""
        rows = parse_trial_balance(mock_tally_request(_tb_request()))
        names = [r["account_name"] for r in rows]
        assert "Capital Account" in names
        assert "Sales Accounts" in names

    def test_tb_no_empty_account_names(self):
        """No row should have a blank account_name."""
        rows = parse_trial_balance(mock_tally_request(_tb_request()))
        for row in rows:
            assert row["account_name"], f"Empty account_name found: {row}"


# ---------------------------------------------------------------------------
# Profit & Loss — full year
# ---------------------------------------------------------------------------

class TestProfitAndLossFullYear:
    @pytest.fixture
    def pl_rows(self):
        return parse_profit_and_loss(mock_tally_request(_pl_request("31-03-2026")))

    def test_pl_parses_without_error(self, pl_rows):
        assert isinstance(pl_rows, list)

    def test_pl_row_count(self, pl_rows):
        """Full-year P&L should return 7 rows."""
        assert len(pl_rows) == 7

    def test_pl_required_fields(self, pl_rows):
        required = {"account_name", "debit_amount", "credit_amount", "closing_balance"}
        for row in pl_rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_pl_amounts_are_numeric(self, pl_rows):
        for row in pl_rows:
            assert isinstance(row["debit_amount"], float), row
            assert isinstance(row["credit_amount"], float), row
            assert isinstance(row["closing_balance"], float), row

    def test_pl_contains_sales_accounts(self, pl_rows):
        names = [r["account_name"] for r in pl_rows]
        assert "Sales Accounts" in names

    def test_pl_contains_indirect_expenses(self, pl_rows):
        names = [r["account_name"] for r in pl_rows]
        assert "Indirect Expenses" in names

    def test_pl_sales_positive(self, pl_rows):
        """Sales Accounts should be credit (positive) in full year."""
        sales_rows = [r for r in pl_rows if r["account_name"] == "Sales Accounts"]
        assert len(sales_rows) == 1
        assert sales_rows[0]["closing_balance"] > 0, "Full-year Sales should be positive"

    def test_pl_indirect_expenses_negative(self, pl_rows):
        """Indirect Expenses should be debit (negative)."""
        exp_rows = [r for r in pl_rows if r["account_name"] == "Indirect Expenses"]
        assert len(exp_rows) == 1
        assert exp_rows[0]["closing_balance"] < 0, "Indirect Expenses should be negative"


# ---------------------------------------------------------------------------
# Profit & Loss — period-specific (same structure, different amounts)
# ---------------------------------------------------------------------------

class TestProfitAndLossPeriod:
    def test_pl_sep_same_structure_as_full_year(self):
        """Sep P&L should have same number of rows and fields as full-year."""
        rows_sep = parse_profit_and_loss(mock_tally_request(_pl_request("30-09-2025")))
        rows_mar = parse_profit_and_loss(mock_tally_request(_pl_request("31-03-2026")))
        assert len(rows_sep) == len(rows_mar)
        assert [r["account_name"] for r in rows_sep] == [r["account_name"] for r in rows_mar]

    def test_pl_amounts_differ_by_period(self):
        """Sep and full-year amounts should differ (different voucher coverage)."""
        rows_sep = parse_profit_and_loss(mock_tally_request(_pl_request("30-09-2025")))
        rows_mar = parse_profit_and_loss(mock_tally_request(_pl_request("31-03-2026")))
        # At least one account must have a different balance (data is date-aware)
        any_diff = any(
            s["closing_balance"] != m["closing_balance"]
            for s, m in zip(rows_sep, rows_mar)
        )
        assert any_diff, "Sep and full-year P&L should differ in at least one account"


# ---------------------------------------------------------------------------
# Profit & Loss — cumulative increases over time
# ---------------------------------------------------------------------------

class TestProfitAndLossCumulative:
    def _sales_for(self, to_date: str) -> float:
        rows = parse_profit_and_loss(mock_tally_request(_pl_request(to_date)))
        for r in rows:
            if r["account_name"] == "Sales Accounts":
                return r["closing_balance"]
        return 0.0

    def test_cumulative_sales_increases_sep_to_dec(self):
        """Sales at Dec should be >= Sep (cumulative, more vouchers)."""
        sep = self._sales_for("30-09-2025")
        dec = self._sales_for("31-12-2025")
        assert dec >= sep, f"Expected Dec ({dec}) >= Sep ({sep})"

    def test_cumulative_sales_increases_dec_to_mar(self):
        """Sales at Mar should be >= Dec."""
        dec = self._sales_for("31-12-2025")
        mar = self._sales_for("31-03-2026")
        assert mar >= dec, f"Expected Mar ({mar}) >= Dec ({dec})"

    def test_cumulative_sales_mar_exceeds_sep(self):
        """Full-year sales should strictly exceed Sep (real sales exist in Q3/Q4)."""
        sep = self._sales_for("30-09-2025")
        mar = self._sales_for("31-03-2026")
        assert mar > sep, f"Expected Mar ({mar}) > Sep ({sep})"


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------

class TestBalanceSheetParity:
    @pytest.fixture
    def bs_rows(self):
        return parse_balance_sheet(mock_tally_request(_bs_request()))

    def test_bs_parses_without_error(self, bs_rows):
        assert isinstance(bs_rows, list)

    def test_bs_row_count(self, bs_rows):
        """Balance Sheet fixture has 7 rows."""
        assert len(bs_rows) == 7

    def test_bs_required_fields(self, bs_rows):
        required = {"account_name", "debit_amount", "credit_amount", "closing_balance"}
        for row in bs_rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_bs_amounts_are_numeric(self, bs_rows):
        for row in bs_rows:
            assert isinstance(row["debit_amount"], float), row
            assert isinstance(row["credit_amount"], float), row
            assert isinstance(row["closing_balance"], float), row

    def test_bs_contains_capital_account(self, bs_rows):
        names = [r["account_name"] for r in bs_rows]
        assert "Capital Account" in names

    def test_bs_contains_current_assets(self, bs_rows):
        names = [r["account_name"] for r in bs_rows]
        assert "Current Assets" in names

    def test_bs_capital_account_positive(self, bs_rows):
        cap_rows = [r for r in bs_rows if r["account_name"] == "Capital Account"]
        assert len(cap_rows) == 1
        assert cap_rows[0]["closing_balance"] > 0, "Capital Account should be positive (credit)"


# ---------------------------------------------------------------------------
# Stock Summary
# ---------------------------------------------------------------------------

class TestStockSummaryParity:
    @pytest.fixture
    def ss_rows(self):
        return parse_stock_summary(mock_tally_request(_stock_request()))

    def test_ss_parses_without_error(self, ss_rows):
        assert isinstance(ss_rows, list)

    def test_ss_item_count(self, ss_rows):
        """Stock Summary fixture has 15 items."""
        assert len(ss_rows) == 15

    def test_ss_required_fields(self, ss_rows):
        required = {"name", "parent_group", "base_units", "closing_quantity", "closing_rate", "closing_value"}
        for row in ss_rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_ss_amounts_are_numeric(self, ss_rows):
        for row in ss_rows:
            assert isinstance(row["closing_quantity"], float), row
            assert isinstance(row["closing_rate"], float), row
            assert isinstance(row["closing_value"], float), row

    def test_ss_no_empty_names(self, ss_rows):
        for row in ss_rows:
            assert row["name"], f"Empty item name found: {row}"

    def test_ss_base_units_present(self, ss_rows):
        """All stock items should have a base unit string."""
        for row in ss_rows:
            assert row["base_units"], f"Empty base_units for item: {row['name']}"

    def test_ss_contains_known_item(self, ss_rows):
        names = [r["name"] for r in ss_rows]
        assert "Samsung 24 inch Monitor" in names


# ---------------------------------------------------------------------------
# Sales Vouchers
# ---------------------------------------------------------------------------

class TestSalesVouchersParity:
    @pytest.fixture
    def sales_rows(self):
        return parse_vouchers(mock_tally_request(_sales_request()))

    def test_sales_parses_without_error(self, sales_rows):
        assert isinstance(sales_rows, list)

    def test_sales_count(self, sales_rows):
        """Sales register fixture has 16 entries."""
        assert len(sales_rows) == 16

    def test_sales_required_fields(self, sales_rows):
        required = {"date", "month", "voucher_type", "voucher_number", "party_name", "narration", "ledger_entries"}
        for row in sales_rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_sales_voucher_type_is_sales(self, sales_rows):
        """All vouchers in the sales register should have type Sales."""
        for row in sales_rows:
            assert row["voucher_type"] == "Sales", f"Unexpected type: {row['voucher_type']}"

    def test_sales_date_format(self, sales_rows):
        """Date should be YYYYMMDD string."""
        for row in sales_rows:
            assert len(row["date"]) == 8, f"Expected YYYYMMDD date: {row['date']}"
            assert row["date"].isdigit(), f"Non-numeric date: {row['date']}"

    def test_sales_ledger_entries_present(self, sales_rows):
        """Each sales voucher should have at least one ledger entry."""
        for row in sales_rows:
            assert isinstance(row["ledger_entries"], list), row
            assert len(row["ledger_entries"]) >= 1, f"No ledger entries: {row}"

    def test_sales_party_name_not_empty(self, sales_rows):
        for row in sales_rows:
            assert row["party_name"], f"Empty party_name in voucher: {row['voucher_number']}"


# ---------------------------------------------------------------------------
# Purchase Vouchers
# ---------------------------------------------------------------------------

class TestPurchaseVouchersParity:
    @pytest.fixture
    def purchase_rows(self):
        return parse_vouchers(mock_tally_request(_purchase_request()))

    def test_purchase_parses_without_error(self, purchase_rows):
        assert isinstance(purchase_rows, list)

    def test_purchase_count(self, purchase_rows):
        """Purchase register fixture has 8 entries."""
        assert len(purchase_rows) == 8

    def test_purchase_required_fields(self, purchase_rows):
        required = {"date", "month", "voucher_type", "voucher_number", "party_name", "narration", "ledger_entries"}
        for row in purchase_rows:
            assert required.issubset(row.keys()), f"Missing fields in row: {row}"

    def test_purchase_voucher_type(self, purchase_rows):
        for row in purchase_rows:
            assert row["voucher_type"] == "Purchase", f"Unexpected type: {row['voucher_type']}"

    def test_purchase_date_format(self, purchase_rows):
        for row in purchase_rows:
            assert len(row["date"]) == 8
            assert row["date"].isdigit()


# ---------------------------------------------------------------------------
# Day Book Vouchers
# ---------------------------------------------------------------------------

class TestDayBookVouchersParity:
    @pytest.fixture
    def db_rows(self):
        return parse_vouchers(mock_tally_request(_daybook_request()))

    def test_daybook_parses_without_error(self, db_rows):
        assert isinstance(db_rows, list)

    def test_daybook_count(self, db_rows):
        """Day book fixture has 50 entries: 16 Sales + 8 Purchase + 16 Payment + 10 Receipt."""
        assert len(db_rows) == 50

    def test_daybook_voucher_types_breakdown(self, db_rows):
        """Verify counts per voucher type match fixture design."""
        counts: dict[str, int] = {}
        for row in db_rows:
            vt = row["voucher_type"]
            counts[vt] = counts.get(vt, 0) + 1
        assert counts.get("Sales") == 16, f"Expected 16 Sales, got: {counts}"
        assert counts.get("Purchase") == 8, f"Expected 8 Purchase, got: {counts}"
        assert counts.get("Payment") == 16, f"Expected 16 Payment, got: {counts}"
        assert counts.get("Receipt") == 10, f"Expected 10 Receipt, got: {counts}"

    def test_daybook_required_fields(self, db_rows):
        required = {"date", "month", "voucher_type", "voucher_number", "party_name", "narration", "ledger_entries"}
        for row in db_rows:
            assert required.issubset(row.keys()), f"Missing fields: {row}"

    def test_daybook_dates_are_yyyymmdd(self, db_rows):
        for row in db_rows:
            assert len(row["date"]) == 8
            assert row["date"].isdigit(), f"Non-numeric date: {row['date']}"

    def test_daybook_month_populated(self, db_rows):
        """Month field should be formatted as 'Mon YYYY'."""
        for row in db_rows:
            assert row["month"], f"Empty month in: {row}"
            assert len(row["month"].split()) == 2, f"Unexpected month format: {row['month']}"


# ---------------------------------------------------------------------------
# Bills Receivable
# ---------------------------------------------------------------------------

class TestBillsReceivableParity:
    @pytest.fixture
    def br_rows(self):
        return parse_bills(mock_tally_request(_bills_receivable_request()))

    def test_br_parses_without_error(self, br_rows):
        assert isinstance(br_rows, list)

    def test_br_count_at_least_six(self, br_rows):
        """Bills receivable fixture has at least 6 parties."""
        assert len(br_rows) >= 6

    def test_br_required_fields(self, br_rows):
        required = {"bill_number", "party_name", "bill_date", "amount", "pending_amount", "due_date", "overdue_days"}
        for row in br_rows:
            assert required.issubset(row.keys()), f"Missing fields: {row}"

    def test_br_amounts_are_numeric(self, br_rows):
        for row in br_rows:
            assert isinstance(row["amount"], float), row
            assert isinstance(row["pending_amount"], float), row

    def test_br_amounts_positive(self, br_rows):
        """All receivable amounts should be positive (absolute values)."""
        for row in br_rows:
            assert row["amount"] >= 0, f"Negative amount in receivable: {row}"
            assert row["pending_amount"] >= 0, f"Negative pending amount: {row}"

    def test_br_party_names_not_empty(self, br_rows):
        for row in br_rows:
            assert row["party_name"], f"Empty party_name: {row}"

    def test_br_contains_known_party(self, br_rows):
        """Apex Technologies should be in bills receivable."""
        party_names = [r["party_name"] for r in br_rows]
        assert any("Apex" in name for name in party_names), (
            f"Expected Apex Technologies in receivables; got: {party_names}"
        )


# ---------------------------------------------------------------------------
# Bills Payable
# ---------------------------------------------------------------------------

class TestBillsPayableParity:
    @pytest.fixture
    def bp_rows(self):
        return parse_bills(mock_tally_request(_bills_payable_request()))

    def test_bp_parses_without_error(self, bp_rows):
        assert isinstance(bp_rows, list)

    def test_bp_count(self, bp_rows):
        """Bills payable fixture has 5 suppliers."""
        assert len(bp_rows) == 5

    def test_bp_required_fields(self, bp_rows):
        required = {"bill_number", "party_name", "bill_date", "amount", "pending_amount", "due_date", "overdue_days"}
        for row in bp_rows:
            assert required.issubset(row.keys()), f"Missing fields: {row}"

    def test_bp_amounts_are_numeric(self, bp_rows):
        for row in bp_rows:
            assert isinstance(row["amount"], float), row

    def test_bp_amounts_positive(self, bp_rows):
        for row in bp_rows:
            assert row["amount"] >= 0, f"Negative payable amount: {row}"

    def test_bp_contains_samsung(self, bp_rows):
        """Samsung India Electronics should be in payables."""
        party_names = [r["party_name"] for r in bp_rows]
        assert any("Samsung" in name for name in party_names), (
            f"Expected Samsung in payables; got: {party_names}"
        )


# ---------------------------------------------------------------------------
# Ledger List
# ---------------------------------------------------------------------------

class TestLedgerListParity:
    @pytest.fixture
    def ll_rows(self):
        return parse_ledger_list(mock_tally_request(_ledger_request()))

    def test_ll_parses_without_error(self, ll_rows):
        assert isinstance(ll_rows, list)

    def test_ll_count_at_least_34(self, ll_rows):
        """Ledger list fixture has at least 34 ledgers."""
        assert len(ll_rows) >= 34

    def test_ll_required_fields(self, ll_rows):
        required = {"name", "parent_group", "closing_balance", "opening_balance"}
        for row in ll_rows:
            assert required.issubset(row.keys()), f"Missing fields: {row}"

    def test_ll_amounts_are_numeric(self, ll_rows):
        for row in ll_rows:
            assert isinstance(row["closing_balance"], float), row
            assert isinstance(row["opening_balance"], float), row

    def test_ll_no_empty_names(self, ll_rows):
        for row in ll_rows:
            assert row["name"], f"Empty ledger name found: {row}"

    def test_ll_parent_group_present(self, ll_rows):
        """Every ledger should have a parent group."""
        for row in ll_rows:
            assert row["parent_group"], f"Empty parent_group for ledger: {row['name']}"

    def test_ll_contains_known_ledger(self, ll_rows):
        names = [r["name"] for r in ll_rows]
        assert "Apex Technologies Pvt Ltd" in names, f"Ledger list: {names}"
