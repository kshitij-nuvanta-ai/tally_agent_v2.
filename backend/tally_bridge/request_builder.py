"""
Build XML request payloads for TallyPrime HTTP API.
All functions are pure — no I/O, no side effects. Each returns an XML string.
Tally date format: DD-MM-YYYY
"""

from xml.sax.saxutils import escape as xml_escape


def _date_vars(from_date: str, to_date: str) -> str:
    """Render the SVFROMDATE/SVTODATE static variables, TYPED as Date.

    C33 (live-verified 2026-09-24): on a Voucher COLLECTION export Tally honours
    the date window only when the variables carry ``TYPE="Date"``; untyped, it
    silently answers for the company's CURRENT period (past-FY reads returned
    current-period vouchers, which the Python date filter then emptied).
    Reports (TYPE=Data) honour both forms byte-identically, so every dated
    request uses this one typed form. Dates are DD-MM-YYYY.

    C43 (Educational TallyPrime only): a date whose day is not 1/2/31 is ignored
    and falls back to the current period's end — see LESSONS.md
    "C33/C43 — date static variables (2026-09-24)". Not clamped here.
    """
    return (
        f'<SVFROMDATE TYPE="Date">{xml_escape(from_date)}</SVFROMDATE>\n'
        f'<SVTODATE TYPE="Date">{xml_escape(to_date)}</SVTODATE>'
    )


def _company_var(company: str | None) -> str:
    """SVCurrentCompany static variable, XML-escaped ('&' in a company name
    otherwise makes Tally answer "Unknown Request, cannot be processed")."""
    return f"<SVCurrentCompany>{xml_escape(company)}</SVCurrentCompany>" if company else ""


def _wrap_envelope(header_id: str, body_desc: str) -> str:
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE>
<ID>{header_id}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES>
{body_desc}
</DESC>
</BODY>
</ENVELOPE>"""


def _wrap_collection_envelope(collection_name: str, object_type: str, native_methods: list[str], company: str | None = None) -> str:
    methods_xml = "\n".join(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>" for m in native_methods)
    company_var = _company_var(company)
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>{collection_name}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{company_var}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="{collection_name}" ISMODIFY="No">
<TYPE>{object_type}</TYPE>
{methods_xml}
</COLLECTION>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


def _voucher_native_methods() -> str:
    """Return NATIVEMETHOD elements for the specific voucher fields we need.
    Using * fetches ALL fields and can overload Tally with large datasets.
    """
    fields = ["Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName",
              "Narration", "AllLedgerEntries", "AllInventoryEntries"]
    return "\n".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fields)


def _wrap_voucher_collection(collection_name: str, from_date: str, to_date: str, voucher_type_filter: str | None = None, company: str | None = None) -> str:
    """Build a TDL Collection query for vouchers. Returns voucher objects with specific fields only."""
    company_var = _company_var(company)
    filters = []
    systems = []
    # Dates are TYPED (see _date_vars / C33) — untyped vars are ignored on a
    # Voucher collection. Python-side _filter_vouchers_by_date() in
    # response_parser.py stays as the safety net (e.g. C43 on Educational).
    if voucher_type_filter:
        voucher_type_filter = voucher_type_filter.title()
        safe_type = xml_escape(voucher_type_filter, {'"': "&quot;"})
        filters.append("<FILTER>VchTypeFilter</FILTER>")
        systems.append(f'<SYSTEM TYPE="Formulae" NAME="VchTypeFilter">$VoucherTypeName = "{safe_type}"</SYSTEM>')
    filter_xml = "\n".join(filters)
    system_xml = "\n".join(systems)
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>{collection_name}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{_date_vars(from_date, to_date)}
{company_var}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="{collection_name}" ISMODIFY="No">
<TYPE>Voucher</TYPE>
{filter_xml}
{_voucher_native_methods()}
</COLLECTION>
{system_xml}
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


def _wrap_report_envelope(report_id: str, from_date: str, to_date: str, company: str | None = None, extra_vars: str = "") -> str:
    company_var = _company_var(company)
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE>
<ID>{report_id}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{_date_vars(from_date, to_date)}
{company_var}
{extra_vars}
</STATICVARIABLES>
</DESC>
</BODY>
</ENVELOPE>"""


# --- Master Queries ---

def build_list_companies() -> str:
    return """<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>EXPORT</TALLYREQUEST>
<TYPE>COLLECTION</TYPE>
<ID>List of Companies</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES>
</DESC>
</BODY>
</ENVELOPE>"""

def build_company_list() -> str:
    """List loaded companies (verified probe E7 — Collection TYPE=Company).

    Returns the same `List of Companies` collection envelope verified live in
    probe_group_b.py::probe_e7. Used to populate the connect-company dropdown.
    """
    return _wrap_collection_envelope("List of Companies", "Company", ["Name"])


