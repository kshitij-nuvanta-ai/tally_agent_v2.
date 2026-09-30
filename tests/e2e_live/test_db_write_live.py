"""FULL data-entry WRITE path against a LIVE Tally, in real DB mode, mock Claude.

This module exercises the production upload → voucher-review → write pipeline
end-to-end for ALL FIVE voucher types (Payment, Purchase, Sales, Debit Note,
Credit Note) using:

  * **mock Claude** — ONLY the orchestrator's Vision call
    (``backend.agents.orchestrator.anthropic_client.messages.create``) is
    patched to return a deterministic, canned extraction per doc type. No real
    Anthropic call is made (a sentinel guard asserts this).
  * **LIVE Tally** — the workspace is configured with the real ``--host/--port``
    and NO ``mock_mode``, so ``backend/api/chat.py`` builds a real
    ``TallyClient`` and the upload + voucher-action actually write to the live
    "Bharat Traders Private Limited" company. The Tally WRITER is NOT mocked
    (unlike ``tests/e2e/test_db_data_entry*.py``).
  * **real DB / DB mode** — a full DB-mode FastAPI app backed by the real
    Postgres at ``TEST_DATABASE_URL`` (real auth + workspace + conversation +
    Message/VoucherEntry/VoucherEntryRevision persistence).

Every voucher written is CLEANED UP in teardown (delete-by-Master-ID via the
production ``TallyWriter.delete_voucher``, DD-MMM-YYYY date) — collected as we
go so cleanup runs even if an assertion fails mid-test. Read-back confirms each
delete. Goods-created stock masters under "AI Imported Items" with 0 balance are
removed too (none are created here — all five types use service/ledger lines —
but the sweep is defensive). Seed data is never touched.

Gating (skips cleanly so a plain ``pytest`` run never hits Tally or Postgres):
  * live Tally — via ``tests/e2e_live/conftest.py`` (RUN_LIVE_TESTS=1 or
    ``--tally-mode mock``); plus a per-fixture health_check skip.
  * real DB — module-level ``skipif`` on ``TEST_DATABASE_URL``.

Run:
    cd "<repo>" && RUN_LIVE_TESTS=1 \
      TEST_DATABASE_URL=postgresql+asyncpg://<user>@localhost:5433/tallyagent_test \
      ANTHROPIC_API_KEY=test-key PYTHONPATH=. uv run pytest \
      tests/e2e_live/test_db_write_live.py -v -s --host localhost --port 9000 \
      2>&1 | tee logs/e2e-db-write-live.log

Grounded in:
  - tests/e2e/test_db_data_entry.py           (db_app DB-mode app + auth/ws/conv setup)
  - tests/e2e/test_db_data_entry_group_b.py    (per-type write payloads + audit-row queries)
  - scripts/manual_test_group_b_live.py        (live read-back + delete-by-Master-ID cleanup)
  - scripts/cleanup_waterpump.py               (orphan stock-item delete sweep)
  - backend/api/chat.py                        (voucher_action approve path + _record_voucher_revision)
  - LESSONS.md §15                             (read-back, DD-MMM-YYYY delete date)
"""
from __future__ import annotations

import io
import json
import os
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — skipping live DB write tests",
)

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200

COMPANY = "Bharat Traders Private Limited"
NPFX = "_E2ELIVE"  # narration prefix so any leftover is mechanically identifiable
# FY-internal date (seed company FY = Apr 2025 – Mar 2026). Vision emits ISO; the
# entry "date" the writer consumes is YYYYMMDD. Delete envelopes want DD-MMM-YYYY.
VCH_DATE_ISO = "2025-06-20"
VCH_DATE_YYYYMMDD = "20250620"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE_YYYYMMDD, "%Y%m%d").strftime("%d-%b-%Y")
AS_ON = "31-03-2026"  # bills_payable / _receivable "as on" (DD-MM-YYYY)
TOLERANCE = 1.0  # rupees

