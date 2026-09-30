# Live Tally Debug Report

**Date**: 2026-03-03
**Tally Host**: 192.168.18.219:9000
**Company**: NUVANTA AI TECHNOLOGIES PRIVATE LIMITED
**FY**: 2025-26 (01-Apr-2025 to 31-Mar-2026)

---

## Executive Summary

Three **ROOT CAUSES** identified for why queries return 0 results:

### Root Cause 1: Sales/Purchase Register returns MONTHLY SUMMARIES, not vouchers
Our `build_sales_register()` and `build_purchase_register()` use `TYPE=Data` with `ID=Sales Register`.
Tally returns `<DSPPERIOD>` monthly summary XML (12 months with Dr/Cr amounts), NOT individual voucher data.
Our `parse_vouchers()` looks for `<VOUCHER>` tags -- finds 0.

### Root Cause 2: Day Book (TYPE=Data) returns only the LAST voucher
Tally's `ID=Day Book` with `TYPE=Data` consistently returns only 1 voucher (the most recent one),
regardless of date range. This is a known Tally behavior -- Day Book as a "Data" export only emits
the last-created/modified voucher.

### Root Cause 3: Bills parser looks for wrong XML tags
`parse_bills()` searches for `<BILLSFIXED>` and `<BILLNAME>` / `<BILLAMOUNT>` / `<BILLPENDING>`.
Tally actually returns `<BILLFIXED>` with `<BILLREF>`, `<BILLCL>`, and no `<BILLPENDING>`.
**Bills Receivable and Payable DO return data** -- the parser just cannot read it.

---

## Detailed Findings Per Query

---

### 1. Sales Register

**Our payload** (`build_sales_register`):
```xml
<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE>
<ID>Sales Register</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>01-04-2025</SVFROMDATE>
<SVTODATE>31-03-2026</SVTODATE>
<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
</STATICVARIABLES>
</DESC>
</BODY>
</ENVELOPE>
```

**Result**: 146 lines, 12 `<DSPPERIOD>` entries (monthly summary), 0 `<VOUCHER>` tags.

**Actual response structure** (abbreviated):
```xml
<ENVELOPE>
 <DSPPERIOD>April</DSPPERIOD>
 <DSPACCINFO>
  <DSPDRAMT><DSPDRAMTA></DSPDRAMTA></DSPDRAMT>
  <DSPCRAMT><DSPCRAMTA></DSPCRAMTA></DSPCRAMT>
  <DSPCLAMT><DSPCLAMTA></DSPCLAMTA></DSPCLAMT>
 </DSPACCINFO>
 ...
 <DSPPERIOD>July</DSPPERIOD>
 <DSPACCINFO>
  <DSPCRAMT><DSPCRAMTA>295000.00</DSPCRAMTA></DSPCRAMT>
  <DSPCLAMT><DSPCLAMTA>295000.00</DSPCLAMTA></DSPCLAMT>
 </DSPACCINFO>
 ...
 <DSPPERIOD>January</DSPPERIOD>
 <DSPACCINFO>
  <DSPCRAMT><DSPCRAMTA>665850.00</DSPCRAMTA></DSPCRAMT>
  <DSPCLAMT><DSPCLAMTA>4034350.00</DSPCLAMTA></DSPCLAMT>
 </DSPACCINFO>
</ENVELOPE>
```

Monthly sales data present: Jul=295000, Aug=300000, Sep=600000, Oct=575000, Nov=590000, Dec=1008500, Jan=665850.
**Total sales: 4,034,350.00**

**WORKING alternative** -- TDL Collection with filter:
```xml
<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>SalesVchs</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>01-04-2025</SVFROMDATE>
<SVTODATE>31-03-2026</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="SalesVchs" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FILTER>SalesOnly</FILTER>
<NATIVEMETHOD>Date</NATIVEMETHOD>
<NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>
<NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>
<NATIVEMETHOD>Amount</NATIVEMETHOD>
<NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="SalesOnly">$VoucherTypeName = "Sales"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>
```

**Result**: 1444 lines, **19 Sales vouchers** returned.

Sample voucher structure:
```xml
<VOUCHER REMOTEID="..." VCHKEY="..." VCHTYPE="Sales" OBJVIEW="Invoice Voucher View">
 <DATE TYPE="Date">20250702</DATE>
 <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
 <PARTYLEDGERNAME TYPE="String">HCODE TECHNOLOGIES PRIVATE LIMITED</PARTYLEDGERNAME>
 <VOUCHERNUMBER>1</VOUCHERNUMBER>
 <REFERENCE TYPE="String">1</REFERENCE>
 <AMOUNT TYPE="Amount">-200000.00</AMOUNT>
 <ISINVOICE>Yes</ISINVOICE>
 <ALLINVENTORYENTRIES.LIST>
  <STOCKITEMNAME TYPE="String">IT Project Technical Consulting(WITHOUT GST)</STOCKITEMNAME>
  <RATE TYPE="Rate">200000.00/NOS</RATE>
  <AMOUNT TYPE="Amount">200000.00</AMOUNT>
  <ACTUALQTY TYPE="Quantity"> 1.0000 NOS</ACTUALQTY>
  ...
 </ALLINVENTORYENTRIES.LIST>
</VOUCHER>
```

