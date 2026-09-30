"""One-shot probe: dump full XML of Purchase voucher MID 19 (P001 to Samsung,
voucher date 28-Sep-2025) which the user just edited in the Tally UI to set
Supplier Invoice Date = 25-Sep-2025.

Goal: identify the canonical XML field name + format Tally uses to carry the
"Supplier Invoice Date" value on a Purchase voucher.

Strategy:
1. Try TYPE=Object/Voucher with LOOKUPVALUE=19 (MASTER ID) — returns full
   voucher XML with ALL fields (no NATIVEMETHOD whitelist).
2. Fallback: fetch day book for 28-Sep-2025 with a wider field list and locate
   the Purchase voucher.
3. Print full raw XML, then post-process to highlight every element/attribute
   whose value contains a "25" date-like pattern.

Read-only. No production code touched.
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from typing import Iterable

from tally_bridge.client import TallyClient

COMPANY = "Bharat Traders Private Limited"
TARGET_MID = "19"
TARGET_DATE_TALLY = "20250928"  # voucher date (YYYYMMDD form for SVFROMDATE/SVTODATE)
TARGET_DATE_DDMMM = "28-Sep-2025"

# Patterns that the supplier-invoice-date value (25-Sep-2025) might appear in.
DATE25_PATTERNS = [
    re.compile(r"25-Sep-2025", re.IGNORECASE),
    re.compile(r"25-09-2025"),
    re.compile(r"25/09/2025"),
    re.compile(r"\b20250925\b"),
    re.compile(r"\b25-9-2025\b"),
    re.compile(r"\b25\.09\.2025\b"),
    re.compile(r"\b25 Sep 2025\b", re.IGNORECASE),
    re.compile(r"\bSep 25,? 2025\b", re.IGNORECASE),
]

KEYWORD_PATTERNS = [
    re.compile(r"SUPPLIER", re.IGNORECASE),
    re.compile(r"INVOICE", re.IGNORECASE),
    re.compile(r"REFERENCE", re.IGNORECASE),
    re.compile(r"BILLDATE", re.IGNORECASE),
    re.compile(r"BASICBASEDATE", re.IGNORECASE),
    re.compile(r"BASICDATEOFREFERENCE", re.IGNORECASE),
    re.compile(r"EFFECTIVEDATE", re.IGNORECASE),
]


def build_voucher_object_lookup(master_id: str, company: str) -> str:
    """TYPE=Object Voucher fetch by master id — returns full voucher XML."""
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Object</TYPE>
<SUBTYPE>Voucher</SUBTYPE>
<ID TYPE="Name">ID</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCurrentCompany>{company}</SVCurrentCompany>
</STATICVARIABLES>
<FETCHLIST>
<FETCH>MasterID</FETCH>
<FETCH>VoucherNumber</FETCH>
<FETCH>VoucherTypeName</FETCH>
<FETCH>Date</FETCH>
<FETCH>PartyLedgerName</FETCH>
<FETCH>Reference</FETCH>
<FETCH>ReferenceDate</FETCH>
<FETCH>BasicBaseDate</FETCH>
<FETCH>BasicDateOfReference</FETCH>
<FETCH>EffectiveDate</FETCH>
<FETCH>BillDate</FETCH>
<FETCH>SupplierInvoiceDate</FETCH>
<FETCH>SupplierInvoiceNumber</FETCH>
<FETCH>Narration</FETCH>
<FETCH>AllLedgerEntries</FETCH>
<FETCH>AllInventoryEntries</FETCH>
<FETCH>BillAllocations</FETCH>
</FETCHLIST>
<STATICVARIABLES>
<LOOKUPVALUE>{master_id}</LOOKUPVALUE>
</STATICVARIABLES>
</DESC>
</BODY>
</ENVELOPE>"""


