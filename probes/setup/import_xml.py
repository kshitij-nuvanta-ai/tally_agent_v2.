"""The Tally XML import envelope for probes/setup/ (the only probe code that writes to Tally).

`esc` and the envelope itself are the bridge's (tally_bridge.import_builder). Added here: the report name is checked
at run time, and ImportResult holds the parsed CREATED/ALTERED/… counts of an import answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tally_bridge.import_builder import esc  # noqa: F401  (re-exported: probes/setup/writes.py imports it from here)
from tally_bridge.import_builder import wrap_import as _bridge_wrap_import

REPORTS = ("Vouchers", "All Masters")


def wrap_import(report_name: str, company: str, inner_xml: str) -> str:
    """Wrap entity XML in the standard IMPORTDATA envelope."""
    if report_name not in REPORTS:
        raise ValueError(f"Unknown import report {report_name!r} (expected one of {REPORTS})")
    return _bridge_wrap_import(report_name, company, inner_xml)


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
