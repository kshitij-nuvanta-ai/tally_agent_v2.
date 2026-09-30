from backend.tally_bridge.request_builder import (
    build_list_companies, build_list_ledgers, build_list_groups, build_list_stock_items,
    build_list_stock_groups,
    build_trial_balance, build_profit_and_loss, build_balance_sheet,
    build_bills_receivable, build_bills_payable, build_stock_summary,
    build_day_book, build_ledger_vouchers, build_sales_register, build_purchase_register,
    build_cash_flow,
)


# ---------------------------------------------------------------------------
# H2: AllInventoryEntries in voucher collection queries
# ---------------------------------------------------------------------------
def test_voucher_native_methods_include_inventory_entries():
    """All voucher collection queries must fetch AllInventoryEntries for item-level data."""
    xml = build_day_book("01-10-2025", "31-10-2025")
    assert "AllInventoryEntries" in xml


def test_sales_register_includes_inventory_entries():
    xml = build_sales_register("01-10-2025", "31-10-2025")
    assert "AllInventoryEntries" in xml


def test_ledger_vouchers_includes_inventory_entries():
    xml = build_ledger_vouchers("Cash", "01-10-2025", "31-10-2025")
    assert "AllInventoryEntries" in xml


class TestMasterBuilders:
    def test_list_companies_has_envelope(self):
        xml = build_list_companies()
        assert "<ENVELOPE>" in xml and "</ENVELOPE>" in xml

    def test_list_companies_has_export_request(self):
        xml = build_list_companies()
        assert "<TALLYREQUEST>EXPORT</TALLYREQUEST>" in xml

    def test_list_companies_has_company_collection(self):
        xml = build_list_companies()
        assert "List of Companies" in xml

    def test_list_ledgers_has_collection_type(self):
        xml = build_list_ledgers()
        assert "<TYPE>Ledger</TYPE>" in xml

    def test_list_ledgers_has_native_methods(self):
        xml = build_list_ledgers()
        assert "<NATIVEMETHOD>Name</NATIVEMETHOD>" in xml
        assert "<NATIVEMETHOD>Parent</NATIVEMETHOD>" in xml
        assert "<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>" in xml

    def test_list_ledgers_with_company_has_svcurrentcompany(self):
        xml = build_list_ledgers(company="ABC Pvt Ltd")
        assert "<SVCurrentCompany>ABC Pvt Ltd</SVCurrentCompany>" in xml

    def test_list_ledgers_without_company_omits_svcurrentcompany(self):
        xml = build_list_ledgers()
        assert "SVCurrentCompany" not in xml

    def test_list_groups_has_group_type(self):
        xml = build_list_groups()
        assert "<TYPE>Group</TYPE>" in xml

    def test_list_stock_items_has_stock_type(self):
        xml = build_list_stock_items()
        assert "<TYPE>StockItem</TYPE>" in xml

    def test_list_stock_groups_has_stock_group_type(self):
        xml = build_list_stock_groups()
        assert "<TYPE>StockGroup</TYPE>" in xml

    def test_list_stock_groups_collection_has_name_method(self):
        xml = build_list_stock_groups()
        assert "<NATIVEMETHOD>Name</NATIVEMETHOD>" in xml
        assert 'TALLYREQUEST>Export' in xml
        assert '<TYPE>Collection</TYPE>' in xml


