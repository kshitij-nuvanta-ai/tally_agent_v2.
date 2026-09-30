"""Build Tally IMPORTDATA XML payloads for creating/cancelling/deleting vouchers, ledgers, groups.

All functions are pure — no I/O, no side effects. Each returns an XML string.
Tally import date format: YYYYMMDD (different from query format DD-MM-YYYY).

CRITICAL (from live Tally exploration — docs/tally-write-exploration.md):
- NAME.LIST is REQUIRED for all master operations (create AND delete). Without it, Tally crashes.
- Voucher delete/cancel uses TAGNAME="Master ID" + TAGVALUE=LASTVCHID (not VCHKEY).
- PERSISTEDVIEW is required for Sales/Purchase vouchers.
"""
import re
from typing import Literal
from xml.sax.saxutils import escape as xml_escape

_YYYYMMDD_RE = re.compile(r"^\d{8}$")


def _validate_reference_date(reference_date: str | None) -> None:
    """Validate REFERENCEDATE is YYYYMMDD. Tally rejects other formats
    (see LESSONS.md §14)."""
    if reference_date is None:
        return
    if not _YYYYMMDD_RE.match(reference_date):
        raise ValueError(
            f"reference_date must be YYYYMMDD (8 digits); got {reference_date!r}. "
            "Tally rejects other formats — see LESSONS.md §14."
        )


def compute_invoice_gross(items: list[tuple], gst_mode: str) -> float:
    """Compute the party gross (base + GST) for a stock-based invoice.

    Single source of truth for the inventory party leg: the New Ref bill
    allocation amount AND the posted party AMOUNT must both equal this value so
    the voucher balances exactly. Mirrors the builder calc in
    ``build_create_purchase_voucher`` / ``build_create_sales_voucher``: groups
    the taxable base by gst_rate, applies tax, sums.

    items shape: (name, qty, rate, ledger, uom, gst_rate). intra (CGST+SGST) and
    inter (IGST) yield the same gross, so gst_mode is informational only.
    """
    rate_buckets: dict[int, float] = {}
    for _name, qty, rate, _ledger, _uom, gst_rate in items:
        rate_buckets[gst_rate] = rate_buckets.get(gst_rate, 0.0) + (qty * rate)
    base = sum(rate_buckets.values())
    tax = sum(b * r / 100 for r, b in rate_buckets.items() if r > 0)
    return round(base + tax, 2)


def _render_reference_block(reference: str | None, reference_date: str | None) -> str:
    """Render <REFERENCE> + <REFERENCEDATE> elements (each emitted only when
    its arg is provided). Returns leading-newline string ready to inline."""
    parts: list[str] = []
    if reference:
        parts.append(f"<REFERENCE>{_esc(reference)}</REFERENCE>")
    if reference_date:
        parts.append(f"<REFERENCEDATE>{_esc(reference_date)}</REFERENCEDATE>")
    if not parts:
        return ""
    return "\n" + "\n".join(parts)


def _esc(s: str) -> str:
    """Escape XML special characters in user-provided strings.

    Includes attribute escaping (`"` and `'`) so the same helper works
    for both element text and attribute values.
    """
    return xml_escape(s, {'"': "&quot;", "'": "&apos;"})


def _require(value: str, name: str) -> str:
    """Validate that a required string parameter is non-empty after stripping."""
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _render_bill_allocations(allocs: list[dict] | None, party_line_sign: int) -> str:
    """Render BILLALLOCATIONS.LIST blocks for a party LEDGERENTRY.

    Each alloc dict: {"name": str, "type": "New Ref"|"Agst Ref"|"On Account",
                      "amount": float (positive magnitude),
                      "credit_period": Optional[str]}.

    `party_line_sign` is +1 or -1 — the sign of the AMOUNT on the parent
    party LEDGERENTRY. BILLALLOCATIONS.AMOUNT mirrors that sign. The caller
    passes the magnitude (positive) in `amount`; we apply the sign here.

    Returns "" when allocs is None or empty (caller emits nothing).
    """
    if not allocs:
        return ""
    if party_line_sign not in (1, -1):
        raise ValueError(f"party_line_sign must be 1 or -1, got {party_line_sign}")

    blocks: list[str] = []
    for alloc in allocs:
        name = _require(alloc.get("name", ""), "bill_allocation.name")
        btype = alloc.get("type", "")
        if btype not in ("New Ref", "Agst Ref", "On Account"):
            raise ValueError(
                f"bill_allocation.type must be 'New Ref'|'Agst Ref'|'On Account'; got {btype!r}"
            )
        amount = float(alloc.get("amount", 0))
        if amount < 0:
            raise ValueError(
                f"bill_allocation.amount must be a positive magnitude; got {amount}. "
                "Sign is mirrored from the party ledger entry automatically."
            )
        signed = party_line_sign * amount
        credit_period = alloc.get("credit_period")
        cp_xml = (
            f"\n<BILLCREDITPERIOD>{_esc(credit_period)}</BILLCREDITPERIOD>"
            if credit_period else ""
        )
        blocks.append(
            f"""<BILLALLOCATIONS.LIST>
<NAME>{_esc(name)}</NAME>
<BILLTYPE>{_esc(btype)}</BILLTYPE>
<AMOUNT>{signed:.2f}</AMOUNT>{cp_xml}
</BILLALLOCATIONS.LIST>"""
        )
    return "\n" + "\n".join(blocks)