def build_party_vouchers(
    party: str,
    voucher_types: list[str],
    from_date: str,
    to_date: str,
    company: str | None = None,
) -> str:
    """Vouchers for a party filtered by type (verified probe E8).

    Mirrors probe_group_b.py::build_party_vouchers_query: a TDL Collection of
    Voucher objects constrained to the requested voucher types via
    `CHILDOF $$VchType<Type>` and filtered to the party via a `$PartyLedgerName`
    FILTER formula. Used by the DN/CN flow to find a party's original invoices.

    When multiple voucher_types are given, the CHILDOF lines are emitted once
    per type (Tally treats repeated CHILDOF as a union).
    """
    company_var = _company_var(company)
    safe_party = xml_escape(party, {'"': "&quot;"})
    childof_xml = "\n".join(
        f"<CHILDOF>$$VchType{xml_escape(vt.title())}</CHILDOF>" for vt in voucher_types
    )
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>PartyVouchers</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{_date_vars(from_date, to_date)}
{company_var}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="PartyVouchers" ISMODIFY="No">
<TYPE>Voucher</TYPE>
{childof_xml}
<NATIVEMETHOD>Date</NATIVEMETHOD>
<NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>
<NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>
<NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>
<NATIVEMETHOD>Reference</NATIVEMETHOD>
<NATIVEMETHOD>Amount</NATIVEMETHOD>
<FILTER>PartyVchFilter</FILTER>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="PartyVchFilter">$PartyLedgerName = "{safe_party}"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


def build_list_ledgers(company: str | None = None) -> str:
    return _wrap_collection_envelope("CustomLedgerList", "Ledger", ["Name", "Parent", "ClosingBalance", "OpeningBalance"], company=company)

def build_list_groups() -> str:
    return _wrap_collection_envelope("CustomGroupList", "Group", ["Name", "Parent"])

def build_list_stock_items() -> str:
    return _wrap_collection_envelope("CustomStockItemList", "StockItem", ["Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingRate", "ClosingValue"])


def build_list_stock_groups() -> str:
    return _wrap_collection_envelope("CustomStockGroupList", "StockGroup", ["Name"])


# --- Report Queries ---

def build_trial_balance(from_date: str, to_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Trial Balance", from_date, to_date, company)

def build_profit_and_loss(from_date: str, to_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Profit and Loss", from_date, to_date, company)

def build_balance_sheet(as_on_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Balance Sheet", as_on_date, as_on_date, company)

def build_bills_receivable(as_on_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Bills Receivable", as_on_date, as_on_date, company)

def build_bills_payable(as_on_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Bills Payable", as_on_date, as_on_date, company)

def build_stock_summary(as_on_date: str, stock_group: str | None = None, company: str | None = None) -> str:
    extra = f"<SVSTOCKGROUP>{xml_escape(stock_group)}</SVSTOCKGROUP>" if stock_group else ""
    return _wrap_report_envelope("Stock Summary", as_on_date, as_on_date, company, extra)

def build_cash_flow(from_date: str, to_date: str, company: str | None = None) -> str:
    return _wrap_report_envelope("Cash Flow", from_date, to_date, company)


# --- Voucher Queries ---

def build_day_book(from_date: str, to_date: str, voucher_type: str | None = None, company: str | None = None) -> str:
    return _wrap_voucher_collection("DayBookVchs", from_date, to_date, voucher_type, company)

def build_ledger_vouchers(ledger_name: str, from_date: str, to_date: str, company: str | None = None) -> str:
    """Fetch vouchers for a specific ledger using TDL Collection with filters.
    Uses $PartyLedgerName comparison instead of $$IsLedgerInVoucher because
    the latter cannot handle ledger names containing commas.
    Python-side _filter_vouchers_by_date() in response_parser.py handles date filtering.
    """
    company_var = _company_var(company)
    safe_name = xml_escape(ledger_name, {'"': "&quot;"})
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>LedgerVchs</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
{_date_vars(from_date, to_date)}
{company_var}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="LedgerVchs" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FILTER>LedgerFilter</FILTER>
{_voucher_native_methods()}
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="LedgerFilter">$PartyLedgerName = "{safe_name}"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""

def build_sales_register(from_date: str, to_date: str, company: str | None = None) -> str:
    return _wrap_voucher_collection("SalesVchs", from_date, to_date, "Sales", company)

def build_purchase_register(from_date: str, to_date: str, company: str | None = None) -> str:
    return _wrap_voucher_collection("PurchaseVchs", from_date, to_date, "Purchase", company)