class TestReportBuilders:
    def test_trial_balance_dates(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml

    def test_trial_balance_export_format(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026")
        assert "$$SysName:XML" in xml

    def test_trial_balance_report_id(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026")
        assert "Trial Balance" in xml

    def test_trial_balance_with_company(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026", company="ABC Pvt Ltd")
        assert "<SVCurrentCompany>ABC Pvt Ltd</SVCurrentCompany>" in xml

    def test_trial_balance_without_company(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026")
        assert "SVCurrentCompany" not in xml

    def test_profit_and_loss_report_id(self):
        xml = build_profit_and_loss("01-04-2025", "31-03-2026")
        assert "Profit and Loss" in xml

    def test_profit_and_loss_dates(self):
        xml = build_profit_and_loss("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml

    def test_balance_sheet_date(self):
        xml = build_balance_sheet("31-03-2026")
        assert "Balance Sheet" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml

    def test_bills_receivable(self):
        xml = build_bills_receivable("31-03-2026")
        assert "Bills Receivable" in xml

    def test_bills_payable(self):
        xml = build_bills_payable("31-03-2026")
        assert "Bills Payable" in xml

    def test_stock_summary(self):
        xml = build_stock_summary("31-03-2026")
        assert "Stock Summary" in xml

    def test_stock_summary_with_group(self):
        xml = build_stock_summary("31-03-2026", stock_group="Electronics")
        assert "Electronics" in xml


class TestVoucherBuilders:
    def test_day_book_dates(self):
        xml = build_day_book("01-04-2025", "30-04-2025")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">30-04-2025</SVTODATE>" in xml

    def test_day_book_collection_type(self):
        xml = build_day_book("01-04-2025", "30-04-2025")
        assert "<TYPE>Collection</TYPE>" in xml
        assert "<TYPE>Voucher</TYPE>" in xml

    def test_day_book_with_voucher_type(self):
        xml = build_day_book("01-04-2025", "30-04-2025", voucher_type="Sales")
        assert "Sales" in xml
        assert "VchTypeFilter" in xml

    def test_day_book_without_voucher_type(self):
        xml = build_day_book("01-04-2025", "30-04-2025")
        assert "VchTypeFilter" not in xml

    def test_ledger_vouchers_includes_ledger_name(self):
        xml = build_ledger_vouchers("HDFC Bank", "01-04-2025", "31-03-2026")
        assert "HDFC Bank" in xml

    def test_ledger_vouchers_dates(self):
        xml = build_ledger_vouchers("Cash", "01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml

    def test_sales_register(self):
        xml = build_sales_register("01-04-2025", "31-03-2026")
        assert "Sales" in xml
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml

    def test_purchase_register(self):
        xml = build_purchase_register("01-04-2025", "31-03-2026")
        assert "Purchase" in xml


class TestXmlEscaping:
    """Issue #6: XML injection — special chars in names must be escaped."""

    def test_ledger_vouchers_escapes_ampersand(self):
        xml = build_ledger_vouchers("M/s Sharma & Sons", "01-04-2025", "31-03-2026")
        assert "&amp;" in xml
        assert "& Sons" not in xml  # raw & should not appear

    def test_ledger_vouchers_escapes_quotes(self):
        xml = build_ledger_vouchers('He said "hello"', "01-04-2025", "31-03-2026")
        assert "&quot;" in xml

    def test_ledger_vouchers_escapes_angle_brackets(self):
        xml = build_ledger_vouchers("A <B> C", "01-04-2025", "31-03-2026")
        assert "&lt;" in xml
        assert "&gt;" in xml

    def test_day_book_voucher_type_escapes_ampersand(self):
        xml = build_day_book("01-04-2025", "30-04-2025", voucher_type="Sales & Returns")
        assert "&amp;" in xml

    def test_stock_summary_group_escapes_ampersand(self):
        xml = build_stock_summary("31-03-2026", stock_group="Oil & Gas")
        assert "&amp;" in xml


class TestVoucherTypeTitleCase:
    """Issue #16: voucher_type_filter must be title-cased for Tally matching."""

    def test_day_book_voucher_type_title_cased(self):
        xml = build_day_book("01-04-2025", "30-04-2025", voucher_type="sales")
        assert '"Sales"' in xml
        assert '"sales"' not in xml

    def test_day_book_voucher_type_uppercase_normalized(self):
        xml = build_day_book("01-04-2025", "30-04-2025", voucher_type="PURCHASE")
        assert '"Purchase"' in xml
        assert '"PURCHASE"' not in xml

    def test_day_book_voucher_type_already_correct(self):
        xml = build_day_book("01-04-2025", "30-04-2025", voucher_type="Sales")
        assert '"Sales"' in xml


class TestNoInDateRange:
    """$$InDateRange is NOT a valid TDL function — must not appear in XML.
    Date filtering relies on SVFROMDATE/SVTODATE + Python-side _filter_vouchers_by_date()."""

    def test_sales_register_no_indaterange(self):
        xml = build_sales_register("01-07-2025", "31-07-2025")
        assert "InDateRange" not in xml
        assert "VchTypeFilter" in xml

    def test_day_book_no_indaterange(self):
        xml = build_day_book("01-04-2025", "30-04-2025")
        assert "InDateRange" not in xml

    def test_sales_register_has_svfromdate(self):
        xml = build_sales_register("01-07-2025", "31-07-2025")
        assert "<SVFROMDATE TYPE=\"Date\">01-07-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-07-2025</SVTODATE>" in xml

    def test_day_book_no_type_no_vchfilter(self):
        xml = build_day_book("01-04-2025", "30-04-2025")
        assert "VchTypeFilter" not in xml

    def test_ledger_vouchers_no_indaterange(self):
        xml = build_ledger_vouchers("Cash", "01-07-2025", "31-07-2025")
        assert "InDateRange" not in xml

    def test_ledger_vouchers_has_ledger_filter(self):
        xml = build_ledger_vouchers("HDFC Bank", "01-04-2025", "31-03-2026")
        assert "LedgerFilter" in xml
        assert "HDFC Bank" in xml

    def test_ledger_vouchers_has_svdates(self):
        xml = build_ledger_vouchers("Cash", "01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml


class TestFullFYDateRange:
    """Verify all report/voucher builders handle full FY date ranges correctly."""

    def test_trial_balance_full_fy(self):
        xml = build_trial_balance("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml

    def test_profit_and_loss_full_fy(self):
        xml = build_profit_and_loss("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml

    def test_sales_register_full_fy(self):
        xml = build_sales_register("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "InDateRange" not in xml

    def test_purchase_register_full_fy(self):
        xml = build_purchase_register("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "InDateRange" not in xml

    def test_day_book_full_fy(self):
        xml = build_day_book("01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "InDateRange" not in xml

    def test_ledger_vouchers_full_fy(self):
        xml = build_ledger_vouchers("Cash", "01-04-2025", "31-03-2026")
        assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml
        assert "InDateRange" not in xml

    def test_balance_sheet_single_date(self):
        xml = build_balance_sheet("31-03-2026")
        assert "<SVTODATE TYPE=\"Date\">31-03-2026</SVTODATE>" in xml


def test_build_cash_flow_has_report_id():
    from backend.tally_bridge.request_builder import build_cash_flow
    xml = build_cash_flow("01-04-2025", "31-03-2026")
    assert "Cash Flow" in xml
    assert "<SVFROMDATE TYPE=\"Date\">01-04-2025</SVFROMDATE>" in xml


# ---------------------------------------------------------------------------
# C33 (2026-09-24, live-verified): Tally honours SVFROMDATE/SVTODATE on a
# Voucher COLLECTION export only when the variable carries TYPE="Date";
# untyped, it silently answers for the company's CURRENT period (every past-FY
# day book / register / ledger read came back as current-period data, which
# the Python date filter turned into an empty result). Reports honour both
# forms byte-identically, so every dated builder uses the typed form.
# Evidence: docs/backend-date-vars-audit-2026-09-24.md (BI branch).
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as _ET

import pytest as _pytest

from backend.tally_bridge.request_builder import build_party_vouchers

_PAST_FROM, _PAST_TO = "01-04-2022", "31-03-2023"

_DATED_BUILDERS = {
    # voucher collections — C33 applies (typed is REQUIRED)
    "day_book": lambda co=None: build_day_book(_PAST_FROM, _PAST_TO, company=co),
    "day_book_payment": lambda co=None: build_day_book(_PAST_FROM, _PAST_TO, "Payment", co),
    "sales_register": lambda co=None: build_sales_register(_PAST_FROM, _PAST_TO, co),
    "purchase_register": lambda co=None: build_purchase_register(_PAST_FROM, _PAST_TO, co),
    "ledger_vouchers": lambda co=None: build_ledger_vouchers("Apex Technologies Pvt Ltd", _PAST_FROM, _PAST_TO, co),
    "party_vouchers": lambda co=None: build_party_vouchers("Apex Technologies Pvt Ltd", ["Sales"], _PAST_FROM, _PAST_TO, co),
    # reports — typed is equally valid (byte-identical live); one convention
    "trial_balance": lambda co=None: build_trial_balance(_PAST_FROM, _PAST_TO, co),
    "profit_and_loss": lambda co=None: build_profit_and_loss(_PAST_FROM, _PAST_TO, co),
    "balance_sheet": lambda co=None: build_balance_sheet(_PAST_TO, co),
    "bills_receivable": lambda co=None: build_bills_receivable(_PAST_TO, co),
    "bills_payable": lambda co=None: build_bills_payable(_PAST_TO, co),
    "stock_summary": lambda co=None: build_stock_summary(_PAST_TO, None, co),
    "cash_flow": lambda co=None: build_cash_flow(_PAST_FROM, _PAST_TO, co),
}


class TestTypedDateStaticVariables:
    @_pytest.mark.parametrize("name", sorted(_DATED_BUILDERS))
    def test_every_date_var_is_typed(self, name):
        root = _ET.fromstring(_DATED_BUILDERS[name]())
        date_vars = [e for e in root.iter() if e.tag in ("SVFROMDATE", "SVTODATE")]
        assert {e.tag for e in date_vars} == {"SVFROMDATE", "SVTODATE"}
        for e in date_vars:
            assert e.get("TYPE") == "Date", f"{name}: <{e.tag}> must carry TYPE=\"Date\" (C33)"

    @_pytest.mark.parametrize("name", sorted(_DATED_BUILDERS))
    def test_no_untyped_date_tag_anywhere(self, name):
        xml = _DATED_BUILDERS[name]()
        assert "<SVFROMDATE>" not in xml
        assert "<SVTODATE>" not in xml

    def test_past_window_values_are_carried_verbatim(self):
        xml = build_sales_register(_PAST_FROM, _PAST_TO)
        assert f'<SVFROMDATE TYPE="Date">{_PAST_FROM}</SVFROMDATE>' in xml
        assert f'<SVTODATE TYPE="Date">{_PAST_TO}</SVTODATE>' in xml

    def test_as_on_reports_send_same_typed_date_for_from_and_to(self):
        xml = build_balance_sheet("31-03-2023")
        assert '<SVFROMDATE TYPE="Date">31-03-2023</SVFROMDATE>' in xml
        assert '<SVTODATE TYPE="Date">31-03-2023</SVTODATE>' in xml


class TestCompanyNameEscaping:
    """Audit side finding: an unescaped '&' in the company name made Tally answer
    "Unknown Request, cannot be processed" for every report/voucher read."""

    COMPANY = "Sharma & Sons' Probe Traders"

    @_pytest.mark.parametrize("name", sorted(_DATED_BUILDERS))
    def test_company_with_ampersand_is_well_formed_and_round_trips(self, name):
        xml = _DATED_BUILDERS[name](self.COMPANY)
        assert "Sharma & Sons" not in xml  # raw '&' must never reach Tally
        root = _ET.fromstring(xml)  # well-formed
        companies = [e.text for e in root.iter("SVCurrentCompany")]
        assert companies == [self.COMPANY]

    @_pytest.mark.parametrize("name", sorted(_DATED_BUILDERS))
    def test_company_with_angle_brackets_is_escaped(self, name):
        xml = _DATED_BUILDERS[name]("A <B> Co")
        root = _ET.fromstring(xml)
        assert [e.text for e in root.iter("SVCurrentCompany")] == ["A <B> Co"]