**Note**: The Collection approach returns full VOUCHER objects with ALLLEDGERENTRIES.LIST,
ALLINVENTORYENTRIES.LIST, etc. -- compatible with `parse_vouchers()`.

---

### 2. Purchase Register

**Our payload** (`build_purchase_register`): Same structure as sales, ID="Purchase Register", VOUCHERTYPENAME=Purchase.

**Result**: 146 lines, 12 `<DSPPERIOD>` monthly summaries, 0 `<VOUCHER>` tags.

Monthly purchase data: Aug=-2074.44, Sep=-2096.86, Oct=-3568.00, Nov=-3580.00, Dec=-9966.31, Jan=-10896.00.
**Total purchases: -32,181.61** (negative = debit in Tally convention)

**WORKING alternative** -- TDL Collection with `$VoucherTypeName = "Purchase"` filter:
**Result**: 608 lines, **10 Purchase vouchers** returned.

Sample:
```xml
<VOUCHER ... VCHTYPE="Purchase" OBJVIEW="Invoice Voucher View">
 <DATE TYPE="Date">20250826</DATE>
 <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
 <PARTYLEDGERNAME TYPE="String">Anthropic, PBC</PARTYLEDGERNAME>
 <VOUCHERNUMBER>5</VOUCHERNUMBER>
 ...
</VOUCHER>
```

---

### 3. Day Book

**Our payload** (`build_day_book`):
```xml
<TYPE>Data</TYPE>
<ID>Day Book</ID>
... SVFROMDATE / SVTODATE ...
```