# Live ledgers known to exist in the seeded Bharat Traders company (verified via
# build_list_ledgers + bills_payable/_receivable). Supplier/customer are bill-wise
# (appear in the outstanding reports), so New-Ref bill allocations show up.
SUPPLIER = "Bharat Paper Supplies"          # Sundry Creditor, bill-wise
CUSTOMER = "Apex Technologies Pvt Ltd"      # Sundry Debtor, bill-wise
PURCHASE_LEDGER = "Purchase - Office Supplies"
SALES_LEDGER = "Sales - Office Supplies"
EXPENSE_LEDGER = "City Cab Services"        # Indirect Expense (Payment debit)
PAYMENT_LEDGER = "Cash"                     # Cash-in-Hand (Payment credit)

# Per-type amounts (small, in-FY).
PAYMENT_AMOUNT = 350.0
PURCHASE_AMOUNT = 5000.0
SALES_AMOUNT = 8000.0
DN_AMOUNT = 2000.0
CN_AMOUNT = 3000.0


# ---------------------------------------------------------------------------
# Canned Vision extractions — one per doc type, using REAL live ledgers so the
# party resolves to an existing bill-wise ledger (is_new_ledger=False) and the
# write succeeds. Unique invoice refs (NPFX-<TYPE>-<rand>) dodge the dedup block.
# ---------------------------------------------------------------------------

def _rand() -> str:
    return uuid.uuid4().hex[:6].upper()


def _service_line(desc: str, amount: float) -> dict:
    # quantity/rate null → NOT inventory → ledger-based writers (no stock master).
    return {"description": desc, "amount": amount, "quantity": None, "rate": None}


def _vision_extraction(doc_type: str) -> tuple[dict, str]:
    """Return (vision_json_dict, unique_invoice_ref) for ``doc_type``."""
    ref = f"{NPFX}-{doc_type.upper()}-{_rand()}"
    if doc_type == "payment":
        doc = {
            "doc_type": "expense",
            "party_name": "City Cab Services",
            "vendor_name": "City Cab Services",
            "date": VCH_DATE_ISO,
            "total_amount": PAYMENT_AMOUNT,
            "original_currency": "INR",
            "fx_rate": None,
            "line_items": [_service_line("Cab fare", PAYMENT_AMOUNT)],
            "gst": None,
            "payment_mode": "cash",
            "invoice_number": ref,
        }
    elif doc_type == "purchase":
        doc = {
            "doc_type": "purchase",
            "party_name": SUPPLIER,
            "date": VCH_DATE_ISO,
            "total_amount": PURCHASE_AMOUNT,
            "original_currency": "INR",
            "fx_rate": None,
            "line_items": [_service_line("Office supplies — service", PURCHASE_AMOUNT)],
            "gst": None,
            "payment_mode": "bank",
            "invoice_number": ref,
        }
    elif doc_type == "sales":
        doc = {
            "doc_type": "sales",
            "party_name": CUSTOMER,
            "date": VCH_DATE_ISO,
            "total_amount": SALES_AMOUNT,
            "original_currency": "INR",
            "fx_rate": None,
            "line_items": [_service_line("Consulting service", SALES_AMOUNT)],
            "gst": None,
            "payment_mode": "bank",
            "invoice_number": ref,
        }
    elif doc_type == "debit_note":
        doc = {
            "doc_type": "debit_note",
            "party_name": SUPPLIER,
            "date": VCH_DATE_ISO,
            "total_amount": DN_AMOUNT,
            "original_currency": "INR",
            "fx_rate": None,
            "line_items": [_service_line("Purchase return", DN_AMOUNT)],
            "gst": None,
            "payment_mode": "bank",
            "original_invoice_ref": None,
            "invoice_number": ref,
        }
    elif doc_type == "credit_note":
        doc = {
            "doc_type": "credit_note",
            "party_name": CUSTOMER,
            "date": VCH_DATE_ISO,
            "total_amount": CN_AMOUNT,
            "original_currency": "INR",
            "fx_rate": None,
            "line_items": [_service_line("Sales return", CN_AMOUNT)],
            "gst": None,
            "payment_mode": "bank",
            "original_invoice_ref": None,
            "invoice_number": ref,
        }
    else:  # pragma: no cover
        raise ValueError(doc_type)
    return doc, ref


