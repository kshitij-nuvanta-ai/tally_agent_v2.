"""Probe: read back GST details on Samsung 24 inch Monitor."""
import asyncio
from xml.sax.saxutils import escape as xml_escape

from tally_bridge.client import TallyClient

COMPANY = "Bharat Traders Private Limited"
ITEM = "Samsung 24 inch Monitor"


def _esc(s):
    return xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def query_envelope() -> str:
    return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>StockItemDetails</ID></HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVCURRENTCOMPANY>{_esc(COMPANY)}</SVCURRENTCOMPANY>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="StockItemDetails" ISMODIFY="No">
<TYPE>StockItem</TYPE>
<FILTER>NameMatch</FILTER>
<NATIVEMETHOD>Name</NATIVEMETHOD>
<NATIVEMETHOD>Parent</NATIVEMETHOD>
<NATIVEMETHOD>HSNCode</NATIVEMETHOD>
<NATIVEMETHOD>HSN</NATIVEMETHOD>
<NATIVEMETHOD>HSNDetails</NATIVEMETHOD>
<NATIVEMETHOD>GSTApplicable</NATIVEMETHOD>
<NATIVEMETHOD>GSTTypeOfSupply</NATIVEMETHOD>
<NATIVEMETHOD>GSTDetails</NATIVEMETHOD>
<NATIVEMETHOD>IGSTRate</NATIVEMETHOD>
<NATIVEMETHOD>CGSTRate</NATIVEMETHOD>
<NATIVEMETHOD>SGSTRate</NATIVEMETHOD>
<NATIVEMETHOD>StateWiseDetails</NATIVEMETHOD>
<NATIVEMETHOD>SetAlterGSTDetails</NATIVEMETHOD>
<NATIVEMETHOD>RateOfTaxCalculation</NATIVEMETHOD>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="NameMatch">$Name = "{_esc(ITEM)}"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""


async def main():
    client = TallyClient()
    resp = await client.post_xml(query_envelope())
    print(resp)


if __name__ == "__main__":
    asyncio.run(main())