**Result (full year)**: 1386 lines, but only **1 VOUCHER** (Payment #59, dated 20260225).
**Result (Jul 2025 only)**: 1386 lines, still only **1 VOUCHER** (same Payment #59).
**Result (Feb 2026 only)**: 1386 lines, still only **1 VOUCHER** (same Payment #59).
**Result (with VOUCHERTYPENAME=Sales)**: 1386 lines, still only **1 VOUCHER** (same Payment #59).

**Conclusion**: Day Book with TYPE=Data ALWAYS returns only the last-modified/created voucher,
ignoring date range and voucher type filters entirely. This is a Tally behavior.

**WORKING alternative** -- TDL Collection (no filter for all voucher types):
```xml
<TYPE>Collection</TYPE>
<ID>AllVouchers</ID>
... with TDL Collection TYPE=Voucher ...
```

**Result**: 12854 lines, **221 total vouchers**.

Voucher type breakdown:
- Journal: 112
- Payment: 50
- Receipt: 26
- Sales: 19
- Purchase: 10
- Contra: 4

---

### 4. Bills Receivable

**Our payload** (`build_bills_receivable`):
```xml
<TYPE>Data</TYPE>
<ID>Bills Receivable</ID>
<SVFROMDATE>31-03-2026</SVFROMDATE>
<SVTODATE>31-03-2026</SVTODATE>
```

**Result**: 98 lines, **12 bills** (24 BILLFIXED open+close tags / 2). **DATA IS RETURNED!**

**But parser fails** because `parse_bills()` looks for:
- `<BILLSFIXED>` -- actual tag is `<BILLFIXED>`
- `<BILLNAME>` -- actual tag is `<BILLREF>`
- `<BILLAMOUNT>` -- actual tag is `<BILLCL>` (closing balance)
- `<BILLPENDING>` -- does not exist in response

**Actual response structure**:
```xml
<ENVELOPE>
 <BILLFIXED>
  <BILLDATE>2-Jul-25</BILLDATE>
  <BILLREF>#1</BILLREF>
  <BILLPARTY>HCODE TECHNOLOGIES PRIVATE LIMITED</BILLPARTY>
 </BILLFIXED>
 <BILLCL>-200000.00</BILLCL>
 <BILLDUE>2-Jul-25</BILLDUE>
 <BILLOVERDUE>272</BILLOVERDUE>
 ...
</ENVELOPE>
```

Bills receivable data (12 outstanding bills):
| BILLDATE    | BILLREF | BILLPARTY                                | BILLCL        |
|-------------|---------|------------------------------------------|---------------|
| 2-Jul-25    | #1      | HCODE TECHNOLOGIES PRIVATE LIMITED       | -200,000.00   |
| 2-Jul-25    | 2       | HCODE TECHNOLOGIES PRIVATE LIMITED       | -95,000.00    |
| 2-Aug-25    | # 4     | HCODE TECHNOLOGIES PRIVATE LIMITED       | -200,000.00   |
| 2-Aug-25    | 3       | HCODE TECHNOLOGIES PRIVATE LIMITED       | -100,000.00   |
| 1-Sep-25    | # 5     | HCODE TECHNOLOGIES PRIVATE LIMITED       | -200,000.00   |
| 1-Sep-25    | 6       | HCODE TECHNOLOGIES PRIVATE LIMITED       | -100,000.00   |
| 1-Sep-25    | 7       | SMARTBIKE MOBILITY PRIVATE LIMITED       | -300,000.00   |
| 1-Oct-25    | 8       | HCODE TECHNOLOGIES PRIVATE LIMITED       | -200,000.00   |
| 1-Oct-25    | 9       | HCODE TECHNOLOGIES PRIVATE LIMITED       | -75,000.00    |
| 1-Oct-25    | 10      | SMARTBIKE MOBILITY PRIVATE LIMITED       | -300,000.00   |
| 1-Nov-25    | #11     | HCODE TECHNOLOGIES PRIVATE LIMITED       | -236,000.00   |
| 1-Nov-25    | #12     | SMARTBIKE TECH PRIVATE LIMITED           | -354,000.00   |

**Note**: With only SVTODATE (no SVFROMDATE), same 12 bills returned. SVFROMDATE is irrelevant for this report.

---

### 5. Bills Payable

**Our payload**: Same as Bills Receivable but ID="Bills Payable".

**Result**: 10 lines, **1 bill**. **DATA IS RETURNED!**

**But parser fails** for the same tag-name mismatch reasons as Bills Receivable.

**Actual response**:
```xml
<ENVELOPE>
 <BILLFIXED>
  <BILLDATE>26-Sep-25</BILLDATE>
  <BILLREF>6ZBO7JXW-0003</BILLREF>
  <BILLPARTY>Anthropic, PBC</BILLPARTY>
 </BILLFIXED>
 <BILLCL>2096.86</BILLCL>
 <BILLDUE>26-Sep-25</BILLDUE>
 <BILLOVERDUE>186</BILLOVERDUE>
</ENVELOPE>
```

---

## Summary of Required Fixes

### Fix 1: Sales/Purchase Register -- Switch to TDL Collection
**File**: `tally_bridge/request_builder.py`

Replace `build_sales_register()` and `build_purchase_register()` to use `TYPE=Collection` with TDL
and `$VoucherTypeName = "Sales"` / `$VoucherTypeName = "Purchase"` filter.

Alternatively, add NEW functions that use Collection approach while keeping the register functions
for monthly summary data (which is also useful).

### Fix 2: Day Book -- Switch to TDL Collection
**File**: `tally_bridge/request_builder.py`

Replace `build_day_book()` to use `TYPE=Collection` with `TYPE=Voucher` and optional filter.
Current approach returns only 1 voucher. Collection returns all 221.

### Fix 3: Bills Parser -- Fix tag names
**File**: `tally_bridge/response_parser.py`

In `parse_bills()`:
- `BILLSFIXED` -> `BILLFIXED`
- `BILLNAME` -> `BILLREF`
- `BILLAMOUNT` -> `BILLCL`
- Remove `BILLPENDING` (does not exist; or compute from BILLCL)
- Add `BILLDUE` and `BILLOVERDUE` fields

### Fix 4: Add Narration and ALLLEDGERENTRIES to Collection queries
The Collection NATIVEMETHOD list should include `Narration` and the full voucher structure
so `parse_vouchers()` can extract ledger entries. Current NATIVEMETHODs are minimal.

---

## Working TDL Collection Patterns (Golden Reference)

### Sales Vouchers (19 found)
```xml
<ENVELOPE>
<HEADER>
<VERSION>1</VERSION>
<TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE>
<ID>SalesVchs</ID>
</HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE>01-04-2025</SVFROMDATE>
<SVTODATE>31-03-2026</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<COLLECTION NAME="SalesVchs" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FILTER>SalesOnly</FILTER>
<NATIVEMETHOD>*</NATIVEMETHOD>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="SalesOnly">$VoucherTypeName = "Sales"</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>
```

### Purchase Vouchers (10 found)
Same as above but filter: `$VoucherTypeName = "Purchase"`

### All Vouchers / Day Book (221 found)
Same as above but remove the FILTER line entirely.

### Bills Receivable (12 found) -- WORKS with current request_builder
Request is correct. Only the parser needs fixing.

### Bills Payable (1 found) -- WORKS with current request_builder
Request is correct. Only the parser needs fixing.

---

## Other Approaches Tested

| Approach | Result |
|----------|--------|
| Collection with `$$VchTypeSales` in FILTER (Formulae) | Returned 0 vouchers (invalid TDL formula) |
| Collection with `$$VchTypeSales` in BELONGSTO | Returned ALL 221 vouchers (ignores Sales filter) |
| Collection with `CHILDOF=Sales` | Same as BELONGSTO -- returned ALL 221 vouchers |
| `ID=All Items` with VOUCHERTYPENAME=Sales | Empty response (11 lines, no data) |
| Day Book with VOUCHERTYPENAME=Sales | Still 1 voucher (Payment, not Sales!) |
| Day Book with different date ranges | Always 1 voucher regardless |