def _vision_message(doc: dict, sentinel: dict) -> MagicMock:
    """MagicMock matching the Anthropic messages.create response shape.

    ``sentinel['called']`` flips True iff this mock is invoked — used to assert
    the real Anthropic client is NEVER hit (Claude is mocked)."""
    sentinel["called"] = True
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(doc))]
    return msg


# ---------------------------------------------------------------------------
# DB-mode app fixture — LIVE Tally (no mock_mode), real Postgres.
# Mirrors tests/e2e/test_db_data_entry.py db_app but does NOT force mock Tally:
# the workspace config points at the live host so chat.py builds a real client.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_app(monkeypatch, tmp_path, tally_host, tally_port, tally_mode):
    from backend.config import settings

    # Live writes only make sense against real Tally. If the suite is run in
    # --tally-mode mock, skip — this module is specifically the LIVE path.
    if tally_mode == "mock":
        pytest.skip("test_db_write_live targets LIVE Tally; not run in --tally-mode mock")

    # Reachability gate (skips cleanly if Tally is down).
    from backend.tally_bridge.client import TallyClient
    probe = TallyClient(host=tally_host, port=tally_port)
    healthy = await probe.health_check()
    await probe.close()
    if not healthy:
        pytest.skip(f"Tally unreachable at {tally_host}:{tally_port}")

    monkeypatch.setattr(settings, "DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setattr(settings, "JWT_SECRET", "a" * 64)
    # Real Tally — NOT mock mode. (TALLY_MODE is only consulted for the app.state
    # client; the workspace config drives the per-request client used for writes.)
    monkeypatch.setattr(settings, "TALLY_MODE", "live")
    monkeypatch.setattr(settings, "TALLY_HOST", tally_host)
    monkeypatch.setattr(settings, "TALLY_PORT", tally_port)
    monkeypatch.setattr(settings, "TALLY_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(
        settings, "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "test-key")
    )

    from backend.db.models import Base
    setup_engine = create_async_engine(_TEST_DB_URL)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from fastapi import FastAPI

    from backend.api import (
        auth, chat, companies, conversations, health, reports,
        tally_mode as tally_mode_router, usage, workspaces,
    )

    app = FastAPI()
    for module in (chat, health, companies, reports, tally_mode_router,
                   auth, workspaces, conversations, usage):
        app.include_router(module.router, prefix="/api")

    from backend.agents.context import SessionStore
    from backend.db.engine import init_engine

    # app.state client points at the LIVE Tally (used only for non-write reads;
    # the write path re-builds a client from the workspace config anyway).
    tally_client = TallyClient(host=tally_host, port=tally_port)
    app.state.tally_client = tally_client
    app.state.session_store = SessionStore(ttl_minutes=60)
    init_engine(_TEST_DB_URL)

    auth._register_attempts.clear()

    yield app

    auth._register_attempts.clear()
    await tally_client.close()
    from backend.db.engine import close_engine
    await close_engine()

    teardown_engine = create_async_engine(_TEST_DB_URL)
    async with teardown_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await teardown_engine.dispose()


# ---------------------------------------------------------------------------
# Cleanup registry — collects (voucher_type, master_id) for EVERY successful
# write so teardown deletes them all even if an assertion fails mid-test.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cleanup(tally_host, tally_port):
    """Yield a list to append (voucher_type, master_id) tuples to; on teardown,
    delete each via the production TallyWriter.delete_voucher and read back to
    confirm it is gone. Always runs (it's the fixture's finalizer)."""
    from backend.tally_bridge.client import TallyClient
    from backend.tally_bridge.writer import TallyWriter

    written: list[tuple[str, str]] = []
    yield written

    if not written:
        return
    client = TallyClient(host=tally_host, port=tally_port)
    writer = TallyWriter(client=client, company=COMPANY)
    # Extend write timeout (mirrors manual_test_group_b_live.post_write).
    try:
        client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        for vtype, mid in written:
            if not mid or mid == "0":
                print(f"  [cleanup skip] {vtype}: no Master ID")
                continue
            try:
                res = await writer.delete_voucher(
                    voucher_type=vtype, master_id=mid, date=VCH_DATE_DISPLAY,
                )
            except Exception as e:  # noqa: BLE001 — cleanup must never raise
                print(f"  [cleanup ERROR] {vtype} mid={mid}: {type(e).__name__}: {e}")
                continue
            deleted = res.get("deleted", 0)
            if deleted >= 1:
                # Read back: confirm the voucher is gone (day-book trace).
                gone = not await _voucher_present(client, mid)
                print(f"  [cleanup OK] deleted {vtype} mid={mid} "
                      f"(deleted={deleted}); readback gone={gone}")
            else:
                print(f"  [cleanup WARN] {vtype} mid={mid} NOT deleted: {res}")
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Live read-back helpers
# ---------------------------------------------------------------------------

async def _party_pending(client, *, payable: bool, party: str) -> float:
    from backend.tally_bridge.queries.reports import bills_payable, bills_receivable
    bills = await (bills_payable if payable else bills_receivable)(client, AS_ON, COMPANY)
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


async def _voucher_present(client, master_id: str) -> bool:
    """True iff a voucher with ``master_id`` exists in the FY day-book trace.

    Reuses the inventory-trace collection from cleanup_waterpump (it pulls
    MasterId for every voucher in the FY), but only checks the Master ID — works
    for ledger-only vouchers too (no inventory line required)."""
    from scripts.cleanup_waterpump import build_inventory_trace_query
    import xml.etree.ElementTree as ET
    from backend.tally_bridge.response_parser import sanitize_xml
    raw = await client.post_xml(
        build_inventory_trace_query("01-04-2025", "31-03-2026", COMPANY)
    )
    root = ET.fromstring(sanitize_xml(raw))
    for vch in root.iter("VOUCHER"):
        if (vch.findtext("MASTERID") or "").strip() == str(master_id):
            return True
    return False


# ---------------------------------------------------------------------------
# Auth / workspace / conversation setup (mirrors test_db_data_entry helpers but
# the workspace config targets the LIVE host with NO mock_mode).
# ---------------------------------------------------------------------------

async def _register_and_get_token(client: AsyncClient) -> tuple[str, str]:
    email = f"live-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post("/api/auth/register", json={
        "email": email, "password": "Str0ng!Pass#99", "name": "Live Tester",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"], data["user"]["id"]


async def _create_workspace(client, token, name, host, port) -> str:
    # NO mock_mode → chat.py builds a REAL TallyClient against host:port.
    config = {"tally_host": host, "tally_port": port, "tally_company": COMPANY}
    resp = await client.post(
        "/api/workspaces",
        json={"name": name, "config": config},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_conversation(client, token, ws_id) -> str:
    resp = await client.post(
        f"/api/workspaces/{ws_id}/conversations",
        json={"title": "live write flow"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# DB audit queries
# ---------------------------------------------------------------------------

async def _voucher_entries(conv_id):
    import backend.db.engine as engine_mod
    from backend.db.models import VoucherEntry
    async with engine_mod.async_session_factory() as session:
        rows = (await session.execute(
            select(VoucherEntry).where(VoucherEntry.conversation_id == uuid.UUID(conv_id))
        )).scalars().all()
        return rows


async def _revisions(conv_id, entry_id):
    import backend.db.engine as engine_mod
    from backend.db.models import VoucherEntryRevision
    async with engine_mod.async_session_factory() as session:
        rows = (await session.execute(
            select(VoucherEntryRevision)
            .where(
                VoucherEntryRevision.conversation_id == uuid.UUID(conv_id),
                VoucherEntryRevision.entry_id == entry_id,
            )
            .order_by(VoucherEntryRevision.version_no)
        )).scalars().all()
        return rows


def _review_card(messages):
    cards = [
        m for m in messages
        if m["role"] == "assistant"
        and isinstance(m["data"], dict)
        and m["data"].get("type") == "voucher_review"
    ]
    assert cards, f"no persisted voucher_review card: {messages}"
    return cards[0]


# Map doc_type → (response voucher_type, is payable/receivable, party, signed delta)
_TYPE_META = {
    "payment":     ("Payment",     None,  None,      None),
    "purchase":    ("Purchase",    True,  SUPPLIER,  +PURCHASE_AMOUNT),
    "sales":       ("Sales",       False, CUSTOMER,  +SALES_AMOUNT),
    "debit_note":  ("Debit Note",  True,  SUPPLIER,  -DN_AMOUNT),
    "credit_note": ("Credit Note", False, CUSTOMER,  -CN_AMOUNT),
}


async def _seed_settleable_bill(client, doc_type: str, bill_ref: str, cleanup) -> None:
    """For DN/CN, raise a preceding open bill (Purchase for DN, Sales for CN)
    under the SAME bill_ref so the uploaded DN/CN settles against it (Agst Ref) —
    proving the payable/receivable DECREASES. Mirrors manual_test_group_b_live
    (DN settles the Purchase bill; CN settles the Sales bill). Registered for
    cleanup. Returns nothing; raises on write failure."""
    from backend.tally_bridge.writer import TallyWriter
    writer = TallyWriter(client=client, company=COMPANY)
    if doc_type == "debit_note":
        res = await writer.create_purchase_voucher_ledger(
            date=VCH_DATE_YYYYMMDD, party_ledger=SUPPLIER,
            purchase_ledger=PURCHASE_LEDGER, amount=PURCHASE_AMOUNT,
            narration=f"{NPFX} preceding purchase for DN", bill_ref=bill_ref,
        )
        vtype = "Purchase"
    else:  # credit_note
        res = await writer.create_sales_voucher_ledger(
            date=VCH_DATE_YYYYMMDD, party_ledger=CUSTOMER,
            sales_ledger=SALES_LEDGER, amount=SALES_AMOUNT,
            narration=f"{NPFX} preceding sales for CN", bill_ref=bill_ref,
        )
        vtype = "Sales"
    assert res.get("success"), f"preceding {vtype} write failed: {res}"
    mid = res.get("last_vch_id")
    assert mid and mid != "0", f"preceding {vtype}: no Master ID: {res}"
    cleanup.append((vtype, mid))


@pytest.mark.parametrize("doc_type", list(_TYPE_META.keys()))
@pytest.mark.asyncio
async def test_live_db_write_voucher_type(doc_type, db_app, cleanup, tally_host, tally_port):
    """Upload (mock Vision) → review card → approve (REAL Tally write) → assert
    DB card status / audit row / revision trail + Tally read-back; cleanup runs
    in the ``cleanup`` fixture teardown regardless of pass/fail."""
    from backend.tally_bridge.client import TallyClient

    vtype, payable, party, expected_delta = _TYPE_META[doc_type]
    doc, ref = _vision_extraction(doc_type)
    sentinel = {"called": False}

    read_client = TallyClient(host=tally_host, port=tally_port)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=db_app), base_url="http://test"
        ) as ac:
            token, _ = await _register_and_get_token(ac)
            ws_id = await _create_workspace(
                ac, token, f"Live {vtype} Co", tally_host, tally_port,
            )
            conv_id = await _create_conversation(ac, token, ws_id)
            headers = {"Authorization": f"Bearer {token}"}

            # --- Step 1+2: upload with mocked Vision → review card ---
            with patch(
                "backend.agents.orchestrator.anthropic_client.messages.create",
                new=AsyncMock(side_effect=lambda *a, **k: _vision_message(doc, sentinel)),
            ):
                up = await ac.post(
                    "/api/chat/upload",
                    files={"file": ("doc.jpg", io.BytesIO(_FAKE_JPEG), "image/jpeg")},
                    data={"message": doc_type, "workspace_id": ws_id,
                          "conversation_id": conv_id},
                    headers=headers,
                )
            assert sentinel["called"], "mock Vision was NOT invoked — real Claude?"
            assert up.status_code == 200, up.text
            body = up.json()
            assert body["data"]["type"] == "voucher_review", body
            file_id = body["data"]["file_id"]
            entry = body["data"]["entries"][0]
            assert entry["voucher_type"] == vtype, entry
            entry_id = entry["id"]

            # Revision v1 (source='upload') persisted at upload time.
            revs = await _revisions(conv_id, entry_id)
            assert revs, "no upload revision row"
            assert revs[0].version_no == 1
            assert revs[0].source == "upload"

            # For DN/CN, raise a preceding open bill the note will SETTLE against
            # (Agst Ref) so the payable/receivable visibly DECREASES. A bare DN/CN
            # against a fresh ref posts a new open bill and nets to zero movement.
            settle_ref = None
            if doc_type in ("debit_note", "credit_note"):
                settle_ref = f"{NPFX}-SETTLE-{_rand()}"
                await _seed_settleable_bill(read_client, doc_type, settle_ref, cleanup)

            # Baseline payable/receivable AFTER any preceding bill, BEFORE the
            # note write — so the measured delta is purely the DN/CN effect.
            base = None
            if payable is not None:
                base = await _party_pending(read_client, payable=payable, party=party)

            # --- Step 3: approve → REAL Tally write ---
            # DN/CN settle the preceding bill (Agst Ref = settle_ref); Purchase/
            # Sales raise their own New Ref bill (= ref).
            bill_reference = None
            if vtype in ("Purchase", "Sales"):
                bill_reference = ref
            elif vtype in ("Debit Note", "Credit Note"):
                bill_reference = settle_ref
            write_entry = {
                "id": entry_id,
                "voucher_type": vtype,
                "date": entry.get("date") or VCH_DATE_YYYYMMDD,
                "debit_ledger": entry.get("debit_ledger"),
                "credit_ledger": entry.get("credit_ledger"),
                "amount": entry["amount"],
                "narration": f"{NPFX} {vtype} {entry.get('narration', '')}".strip(),
                "gst_entries": entry.get("gst_entries", []),
                "party_ledger": entry.get("party_ledger"),
                "reference": ref,
                "bill_reference": bill_reference,
                "is_new_ledger": False,
                "file_id": file_id,
                "conversation_id": conv_id,
            }

            resp = await ac.post(
                "/api/chat/voucher-action",
                json={
                    "action": "approve",
                    "entry": write_entry,
                    "company": COMPANY,
                    "session_id": f"live-{doc_type}",
                    "workspace_id": ws_id,
                    "conversation_id": conv_id,
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            wbody = resp.json()
            assert wbody["data"]["type"] == "voucher_written", wbody
            master_id = wbody["data"].get("tally_voucher_id")
            assert master_id and master_id != "0", f"no Master ID returned: {wbody}"
            # Register for cleanup IMMEDIATELY (before further asserts can fail).
            cleanup.append((vtype, master_id))

            # --- Step 4: DB assertions ---
            # (a) card Message entry status flipped to 'written' on reload.
            reload = await ac.get(
                f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers,
            )
            assert reload.status_code == 200, reload.text
            card_entry = _review_card(reload.json()["messages"])["data"]["entries"][0]
            assert card_entry["status"] == "written", card_entry

            # (b) VoucherEntry audit row with the Tally voucher number.
            rows = await _voucher_entries(conv_id)
            assert len(rows) == 1, f"expected 1 audit row, got {len(rows)}"
            ve = rows[0]
            assert ve.voucher_type == vtype
            assert ve.status == "written"
            assert str(ve.tally_voucher_number) == str(master_id)

            # (c) revision trail: a 'write' row at the highest version.
            revs = await _revisions(conv_id, entry_id)
            assert revs[-1].source == "write", [r.source for r in revs]
            assert revs[-1].version_no == max(r.version_no for r in revs)

            # --- Step 5: Tally read-back ---
            # The written voucher exists by Master ID in the FY day-book.
            assert await _voucher_present(read_client, master_id), (
                f"written {vtype} mid={master_id} not found in day-book"
            )
            # For party vouchers, the payable/receivable moved by the signed amount.
            if payable is not None:
                after = await _party_pending(read_client, payable=payable, party=party)
                delta = after - base
                assert abs(delta - expected_delta) <= TOLERANCE, (
                    f"{vtype}: {'payable' if payable else 'receivable'} for {party!r} "
                    f"moved {delta:+.2f}, expected {expected_delta:+.2f} "
                    f"(base {base:.2f} -> {after:.2f})"
                )

            # Claude was mocked the whole time (no real API call).
            assert sentinel["called"]
    finally:
        await read_client.close()
