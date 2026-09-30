"""Probe: ALTER one stock item to find the flag that makes GST rates stick.

Background: docs/tally-write-exploration-v4.md Op 3 envelope created stock items
with CREATED=1 but Tally UI shows GST as "as per company/stock group" — meaning
the GSTDETAILS.LIST was accepted at parse time but item-level rates didn't
persist. Hypothesis: missing <SETALTERGSTDETAILS>Yes</SETALTERGSTDETAILS> flag.
"""
import asyncio
from xml.sax.saxutils import escape as xml_escape

from tally_bridge.client import TallyClient
from tally_bridge.response_parser import parse_import_response

COMPANY = "Bharat Traders Private Limited"
ITEM = "Samsung 24 inch Monitor"


def _esc(s: str) -> str:
    return xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def alter_envelope(set_alter_flag: bool) -> str:
    """ALTER Samsung 24 inch Monitor with full GST envelope, with/without
    SETALTERGSTDETAILS flag."""
    flag_xml = "<SETALTERGSTDETAILS>Yes</SETALTERGSTDETAILS>\n" if set_alter_flag else ""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>All Masters</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{_esc(COMPANY)}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
<STOCKITEM NAME="{_esc(ITEM)}" ACTION="Alter">
<NAME.LIST><NAME>{_esc(ITEM)}</NAME></NAME.LIST>
<GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
<GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
{flag_xml}<HSNCODE>8528</HSNCODE>
<HSN>8528</HSN>
<HSNDETAILS.LIST>
<APPLICABLEFROM>20250401</APPLICABLEFROM>
<HSNCODE>8528</HSNCODE>
<HSN>8528</HSN>
</HSNDETAILS.LIST>
<GSTDETAILS.LIST>
<APPLICABLEFROM>20250401</APPLICABLEFROM>
<TAXABILITY>Taxable</TAXABILITY>
<IGSTRATE>18</IGSTRATE>
<CGSTRATE>9</CGSTRATE>
<SGSTRATE>9</SGSTRATE>
<STATEWISEDETAILS.LIST>
<STATENAME>Any</STATENAME>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>9</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>9</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>18</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>0</GSTRATE></RATEDETAILS.LIST>
</STATEWISEDETAILS.LIST>
</GSTDETAILS.LIST>
</STOCKITEM>
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


async def main():
    client = TallyClient()
    print("Probe 1: ALTER with <SETALTERGSTDETAILS>Yes</SETALTERGSTDETAILS>")
    xml = alter_envelope(set_alter_flag=True)
    resp = await client.post_xml(xml)
    parsed = parse_import_response(resp)
    print(f"  parsed: {parsed}")
    print(f"  raw response (first 800 chars): {resp[:800]}")


if __name__ == "__main__":
    asyncio.run(main())
