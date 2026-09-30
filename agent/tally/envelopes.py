# Copied from: backend/tally_bridge/request_builder.py @ c04d7d2
# Changes: the company name is always XML-escaped (the source leaves it raw in _wrap_voucher_collection,
# _wrap_report_envelope and build_ledger_vouchers — Part 1 R13); the collection wrapper also takes static
# variables, filters and extra TDL; only the wrappers and build_company_list are copied; SV*DATE static variables
# carry TYPE="Date" (C33 — the source sends them untyped and Tally then ignores them).
"""Pure XML envelope builders for Tally export requests. No I/O."""
from __future__ import annotations

import re
from xml.sax.saxutils import escape as _xml_escape

COMPANY_PLACEHOLDER = "__COMPANY__"
_VAR_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# C33 (live 2026-09-24): Tally honours a period variable ONLY with TYPE="Date". Untyped, `<SVFROMDATE>01-04-2022`
# (and 20220401, 1-Apr-2022) is silently replaced by the company's current period — a healthy answer for the
# wrong window. Typed, all three formats are honoured. Every SV*DATE variable is typed here, and nothing else.
_DATE_VAR = re.compile(r"^SV[A-Z0-9]*DATE$", re.IGNORECASE)


def esc(value: str) -> str:
    """Escape text for XML element content or attribute values."""
    return _xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def formula_string(value: str) -> str:
    """A TDL string literal for formulas like `$Name = "..."`. Values containing '"' are refused."""
    if '"' in value:
        raise ValueError(f"Can't put a double quote inside a TDL string literal: {value!r}")
    return f'"{value}"'


def _static_vars(company: str | None, static_vars: dict[str, str] | None) -> str:
    lines = ["<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"]
    for name, value in (static_vars or {}).items():
        if not _VAR_NAME.match(name):
            raise ValueError(f"Invalid static variable name: {name!r}")
        type_attr = ' TYPE="Date"' if _DATE_VAR.match(name) else ""
        lines.append(f"<{name}{type_attr}>{esc(value)}</{name}>")
    if company:
        lines.append(f"<SVCurrentCompany>{esc(company)}</SVCurrentCompany>")
    return "\n".join(lines)


def wrap_collection(
    collection_name: str,
    object_type: str,
    native_methods: list[str],
    company: str | None = None,
    *,
    static_vars: dict[str, str] | None = None,
    filters: list[tuple[str, str]] | None = None,
    extra_collection_xml: str = "",
    extra_tdl_xml: str = "",
) -> str:
    """A TDL collection export. `filters` are (name, formula) pairs; formulas are XML-escaped."""
    filters = filters or []
    filter_refs = "\n".join(f"<FILTER>{esc(name)}</FILTER>" for name, _ in filters)
    methods_xml = "\n".join(f"<NATIVEMETHOD>{method}</NATIVEMETHOD>" for method in native_methods)
    systems = "\n".join(
        f'<SYSTEM TYPE="Formulae" NAME="{esc(name)}">{_xml_escape(formula)}</SYSTEM>' for name, formula in filters
    )
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>{esc(collection_name)}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
{_static_vars(company, static_vars)}
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="{esc(collection_name)}" ISMODIFY="No">
<TYPE>{esc(object_type)}</TYPE>
{filter_refs}
{methods_xml}
{extra_collection_xml}
</COLLECTION>
{systems}
{extra_tdl_xml}
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


def wrap_report(
    report_id: str,
    from_date: str,
    to_date: str,
    company: str | None = None,
    extra_vars: dict[str, str] | None = None,
) -> str:
    """A TYPE=Data report export (Trial Balance, Bills Receivable, …). Dates are DD-MM-YYYY."""
    variables = {"SVFROMDATE": from_date, "SVTODATE": to_date, **(extra_vars or {})}
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE>
<ID>{esc(report_id)}</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
{_static_vars(company, variables)}
</STATICVARIABLES>
</DESC>
</BODY>
</ENVELOPE>"""


def build_company_list() -> str:
    """List loaded companies (verified live as probe E7 — Collection TYPE=Company)."""
    return wrap_collection("List of Companies", "Company", ["Name"])