def _wrap_import(report_name: Literal["Vouchers", "All Masters"], company: str, inner_xml: str) -> str:
    """Wrap entity XML in the standard IMPORTDATA envelope."""
    return f"""<ENVELOPE>
<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
<BODY><IMPORTDATA>
<REQUESTDESC>
<REPORTNAME>{report_name}</REPORTNAME>
<STATICVARIABLES><SVCURRENTCOMPANY>{_esc(company)}</SVCURRENTCOMPANY></STATICVARIABLES>
</REQUESTDESC>
<REQUESTDATA>
<TALLYMESSAGE xmlns:UDF="TallyUDF">
{inner_xml}
</TALLYMESSAGE>
</REQUESTDATA>
</IMPORTDATA></BODY></ENVELOPE>"""


def build_create_payment_voucher(
    date: str,
    debit_ledger: str,
    credit_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_allocations: list[dict] | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML to create a Payment voucher in Tally.

    Args:
        date: YYYYMMDD format.
        debit_ledger: Expense ledger name (e.g., "Travel Expenses").
        credit_ledger: Cash/Bank ledger name (e.g., "Cash").
        amount: Total payment amount (positive number).
        narration: Description of the expense.
        company: Tally company name.
        gst_entries: Optional list of {"ledger": str, "amount": float} for GST components.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    gst_total = sum(e["amount"] for e in (gst_entries or []))
    if gst_total < 0 or gst_total > amount:
        raise ValueError(f"invalid gst total {gst_total} for amount {amount}")

    _require(date, "date")
    _require(debit_ledger, "debit_ledger")
    _require(credit_ledger, "credit_ledger")
    _require(narration, "narration")
    _require(company, "company")
    _validate_reference_date(reference_date)

    base_amount = amount - gst_total

    entries = []
    debit_bill_alloc_xml = _render_bill_allocations(bill_allocations, party_line_sign=-1)
    entries.append(
        f"""<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(debit_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-{base_amount:.2f}</AMOUNT>{debit_bill_alloc_xml}
</ALLLEDGERENTRIES.LIST>"""
    )
    for gst in gst_entries or []:
        entries.append(
            f"""<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(gst["ledger"])}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>-{gst["amount"]:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>"""
        )
    entries.append(
        f"""<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(credit_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>"""
    )

    entries_xml = "\n".join(entries)
    ref_xml = _render_reference_block(reference, reference_date)
    voucher_xml = f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
<DATE>{_esc(date)}</DATE>{ref_xml}
<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
<NARRATION>{_esc(narration)}</NARRATION>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{entries_xml}
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def _build_ledger_invoice_voucher(
    vch_type: str,
    date: str,
    party_ledger: str,
    contra_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_ref: str | None = None,
    bill_type: str = "New Ref",
    party_on_debit: bool = False,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML for ledger-only invoice-style vouchers (Purchase / Sales /
    Debit Note / Credit Note).

    Uses ``LEDGERENTRIES.LIST`` + ``Invoice Voucher View`` + ``ISPARTYLEDGER`` —
    the verified shape from Task 0 probes E5/E6 (no inventory entries).

    Sign convention is driven by ``party_on_debit`` — the party leg and the
    contra (GST + returns/purchase/sales ledger) leg always sit on OPPOSITE
    sides. The party leg with ISDEEMEDPOSITIVE=Yes carries a NEGATIVE amount
    (Tally debit); with ISDEEMEDPOSITIVE=No a POSITIVE amount (Tally credit):

      - ``party_on_debit=False`` → party on CREDIT (No / +amount); contra on
        DEBIT (Yes / -amount). Used by Purchase (party = creditor) and Credit
        Note (a sales return that REDUCES the receivable — party = customer on
        credit).
      - ``party_on_debit=True``  → party on DEBIT (Yes / -amount); contra on
        CREDIT (No / +amount). Used by Sales (party = debtor) and Debit Note (a
        purchase return that REDUCES the payable — party = supplier on debit).

    A return (DN/CN) posts in the INVERSE direction of the original invoice so
    that the Agst Ref bill allocation REDUCES the outstanding bill. (Before the
    2026-06-09 fix, DN mirrored Purchase / CN mirrored Sales — that was the bug
    that made returns INCREASE the bill; see logs/manual_test_group_b_live.log.)

    The bill allocation (when ``bill_ref`` is set) mirrors the party-line sign
    via ``_render_bill_allocations``.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    _require(date, "date")
    _require(party_ledger, "party_ledger")
    _require(contra_ledger, "contra_ledger")
    _require(narration, "narration")
    _require(company, "company")
    _validate_reference_date(reference_date)

    gst_total = sum(e["amount"] for e in (gst_entries or []))
    if gst_total < 0 or gst_total > amount:
        raise ValueError(f"invalid gst total {gst_total} for amount {amount}")
    base_amount = amount - gst_total

    if party_on_debit:
        party_sign = -1  # party debit side → -amount
        party_deemed = "Yes"
        contra_sign = 1  # GST + contra ledger on credit side → +amount
        contra_deemed = "No"
    else:
        party_sign = 1  # party credit side → +amount
        party_deemed = "No"
        contra_sign = -1  # GST + contra ledger on debit side → -amount
        contra_deemed = "Yes"

    bill_allocs = (
        [{"name": bill_ref, "type": bill_type, "amount": amount}] if bill_ref else None
    )
    bill_xml = _render_bill_allocations(bill_allocs, party_line_sign=party_sign)

    entries: list[str] = []
    entries.append(
        f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(party_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>{party_deemed}</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>{party_sign * amount:.2f}</AMOUNT>{bill_xml}
</LEDGERENTRIES.LIST>"""
    )

    for gst in gst_entries or []:
        entries.append(
            f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(gst["ledger"])}</LEDGERNAME>
<ISDEEMEDPOSITIVE>{contra_deemed}</ISDEEMEDPOSITIVE>
<AMOUNT>{contra_sign * gst["amount"]:.2f}</AMOUNT>
</LEDGERENTRIES.LIST>"""
        )

    entries.append(
        f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(contra_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>{contra_deemed}</ISDEEMEDPOSITIVE>
<AMOUNT>{contra_sign * base_amount:.2f}</AMOUNT>
</LEDGERENTRIES.LIST>"""
    )

    entries_xml = "\n".join(entries)
    ref_xml = _render_reference_block(reference, reference_date)
    voucher_xml = f"""<VOUCHER VCHTYPE="{_esc(vch_type)}" ACTION="Create">
<DATE>{_esc(date)}</DATE>{ref_xml}
<VOUCHERTYPENAME>{_esc(vch_type)}</VOUCHERTYPENAME>
<NARRATION>{_esc(narration)}</NARRATION>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
{entries_xml}
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def build_create_debit_note(
    date: str,
    party_ledger: str,
    purchase_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_ref: str | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML to create a Debit Note in Tally (purchase return).

    A Debit Note reduces a payable — it DEBITS the supplier (party) and CREDITS
    the purchase-returns/contra ledger (plus reversed Input GST on credit),
    referencing the original bill via ``Agst Ref`` so the allocation reduces the
    outstanding payable. This is the INVERSE of a Purchase (party_on_debit=True).
    Corrected 2026-06-09 (logs/manual_test_group_b_live.log) — the prior
    "mirrors Purchase" polarity wrongly INCREASED the payable.
    """
    return _build_ledger_invoice_voucher(
        vch_type="Debit Note", date=date, party_ledger=party_ledger,
        contra_ledger=purchase_ledger, amount=amount, narration=narration,
        company=company, gst_entries=gst_entries, bill_ref=bill_ref,
        bill_type="Agst Ref", party_on_debit=True,
        reference=reference, reference_date=reference_date,
    )


def build_create_credit_note(
    date: str,
    party_ledger: str,
    sales_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_ref: str | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML to create a Credit Note in Tally (sales return).

    A Credit Note reduces a receivable — it CREDITS the customer (party) and
    DEBITS the sales-returns/contra ledger (plus reversed Output GST on debit),
    referencing the original bill via ``Agst Ref`` so the allocation reduces the
    outstanding receivable. This is the INVERSE of a Sales (party_on_debit=False).
    Corrected 2026-06-09 (logs/manual_test_group_b_live.log) — the prior
    "mirrors Sales" polarity wrongly INCREASED the receivable.
    """
    return _build_ledger_invoice_voucher(
        vch_type="Credit Note", date=date, party_ledger=party_ledger,
        contra_ledger=sales_ledger, amount=amount, narration=narration,
        company=company, gst_entries=gst_entries, bill_ref=bill_ref,
        bill_type="Agst Ref", party_on_debit=False,
        reference=reference, reference_date=reference_date,
    )


def build_create_purchase_voucher_ledger(
    date: str,
    party_ledger: str,
    purchase_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_ref: str | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML for a ledger-only Purchase voucher (party + contra + GST).

    Group B document-driven path: unlike the stock-based
    ``build_create_purchase_voucher`` (seeder), this posts the party (Sundry
    Creditors) credit and the purchase/expense ledger + Input GST debits using
    the verified DN/CN ledger-invoice shape with a fresh ``New Ref`` bill.
    """
    return _build_ledger_invoice_voucher(
        vch_type="Purchase", date=date, party_ledger=party_ledger,
        contra_ledger=purchase_ledger, amount=amount, narration=narration,
        company=company, gst_entries=gst_entries, bill_ref=bill_ref,
        bill_type="New Ref", party_on_debit=False,
        reference=reference, reference_date=reference_date,
    )


def build_create_sales_voucher_ledger(
    date: str,
    party_ledger: str,
    sales_ledger: str,
    amount: float,
    narration: str,
    company: str,
    gst_entries: list[dict] | None = None,
    bill_ref: str | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML for a ledger-only Sales voucher (party + contra + GST).

    Group B document-driven path: posts the party (Sundry Debtors) debit and the
    sales ledger + Output GST credits using the ledger-invoice shape with a fresh
    ``New Ref`` bill (mirrors Credit Note polarity).
    """
    return _build_ledger_invoice_voucher(
        vch_type="Sales", date=date, party_ledger=party_ledger,
        contra_ledger=sales_ledger, amount=amount, narration=narration,
        company=company, gst_entries=gst_entries, bill_ref=bill_ref,
        bill_type="New Ref", party_on_debit=True,
        reference=reference, reference_date=reference_date,
    )


def build_create_group(name: str, parent: str, company: str) -> str:
    """Build XML to create an account group in Tally."""
    _require(name, "name")
    _require(parent, "parent")
    _require(company, "company")

    group_xml = f"""<GROUP NAME="{_esc(name)}" ACTION="Create">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
<PARENT>{_esc(parent)}</PARENT>
</GROUP>"""
    return _wrap_import("All Masters", company, group_xml)


def build_delete_voucher(
    voucher_type: str,
    master_id: str,
    date: str,
    company: str,
) -> str:
    """Build XML to delete a voucher from Tally.

    Uses TAGNAME="Master ID" + TAGVALUE (the LASTVCHID from creation response).
    """
    _require(voucher_type, "voucher_type")
    _require(master_id, "master_id")
    _require(date, "date")
    _require(company, "company")

    voucher_xml = f"""<VOUCHER DATE="{_esc(date)}" TAGNAME="Master ID" TAGVALUE="{_esc(master_id)}" VCHTYPE="{_esc(voucher_type)}" ACTION="Delete">
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def build_cancel_voucher(
    voucher_type: str,
    master_id: str,
    date: str,
    company: str,
    narration: str = "",
) -> str:
    """Build XML to cancel a voucher in Tally (preserves audit trail).

    Cancel returns ALTERED=1 from Tally (cancel is internally an alter).
    Preferred over delete for undo operations.
    """
    _require(voucher_type, "voucher_type")
    _require(master_id, "master_id")
    _require(date, "date")
    _require(company, "company")

    narration_xml = f"\n<NARRATION>{_esc(narration)}</NARRATION>" if narration else ""
    voucher_xml = f"""<VOUCHER DATE="{_esc(date)}" TAGNAME="Master ID" TAGVALUE="{_esc(master_id)}" VCHTYPE="{_esc(voucher_type)}" ACTION="Cancel">{narration_xml}
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def build_delete_ledger(name: str, company: str) -> str:
    """Build XML to delete a ledger master from Tally.

    CRITICAL: NAME.LIST is required — without it Tally crashes with memory violation.
    """
    _require(name, "name")
    _require(company, "company")

    ledger_xml = f"""<LEDGER NAME="{_esc(name)}" ACTION="Delete">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
</LEDGER>"""
    return _wrap_import("All Masters", company, ledger_xml)


def build_delete_group(name: str, company: str) -> str:
    """Build XML to delete an account group from Tally.

    CRITICAL: NAME.LIST is required — without it Tally crashes with memory violation.
    """
    _require(name, "name")
    _require(company, "company")

    group_xml = f"""<GROUP NAME="{_esc(name)}" ACTION="Delete">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
</GROUP>"""
    return _wrap_import("All Masters", company, group_xml)


def build_create_unit(name: str, formal_name: str, company: str) -> str:
    """Build XML to create a unit (UOM) master in Tally.

    NOTE: UNIT does NOT take NAME.LIST — that triggers "BAD UNIT NAME".
    Stick to short ASCII names (Nos, Pcs). See docs/tally-write-exploration-v4.md Op 1.
    """
    _require(name, "name")
    _require(formal_name, "formal_name")
    _require(company, "company")

    unit_xml = f"""<UNIT ACTION="Create">
<NAME>{_esc(name)}</NAME>
<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
<FORMALNAME>{_esc(formal_name)}</FORMALNAME>
</UNIT>"""
    return _wrap_import("All Masters", company, unit_xml)


def build_create_stock_group(name: str, parent: str, company: str) -> str:
    """Build XML to create a stock group. Empty parent = top-level (under Primary)."""
    _require(name, "name")
    _require(company, "company")

    parent_xml = f"<PARENT>{_esc(parent)}</PARENT>" if parent else "<PARENT/>"
    sg_xml = f"""<STOCKGROUP NAME="{_esc(name)}" ACTION="Create">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
{parent_xml}
<ISADDABLE>No</ISADDABLE>
</STOCKGROUP>"""
    return _wrap_import("All Masters", company, sg_xml)


def build_create_stock_item(
    name: str,
    group: str,
    uom: str,
    opening_qty: float,
    opening_rate: float,
    hsn_code: str,
    gst_rate: int,
    company: str,
    applicable_from: str = "20250401",
) -> str:
    """Build XML to create a stock item, GST-applicable only when an HSN is given.

    Splits gst_rate evenly across CGST/SGST (intra-state) and uses full rate for IGST
    (inter-state). E.g. 18% → 9 CGST + 9 SGST + 18 IGST.

    HSN-driven GST gating (live-caught bug, feat/invoice-inventory-items):
    Vision frequently extracts no HSN code. TallyPrime treats a stock item that is
    GST-applicable WITHOUT an HSN/SAC as invalid and pops a blocking
    "HSN/SAC required" modal, which fails the create and locks Tally's API.
    GST on the voucher is posted via explicit tax ledger lines regardless of the
    item's master GST setup, so the item does NOT need master-level GST/HSN.

    - hsn_code non-empty: GSTAPPLICABLE=Applicable + HSN elements + GSTDETAILS rates
      (matches the seeder's proven items).
    - hsn_code empty: GSTAPPLICABLE=Not Applicable, and the entire HSN + GSTDETAILS
      blocks are omitted — a plain name+unit+group+opening stock item.

    See docs/tally-write-exploration-v4.md Op 3.
    """
    _require(name, "name")
    _require(group, "group")
    _require(uom, "uom")
    _require(company, "company")
    if opening_qty < 0 or opening_rate < 0:
        raise ValueError("opening_qty and opening_rate must be non-negative")
    if gst_rate not in (0, 5, 12, 18, 28):
        raise ValueError(f"gst_rate must be one of (0, 5, 12, 18, 28); got {gst_rate}")

    half = gst_rate / 2
    half_str = f"{half:g}"  # 9 not 9.0; 2.5 stays 2.5
    igst_str = f"{gst_rate:g}"
    opening_value = opening_qty * opening_rate

    hsn_str = (hsn_code or "").strip()
    if hsn_str:
        gst_block = f"""<GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
<GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
<HSNCODE>{_esc(hsn_str)}</HSNCODE>
<HSN>{_esc(hsn_str)}</HSN>
<HSNDETAILS.LIST>
<APPLICABLEFROM>{applicable_from}</APPLICABLEFROM>
<HSNCODE>{_esc(hsn_str)}</HSNCODE>
<HSN>{_esc(hsn_str)}</HSN>
</HSNDETAILS.LIST>
<GSTDETAILS.LIST>
<APPLICABLEFROM>{applicable_from}</APPLICABLEFROM>
<TAXABILITY>Taxable</TAXABILITY>
<IGSTRATE>{igst_str}</IGSTRATE>
<CGSTRATE>{half_str}</CGSTRATE>
<SGSTRATE>{half_str}</SGSTRATE>
<STATEWISEDETAILS.LIST>
<STATENAME>Any</STATENAME>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>{half_str}</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>{half_str}</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>{igst_str}</GSTRATE></RATEDETAILS.LIST>
<RATEDETAILS.LIST><GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>0</GSTRATE></RATEDETAILS.LIST>
</STATEWISEDETAILS.LIST>
</GSTDETAILS.LIST>
"""
    else:
        # No HSN → master-level GST off; voucher tax lines carry the GST.
        gst_block = "<GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>\n"

    si_xml = f"""<STOCKITEM NAME="{_esc(name)}" ACTION="Create">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
<PARENT>{_esc(group)}</PARENT>
<BASEUNITS>{_esc(uom)}</BASEUNITS>
{gst_block}<OPENINGBALANCE>{opening_qty:g} {_esc(uom)}</OPENINGBALANCE>
<OPENINGRATE>{opening_rate:.2f}/{_esc(uom)}</OPENINGRATE>
<OPENINGVALUE>{opening_value:.2f}</OPENINGVALUE>
</STOCKITEM>"""
    return _wrap_import("All Masters", company, si_xml)



def build_create_ledger(
    name: str,
    parent: str,
    company: str,
    gstin: str | None = None,
    state: str | None = None,
    gst_reg_type: str | None = None,
    opening_balance: float | None = None,
    is_billwise: bool = False,
) -> str:
    """Build XML to create a ledger master in Tally.

    CRITICAL: NAME.LIST is required — without it Tally crashes with memory violation.

    Args:
        opening_balance: Positive value; sign inferred from parent group nature
            (Capital → credit; Cash-in-Hand → debit). Pass None or 0 to omit.
        is_billwise: Set True for Sundry Debtors/Creditors (otherwise voucher
            bill-allocation fails).
    """
    _require(name, "name")
    _require(parent, "parent")
    _require(company, "company")

    extras = []
    if gstin:
        extras.append(f"<PARTYGSTIN>{_esc(gstin)}</PARTYGSTIN>")
    if state:
        extras.append(f"<LEDSTATENAME>{_esc(state)}</LEDSTATENAME>")
    if gst_reg_type:
        extras.append(f"<GSTREGISTRATIONTYPE>{_esc(gst_reg_type)}</GSTREGISTRATIONTYPE>")
    if opening_balance is not None and opening_balance != 0:
        extras.append(f"<OPENINGBALANCE>{abs(float(opening_balance)):.2f}</OPENINGBALANCE>")
    if is_billwise:
        extras.append("<ISBILLWISEON>Yes</ISBILLWISEON>")
    extras_xml = ("\n" + "\n".join(extras)) if extras else ""

    ledger_xml = f"""<LEDGER NAME="{_esc(name)}" ACTION="Create">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
<PARENT>{_esc(parent)}</PARENT>{extras_xml}
</LEDGER>"""
    return _wrap_import("All Masters", company, ledger_xml)


def build_create_gst_ledger(name: str, duty_head: str, company: str) -> str:
    """Build XML to create a GST tax ledger under Duties & Taxes.

    duty_head: one of "Central Tax", "State Tax", "Integrated Tax".
    Same envelope serves both Output and Input ledgers — Tally infers direction
    from voucher usage. See docs/tally-write-exploration-v4.md Op 4.
    """
    _require(name, "name")
    _require(company, "company")
    if duty_head not in ("Central Tax", "State Tax", "Integrated Tax"):
        raise ValueError(f"invalid duty_head: {duty_head}")

    led_xml = f"""<LEDGER NAME="{_esc(name)}" ACTION="Create">
<NAME.LIST><NAME>{_esc(name)}</NAME></NAME.LIST>
<PARENT>Duties &amp; Taxes</PARENT>
<TAXTYPE>GST</TAXTYPE>
<GSTDUTYHEAD>{_esc(duty_head)}</GSTDUTYHEAD>
<RATEOFTAXCALCULATION>0</RATEOFTAXCALCULATION>
<ROUNDINGMETHOD/>
<ROUNDINGLIMIT>0</ROUNDINGLIMIT>
<ISBILLWISEON>No</ISBILLWISEON>
<AFFECTSSTOCK>No</AFFECTSSTOCK>
<ISCOSTCENTRESON>No</ISCOSTCENTRESON>
</LEDGER>"""
    return _wrap_import("All Masters", company, led_xml)


def build_create_sales_voucher(
    date: str,
    voucher_number: str,
    party: str,
    items: list[tuple],
    narration: str,
    gst_mode: str,  # "intra" or "inter"
    company: str,
    bill_allocations: list[dict] | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML to create a Sales voucher with stock + GST.

    items: list of (item_name, qty, rate, sales_ledger, uom, gst_rate) tuples.
    gst_mode: "intra" → CGST+SGST split; "inter" → IGST only.

    Sign convention (v4 Op 6): party Yes/-tot, GST No/+tax, inv/alloc No/+goods.
    Mixed-rate invoices are supported — per-rate GST totals are summed and emitted
    as one CGST+SGST (or IGST) line per rate bucket.
    """
    _require(date, "date")
    _require(voucher_number, "voucher_number")
    _require(party, "party")
    _require(narration, "narration")
    _require(company, "company")
    if gst_mode not in ("intra", "inter"):
        raise ValueError(f"gst_mode must be 'intra' or 'inter'; got {gst_mode!r}")
    if not items:
        raise ValueError("items must be non-empty")
    _validate_reference_date(reference_date)

    # Group line totals by GST rate (taxable_base per rate)
    rate_buckets: dict[int, float] = {}
    for _name, qty, rate, _ledger, _uom, gst_rate in items:
        rate_buckets[gst_rate] = rate_buckets.get(gst_rate, 0.0) + (qty * rate)

    base_total = sum(rate_buckets.values())
    tax_lines: list[str] = []
    tax_total = 0.0
    for rate, base in sorted(rate_buckets.items()):
        if rate == 0:
            continue
        if gst_mode == "intra":
            half = base * (rate / 2) / 100
            tax_total += 2 * half
            tax_lines.append(_sales_tax_line("CGST Output", half))
            tax_lines.append(_sales_tax_line("SGST Output", half))
        else:
            full = base * rate / 100
            tax_total += full
            tax_lines.append(_sales_tax_line("IGST Output", full))

    party_total = base_total + tax_total
    bill_alloc_xml = _render_bill_allocations(bill_allocations, party_line_sign=-1)
    party_block = f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(party)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>{-party_total:.2f}</AMOUNT>{bill_alloc_xml}
</LEDGERENTRIES.LIST>"""

    inventory_blocks: list[str] = []
    for item_name, qty, rate, sales_ledger, uom, _gst_rate in items:
        amount = qty * rate
        inventory_blocks.append(f"""<ALLINVENTORYENTRIES.LIST>
<STOCKITEMNAME>{_esc(item_name)}</STOCKITEMNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<RATE>{rate:.2f}/{_esc(uom)}</RATE>
<AMOUNT>{amount:.2f}</AMOUNT>
<ACTUALQTY>{qty:g} {_esc(uom)}</ACTUALQTY>
<BILLEDQTY>{qty:g} {_esc(uom)}</BILLEDQTY>
<ACCOUNTINGALLOCATIONS.LIST>
<LEDGERNAME>{_esc(sales_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</ACCOUNTINGALLOCATIONS.LIST>
</ALLINVENTORYENTRIES.LIST>""")

    ref_xml = _render_reference_block(reference, reference_date)
    voucher_xml = f"""<VOUCHER VCHTYPE="Sales" ACTION="Create">
<DATE>{_esc(date)}</DATE>{ref_xml}
<NARRATION>{_esc(narration)}</NARRATION>
<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
<VOUCHERNUMBER>{_esc(voucher_number)}</VOUCHERNUMBER>
<PARTYLEDGERNAME>{_esc(party)}</PARTYLEDGERNAME>
<PARTYNAME>{_esc(party)}</PARTYNAME>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<EFFECTIVEDATE>{_esc(date)}</EFFECTIVEDATE>
{party_block}
{chr(10).join(tax_lines)}
{chr(10).join(inventory_blocks)}
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def _sales_tax_line(ledger: str, amount: float) -> str:
    return f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</LEDGERENTRIES.LIST>"""


def build_create_purchase_voucher(
    date: str,
    voucher_number: str,
    party: str,
    items: list[tuple],
    narration: str,
    gst_mode: str,
    company: str,
    bill_allocations: list[dict] | None = None,
    reference: str | None = None,
    reference_date: str | None = None,
) -> str:
    """Build XML to create a Purchase voucher with stock + GST.

    items: list of (item_name, qty, rate, purchase_ledger, uom, gst_rate) tuples.

    INVERSE sign convention from sales (v4 Op 7):
      party No/+tot, GST Yes/-tax, inv/alloc Yes/-goods.
    """
    _require(date, "date")
    _require(voucher_number, "voucher_number")
    _require(party, "party")
    _require(narration, "narration")
    _require(company, "company")
    if gst_mode not in ("intra", "inter"):
        raise ValueError(f"gst_mode must be 'intra' or 'inter'; got {gst_mode!r}")
    if not items:
        raise ValueError("items must be non-empty")
    _validate_reference_date(reference_date)

    rate_buckets: dict[int, float] = {}
    for _name, qty, rate, _ledger, _uom, gst_rate in items:
        rate_buckets[gst_rate] = rate_buckets.get(gst_rate, 0.0) + (qty * rate)

    base_total = sum(rate_buckets.values())
    tax_lines: list[str] = []
    tax_total = 0.0
    for rate, base in sorted(rate_buckets.items()):
        if rate == 0:
            continue
        if gst_mode == "intra":
            half = base * (rate / 2) / 100
            tax_total += 2 * half
            tax_lines.append(_purchase_tax_line("CGST Input", half))
            tax_lines.append(_purchase_tax_line("SGST Input", half))
        else:
            full = base * rate / 100
            tax_total += full
            tax_lines.append(_purchase_tax_line("IGST Input", full))

    party_total = base_total + tax_total
    bill_alloc_xml = _render_bill_allocations(bill_allocations, party_line_sign=1)
    party_block = f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(party)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<ISPARTYLEDGER>Yes</ISPARTYLEDGER>
<AMOUNT>{party_total:.2f}</AMOUNT>{bill_alloc_xml}
</LEDGERENTRIES.LIST>"""

    inventory_blocks: list[str] = []
    for item_name, qty, rate, purchase_ledger, uom, _gst_rate in items:
        amount = qty * rate
        inventory_blocks.append(f"""<ALLINVENTORYENTRIES.LIST>
<STOCKITEMNAME>{_esc(item_name)}</STOCKITEMNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<RATE>{rate:.2f}/{_esc(uom)}</RATE>
<AMOUNT>{-amount:.2f}</AMOUNT>
<ACTUALQTY>{qty:g} {_esc(uom)}</ACTUALQTY>
<BILLEDQTY>{qty:g} {_esc(uom)}</BILLEDQTY>
<ACCOUNTINGALLOCATIONS.LIST>
<LEDGERNAME>{_esc(purchase_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>{-amount:.2f}</AMOUNT>
</ACCOUNTINGALLOCATIONS.LIST>
</ALLINVENTORYENTRIES.LIST>""")

    ref_xml = _render_reference_block(reference, reference_date)
    voucher_xml = f"""<VOUCHER VCHTYPE="Purchase" ACTION="Create">
<DATE>{_esc(date)}</DATE>{ref_xml}
<NARRATION>{_esc(narration)}</NARRATION>
<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
<VOUCHERNUMBER>{_esc(voucher_number)}</VOUCHERNUMBER>
<PARTYLEDGERNAME>{_esc(party)}</PARTYLEDGERNAME>
<PARTYNAME>{_esc(party)}</PARTYNAME>
<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
<ISINVOICE>Yes</ISINVOICE>
<EFFECTIVEDATE>{_esc(date)}</EFFECTIVEDATE>
{party_block}
{chr(10).join(tax_lines)}
{chr(10).join(inventory_blocks)}
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def _purchase_tax_line(ledger: str, amount: float) -> str:
    return f"""<LEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>{-amount:.2f}</AMOUNT>
</LEDGERENTRIES.LIST>"""


def build_create_receipt_voucher(
    date: str,
    voucher_number: str,
    party: str,
    bank_ledger: str,
    amount: float,
    narration: str,
    company: str,
    bill_allocations: list[dict] | None = None,
) -> str:
    """Build XML to create a Receipt voucher (party → bank).

    Bank debit (Yes/-amount), party credit (No/+amount). See v4 Op 8.

    `bill_allocations` (optional): renders BILLALLOCATIONS.LIST inside the
    party LEDGERENTRY. Sign is mirrored (+ on receipt party line).
    """
    _require(date, "date")
    _require(voucher_number, "voucher_number")
    _require(party, "party")
    _require(bank_ledger, "bank_ledger")
    _require(narration, "narration")
    _require(company, "company")
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    bill_alloc_xml = _render_bill_allocations(bill_allocations, party_line_sign=1)
    voucher_xml = f"""<VOUCHER VCHTYPE="Receipt" ACTION="Create">
<DATE>{_esc(date)}</DATE>
<NARRATION>{_esc(narration)}</NARRATION>
<VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
<VOUCHERNUMBER>{_esc(voucher_number)}</VOUCHERNUMBER>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(bank_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>{-amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(party)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>{bill_alloc_xml}
</ALLLEDGERENTRIES.LIST>
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


def build_create_journal_voucher(
    date: str,
    voucher_number: str,
    debit_ledger: str,
    credit_ledger: str,
    amount: float,
    narration: str,
    company: str,
) -> str:
    """Build XML to create a Journal voucher.

    Debit (Yes/-amount), credit (No/+amount). See v4 Op 9.
    """
    _require(date, "date")
    _require(voucher_number, "voucher_number")
    _require(debit_ledger, "debit_ledger")
    _require(credit_ledger, "credit_ledger")
    _require(narration, "narration")
    _require(company, "company")
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    voucher_xml = f"""<VOUCHER VCHTYPE="Journal" ACTION="Create">
<DATE>{_esc(date)}</DATE>
<NARRATION>{_esc(narration)}</NARRATION>
<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
<VOUCHERNUMBER>{_esc(voucher_number)}</VOUCHERNUMBER>
<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(debit_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
<AMOUNT>{-amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST>
<LEDGERNAME>{_esc(credit_ledger)}</LEDGERNAME>
<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
<AMOUNT>{amount:.2f}</AMOUNT>
</ALLLEDGERENTRIES.LIST>
</VOUCHER>"""
    return _wrap_import("Vouchers", company, voucher_xml)


# Public names for the two helpers other packages build on (probes/setup). The private names stay: every builder
# above and the scripts use them.
esc = _esc
wrap_import = _wrap_import
