# Copied from: backend/tally_bridge/import_builder.py @ c04d7d2
# Changes: only _esc (now esc) and _wrap_import (now wrap_import) are copied, no voucher/master builders; the report
# name is checked at run time instead of by Literal; ImportResult (the parsed CREATED/ALTERED/… counts) is new.
"""The Tally XML import envelope. Used only by probes/setup/ (the agent ships with no write code, S0-D8)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape

REPORTS = ("Vouchers", "All Masters")


def esc(s: str) -> str:
    """Escape XML special characters in element text and attribute values."""
    return xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def wrap_import(report_name: str, company: str, inner_xml: str) -> str:
    """Wrap entity XML in the standard IMPORTDATA envelope."""
    if report_name not in REPORTS:
        raise ValueError(f"Unknown import report {report_name!r} (expected one of {REPORTS})")
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>{report_name}</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
{inner_xml}
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def _count(text: str, tag: str) -> int:
    match = re.search(rf"<{tag}>\s*(-?\d+)\s*</{tag}>", text)
    return int(match.group(1)) if match else 0


@dataclass(frozen=True)
class ImportResult:
    created: int
    altered: int
    deleted: int
    errors: int
    exceptions: int
    last_vch_id: str
    line_error: str

    @classmethod
    def parse(cls, text: str) -> "ImportResult":
        vch = re.search(r"<LASTVCHID>\s*([^<]*?)\s*</LASTVCHID>", text)
        line_error = re.search(r"<LINEERROR>([^<]*)</LINEERROR>", text)
        return cls(created=_count(text, "CREATED"), altered=_count(text, "ALTERED"), deleted=_count(text, "DELETED"),
                   errors=_count(text, "ERRORS"), exceptions=_count(text, "EXCEPTIONS"),
                   last_vch_id=vch.group(1) if vch else "",
                   line_error=line_error.group(1).strip() if line_error else "")

    @property
    def clean(self) -> bool:
        return self.errors == 0 and self.exceptions == 0 and not self.line_error