def build_day_book_full(from_date: str, to_date: str, company: str) -> str:
    """Day book without NATIVEMETHOD whitelist — relies on Tally returning
    voucher objects with the standard field set. We pull all candidate
    supplier-invoice-date fields explicitly via NATIVEMETHOD (avoiding `*`
    which is documented to crash Tally per LESSONS.md).
    """
    fields = [
        "Date",
        "VoucherTypeName",
        "VoucherNumber",
        "PartyLedgerName",
        "Narration",
        "Reference",
        "ReferenceDate",
        "BasicBaseDate",
        "BasicDateOfReference",
        "EffectiveDate",
        "BillDate",
        "SupplierInvoiceDate",
        "SupplierInvoiceNumber",
        "MasterID",
        "AlterID",
        "AllLedgerEntries",
        "AllInventoryEntries",
        "BillAllocations",
    ]
    natives = "\n".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fields)
    return f"""<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>DayBookProbe</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
<SVCurrentCompany>{company}</SVCurrentCompany>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="DayBookProbe" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FILTER>PurchFilter</FILTER>
{natives}
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="PurchFilter">$VoucherTypeName = "Purchase"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


# ----- post-processing --------------------------------------------------------

def _xpath_for(elem: ET.Element, parent_map: dict[ET.Element, ET.Element]) -> str:
    parts: list[str] = []
    cur: ET.Element | None = elem
    while cur is not None:
        parts.append(cur.tag)
        cur = parent_map.get(cur)
    return "/" + "/".join(reversed(parts))


def _matches_any(text: str, patterns: Iterable[re.Pattern]) -> list[str]:
    return [p.pattern for p in patterns if p.search(text)]


def scan_xml(xml_text: str, label: str) -> None:
    print(f"\n=== [{label}] POST-PROCESS: matches for 25-Sep-2025 patterns ===")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        return
    parent_map = {child: parent for parent in root.iter() for child in parent}
    date_hits = 0
    keyword_hits = 0
    for elem in root.iter():
        text = (elem.text or "").strip()
        # 25-date matches in element text
        if text:
            matched = _matches_any(text, DATE25_PATTERNS)
            if matched:
                date_hits += 1
                xpath = _xpath_for(elem, parent_map)
                print(f"  [DATE-25] {xpath} = {text!r}  (matched: {matched})")
        # check attribute values too
        for k, v in elem.attrib.items():
            if v and _matches_any(v, DATE25_PATTERNS):
                date_hits += 1
                xpath = _xpath_for(elem, parent_map)
                print(f"  [DATE-25 ATTR] {xpath}@{k} = {v!r}")
    print(f"  total date-25 hits: {date_hits}")

    print(f"\n=== [{label}] POST-PROCESS: SUPPLIER/INVOICE/REFERENCE/*DATE elements ===")
    for elem in root.iter():
        tag = elem.tag
        if _matches_any(tag, KEYWORD_PATTERNS):
            keyword_hits += 1
            xpath = _xpath_for(elem, parent_map)
            text = (elem.text or "").strip()
            attrs = dict(elem.attrib)
            if text or attrs:
                print(f"  [KW] {xpath} text={text!r} attrs={attrs}")
            else:
                # container; show its children one level
                children = [
                    f"{c.tag}={(c.text or '').strip()!r}" for c in list(elem)
                ]
                print(f"  [KW container] {xpath} children=[{', '.join(children[:20])}]")
    print(f"  total keyword-tag hits: {keyword_hits}")


def find_target_voucher_chunk(xml_text: str) -> str | None:
    """Locate the <VOUCHER ...>...</VOUCHER> block whose MASTERID == TARGET_MID
    so we can scan just that one record.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for v in root.iter("VOUCHER"):
        mid_el = v.find("MASTERID")
        if mid_el is not None and (mid_el.text or "").strip() == TARGET_MID:
            return ET.tostring(v, encoding="unicode")
    # fallback: find first Purchase voucher dated 28-Sep-2025
    for v in root.iter("VOUCHER"):
        d = v.find("DATE")
        vt = v.find("VOUCHERTYPENAME")
        if d is not None and vt is not None:
            if (d.text or "").strip() == TARGET_DATE_TALLY and "Purchase" in (vt.text or ""):
                return ET.tostring(v, encoding="unicode")
    return None


# ----- driver -----------------------------------------------------------------

async def main() -> None:
    client = TallyClient()

    print("=" * 80)
    print(f"PROBE: Supplier Invoice Date for MID {TARGET_MID} ({TARGET_DATE_DDMMM})")
    print(f"Company: {COMPANY}")
    print("=" * 80)

    # --- Attempt 1: Object/Voucher LOOKUPVALUE -------------------------------
    print("\n--- Attempt 1: TYPE=Object/Voucher LOOKUPVALUE=19 ---")
    req1 = build_voucher_object_lookup(TARGET_MID, COMPANY)
    print("\n=== REQUEST 1 ===")
    print(req1)
    try:
        resp1 = await client.post_xml(req1)
    except Exception as e:  # noqa: BLE001
        resp1 = f"<ERROR>{e}</ERROR>"
    print("\n=== RESPONSE 1 (raw, full) ===")
    print(resp1)
    scan_xml(resp1, "Attempt1-Object")

    # --- Attempt 2: day book Purchase 28-Sep-2025 ----------------------------
    print("\n\n--- Attempt 2: Collection day-book Purchase, 28-Sep-2025 ---")
    req2 = build_day_book_full(TARGET_DATE_TALLY, TARGET_DATE_TALLY, COMPANY)
    print("\n=== REQUEST 2 ===")
    print(req2)
    try:
        resp2 = await client.post_xml(req2)
    except Exception as e:  # noqa: BLE001
        resp2 = f"<ERROR>{e}</ERROR>"
    print("\n=== RESPONSE 2 (raw, full) ===")
    print(resp2)

    # narrow down to MID 19 voucher block before scanning, to reduce noise
    target_chunk = find_target_voucher_chunk(resp2)
    if target_chunk:
        print("\n=== ISOLATED MID-19 VOUCHER BLOCK (raw) ===")
        print(target_chunk)
        scan_xml(target_chunk, "Attempt2-MID19-block")
    else:
        print("\n[!] Could not isolate MID 19 voucher block — scanning full response.")
        scan_xml(resp2, "Attempt2-full")


if __name__ == "__main__":
    asyncio.run(main())
