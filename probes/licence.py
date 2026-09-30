"""TallyPrime's licence mode and release, read with a TYPE=Function export.

Live 2026-09-22: `$$LicenseInfo` with the parameter `IsEducationalMode` answered <RESULT>Yes</RESULT>, and the XML
header carried PRODMAJORREL 7 / PRODMINORREL 0. Used by the automated operator's answers and by probe 10.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LICENCE_REQUEST = """<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Function</TYPE>
<ID>$$LicenseInfo</ID>
</HEADER>
<BODY>
<DESC>
<FUNCPARAMLIST>
<PARAM>IsEducationalMode</PARAM>
</FUNCPARAMLIST>
</DESC>
</BODY>
</ENVELOPE>"""


@dataclass(frozen=True)
class LicenceInfo:
    educational: bool | None     # None when the answer holds no Yes / No
    release: str                 # e.g. "7.0"; "" when the header doesn't carry it


def parse_licence_info(text: str) -> LicenceInfo:
    result = re.search(r"<RESULT[^>]*>\s*([^<]*?)\s*</RESULT>", text)
    major = re.search(r"<PRODMAJORREL[^>]*>\s*(\d+)\s*</PRODMAJORREL>", text)
    minor = re.search(r"<PRODMINORREL[^>]*>\s*(\d+)\s*</PRODMINORREL>", text)
    value = result.group(1).strip().lower() if result else ""
    educational = True if value == "yes" else False if value == "no" else None
    release = f"{major.group(1)}.{minor.group(1) if minor else '0'}" if major else ""
    return LicenceInfo(educational=educational, release=release)
