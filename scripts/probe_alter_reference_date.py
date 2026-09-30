"""One-shot probe: ALTER REFERENCEDATE on Purchase voucher MID 19 in Tally.

Target: Bharat Traders Private Limited @ localhost:9000
- Voucher MID 19 (Purchase P001 to Samsung India Electronics, DATE=28-Sep-2025)
- Currently REFERENCEDATE=20250925 (UI-set), REFERENCE=REF_P001
- Goal: shift REFERENCEDATE to 20250926 via XML ALTER, then readback-verify.

Authoritative test of whether XML writes can set REFERENCEDATE now that the
user has UI-enabled supplier-invoice-date capture. Previous session showed the
field was silently overwritten with voucher DATE (28-Sep) instead of accepting
our value.

Read-only-ish: one ALTER, one readback. No production code touched.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from tally_bridge.client import TallyClient
from tally_bridge.response_parser import parse_import_response

COMPANY = "Bharat Traders Private Limited"
TARGET_MID = "19"
VCH_DATE_DDMMM = "28-Sep-2025"        # voucher DATE (used in TAGNAME envelope)
TARGET_DATE_TALLY = "20250928"        # YYYYMMDD form for SVFROMDATE/SVTODATE
NEW_REFERENCEDATE = "20250926"        # what we want REFERENCEDATE to become


def _esc(s: str) -> str:
    return xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def build_alter_reference_date(
    master_id: str, vch_date: str, new_ref_date: str, company: str
) -> str:
    """Minimal ALTER envelope. Mirrors import_builder._wrap_import IMPORTDATA
    pattern + the TAGNAME='Master ID' addressing used by build_cancel_voucher /
    build_delete_voucher (verified working for ALTERs in prior session).
    """
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>Vouchers</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER DATE="{_esc(vch_date)}" TAGNAME="Master ID" TAGVALUE="{_esc(master_id)}" VCHTYPE="Purchase" ACTION="Alter">
<REFERENCEDATE>{_esc(new_ref_date)}</REFERENCEDATE>
</VOUCHER>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_day_book_readback(from_date: str, to_date: str, company: str) -> str:
    """Day-book Collection over Purchase vouchers; pulls REFERENCE/REFERENCEDATE
    explicitly via NATIVEMETHOD. Matches the working pattern in
    probe_supplier_invoice_date.py (Attempt 2).
    """
    fields = [
        "Date",
        "VoucherTypeName",
        "VoucherNumber",
        "MasterID",
        "AlterID",
        "Reference",
        "ReferenceDate",
        "BasicBaseDate",
        "BasicDateOfReference",
        "EffectiveDate",
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
<SVCurrentCompany>{_esc(company)}</SVCurrentCompany>
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


def extract_mid_voucher(xml_text: str, mid: str) -> ET.Element | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[!] readback XML parse error: {e}")
        return None
    for v in root.iter("VOUCHER"):
        mid_el = v.find("MASTERID")
        if mid_el is not None and (mid_el.text or "").strip() == mid:
            return v
    return None


async def main() -> None:
    client = TallyClient()
    print("=" * 80)
    print(f"PROBE: ALTER REFERENCEDATE on MID {TARGET_MID}")
    print(f"  voucher date     = {VCH_DATE_DDMMM}")
    print(f"  current refdate  = 20250925 (UI-set)")
    print(f"  new refdate      = {NEW_REFERENCEDATE}")
    print(f"  company          = {COMPANY}")
    print("=" * 80)

    # --- Step 1: ALTER request ----------------------------------------------
    req = build_alter_reference_date(
        TARGET_MID, VCH_DATE_DDMMM, NEW_REFERENCEDATE, COMPANY
    )
    print("\n=== ALTER REQUEST ===")
    print(req)

    try:
        resp = await client.post_xml(req)
    except Exception as e:  # noqa: BLE001
        resp = f"<ERROR>{e}</ERROR>"
    print("\n=== ALTER RESPONSE (full raw) ===")
    print(resp)

    parsed = parse_import_response(resp)
    print("\n=== PARSED ALTER COUNTERS ===")
    for k in ("success", "created", "altered", "deleted", "errors", "exceptions",
              "last_vch_id", "error_message"):
        print(f"  {k}: {parsed.get(k)}")

    # --- Step 2: readback day-book on 28-Sep-2025 ---------------------------
    print("\n\n=== READBACK REQUEST (day-book Purchase 28-Sep-2025) ===")
    req2 = build_day_book_readback(TARGET_DATE_TALLY, TARGET_DATE_TALLY, COMPANY)
    print(req2)

    try:
        resp2 = await client.post_xml(req2)
    except Exception as e:  # noqa: BLE001
        resp2 = f"<ERROR>{e}</ERROR>"
    print("\n=== READBACK RESPONSE (full raw) ===")
    print(resp2)

    v = extract_mid_voucher(resp2, TARGET_MID)
    print("\n=== READBACK FOR MID 19 ===")
    if v is None:
        print(f"  [!] could not locate VOUCHER with MASTERID={TARGET_MID} in response")
        return

    ref_el = v.find("REFERENCE")
    refdate_el = v.find("REFERENCEDATE")
    date_el = v.find("DATE")
    vno_el = v.find("VOUCHERNUMBER")
    alterid_el = v.find("ALTERID")

    def _t(el: ET.Element | None) -> str:
        return "<missing>" if el is None else ((el.text or "").strip() or "<empty>")

    print(f"  DATE           = {_t(date_el)}")
    print(f"  VOUCHERNUMBER  = {_t(vno_el)}")
    print(f"  ALTERID        = {_t(alterid_el)}")
    print(f"  REFERENCE      = {_t(ref_el)}")
    print(f"  REFERENCEDATE  = {_t(refdate_el)}")

    # --- Verdict -------------------------------------------------------------
    rd = (refdate_el.text or "").strip() if refdate_el is not None else ""
    print("\n=== VERDICT ===")
    if rd == NEW_REFERENCEDATE:
        print(f"  SUCCESS — XML-set REFERENCEDATE stuck ({rd})")
    elif rd == "20250925":
        print(f"  NO-OP — REFERENCEDATE unchanged ({rd}); ALTER did not apply")
    elif rd == "20250928":
        print(f"  OVERWRITTEN — REFERENCEDATE clamped to voucher DATE ({rd}); "
              "prior-session failure mode reproduced")
    elif rd in ("", "<empty>", "<missing>"):
        print(f"  EMPTY — REFERENCEDATE not present ({rd!r})")
    else:
        print(f"  UNEXPECTED — REFERENCEDATE = {rd!r}")


if __name__ == "__main__":
    asyncio.run(main())
