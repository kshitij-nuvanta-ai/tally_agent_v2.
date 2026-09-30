"""REAL-Vision end-to-end PDF flow test against a LIVE Tally — Purchase / Sales / DN / CN.

Proves the FULL production write pipeline for FOUR document types, end to end, with NO
mocks, driving the same code the ``POST /chat`` file-upload endpoint runs:

    a realistic invoice PDF (committed under tests/fixtures/pdf_flows/, regenerable via PIL)
      → REAL Claude Vision extraction + classification + ledger mapping + voucher build
         (``Orchestrator.process_file_upload`` — the exact production entry point)
      → assert doc_type / party / amount / GST structurally (Vision ±1 tolerance)
      → WRITE to LIVE Tally via the SAME ``TallyWriter`` method ``backend/api/chat.py``
         voucher_action dispatches to for that voucher_type
      → read bills_payable / bills_receivable + GST ledger closing balances back and
         ASSERT the move + direction (DN reduces payable, CN reduces receivable)
      → DELETE every voucher (delete-by-Master-ID) in ``finally`` and assert books restored.

The four documents form two settlement pairs so DN/CN can settle the bills we raise:

    purchase_invoice.pdf  (PINV-FLOW-01, ₹11,800)  ──┐
    debit_note.pdf  (Agst PINV-FLOW-01, ₹2,360)   ──┘  payable: +11,800 then −2,360
    sales_invoice.pdf  (SINV-FLOW-01, ₹23,600)    ──┐
    credit_note.pdf  (Agst SINV-FLOW-01, ₹1,180)  ──┘  receivable: +23,600 then −1,180

Vision-ref tolerance: we always WRITE using the KNOWN printed bill ref (PINV-FLOW-01 etc.)
so the DN/CN settle the bills we created even if Vision misreads the printed invoice number.
A ref mismatch is logged (not failed) so a minor extraction miss never breaks the direction
check. doc_type + party + amount + GST direction ARE asserted firmly.

Grounded in:
  - backend/agents/orchestrator.py            (process_file_upload — Vision + routing + review entry)
  - backend/services/document_parser.py       (doc_type / gst extraction)
  - backend/api/chat.py                        (voucher_action dispatch — exact call shapes mirrored)
  - tally_bridge/writer.py             (create_purchase/sales_voucher_ledger, create_debit/credit_note)
  - tally_bridge/queries/reports.py    (bills_payable / bills_receivable)
  - tally_bridge/request_builder.py    (build_list_ledgers — GST ledger closing balances)
  - tally_bridge/response_parser.py    (parse_ledger_list — closing_balance per ledger)
  - scripts/manual_test_live_vision.py         (Vision/upload + GST-leg patterns)
  - scripts/manual_test_group_b_live.py        (client/arg/cleanup/direction patterns, FY dates, results table)
  - LESSONS.md §15                             (write safety: read-back, DD-MMM-YYYY delete date)

⚠️  WARNING — this makes FOUR REAL Claude Vision API calls AND WRITES to a live Tally.
    It targets "Bharat Traders Private Limited", uses a "_PF" narration prefix and small
    amounts, and deletes everything it creates in a ``finally`` block.

Usage (do NOT run casually — costs 4 Vision calls + writes to live Tally):
    TALLY_WRITE_ENABLED=true ANTHROPIC_API_KEY=<key> PYTHONPATH=. \
        uv run --with pillow python scripts/manual_test_pdf_flows_live.py --host localhost --port 9000 \
        2>&1 | tee docs/manual-test-pdf-flows-live.log

Regenerate the committed PDFs only (no Vision, no Tally, no write):
    PYTHONPATH=. uv run --with pillow python scripts/manual_test_pdf_flows_live.py --generate-only

Import check (safe — no Vision, no Tally, no run):
    PYTHONPATH=. uv run --with pillow python -c "import scripts.manual_test_pdf_flows_live"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from backend.agents.context import SessionContext
from backend.agents.orchestrator import Orchestrator
from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.models import OutstandingBill
from tally_bridge.queries.reports import bills_payable, bills_receivable
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import parse_import_response, parse_ledger_list
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
NPFX = "_PF"  # narration prefix so leftovers are mechanically identifiable

# Committed fixture location.
PDF_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pdf_flows"

# Parties — these match seed-company ledgers so the party resolves on write.
SUPPLIER = "Bharat Paper Supplies"        # Sundry Creditor
CUSTOMER = "Apex Technologies Pvt Ltd"    # Sundry Debtor
GSTIN = "27AABCB1234C1Z5"

# Known bill refs. We WRITE with these regardless of what Vision reads, so DN/CN settle.
PINV_REF = "PINV-FLOW-01"
SINV_REF = "SINV-FLOW-01"
DN_REF = "DN-FLOW-01"
CN_REF = "CN-FLOW-01"

# Tally import dates are YYYYMMDD; delete envelopes want DD-MMM-YYYY (LESSONS §15 / v4).
INV_DATE = "20250615"   # 15-Jun-2025 (purchase + sales)
NOTE_DATE = "20250616"  # 16-Jun-2025 (debit note + credit note)
INV_DATE_DISPLAY = datetime.strptime(INV_DATE, "%Y%m%d").strftime("%d-%b-%Y")    # 15-Jun-2025
NOTE_DATE_DISPLAY = datetime.strptime(NOTE_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 16-Jun-2025
# bills_payable / bills_receivable take a DD-MM-YYYY "as on" date.
AS_ON = "31-03-2026"

# Expected amounts (subtotal / CGST 9% / SGST 9% / total).
PINV = {"subtotal": 10000.0, "cgst": 900.0, "sgst": 900.0, "total": 11800.0}
SINV = {"subtotal": 20000.0, "cgst": 1800.0, "sgst": 1800.0, "total": 23600.0}
DNOTE = {"subtotal": 2000.0, "cgst": 180.0, "sgst": 180.0, "total": 2360.0}
CNOTE = {"subtotal": 1000.0, "cgst": 90.0, "sgst": 90.0, "total": 1180.0}

TOLERANCE = 1.0  # rupees — accept ±1 rounding noise on Vision + read-backs


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking (mirrors manual_test_group_b_live.py)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    doc: str
    classification: str
    write: str
    checks: str
    passed: bool


RESULTS: list[StepResult] = []
MASTER_IDS: list[tuple[str, str]] = []  # (voucher_type, master_id) for cleanup


def record(doc: str, classification: str, write: str, checks: str, passed: bool) -> None:
    RESULTS.append(StepResult(doc, classification, write, checks, passed))
    print(f"\n  >>> {'PASS' if passed else 'FAIL'}: {doc}")
    print(f"      classification: {classification}")
    print(f"      write:          {write}")
    print(f"      checks:         {checks}")


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _short(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


# ─────────────────────────────────────────────────────────────────────────────
# PDF generation — legible black-on-white invoice rendered with PIL, saved as PDF.
# Idempotent: only writes a PDF if it does not already exist (so committed PDFs win).
# ─────────────────────────────────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    from PIL import ImageFont as _IF
    return _IF.load_default()


def _render_invoice_pdf(
    path: Path,
    *,
    doc_title: str,
    party_label: str,
    party_name: str,
    bill_to: str,
    invoice_no: str,
    date_display: str,
    line_items: list[tuple[str, str, str]],   # (description, qty, amount)
    subtotal: float,
    cgst: float,
    sgst: float,
    total: float,
    footer_lines: list[str],
    against_ref: str | None = None,
) -> None:
    """Draw one invoice/note image (1000x1400, black on white) and save as PDF."""
    from PIL import Image, ImageDraw

    W, H = 1000, 1400
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    f_title = _font(56)
    f_h = _font(34)
    f_b = _font(28)

    def text(x: int, y: int, s: str, font, fill="black") -> None:
        draw.text((x, y), s, font=font, fill=fill)

    margin = 60
    y = 60
    text(margin, y, doc_title, f_title); y += 90
    text(margin, y, party_name, f_h); y += 50
    text(margin, y, f"{party_label} — Maharashtra", f_b); y += 40
    text(margin, y, f"GSTIN: {GSTIN}", f_b); y += 70

    text(margin, y, f"Invoice No: {invoice_no}", f_b)
    text(margin + 500, y, f"Date: {date_display}", f_b); y += 50
    text(margin, y, bill_to, f_b); y += 50
    if against_ref:
        text(margin, y, f"Against Original Invoice: {against_ref}", f_b); y += 50
    y += 20

    # Table header rule.
    draw.line([(margin, y), (W - margin, y)], fill="black", width=2); y += 16
    text(margin, y, "Description", f_b)
    text(margin + 520, y, "Qty", f_b)
    text(margin + 700, y, "Amount (Rs)", f_b); y += 46
    draw.line([(margin, y), (W - margin, y)], fill="black", width=1); y += 20

    for desc, qty, amt in line_items:
        text(margin, y, desc, f_b)
        text(margin + 520, y, qty, f_b)
        text(margin + 700, y, amt, f_b); y += 44
    y += 16
    draw.line([(margin, y), (W - margin, y)], fill="black", width=1); y += 24

    def total_row(label: str, amount: float, font) -> None:
        nonlocal y
        text(margin + 400, y, label, font)
        text(margin + 700, y, f"{amount:,.2f}", font); y += 46

    total_row("Subtotal:", subtotal, f_b)
    total_row("CGST @ 9%:", cgst, f_b)
    total_row("SGST @ 9%:", sgst, f_b)
    draw.line([(margin + 380, y), (W - margin, y)], fill="black", width=2); y += 16
    total_row("TOTAL:", total, f_h)

    y += 70
    for line in footer_lines:
        text(margin, y, line, f_b); y += 40

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PDF", resolution=150.0)
    print(f"  [pdf] wrote {path.name} ({path.stat().st_size:,} bytes)")


def generate_pdfs(force: bool = False) -> dict[str, Path]:
    """Generate the four flow-test PDFs into PDF_DIR if missing (or all if force).

    Returns a {key: path} map. Idempotent so the committed PDFs are reused.
    """
    paths = {
        "purchase": PDF_DIR / "purchase_invoice.pdf",
        "sales": PDF_DIR / "sales_invoice.pdf",
        "debit_note": PDF_DIR / "debit_note.pdf",
        "credit_note": PDF_DIR / "credit_note.pdf",
    }

    if force or not paths["purchase"].exists():
        _render_invoice_pdf(
            paths["purchase"],
            doc_title="TAX INVOICE",
            party_label="GST Registered Supplier",
            party_name=SUPPLIER,
            bill_to="Bill To: Bharat Traders Private Limited",
            invoice_no=PINV_REF,
            date_display="15-Jun-2025",
            line_items=[
                ("A4 Copier Paper (Box)", "20", "6,000.00"),
                ("Printer Toner Cartridge", "4", "4,000.00"),
            ],
            subtotal=PINV["subtotal"], cgst=PINV["cgst"], sgst=PINV["sgst"], total=PINV["total"],
            footer_lines=[
                "Payment Terms: Net 30 days (on credit).",
                "This is a supplier invoice for goods purchased on credit.",
            ],
        )
    if force or not paths["sales"].exists():
        _render_invoice_pdf(
            paths["sales"],
            doc_title="TAX INVOICE",
            party_label="Customer",
            party_name=CUSTOMER,
            bill_to="Sold By: Bharat Traders Private Limited",
            invoice_no=SINV_REF,
            date_display="15-Jun-2025",
            line_items=[
                ("Laptop Docking Station", "10", "12,000.00"),
                ("Wireless Keyboard & Mouse", "20", "8,000.00"),
            ],
            subtotal=SINV["subtotal"], cgst=SINV["cgst"], sgst=SINV["sgst"], total=SINV["total"],
            footer_lines=[
                "Payment Terms: Net 30 days.",
                "This is a sales invoice raised on the customer (on credit).",
            ],
        )
    if force or not paths["debit_note"].exists():
        _render_invoice_pdf(
            paths["debit_note"],
            doc_title="DEBIT NOTE",
            party_label="GST Registered Supplier",
            party_name=SUPPLIER,
            bill_to="Issued To: Bharat Paper Supplies (Purchase Return)",
            invoice_no=DN_REF,
            date_display="16-Jun-2025",
            line_items=[("A4 Copier Paper (returned)", "6", "2,000.00")],
            subtotal=DNOTE["subtotal"], cgst=DNOTE["cgst"], sgst=DNOTE["sgst"], total=DNOTE["total"],
            footer_lines=[
                "This is a DEBIT NOTE for goods returned to the supplier.",
                "It reduces the amount payable to the supplier.",
            ],
            against_ref=PINV_REF,
        )
    if force or not paths["credit_note"].exists():
        _render_invoice_pdf(
            paths["credit_note"],
            doc_title="CREDIT NOTE",
            party_label="Customer",
            party_name=CUSTOMER,
            bill_to="Issued To: Apex Technologies Pvt Ltd (Sales Return)",
            invoice_no=CN_REF,
            date_display="16-Jun-2025",
            line_items=[("Wireless Keyboard (returned)", "3", "1,000.00")],
            subtotal=CNOTE["subtotal"], cgst=CNOTE["cgst"], sgst=CNOTE["sgst"], total=CNOTE["total"],
            footer_lines=[
                "This is a CREDIT NOTE for goods returned by the customer.",
                "It reduces the amount receivable from the customer.",
            ],
            against_ref=SINV_REF,
        )
    return paths


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — extend write timeout (mirrors manual_test_group_b_live.post_write)
# ─────────────────────────────────────────────────────────────────────────────
async def post_write(client: TallyClient, xml: str) -> str:
    saved = client._client.timeout
    client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    try:
        return await client.post_xml(xml)
    finally:
        client._client.timeout = saved


# ─────────────────────────────────────────────────────────────────────────────
# Read-back helpers
# ─────────────────────────────────────────────────────────────────────────────
def _party_pending(bills: list[OutstandingBill], party: str) -> float:
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


async def read_payable(client: TallyClient, supplier: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_payable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, supplier)
    print(f"  [bills_payable as-on {AS_ON}] {supplier!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills)")
    return pending, bills


async def read_receivable(client: TallyClient, customer: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_receivable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, customer)
    print(f"  [bills_receivable as-on {AS_ON}] {customer!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills)")
    return pending, bills


async def read_ledger_balances(client: TallyClient) -> dict[str, float]:
    """Closing balance for every ledger, keyed by lowercased name (via build_list_ledgers)."""
    raw = await client.post_xml(build_list_ledgers())
    out: dict[str, float] = {}
    for led in parse_ledger_list(raw):
        out[led["name"].strip().lower()] = float(led.get("closing_balance") or 0.0)
    return out


def _gst_balance(balances: dict[str, float], ledger_name: str) -> float:
    return balances.get((ledger_name or "").strip().lower(), 0.0)


def _gst_ledger_names(entry: dict) -> list[str]:
    return [g.get("ledger", "") for g in (entry.get("gst_entries") or []) if g.get("ledger")]


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup — delete a voucher by Master ID, DD-MMM-YYYY date.
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None,
                          label: str, date_display: str) -> None:
    if not master_id or master_id == "0":
        print(f"  [skip cleanup — no Master ID for {label}]")
        return
    voucher_xml = (
        f'<VOUCHER DATE="{_esc(date_display)}" TAGNAME="Master ID" '
        f'TAGVALUE="{_esc(master_id)}" VCHTYPE="{_esc(voucher_type)}" ACTION="Delete">\n'
        f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, voucher_xml)
    try:
        raw = await post_write(client, xml)
        parsed = parse_import_response(raw)
        if parsed.get("deleted", 0) >= 1:
            print(f"  [cleanup OK] deleted {label} (mid={master_id}, deleted={parsed['deleted']})")
        else:
            print(f"  [cleanup WARN] {label} (mid={master_id}) NOT deleted: {_short(raw, 300)}")
    except Exception as e:  # noqa: BLE001 — cleanup must never abort the run
        print(f"  [cleanup ERROR] {label}: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Run one document through the REAL Vision pipeline → structural asserts → entry.
# ─────────────────────────────────────────────────────────────────────────────
async def vision_extract(orchestrator: Orchestrator, client: TallyClient,
                         pdf_path: Path, filename: str) -> dict:
    session = SessionContext(company=COMPANY)
    upload = await orchestrator.process_file_upload(
        file_path=str(pdf_path),
        filename=filename,
        mime_type="application/pdf",
        user_message="Please record this document.",
        client=client,
        session=session,
        file_id=f"pf-{filename}",
        db=None,
    )
    data = upload.get("data") or {}
    entries = data.get("entries") or []
    print(f"\n  process_file_upload message:\n{_short(upload.get('message', ''), 500)}")
    if not entries:
        raise RuntimeError(
            f"Vision/processing produced no entry for {filename}: "
            f"{_short(upload.get('message', ''), 300)}"
        )
    entry = entries[0]
    entry["_available_ledgers"] = data.get("available_ledgers")
    print("\n  RAW review entry (production output):")
    print(_short(json.dumps({k: v for k, v in entry.items() if k != "_available_ledgers"},
                            indent=2, default=str), 1400))
    return entry


def assert_classification(entry: dict, want_vtype: str, want_party: str,
                          spec: dict, log: list[str]) -> tuple[bool, str]:
    """Assert voucher_type + party + total + GST structurally (±TOLERANCE)."""
    vtype = (entry.get("voucher_type") or "").strip()
    amount = float(entry.get("amount") or 0.0)
    party_ledger = (entry.get("party_ledger") or "").strip()
    party_name = (entry.get("party_name") or entry.get("vendor_name") or "").strip()
    gst_entries = entry.get("gst_entries") or []
    gst_names = _gst_ledger_names(entry)
    gst_total = sum(float(g.get("amount") or 0.0) for g in gst_entries)
    expected_gst = spec["cgst"] + spec["sgst"]

    vtype_ok = vtype.lower() == want_vtype.lower()
    total_ok = abs(amount - spec["total"]) <= TOLERANCE
    party_blob = f"{party_ledger.lower()} {party_name.lower()}"
    party_ok = want_party.lower() in party_blob
    gst_present = len(gst_entries) >= 2 and abs(gst_total - expected_gst) <= TOLERANCE
    gst_resolved = len(gst_names) >= 2

    print(f"  voucher_type = {vtype!r} (want {want_vtype!r}) -> {vtype_ok}")
    print(f"  amount       = {amount:,.2f} (want ≈ {spec['total']:,.2f}) -> {total_ok}")
    print(f"  party        = ledger={party_ledger!r} name={party_name!r} "
          f"(want contains {want_party!r}) -> {party_ok}")
    print(f"  gst_entries  = {gst_entries}")
    print(f"  gst_total    = {gst_total:,.2f} (want ≈ {expected_gst:,.2f}); "
          f"ledgers={gst_names} -> present={gst_present}, resolved={gst_resolved}")
    for w in entry.get("warnings") or []:
        log.append(f"warning: {w}")

    ok = vtype_ok and total_ok and party_ok and gst_present and gst_resolved
    detail = (f"vtype={vtype}({vtype_ok}); amount={amount:,.2f}({total_ok}); "
              f"party={party_name or party_ledger}({party_ok}); "
              f"gst={gst_total:,.2f}/{gst_names}(present={gst_present},resolved={gst_resolved})")
    return ok, detail


def note_ref_mismatch(entry: dict, printed_ref: str, known_against: str, log: list[str]) -> None:
    """Log (don't fail) if Vision's bill_reference differs from the printed ref."""
    got = (entry.get("bill_reference") or "").strip()
    if got and got.lower() != printed_ref.lower():
        msg = (f"Vision bill_reference {got!r} != printed {printed_ref!r}; "
               f"writing with KNOWN ref {known_against!r} so settlement still works.")
        print(f"  [ref-mismatch] {msg}")
        log.append(msg)


def capture_mid(result: dict, voucher_type: str) -> str | None:
    mid = result.get("last_vch_id")
    if mid and mid != "0":
        MASTER_IDS.append((voucher_type, mid))
        print(f"  [captured Master ID] {voucher_type} mid={mid}")
    else:
        print(f"  [WARN] no Master ID (last_vch_id={mid!r}) — cleanup will be skipped")
    return mid


# ─────────────────────────────────────────────────────────────────────────────
# Main sequence
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    print(f"REAL-Vision live PDF-flow test — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Invoice date: {INV_DATE_DISPLAY} | Note date: {NOTE_DATE_DISPLAY} | as-on: {AS_ON}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ABORT: ANTHROPIC_API_KEY not set — this test needs the REAL Vision API.")
        sys.exit(1)
    if os.environ.get("TALLY_WRITE_ENABLED", "").lower() != "true":
        print("WARN: TALLY_WRITE_ENABLED is not 'true' — writer guards may block the write.")

    banner("Step 0 — Ensure the 4 flow-test PDFs exist (generate missing via PIL)")
    pdfs = generate_pdfs(force=False)
    for key, p in pdfs.items():
        print(f"  {key:<12} -> {p} ({p.stat().st_size:,} bytes)")

    client = TallyClient(host=host, port=port)
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    orchestrator = Orchestrator()

    base_payable = 0.0
    base_receivable = 0.0
    base_balances: dict[str, float] = {}
    # GST ledger names resolved per doc, for the final restore check.
    all_gst_names: set[str] = set()

    try:
        # ── Step 1: Baseline ──────────────────────────────────────────────
        banner("Step 1 — Baseline payable / receivable / GST ledger closing balances")
        base_payable, _ = await read_payable(client, SUPPLIER)
        base_receivable, _ = await read_receivable(client, CUSTOMER)
        base_balances = await read_ledger_balances(client)
        gst_like = {n: b for n, b in base_balances.items() if "gst" in n}
        print("  GST-like ledger closing balances at baseline:")
        for n, b in sorted(gst_like.items()):
            print(f"    {n!r}: {b:,.2f}")
        record("Baseline", "(read only)", "(read only)",
               f"payable={base_payable:,.2f}; receivable={base_receivable:,.2f}; "
               f"{len(gst_like)} GST-like ledgers read", True)

        # ── Step 2: PURCHASE PDF → Purchase voucher (payable +11,800) ─────
        log: list[str] = []
        try:
            banner("Step 2 — PURCHASE PDF → Vision → write Purchase (New Ref PINV-FLOW-01)")
            entry = await vision_extract(orchestrator, client, pdfs["purchase"],
                                         "purchase_invoice.pdf")
            cls_ok, cls_detail = assert_classification(entry, "Purchase", SUPPLIER, PINV, log)
            gst_names = _gst_ledger_names(entry); all_gst_names.update(gst_names)
            party_ledger = (entry.get("party_ledger") or "").strip()
            if not cls_ok or not party_ledger:
                raise RuntimeError(f"Purchase classification gate failed: {cls_detail}; "
                                   f"party_ledger={party_ledger!r}")
            before = {n: _gst_balance(base_balances, n) for n in gst_names}
            writer = TallyWriter(client, COMPANY)
            # Mirror chat.py Purchase dispatch: purchase_ledger = entry["debit_ledger"].
            result = await writer.create_purchase_voucher_ledger(
                date=INV_DATE,
                party_ledger=party_ledger,
                purchase_ledger=entry["debit_ledger"],
                amount=float(entry["amount"]),
                narration=f"{NPFX} purchase {PINV_REF}",
                gst_entries=entry.get("gst_entries") or None,
                bill_ref=PINV_REF,
                known_ledgers=entry.get("_available_ledgers"),
            )
            print(f"  RAW create response: {json.dumps(result, indent=2, default=str)}")
            capture_mid(result, "Purchase")

            after_pay, _ = await read_payable(client, SUPPLIER)
            after_balances = await read_ledger_balances(client)
            pay_delta = after_pay - base_payable
            pay_ok = abs(pay_delta - PINV["total"]) <= TOLERANCE
            gst_checks = []
            for n in gst_names:
                d = _gst_balance(after_balances, n) - before[n]
                gst_checks.append(f"{n}:{d:+,.2f}")
            # Each Input GST ledger should move ~900 in magnitude.
            gst_ok = all(
                abs(abs(_gst_balance(after_balances, n) - before[n]) - PINV["cgst"]) <= TOLERANCE
                for n in gst_names
            )
            print(f"  payable {base_payable:,.2f} -> {after_pay:,.2f} (delta {pay_delta:+,.2f}) -> {pay_ok}")
            print(f"  GST input deltas: {gst_checks} -> each ≈ +/-900 = {gst_ok}")
            record("Purchase", cls_detail, "create_purchase_voucher_ledger",
                   f"payable {pay_delta:+,.2f}(want +11800, ok={pay_ok}); GST {gst_checks}(ok={gst_ok})"
                   + ("; " + "; ".join(log) if log else ""),
                   cls_ok and pay_ok and gst_ok)
        except Exception as e:  # noqa: BLE001
            record("Purchase", "EXCEPTION", "create_purchase_voucher_ledger",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 3: SALES PDF → Sales voucher (receivable +23,600) ────────
        log = []
        try:
            banner("Step 3 — SALES PDF → Vision → write Sales (New Ref SINV-FLOW-01)")
            entry = await vision_extract(orchestrator, client, pdfs["sales"],
                                         "sales_invoice.pdf")
            cls_ok, cls_detail = assert_classification(entry, "Sales", CUSTOMER, SINV, log)
            gst_names = _gst_ledger_names(entry); all_gst_names.update(gst_names)
            party_ledger = (entry.get("party_ledger") or "").strip()
            if not cls_ok or not party_ledger:
                raise RuntimeError(f"Sales classification gate failed: {cls_detail}; "
                                   f"party_ledger={party_ledger!r}")
            before = {n: _gst_balance(base_balances, n) for n in gst_names}
            writer = TallyWriter(client, COMPANY)
            # Mirror chat.py Sales dispatch: sales_ledger = entry["credit_ledger"].
            result = await writer.create_sales_voucher_ledger(
                date=INV_DATE,
                party_ledger=party_ledger,
                sales_ledger=entry["credit_ledger"],
                amount=float(entry["amount"]),
                narration=f"{NPFX} sales {SINV_REF}",
                gst_entries=entry.get("gst_entries") or None,
                bill_ref=SINV_REF,
                known_ledgers=entry.get("_available_ledgers"),
            )
            print(f"  RAW create response: {json.dumps(result, indent=2, default=str)}")
            capture_mid(result, "Sales")

            after_recv, _ = await read_receivable(client, CUSTOMER)
            after_balances = await read_ledger_balances(client)
            recv_delta = after_recv - base_receivable
            recv_ok = abs(recv_delta - SINV["total"]) <= TOLERANCE
            gst_checks = []
            for n in gst_names:
                d = _gst_balance(after_balances, n) - before[n]
                gst_checks.append(f"{n}:{d:+,.2f}")
            gst_ok = all(
                abs(abs(_gst_balance(after_balances, n) - before[n]) - SINV["cgst"]) <= TOLERANCE
                for n in gst_names
            )
            print(f"  receivable {base_receivable:,.2f} -> {after_recv:,.2f} "
                  f"(delta {recv_delta:+,.2f}) -> {recv_ok}")
            print(f"  GST output deltas: {gst_checks} -> each ≈ +/-1800 = {gst_ok}")
            record("Sales", cls_detail, "create_sales_voucher_ledger",
                   f"receivable {recv_delta:+,.2f}(want +23600, ok={recv_ok}); GST {gst_checks}(ok={gst_ok})"
                   + ("; " + "; ".join(log) if log else ""),
                   cls_ok and recv_ok and gst_ok)
        except Exception as e:  # noqa: BLE001
            record("Sales", "EXCEPTION", "create_sales_voucher_ledger",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 4: DEBIT NOTE PDF → payable DECREASES by ~2,360 ──────────
        log = []
        try:
            banner("Step 4 — DEBIT NOTE PDF → Vision → write Debit Note (Agst PINV-FLOW-01) "
                   "→ payable should DECREASE  [CRITICAL DIRECTION]")
            entry = await vision_extract(orchestrator, client, pdfs["debit_note"],
                                         "debit_note.pdf")
            cls_ok, cls_detail = assert_classification(entry, "Debit Note", SUPPLIER, DNOTE, log)
            note_ref_mismatch(entry, DN_REF, PINV_REF, log)
            # Confirm it references the original purchase invoice (best-effort, logged).
            refs_orig = PINV_REF.lower() in json.dumps(entry, default=str).lower()
            print(f"  references original {PINV_REF!r} somewhere in entry: {refs_orig}")
            log.append(f"references PINV-FLOW-01={refs_orig}")
            gst_names = _gst_ledger_names(entry); all_gst_names.update(gst_names)
            party_ledger = (entry.get("party_ledger") or "").strip()
            if not cls_ok or not party_ledger:
                raise RuntimeError(f"Debit Note classification gate failed: {cls_detail}; "
                                   f"party_ledger={party_ledger!r}")
            before_pay, _ = await read_payable(client, SUPPLIER)
            writer = TallyWriter(client, COMPANY)
            # Mirror chat.py DN dispatch: purchase_ledger = entry["credit_ledger"] (the contra),
            # settle against the KNOWN PINV-FLOW-01 ref so the bill we created clears.
            result = await writer.create_debit_note(
                date=NOTE_DATE,
                party_ledger=party_ledger,
                purchase_ledger=entry["credit_ledger"],
                amount=float(entry["amount"]),
                narration=f"{NPFX} debit note {DN_REF}",
                gst_entries=entry.get("gst_entries") or None,
                bill_ref=PINV_REF,
                known_ledgers=entry.get("_available_ledgers"),
            )
            print(f"  RAW create response: {json.dumps(result, indent=2, default=str)}")
            capture_mid(result, "Debit Note")

            after_pay, _ = await read_payable(client, SUPPLIER)
            delta = after_pay - before_pay  # expect ≈ -2360
            decreased = delta < 0 and abs(delta + DNOTE["total"]) <= TOLERANCE
            if not decreased:
                print("\n" + "!" * 78)
                print("  !!!  DN DIRECTION WRONG  !!!  payable did NOT reduce.")
                print(f"  payable {before_pay:,.2f} -> {after_pay:,.2f} (delta {delta:+,.2f}); "
                      f"expected ≈ {-DNOTE['total']:+,.2f}")
                print("!" * 78)
            print(f"  payable {before_pay:,.2f} -> {after_pay:,.2f} (delta {delta:+,.2f}) "
                  f"-> decreased={decreased}")
            record("Debit Note", cls_detail, "create_debit_note",
                   f"payable {delta:+,.2f}(want -2360, DECREASE ok={decreased})"
                   + ("  <-- DN DIRECTION WRONG" if not decreased else "")
                   + ("; " + "; ".join(log) if log else ""),
                   cls_ok and decreased)
        except Exception as e:  # noqa: BLE001
            record("Debit Note", "EXCEPTION", "create_debit_note",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 5: CREDIT NOTE PDF → receivable DECREASES by ~1,180 ──────
        log = []
        try:
            banner("Step 5 — CREDIT NOTE PDF → Vision → write Credit Note (Agst SINV-FLOW-01) "
                   "→ receivable should DECREASE  [CRITICAL DIRECTION]")
            entry = await vision_extract(orchestrator, client, pdfs["credit_note"],
                                         "credit_note.pdf")
            cls_ok, cls_detail = assert_classification(entry, "Credit Note", CUSTOMER, CNOTE, log)
            note_ref_mismatch(entry, CN_REF, SINV_REF, log)
            refs_orig = SINV_REF.lower() in json.dumps(entry, default=str).lower()
            print(f"  references original {SINV_REF!r} somewhere in entry: {refs_orig}")
            log.append(f"references SINV-FLOW-01={refs_orig}")
            gst_names = _gst_ledger_names(entry); all_gst_names.update(gst_names)
            party_ledger = (entry.get("party_ledger") or "").strip()
            if not cls_ok or not party_ledger:
                raise RuntimeError(f"Credit Note classification gate failed: {cls_detail}; "
                                   f"party_ledger={party_ledger!r}")
            before_recv, _ = await read_receivable(client, CUSTOMER)
            writer = TallyWriter(client, COMPANY)
            # Mirror chat.py CN dispatch: sales_ledger = entry["debit_ledger"] (the contra),
            # settle against the KNOWN SINV-FLOW-01 ref so the bill we created clears.
            result = await writer.create_credit_note(
                date=NOTE_DATE,
                party_ledger=party_ledger,
                sales_ledger=entry["debit_ledger"],
                amount=float(entry["amount"]),
                narration=f"{NPFX} credit note {CN_REF}",
                gst_entries=entry.get("gst_entries") or None,
                bill_ref=SINV_REF,
                known_ledgers=entry.get("_available_ledgers"),
            )
            print(f"  RAW create response: {json.dumps(result, indent=2, default=str)}")
            capture_mid(result, "Credit Note")

            after_recv, _ = await read_receivable(client, CUSTOMER)
            delta = after_recv - before_recv  # expect ≈ -1180
            decreased = delta < 0 and abs(delta + CNOTE["total"]) <= TOLERANCE
            if not decreased:
                print("\n" + "!" * 78)
                print("  !!!  CN DIRECTION WRONG  !!!  receivable did NOT reduce.")
                print(f"  receivable {before_recv:,.2f} -> {after_recv:,.2f} (delta {delta:+,.2f}); "
                      f"expected ≈ {-CNOTE['total']:+,.2f}")
                print("!" * 78)
            print(f"  receivable {before_recv:,.2f} -> {after_recv:,.2f} (delta {delta:+,.2f}) "
                  f"-> decreased={decreased}")
            record("Credit Note", cls_detail, "create_credit_note",
                   f"receivable {delta:+,.2f}(want -1180, DECREASE ok={decreased})"
                   + ("  <-- CN DIRECTION WRONG" if not decreased else "")
                   + ("; " + "; ".join(log) if log else ""),
                   cls_ok and decreased)
        except Exception as e:  # noqa: BLE001
            record("Credit Note", "EXCEPTION", "create_credit_note",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

    finally:
        # ── Step 6: cleanup — delete all vouchers, verify books restored ──
        banner("Step 6 — CLEANUP (delete all captured vouchers by Master ID)")
        if not MASTER_IDS:
            print("  No Master IDs captured — nothing to delete.")
        for vtype, mid in MASTER_IDS:
            # Invoices on INV_DATE, notes on NOTE_DATE.
            ddate = NOTE_DATE_DISPLAY if vtype in ("Debit Note", "Credit Note") else INV_DATE_DISPLAY
            await cleanup_voucher(client, vtype, mid, f"{vtype} (mid={mid})", ddate)

        banner("Step 6 — Verify books restored to baseline (±1)")
        restored = True
        try:
            final_pay, _ = await read_payable(client, SUPPLIER)
            final_recv, _ = await read_receivable(client, CUSTOMER)
            final_balances = await read_ledger_balances(client)
            pay_ok = abs(final_pay - base_payable) <= TOLERANCE
            recv_ok = abs(final_recv - base_receivable) <= TOLERANCE
            gst_ok = True
            gst_detail = []
            for n in sorted(all_gst_names):
                before = _gst_balance(base_balances, n)
                after = _gst_balance(final_balances, n)
                ok = abs(after - before) <= TOLERANCE
                gst_ok = gst_ok and ok
                gst_detail.append(f"{n}:{before:,.2f}->{after:,.2f}({'ok' if ok else 'RESIDUE'})")
            restored = pay_ok and recv_ok and gst_ok
            if restored:
                print("\n  books restored — payable, receivable & GST ledgers back to baseline.")
            else:
                print("\n  !! RESIDUE REMAINS — books NOT fully restored:")
                if not pay_ok:
                    print(f"     payable: {base_payable:,.2f} -> {final_pay:,.2f}")
                if not recv_ok:
                    print(f"     receivable: {base_receivable:,.2f} -> {final_recv:,.2f}")
            record("Restore", "(delete + read-back)", "delete-by-Master-ID",
                   f"payable {base_payable:,.2f}->{final_pay:,.2f}(ok={pay_ok}); "
                   f"receivable {base_receivable:,.2f}->{final_recv:,.2f}(ok={recv_ok}); "
                   f"GST [{'; '.join(gst_detail)}]; "
                   + ("books restored" if restored else "RESIDUE REMAINS"),
                   restored)
        except Exception as e:  # noqa: BLE001
            print(f"  [restore-check ERROR] {type(e).__name__}: {e}")
            record("Restore", "(delete + read-back)", "delete-by-Master-ID",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Final results table ──
        banner("RESULTS TABLE")
        print(f"{'DOC':<14} {'PASS/FAIL':<10} {'WRITE CALL':<32} CLASSIFICATION / DIRECTION & GST CHECKS")
        print("-" * 150)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.doc:<14} {status:<10} {_short(r.write, 30):<32} "
                  f"{_short(r.classification, 40)} || {_short(r.checks, 60)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 150)
        print(f"{len(RESULTS)} rows — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        # ── Overall verdict ──
        banner("OVERALL VERDICT — PDF → Vision → classify → live write (4 doc types)")
        core_docs = {"Purchase", "Sales", "Debit Note", "Credit Note", "Restore"}
        core = [r for r in RESULTS if r.doc in core_docs]
        for r in core:
            print(f"  {r.doc:<14}: {'PASS' if r.passed else 'FAIL'}")
        dn = next((r for r in RESULTS if r.doc == "Debit Note"), None)
        cn = next((r for r in RESULTS if r.doc == "Credit Note"), None)
        print(f"\n  Debit Note  reduces PAYABLE   : {'PASS' if dn and dn.passed else 'FAIL'}")
        print(f"  Credit Note reduces RECEIVABLE: {'PASS' if cn and cn.passed else 'FAIL'}")
        core_pass = bool(core) and all(r.passed for r in core)
        if core_pass:
            print("\n  PASS: PDF → Vision → classify → live write verified for all 4 doc types, "
                  "directions correct, books restored.")
        else:
            print("\n  FAIL: see flagged rows above.")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="REAL-Vision live PDF-flow test (Purchase/Sales/DN/CN, self-cleaning)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--generate-only", action="store_true",
                        help="Only (re)generate the 4 fixture PDFs; no Vision, no Tally, no write.")
    args = parser.parse_args()
    if args.generate_only:
        generate_pdfs(force=True)
        print("Generated all 4 PDFs (force).")
        sys.exit(0)
    asyncio.run(run(args.host, args.port))
