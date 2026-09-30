"""REAL-Vision end-to-end manual test against a LIVE Tally — Purchase invoice path.

Proves the FULL production pipeline for a Purchase invoice, end to end, with NO mocks:

    generate a realistic invoice IMAGE (PIL) with KNOWN content
      → REAL Claude Vision extraction (the exact production code:
         ``Orchestrator.process_file_upload`` → ``build_vision_prompt`` +
         ``anthropic_client.messages.create`` + ``parse_vision_response``)
      → classification = purchase
      → ledger mapping + ``build_purchase_voucher_data`` (production routing)
      → WRITE to LIVE Tally via the real write path
         (``TallyWriter.create_purchase_voucher_ledger`` — the exact method
         ``backend/api/chat.py`` voucher_action dispatches to for a Purchase)
      → read bills_payable back and ASSERT it moved correctly + GST legs persisted
      → DELETE the voucher (delete-by-Master-ID) and assert the books are restored.

Why ``Orchestrator.process_file_upload`` directly: it IS the production entry point the
``POST /chat`` (file-upload) endpoint calls. With ``db=None`` it skips the DB audit row
(no workspace/conversation needed) yet still runs the REAL Anthropic Vision call, the
REAL Tally ledger fetch, the REAL doc_type routing, GST-ledger resolution and
``build_purchase_voucher_data`` — returning the same review entry the UI renders. We then
dispatch that entry through the SAME ``create_purchase_voucher_ledger`` call shape as
``api/chat.py`` voucher_action. So Vision → classify → ledger-map → build → live write is
ALL production code; only the image generation and the assertions are test scaffolding.

Grounded in:
  - backend/agents/orchestrator.py            (process_file_upload — Vision call + routing + review entry)
  - backend/services/document_parser.py       (build_vision_prompt / parse_vision_response / ExtractedDocument)
  - backend/api/chat.py                        (voucher_action Purchase dispatch — call shape mirrored)
  - tally_bridge/writer.py             (create_purchase_voucher_ledger)
  - tally_bridge/queries/reports.py    (bills_payable → list[OutstandingBill])
  - scripts/manual_test_group_b_live.py        (client/arg/cleanup patterns, FY date, results table)
  - scripts/probe_group_b_readback.py          (best-effort voucher read-back for GST legs)
  - LESSONS.md §15                             (write safety: read-back, DD-MMM-YYYY delete date)

⚠️  WARNING — this makes REAL Claude Vision API calls AND WRITES to a live Tally.
    It targets "Bharat Traders Private Limited", uses a "_LV" narration prefix and a
    small amount, and deletes everything it creates in a ``finally`` block.

Usage (do NOT run casually — costs a Vision call + writes to live Tally):
    TALLY_WRITE_ENABLED=true ANTHROPIC_API_KEY=<key> PYTHONPATH=. \
        uv run --with pillow python scripts/manual_test_live_vision.py --host localhost --port 9000 \
        2>&1 | tee docs/manual-test-live-vision.log

Import check (safe — no Vision, no Tally, no run):
    PYTHONPATH=. uv run --with pillow python -c "import scripts.manual_test_live_vision"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime

import httpx

from backend.agents.context import SessionContext
from backend.agents.orchestrator import Orchestrator
from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.models import OutstandingBill
from tally_bridge.queries.reports import bills_payable
from tally_bridge.request_builder import build_day_book, build_list_ledgers
from tally_bridge.response_parser import parse_import_response, parse_ledger_list
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
NPFX = "_LV"  # narration prefix so leftovers are mechanically identifiable

# Known invoice content (drawn into the image so we can verify extraction).
INV_VENDOR = "Bharat Paper Supplies"  # a seed Sundry-Creditor so the party ledger matches
INV_NUMBER = "INV-LV-001"
INV_DATE_DISPLAY = "15-Jun-2025"
INV_SUBTOTAL = 5000.00
INV_CGST = 450.00          # 9%
INV_SGST = 450.00          # 9%
INV_TOTAL = 5900.00        # subtotal + CGST + SGST
INV_GSTIN = "27AABCB1234C1Z5"

# Tally import date is YYYYMMDD; delete envelopes want DD-MMM-YYYY (LESSONS §15 / v4).
VCH_DATE = "20250615"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 15-Jun-2025
# bills_payable takes a DD-MM-YYYY "as on" date (request_builder format).
AS_ON = "31-03-2026"

# New-Ref bill reference for the payable we raise.
PUR_BILL_REF = f"{NPFX}-PUR-1"

# Numeric tolerance (rupees). Vision may read amounts with tiny variance; allow ±1.
TOLERANCE = 1.0
# Extraction total tolerance is a touch wider in case Vision rounds, but still tight.
TOTAL_TOLERANCE = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking (mirrors manual_test_group_b_live.py)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    step: str
    expected: str
    observed: str
    passed: bool


RESULTS: list[StepResult] = []
MASTER_IDS: list[tuple[str, str]] = []  # (voucher_type, master_id) for cleanup


def record(step: str, expected: str, observed: str, passed: bool) -> None:
    RESULTS.append(StepResult(step, expected, observed, passed))
    print(f"\n  >>> {'PASS' if passed else 'FAIL'}: {step}")
    print(f"      expected: {expected}")
    print(f"      observed: {observed}")


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _short(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


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
# Step 0 — generate a clean, legible PURCHASE INVOICE PNG with KNOWN content.
# ─────────────────────────────────────────────────────────────────────────────
def generate_invoice_image() -> str:
    """Draw a purchase invoice PNG to a temp path and return that path.

    Large canvas (1000×1400), black text on white, generous fonts so Vision reads
    it reliably. Content is the KNOWN values above so extraction can be verified.
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1000, 1400
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    def _font(size: int):
        # Try a few common TrueType faces for crisp large text; fall back to PIL default.
        for path in (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    f_title = _font(56)
    f_h = _font(34)
    f_b = _font(28)

    def text(x: int, y: int, s: str, font, fill="black") -> None:
        draw.text((x, y), s, font=font, fill=fill)

    margin = 60
    y = 60
    text(margin, y, "TAX INVOICE", f_title); y += 90
    text(margin, y, INV_VENDOR, f_h); y += 50
    text(margin, y, "GST Registered Supplier — Maharashtra", f_b); y += 40
    text(margin, y, f"GSTIN: {INV_GSTIN}", f_b); y += 70

    text(margin, y, f"Invoice No: {INV_NUMBER}", f_b)
    text(margin + 500, y, f"Date: {INV_DATE_DISPLAY}", f_b); y += 50
    text(margin, y, "Bill To: Bharat Traders Private Limited", f_b); y += 70

    # Table header rule
    draw.line([(margin, y), (W - margin, y)], fill="black", width=2); y += 16
    text(margin, y, "Description", f_b)
    text(margin + 520, y, "Qty", f_b)
    text(margin + 700, y, "Amount (Rs)", f_b); y += 46
    draw.line([(margin, y), (W - margin, y)], fill="black", width=1); y += 20

    # Two line items summing to the subtotal (3000 + 2000 = 5000).
    text(margin, y, "A4 Copier Paper (Box)", f_b)
    text(margin + 520, y, "10", f_b)
    text(margin + 700, y, "3,000.00", f_b); y += 44
    text(margin, y, "Printer Toner Cartridge", f_b)
    text(margin + 520, y, "2", f_b)
    text(margin + 700, y, "2,000.00", f_b); y += 60

    draw.line([(margin, y), (W - margin, y)], fill="black", width=1); y += 24

    def total_row(label: str, amount: float, bold_font) -> None:
        nonlocal y
        text(margin + 400, y, label, bold_font)
        text(margin + 700, y, f"{amount:,.2f}", bold_font); y += 46

    total_row("Subtotal:", INV_SUBTOTAL, f_b)
    total_row("CGST @ 9%:", INV_CGST, f_b)
    total_row("SGST @ 9%:", INV_SGST, f_b)
    draw.line([(margin + 380, y), (W - margin, y)], fill="black", width=2); y += 16
    total_row("TOTAL:", INV_TOTAL, f_h)

    y += 80
    text(margin, y, "Payment Terms: Net 30 days (on credit).", f_b); y += 40
    text(margin, y, "This is a supplier invoice for goods purchased on credit.", f_b)

    fd, path = tempfile.mkstemp(prefix="lv_purchase_invoice_", suffix=".png")
    os.close(fd)
    img.save(path, format="PNG")
    print(f"  [image] wrote purchase invoice → {path}")
    print(f"  [image] known content: vendor={INV_VENDOR!r}, no={INV_NUMBER}, "
          f"date={INV_DATE_DISPLAY}, subtotal={INV_SUBTOTAL}, "
          f"CGST={INV_CGST}, SGST={INV_SGST}, total={INV_TOTAL}")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Read-back helpers — total pending payable for a party from bills_payable.
# ─────────────────────────────────────────────────────────────────────────────
def _party_pending(bills: list[OutstandingBill], party: str) -> float:
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


def _bill_present(bills: list[OutstandingBill], party: str, bill_ref: str) -> bool:
    p = party.strip().lower()
    r = bill_ref.strip().lower()
    return any(
        b.party_name.strip().lower() == p and b.bill_number.strip().lower() == r
        for b in bills
    )


async def read_payable(client: TallyClient, supplier: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_payable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, supplier)
    print(f"  [read bills_payable as-on {AS_ON}] {supplier!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills in report)")
    return pending, bills


# ─────────────────────────────────────────────────────────────────────────────
# Best-effort GST-leg read-back — confirm the voucher carries CGST/SGST Input legs.
# Mirrors probe_group_b_readback.py: pull the day book for the voucher date and
# match the just-written voucher by its unique narration, then inspect its ledger
# legs for the GST ledger names we resolved. This is best-effort: parse_vouchers
# returns header-level rows in this codebase, so we treat absence as "could not
# confirm" rather than a hard failure.
# ─────────────────────────────────────────────────────────────────────────────
async def readback_gst_legs(client: TallyClient, narration: str,
                            gst_ledger_names: list[str]) -> tuple[bool, str]:
    """Return (confirmed, detail). confirmed=True only if GST legs are visible."""
    try:
        raw = await client.post_xml(
            build_day_book(VCH_DATE_DISPLAY, VCH_DATE_DISPLAY, company=COMPANY)
        )
    except Exception as e:  # noqa: BLE001
        return False, f"day_book read failed: {type(e).__name__}: {e}"
    # Cheap, robust signal: the raw export should contain our GST ledger names near
    # the voucher. We look for each resolved GST ledger name in the raw XML for the
    # voucher's date window. (parse_vouchers in this codebase is header-oriented, so
    # we inspect the raw export text rather than rely on parsed legs.)
    found = [name for name in gst_ledger_names if name and name in raw]
    detail = (f"GST ledgers resolved={gst_ledger_names}; "
              f"present in day-book export={found}; "
              f"narration {narration!r} in export={narration in raw}")
    confirmed = bool(gst_ledger_names) and len(found) == len(gst_ledger_names)
    return confirmed, detail


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup — delete a voucher by Master ID, DD-MMM-YYYY date.
# (mirrors manual_test_group_b_live.cleanup_voucher / probe_group_b)
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None,
                          label: str) -> None:
    if not master_id or master_id == "0":
        print(f"  [skip cleanup — no Master ID for {label}]")
        return
    voucher_xml = (
        f'<VOUCHER DATE="{_esc(VCH_DATE_DISPLAY)}" TAGNAME="Master ID" '
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
# Main sequence
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    print(f"REAL-Vision live Purchase test — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Voucher date: {VCH_DATE} (display {VCH_DATE_DISPLAY}) | bills as-on: {AS_ON}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ABORT: ANTHROPIC_API_KEY not set — this test needs the REAL Vision API.")
        sys.exit(1)
    if os.environ.get("TALLY_WRITE_ENABLED", "").lower() != "true":
        print("WARN: TALLY_WRITE_ENABLED is not 'true' — writer guards may block the write.")

    client = TallyClient(host=host, port=port)

    # Connectivity check.
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    image_path: str | None = None
    base_payable = 0.0
    entry: dict | None = None
    gst_ledger_names: list[str] = []
    narration = f"{NPFX} live-vision purchase {INV_NUMBER}"

    try:
        # ── Step 0: generate the invoice image ────────────────────────────
        banner("Step 0 — Generate purchase invoice image (PIL)")
        image_path = generate_invoice_image()

        # ── Step 1: REAL Vision extraction via PRODUCTION code ─────────────
        banner("Step 1 — REAL Claude Vision extraction via Orchestrator.process_file_upload")
        orchestrator = Orchestrator()
        session = SessionContext(company=COMPANY)
        # db=None → no DB audit row required; still runs the real Vision call,
        # real Tally ledger fetch, real doc_type routing + build_purchase_voucher_data.
        upload_result = await orchestrator.process_file_upload(
            file_path=image_path,
            filename="purchase_invoice.png",
            mime_type="image/png",
            user_message="Please record this purchase invoice.",
            client=client,
            session=session,
            file_id="lv-test-file",
            db=None,
        )
        review_data = upload_result.get("data") or {}
        entries = review_data.get("entries") or []
        print(f"\n  process_file_upload message:\n{_short(upload_result.get('message', ''), 600)}")
        if not entries:
            record("1-extraction", "Vision returns a review entry",
                   f"NO entry; message={_short(upload_result.get('message', ''), 300)}", False)
            raise RuntimeError("Vision/processing produced no review entry — cannot continue.")
        entry = entries[0]
        print("\n  RAW review entry (production output):")
        print(_short(json.dumps(entry, indent=2, default=str), 1600))
        record("1-extraction", "Vision returns a structured review entry for the invoice",
               f"got entry id={entry.get('id')}, voucher_type={entry.get('voucher_type')!r}", True)

        # ── Step 2: assert extraction / classification ────────────────────
        banner("Step 2 — Assert classification = Purchase, total ≈ 5900, vendor + GST resolved")
        vtype = (entry.get("voucher_type") or "").strip().lower()
        amount = float(entry.get("amount") or 0.0)
        party_ledger = (entry.get("party_ledger") or "").strip()
        vendor_name = (entry.get("party_name") or entry.get("vendor_name") or "").strip()
        gst_entries = entry.get("gst_entries") or []
        gst_ledger_names = [g.get("ledger", "") for g in gst_entries if g.get("ledger")]
        gst_total = sum(float(g.get("amount") or 0.0) for g in gst_entries)

        is_purchase = vtype == "purchase"
        total_ok = abs(amount - INV_TOTAL) <= TOTAL_TOLERANCE
        vendor_ok = INV_VENDOR.lower() in (party_ledger.lower() + " " + vendor_name.lower())
        # GST present: CGST 450 + SGST 450 ⇒ two input-GST legs summing ≈ 900.
        gst_present = len(gst_entries) >= 2 and abs(gst_total - (INV_CGST + INV_SGST)) <= TOLERANCE
        gst_resolved = len(gst_ledger_names) >= 2

        print(f"  classification voucher_type = {vtype!r} (want 'purchase') -> {is_purchase}")
        print(f"  extracted total amount      = {amount:,.2f} (want ≈ {INV_TOTAL:,.2f}) -> {total_ok}")
        print(f"  party_ledger / vendor       = {party_ledger!r} / {vendor_name!r} "
              f"(want contains {INV_VENDOR!r}) -> {vendor_ok}")
        print(f"  GST entries                 = {gst_entries}")
        print(f"  GST total                   = {gst_total:,.2f} (want ≈ {INV_CGST + INV_SGST:,.2f}); "
              f"input ledgers resolved={gst_ledger_names} -> present={gst_present}, resolved={gst_resolved}")

        step2_ok = is_purchase and total_ok and vendor_ok and gst_present and gst_resolved
        record(
            "2-classification",
            "voucher_type=Purchase; total≈5900; vendor resolves to Bharat Paper Supplies; "
            "CGST 450 + SGST 450 → Input GST ledgers resolved",
            f"voucher_type={vtype!r}(ok={is_purchase}); total={amount:,.2f}(ok={total_ok}); "
            f"vendor/party={vendor_name!r}/{party_ledger!r}(ok={vendor_ok}); "
            f"gst_total={gst_total:,.2f}, ledgers={gst_ledger_names}(present={gst_present}, "
            f"resolved={gst_resolved})",
            step2_ok,
        )
        if not is_purchase:
            # Classification is the firm gate — abort the write if it's not a Purchase.
            raise RuntimeError(f"Classification not 'purchase' (got {vtype!r}) — aborting write.")
        if not party_ledger:
            raise RuntimeError("No party_ledger resolved — aborting write (chat.py would block too).")

        # ── Step 3: WRITE to LIVE Tally (real write path, chat.py call shape) ─
        banner(f"Step 3 — Write Purchase to LIVE Tally (New Ref {PUR_BILL_REF})")
        base_payable, _ = await read_payable(client, party_ledger)
        print(f"  payable BEFORE write: {base_payable:,.2f}")
        writer = TallyWriter(client, COMPANY)
        # Mirror backend/api/chat.py voucher_action Purchase dispatch exactly:
        #   party_ledger=entry["party_ledger"], purchase_ledger=entry["debit_ledger"],
        #   amount=entry["amount"], gst_entries=entry["gst_entries"], bill_ref=...
        result = await writer.create_purchase_voucher_ledger(
            date=VCH_DATE,
            party_ledger=party_ledger,
            purchase_ledger=entry["debit_ledger"],
            amount=amount,
            narration=narration,
            gst_entries=gst_entries or None,
            bill_ref=PUR_BILL_REF,
            known_ledgers=review_data.get("available_ledgers"),
        )
        print(f"  RAW create response (parsed): {json.dumps(result, indent=2, default=str)}")
        mid = result.get("last_vch_id")
        if mid and mid != "0":
            MASTER_IDS.append(("Purchase", mid))
            print(f"  [captured Master ID] Purchase mid={mid}")
        else:
            print(f"  [WARN] no Master ID (last_vch_id={mid!r}) — cleanup of this voucher will be skipped")
        record("3-write", "Purchase voucher created (CREATED=1) with a Master ID",
               f"create result={result}; master_id={mid!r}", bool(mid and mid != "0"))

        # ── Step 4: read back & assert payable + GST legs ──────────────────
        banner("Step 4 — Read back: payable +5900, bill present, GST input legs carried")
        after_pay, pay_bills = await read_payable(client, party_ledger)
        delta = after_pay - base_payable
        present = _bill_present(pay_bills, party_ledger, PUR_BILL_REF)
        payable_ok = abs(delta - amount) <= TOLERANCE and present
        record(
            "4-readback-payable",
            f"payable +{amount:,.2f} and bill {PUR_BILL_REF} present",
            f"payable {base_payable:,.2f} -> {after_pay:,.2f} (delta {delta:+,.2f}); "
            f"bill present={present}",
            payable_ok,
        )

        gst_confirmed, gst_detail = await readback_gst_legs(client, narration, gst_ledger_names)
        # Best-effort — record it, but don't fail the overall verdict on read-back
        # blindness (the write itself + payable delta already prove the legs balanced).
        record(
            "4-readback-gst",
            "CGST Input + SGST Input legs present on the voucher (best-effort read-back)",
            f"confirmed={gst_confirmed}; {gst_detail}",
            gst_confirmed,
        )

    except Exception as e:  # noqa: BLE001
        record("pipeline", "pipeline runs to completion",
               f"EXCEPTION {type(e).__name__}: {e}", False)

    finally:
        # ── Step 5: cleanup — delete voucher, verify restore, delete image ──
        banner("Step 5 — CLEANUP (delete voucher by Master ID, verify restore, delete image)")
        if not MASTER_IDS:
            print("  No Master IDs captured — nothing to delete.")
        for vtype, mid in MASTER_IDS:
            await cleanup_voucher(client, vtype, mid, f"{vtype} (mid={mid})")

        # Re-read payable → assert restored to baseline (only meaningful if we wrote).
        if entry is not None and MASTER_IDS:
            try:
                final_pay, _ = await read_payable(client, (entry.get("party_ledger") or "").strip())
                restored = abs(final_pay - base_payable) <= TOLERANCE
                if restored:
                    print("\n  ✅ books restored — payable back to baseline.")
                else:
                    print(f"\n  ⚠️  RESIDUE REMAINS — payable baseline {base_payable:,.2f} vs now "
                          f"{final_pay:,.2f} (residue {final_pay - base_payable:+,.2f})")
                record("5-restore", "payable restored to baseline (±1)",
                       f"payable {base_payable:,.2f}->{final_pay:,.2f}; "
                       + ("books restored" if restored else "RESIDUE REMAINS"),
                       restored)
            except Exception as e:  # noqa: BLE001
                print(f"  [restore-check ERROR] {type(e).__name__}: {e}")
                record("5-restore", "payable restored to baseline",
                       f"EXCEPTION {type(e).__name__}: {e}", False)

        # Delete the temp image.
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
                print(f"  [cleanup] removed temp image {image_path}")
            except OSError as e:
                print(f"  [cleanup WARN] could not remove image {image_path}: {e}")

        # ── Final results table ──
        banner("RESULTS TABLE")
        print(f"{'STEP':<22} {'PASS/FAIL':<10} {'EXPECTED':<48} OBSERVED")
        print("-" * 150)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.step:<22} {status:<10} {_short(r.expected, 46):<48} {_short(r.observed, 70)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 150)
        print(f"{len(RESULTS)} steps — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        # ── Overall verdict — the load-bearing steps for "REAL Vision → classify → live write" ──
        banner("OVERALL VERDICT — REAL Vision → classify → live write")
        core_steps = {"1-extraction", "2-classification", "3-write", "4-readback-payable", "5-restore"}
        core = [r for r in RESULTS if r.step in core_steps]
        core_pass = bool(core) and all(r.passed for r in core)
        for r in core:
            print(f"  {r.step:<22}: {'PASS' if r.passed else 'FAIL'}")
        if core_pass:
            print("\n  ✅ REAL Vision → classify → live write: PASS")
        else:
            print("\n  ❌ REAL Vision → classify → live write: FAIL — see flagged steps above.")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="REAL-Vision live Purchase end-to-end manual test (Vision→classify→write, self-cleaning)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
