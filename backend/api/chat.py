"""Chat endpoint — main conversational interface to the agent pipeline."""

import logging
import os
import time as time_module
import uuid as uuid_mod
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.context import SessionContext, SessionStore
from backend.agents.orchestrator import Orchestrator
from backend.api.dependencies import get_client, get_current_user, get_session_store
from backend.api.models import ChatRequest, ChatResponse, ChartSpec, VoucherActionRequest
from backend.config import settings
from backend.tally_bridge.client import TallyClient
from backend.utils.currency_format import format_inr

logger = logging.getLogger(__name__)
router = APIRouter()

_tracer = trace.get_tracer(__name__)


async def _get_optional_db() -> AsyncGenerator[AsyncSession | None, None]:
    """Yield DB session if available, None otherwise."""
    if not settings.db_mode:
        yield None
        return
    from backend.db.engine import get_db
    async for session in get_db():
        yield session


_SAFE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".csv", ".xlsx", ".xls"}


async def _record_voucher_revision(
    db, *, workspace_id, conversation_id, entry_id, source, voucher_data, file_id=None
):
    """Append a version row to the voucher_entry_revisions edit-history trail.

    Computes the next monotonic ``version_no`` per (conversation_id, entry_id)
    and inserts a FULL snapshot of the entry. DB-only audit (not shown in the
    chat UI). Does NOT commit — the caller commits in the same unit of work as
    the existing message/status/audit writes. No-op outside DB mode or without a
    real conversation_id / entry_id.
    """
    if db is None or not settings.db_mode or not conversation_id or not entry_id:
        return
    from sqlalchemy import func

    from backend.db.models import VoucherEntryRevision
    result = await db.execute(
        select(func.coalesce(func.max(VoucherEntryRevision.version_no), 0)).where(
            VoucherEntryRevision.conversation_id == conversation_id,
            VoucherEntryRevision.entry_id == entry_id,
        )
    )
    next_version = int(result.scalar() or 0) + 1
    # Hard-link the revision to its original upload. Prefer the explicit
    # file_id arg; otherwise derive from the entry snapshot. Nullable.
    if file_id is None and isinstance(voucher_data, dict):
        file_id = voucher_data.get("file_id") or None
    db.add(VoucherEntryRevision(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        entry_id=entry_id,
        version_no=next_version,
        source=source,
        voucher_data=dict(voucher_data),
        file_id=file_id,
    ))


async def _update_persisted_voucher_status(db, conversation_id, entry_id, new_status):
    """Update the originating review-card Message so the entry's status survives reload.

    Finds the assistant Message in this conversation whose data is a voucher_review
    containing entry_id, sets that entry's status, and marks the JSONB column dirty.
    No-op if not found.
    """
    if db is None or not conversation_id or not entry_id:
        return
    from sqlalchemy.orm.attributes import flag_modified

    from backend.db.models import Message
    result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    for msg in result.scalars().all():
        data = msg.data
        if not isinstance(data, dict) or data.get("type") != "voucher_review":
            continue
        entries = data.get("entries") or []
        changed = False
        for e in entries:
            if isinstance(e, dict) and e.get("id") == entry_id:
                e["status"] = new_status
                changed = True
        if changed:
            flag_modified(msg, "data")


async def _update_persisted_voucher_entry(db, conversation_id, entry_id, new_entry):
    """Replace the originating review-card entry's fields with ``new_entry``.

    Mirrors ``_update_persisted_voucher_status`` but swaps the WHOLE entry so a
    user's draft edits (reference, qty, ledgers, …) survive a conversation
    reload. Preserves the entry ``id`` and forces ``status="draft"`` — a saved
    draft is never a terminal (written/deleted) state. Marks the JSONB column
    dirty so SQLAlchemy flushes it. No-op if the entry isn't found.
    """
    if db is None or not conversation_id or not entry_id:
        return
    from sqlalchemy.orm.attributes import flag_modified

    from backend.db.models import Message
    result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    for msg in result.scalars().all():
        data = msg.data
        if not isinstance(data, dict) or data.get("type") != "voucher_review":
            continue
        entries = data.get("entries") or []
        changed = False
        for i, e in enumerate(entries):
            if isinstance(e, dict) and e.get("id") == entry_id:
                replacement = dict(new_entry)
                replacement["id"] = entry_id
                replacement["status"] = "draft"
                entries[i] = replacement
                changed = True
        if changed:
            flag_modified(msg, "data")


async def _persist_action_response(db, conversation_id, message, data):
    """Persist a voucher-action's terminal RESULT as an assistant Message row.

    Without this, the result message (write success / duplicate block / write
    error / discard) only existed client-side for the live session and vanished
    on reload — the conversation rehydrates from Message rows only, leaving the
    card's persisted status with no explanatory message (LESSONS §17 family).

    Mirrors the returned ChatResponse: role="assistant", content=<message>,
    data=<data> (so the type, e.g. voucher_written / voucher_error, survives).
    The caller commits (alongside the status/audit updates). Per-row created_at
    default orders this AFTER the originating review card. No-op outside DB mode
    or without a real conversation_id.
    """
    if db is None or not conversation_id:
        return
    from backend.db.models import Message
    db.add(Message(
        conversation_id=conversation_id,
        role="assistant",
        content=message,
        data=data,
    ))


async def _verify_workspace_ownership(
    workspace_id: str, user_id: str, db: AsyncSession,
):
    """Load workspace and verify it belongs to the current user.

    Raises HTTPException(404) if not found or not owned.
    """
    from backend.db.models import Workspace
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.user_id == user_id,
            Workspace.is_deleted.is_(False),
        )
    )
    workspace = result.scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    client: TallyClient = Depends(get_client),
    session_store: SessionStore = Depends(get_session_store),
    user_id: str = Depends(get_current_user),
    db: AsyncSession | None = Depends(_get_optional_db),
) -> ChatResponse:
    if settings.db_mode:
        return await _chat_db_mode(request, client, user_id, db)
    else:
        return await _chat_legacy_mode(request, client, session_store)


@router.post("/chat/upload", response_model=ChatResponse)
async def chat_with_file(
    file: UploadFile = File(...),
    message: str = Form(default=""),
    workspace_id: str = Form(default=""),
    conversation_id: str = Form(default=""),
    client: TallyClient = Depends(get_client),
    user_id: str = Depends(get_current_user),
    db: AsyncSession | None = Depends(_get_optional_db),
) -> ChatResponse:
    """Upload a document (receipt/invoice) for data entry.

    Validates and saves the file. Task 12 will replace the placeholder
    response with the full data entry pipeline (parse → map → review card).
    """
    from backend.services.document_parser import detect_file_type

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    file_type = detect_file_type(file.filename, file.content_type or "")
    if file_type == "unsupported":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.filename}",
        )

    # Read and validate size
    contents = await file.read()
    file_size = len(contents)
    max_bytes = settings.FILE_MAX_SIZE_MB * 1024 * 1024
    if file_size > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({file_size} bytes). "
                f"Max: {settings.FILE_MAX_SIZE_MB}MB"
            ),
        )
    if file_size == 0:
        raise HTTPException(status_code=400, detail="File is empty")

    # In DB mode, verify workspace ownership before running the pipeline.
    # Capture the workspace's configured company so the Vision classifier can
    # anchor purchase-vs-sales direction to the user's own books.
    if settings.db_mode:
        if not workspace_id:
            raise HTTPException(status_code=400, detail="workspace_id is required in DB mode")
        if db is None:
            raise HTTPException(status_code=500, detail="Database session not available")
        workspace = await _verify_workspace_ownership(workspace_id, user_id, db)
        ws_config = workspace.config or {}
        company = ws_config.get("tally_company") or workspace.name or "Default"
    else:
        company = None

    # Save to local storage — sanitize the extension to a safe whitelist to
    # avoid stashing arbitrary user-supplied suffixes (e.g. ".jpg.php").
    os.makedirs(settings.FILE_STORAGE_PATH, exist_ok=True)
    file_id = str(uuid_mod.uuid4())
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _SAFE_EXTS:
        ext = ""  # no extension is safer than trusting random input
    storage_path = os.path.join(settings.FILE_STORAGE_PATH, f"{file_id}{ext}")
    with open(storage_path, "wb") as f:
        f.write(contents)

    # Run the data entry pipeline via the orchestrator.
    orchestrator = Orchestrator()
    session = SessionContext(session_id=conversation_id or file_id)

    try:
        result = await orchestrator.process_file_upload(
            file_path=storage_path,
            filename=file.filename,
            mime_type=file.content_type or "",
            user_message=message,
            client=client,
            session=session,
            file_id=file_id,
            db=db if settings.db_mode else None,
            user_id=user_id if settings.db_mode else None,
            workspace_id=workspace_id if settings.db_mode else None,
            conversation_id=conversation_id if settings.db_mode else None,
            company=company,
        )
        if settings.db_mode and db is not None and conversation_id:
            from backend.db.models import Conversation, Message
            user_content = (
                f"{message} [{file.filename}]" if message
                else f"Uploading file [{file.filename}]"
            )
            db.add(Message(
                conversation_id=conversation_id, role="user", content=user_content,
            ))
            # Flush the user message before adding the assistant message so the
            # per-row created_at default guarantees user→assistant reload order.
            await db.flush()
            db.add(Message(
                conversation_id=conversation_id, role="assistant",
                content=result["message"], data=result.get("data"),
            ))
            # Title an upload-first conversation and bump ordering, mirroring
            # _chat_db_mode — otherwise it shows as "New Chat" forever and
            # sorts stale in the sidebar.
            conv_result = await db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.workspace_id == workspace_id,
                    Conversation.user_id == user_id,
                    Conversation.is_deleted.is_(False),
                )
            )
            conversation = conv_result.scalar_one_or_none()
            if conversation is not None:
                if conversation.title is None:
                    conversation.title = user_content[:100]
                conversation.updated_at = datetime.now(timezone.utc)
            # Record version 1 of the edit-history trail for each card entry.
            review = result.get("data") or {}
            if isinstance(review, dict) and review.get("type") == "voucher_review":
                for card_entry in review.get("entries") or []:
                    if isinstance(card_entry, dict) and card_entry.get("id"):
                        await _record_voucher_revision(
                            db,
                            workspace_id=workspace_id,
                            conversation_id=conversation_id,
                            entry_id=card_entry["id"],
                            source="upload",
                            voucher_data=card_entry,
                            file_id=card_entry.get("file_id"),
                        )
        if settings.db_mode and db is not None:
            await db.commit()
    except HTTPException:
        # Clean up the uploaded file on pipeline failure.
        try:
            os.remove(storage_path)
        except OSError:
            pass
        raise
    except Exception as e:
        # Clean up the uploaded file on pipeline failure.
        try:
            os.remove(storage_path)
        except OSError:
            pass
        logger.exception("Data entry pipeline failed")
        raise HTTPException(status_code=500, detail=f"Pipeline error: {e}")

    return ChatResponse(
        message=result["message"],
        data=result.get("data"),
        session_id=conversation_id or file_id,
    )


def _gst_mode_from_entries(gst_entries: list[dict] | None) -> str:
    """Derive Tally GST mode: 'inter' when only IGST present, else 'intra'."""
    names = " ".join((e.get("ledger") or "") for e in (gst_entries or [])).lower()
    if "igst" in names and "cgst" not in names and "sgst" not in names:
        return "inter"
    return "intra"


async def _write_inventory_voucher(
    writer,
    entry: dict,
    reference: str | None,
    reference_date: str | None,
) -> dict:
    """Pre-flight new stock masters then post a stock-based Purchase/Sales voucher.

    For each line marked ``create_new`` (or with no matched item), idempotently
    ensure the unit and stock group exist, then create the stock item. Creates
    are best-effort idempotent — an "already exists" failure is treated as OK.
    Builds ``(stock_name, qty, rate, ledger, uom, gst_rate)`` tuples and calls
    the stock-based writer with the GST mode + Phase-1 reference + a New Ref bill
    allocation for the gross.
    """
    from backend.tally_bridge.import_builder import compute_invoice_gross
    from backend.tally_bridge.queries.masters import list_stock_items, list_stock_groups

    voucher_type = entry["voucher_type"]
    party = entry["party_ledger"]
    lines = entry.get("line_items") or []
    default_group = entry.get("default_stock_group") or settings.DEFAULT_STOCK_GROUP or "AI Imported Items"

    # ROOT CAUSE FIX: never send a CREATE for a master that ALREADY EXISTS. This
    # Tally instance answers a duplicate-master CREATE import with a blocking
    # modal (not a clean error), which freezes the HTTP gateway. So we fetch the
    # existing stock items ONCE and derive the sets of known item names / units /
    # groups; a create is issued only for genuinely-missing masters.
    existing = await list_stock_items(writer.client)
    existing_item_names = {s.name.casefold() for s in existing}
    known_units = {s.base_units.casefold() for s in existing if s.base_units}
    known_groups = {s.parent_group.casefold() for s in existing if s.parent_group}

    # An EMPTY stock group (present in Tally but with no items, e.g. a default
    # "AI Imported Items" left from a prior run) is NOT covered by the item-derived
    # parent_group set above. Re-sending create_stock_group for it pops a blocking
    # modal that freezes the gateway, so list existing stock groups directly and
    # fold them into known_groups. Defensive: if the list query fails, fall back to
    # the item-derived set (the altered=1-idempotent path is the secondary net).
    try:
        for grp in await list_stock_groups(writer.client):
            if grp:
                known_groups.add(grp.casefold())
    except Exception as e:
        logger.warning("list_stock_groups failed; using item-derived groups only: %s", e)

    async def _idempotent(coro):
        """Run a master create; swallow ONLY a genuine already-exists failure.

        Secondary safety net (the primary mechanism is "don't create what
        exists" above). An "already exists" response is benign and swallowed.
        ANY other failure (bad group, Tally down, permissions, malformed XML,
        transport timeout) must NOT be swallowed — it would surface later as a
        confusing "item not found" on the voucher post, or silently wedge the
        request behind a modal. Log it and re-raise so the caller turns it into
        a clean voucher_error instead of hanging.
        """
        try:
            await coro
        except Exception as e:
            msg = str(e).lower()
            if "already exist" in msg or "duplicate" in msg:
                logger.info("Stock master already exists (idempotent skip): %s", e)
                return
            logger.error("Stock master pre-flight failed (not an already-exists case): %s", e)
            raise

    async def _create_master(coro, kind: str, name: str):
        """Wrap a master-create so a transport timeout becomes a clear error
        instead of a hung request (a future blocking modal must never silently
        wedge the gateway)."""
        try:
            await _idempotent(coro)
        except Exception as e:
            raise RuntimeError(
                f"Couldn't create {kind} '{name}' in Tally — add it manually or "
                f"pick an existing item. ({e})"
            ) from e

    for line in lines:
        # Matched line (matched_item set / item already exists): create NOTHING.
        if not (line.get("create_new") or not line.get("matched_item")):
            continue
        unit = (line.get("unit") or "Nos").strip() or "Nos"
        group = (line.get("stock_group") or default_group).strip() or "AI Imported Items"
        stock_name = line.get("stock_name") or line["description"]

        # If the item already exists, skip ALL creates for this line — even if
        # the line was flagged create_new (the flag can be stale / wrong).
        if stock_name.casefold() in existing_item_names:
            continue

        if unit.casefold() not in known_units:
            known_units.add(unit.casefold())
            await _create_master(writer.create_unit(unit, unit), "unit", unit)
        # Ensure the parent group exists (mirrors the seeder). "Primary" is
        # reserved in Tally and creating/using it can trigger a blocking modal —
        # so a non-reserved default group is created on demand only when missing.
        if group.casefold() not in known_groups:
            known_groups.add(group.casefold())
            await _create_master(writer.create_stock_group(group, ""), "stock group", group)
        existing_item_names.add(stock_name.casefold())
        await _create_master(writer.create_stock_item(
            name=stock_name,
            group=group,
            uom=unit,
            opening_qty=0,
            opening_rate=0,
            hsn_code=str(line.get("hsn") or ""),
            gst_rate=int(round(float(line.get("gst_rate") or 0))),
        ), "stock item", stock_name)

    items = [
        (
            line.get("stock_name") or line["description"],
            float(line.get("qty") or 0),
            float(line.get("rate") or 0),
            line.get("ledger") or entry.get("debit_ledger") or entry.get("credit_ledger"),
            (line.get("unit") or "Nos").strip() or "Nos",
            int(round(float(line.get("gst_rate") or 0))),
        )
        for line in lines
    ]
    gst_mode = _gst_mode_from_entries(entry.get("gst_entries"))

    # Finding 1: the party leg (and its New Ref bill allocation) must equal the
    # gross computed from the SAME item tuples the builder posts (Σ qty×rate +
    # per-bucket GST), never Vision's extracted total. After a user edits
    # qty/rate, Vision's total is stale; using it imbalances the voucher.
    gross = compute_invoice_gross(items, gst_mode)

    # The supplier/customer invoice number doubles as the voucher number; fall
    # back to a date-derived value when none was extracted.
    voucher_number = reference or entry.get("bill_reference") or f"AUTO-{entry['date']}"
    bill_allocations = [{
        "name": (entry.get("bill_reference") or reference or voucher_number),
        "type": "New Ref",
        "amount": gross,
    }]

    common = dict(
        date=entry["date"],
        voucher_number=voucher_number,
        party=party,
        items=items,
        narration=entry["narration"],
        gst_mode=gst_mode,
        bill_allocations=bill_allocations,
        reference=reference,
        reference_date=reference_date,
    )
    if voucher_type == "Purchase":
        return await writer.create_purchase_voucher(**common)
    return await writer.create_sales_voucher(**common)


@router.post("/chat/voucher-action", response_model=ChatResponse)
async def voucher_action(
    request: VoucherActionRequest,
    client: TallyClient = Depends(get_client),
    user_id: str = Depends(get_current_user),
    db: AsyncSession | None = Depends(_get_optional_db),
) -> ChatResponse:
    """Handle voucher approve/edit/discard actions from the review card.

    approve/edit both write to Tally (edit means user already modified the
    entry in the EditForm before confirming). discard just acknowledges.
    """
    from backend.tally_bridge.writer import TallyWriter, ValidationError

    entry = request.entry
    session_id = request.session_id
    # Conversation id is threaded through the request body (production) and
    # falls back to the entry dict (legacy/tests). Computed once here so both
    # the discard status-update and the success-write audit/status paths use it.
    conv_id = request.conversation_id or entry.get("conversation_id")

    # In DB mode, verify workspace ownership and prefer the workspace's
    # configured company over the client-supplied value. This prevents a
    # user from targeting another workspace's Tally company.
    if settings.db_mode:
        if not request.workspace_id:
            raise HTTPException(status_code=400, detail="workspace_id is required in DB mode")
        if db is None:
            raise HTTPException(status_code=500, detail="Database session not available")
        workspace = await _verify_workspace_ownership(request.workspace_id, user_id, db)
        ws_config = workspace.config or {}
        company = ws_config.get("tally_company") or workspace.name or "Default"
        # Re-configure TallyClient from workspace config if present.
        if ws_config.get("tally_host"):
            client = TallyClient(
                ws_config["tally_host"],
                ws_config.get("tally_port", 9000),
            )
            if ws_config.get("mock_mode"):
                client.mock_mode = True
    else:
        company = request.company or "Default"

    if request.action == "discard":
        discard_message = "Entry discarded."
        discard_data = {"type": "voucher_discarded", "entry_id": entry.get("id")}
        if settings.db_mode and db is not None:
            await _update_persisted_voucher_status(
                db, conv_id, entry.get("id"), "deleted",
            )
            await _persist_action_response(db, conv_id, discard_message, discard_data)
            await db.commit()
        return ChatResponse(
            message=discard_message,
            data=discard_data,
            session_id=session_id,
        )

    # save_draft persists the edited card to its Message row WITHOUT writing to
    # Tally — so a user's draft edits (reference, qty, ledgers, …) survive a page
    # reload. No TallyWriter, no dedup block, no VoucherEntry audit row. The user
    # writes later via "Write to Tally" (approve). If conversation_id is missing
    # (deferred-creation edge), return success WITHOUT persisting — the local
    # state still holds and a later action carries the id.
    if request.action == "save_draft":
        if settings.db_mode and db is not None and conv_id:
            await _update_persisted_voucher_entry(
                db, conv_id, entry.get("id"), entry,
            )
            await _record_voucher_revision(
                db,
                workspace_id=request.workspace_id,
                conversation_id=conv_id,
                entry_id=entry.get("id"),
                source="edit",
                voucher_data=entry,
                file_id=entry.get("file_id"),
            )
            await db.commit()
        return ChatResponse(
            message="Draft saved.",
            data={"type": "voucher_draft_saved", "entry_id": entry.get("id")},
            session_id=session_id,
        )

    # approve and edit both write to Tally
    if request.action in ("approve", "edit"):
        async def _terminal(message, data):
            """Persist the terminal RESULT message (success / error / duplicate)
            so it survives reload, then return the ChatResponse. In DB mode with
            a real conv_id, commits the message alongside any prior status/audit
            changes already staged on this session."""
            if settings.db_mode and db is not None and conv_id:
                await _persist_action_response(db, conv_id, message, data)
                await db.commit()
            return ChatResponse(message=message, data=data, session_id=session_id)

        # Duplicate hard block (Phase 1 Part B, Finding 1): re-derive the
        # business-key duplicate SERVER-SIDE before writing. We never trust the
        # client-sent ``status`` (bypassable, and it wrongly sticks after an edit
        # that corrects a mis-read invoice no). The check uses the entry's CURRENT
        # party_ledger + reference, so an edited/corrected reference is
        # re-evaluated: a genuine duplicate is blocked, a corrected one writes.
        # Empty reference → no business-key block (consistent with upload B2-skip).
        if settings.db_mode and db is not None and request.workspace_id:
            from backend.services.dedup import find_business_key_duplicate
            dup = await find_business_key_duplicate(
                db, client,
                workspace_id=request.workspace_id,
                party_ledger=entry.get("party_ledger") or entry.get("vendor_name"),
                invoice_ref=entry.get("reference"),
                company=company,
                doc_date=entry.get("date"),
            )
            if dup is not None:
                reason = dup.get("reason") or "duplicate"
                vno = dup.get("voucher_no")
                when = dup.get("date")
                vno_part = f" #{vno}" if vno else ""
                when_part = f" (written {when})" if when else ""
                return await _terminal(
                    (
                        f"Duplicate of voucher{vno_part}{when_part} — {reason}. "
                        "Not written."
                    ),
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )

        if not settings.TALLY_WRITE_ENABLED:
            return await _terminal(
                "Tally write is disabled. Set TALLY_WRITE_ENABLED=true to create vouchers.",
                {"type": "voucher_error", "entry_id": entry.get("id")},
            )

        # FX no-rate guard (Finding 1): a foreign-currency entry with no
        # resolvable rate has amount 0 / fx_rate 0. Block the write and tell the
        # user to set a rate via chat — never post a ₹0 voucher.
        original_currency = (entry.get("original_currency") or "INR").strip().upper()
        if original_currency and original_currency != "INR":
            try:
                fx_rate = float(entry.get("fx_rate") or 0)
            except (TypeError, ValueError):
                fx_rate = 0.0
            try:
                amount = float(entry.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if fx_rate <= 0 or amount <= 0:
                return await _terminal(
                    (
                        "Set a conversion rate before writing — "
                        "reply 'use rate <n>' in chat."
                    ),
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )

        # Party-ledger guard (Finding 3): Purchase/Sales/DN/CN need a party
        # ledger. If the company has no Sundry Creditors/Debtors the candidate
        # list is empty and party_ledger may be blank — block rather than post a
        # voucher with a missing/duplicate party leg.
        vtype = entry.get("voucher_type", "Payment")
        if vtype in ("Purchase", "Sales", "Debit Note", "Credit Note"):
            if not (entry.get("party_ledger") or "").strip():
                return await _terminal(
                    "Select a party ledger before writing.",
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )

        writer = TallyWriter(client=client, company=company)

        # Create new ledger first if the review card marked it as new.
        # The ledger to create depends on voucher_type: for party vouchers
        # (Purchase/Sales/DN/CN) the mapping describes the PARTY ledger (the
        # debit_ledger is the existing purchase/sales account — creating it would
        # re-parent live data); for Payment the mapping describes the expense
        # (debit) ledger.
        if entry.get("is_new_ledger"):
            if vtype in ("Purchase", "Sales", "Debit Note", "Credit Note"):
                ledger_name = entry.get("party_ledger") or entry.get("party_name")
                if vtype in ("Sales", "Credit Note"):
                    parent = entry.get("suggested_parent") or "Sundry Debtors"
                else:
                    parent = entry.get("suggested_parent") or "Sundry Creditors"
                # Party ledgers (Sundry Debtor/Creditor) MUST be bill-wise so the
                # receivable/payable carries a bill reference (New Ref) for future
                # "pay against bill". Expense ledgers stay non-bill-wise.
                is_billwise = True
            else:
                ledger_name = entry.get("debit_ledger")
                parent = entry.get("suggested_parent") or "Indirect Expenses"
                is_billwise = False
            if not ledger_name:
                return await _terminal(
                    "Cannot create new ledger: ledger name is empty.",
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )
            try:
                ledger_result = await writer.create_ledger(
                    name=ledger_name, parent=parent, is_billwise=is_billwise
                )
            except Exception as e:
                logger.exception("Failed to create new ledger")
                return await _terminal(
                    f"Failed to create new ledger '{ledger_name}': {e}",
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )
            if not ledger_result["success"]:
                return await _terminal(
                    (
                        f"Failed to create new ledger '{ledger_name}': "
                        f"{ledger_result.get('error_message', 'Unknown error')}"
                    ),
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )

        # Create the voucher — dispatch by voucher_type. Document-driven
        # Purchase/Sales use the ledger-only writers (party + contra + GST),
        # NOT the stock-based seeder writers of the same base name.
        voucher_type = entry.get("voucher_type", "Payment")
        try:
            gst_entries = entry.get("gst_entries") or None
            bill_ref = entry.get("bill_reference")
            reference = entry.get("reference")
            reference_date = entry.get("reference_date")
            if voucher_type == "Payment":
                result = await writer.create_payment_voucher(
                    date=entry["date"],
                    debit_ledger=entry["debit_ledger"],
                    credit_ledger=entry["credit_ledger"],
                    amount=entry["amount"],
                    narration=entry["narration"],
                    gst_entries=gst_entries,
                    reference=reference,
                    reference_date=reference_date,
                )
            elif entry.get("is_inventory") and voucher_type in ("Purchase", "Sales"):
                # Inventory (goods) invoice — write to the Tally STOCK grid.
                # Pre-flight: idempotently ensure each new line's unit, stock
                # group, and stock item exist; then post the stock-based voucher
                # with item tuples + GST mode + the Phase-1 reference + New Ref.

                # Finding 3: block when the original invoice had qty-null lines
                # that the resolver dropped — posting only the qty lines would
                # silently under-post. Tell the user which descriptions need a
                # quantity (they fill them in the review card).
                if entry.get("has_unquantified_lines"):
                    missing = entry.get("unquantified_descriptions") or []
                    names = ", ".join(f"'{d}'" for d in missing if d) or "some lines"
                    return await _terminal(
                        (
                            f"These lines need a quantity before writing: {names}. "
                            "Add quantities in the card, then write."
                        ),
                        {"type": "voucher_error", "entry_id": entry.get("id")},
                    )

                # Finding 2: block any line with qty<=0 or rate<=0 — never post a
                # zero-qty/zero-rate stock line.
                bad_line = None
                for line in (entry.get("line_items") or []):
                    try:
                        q = float(line.get("qty") or 0)
                        r = float(line.get("rate") or 0)
                    except (TypeError, ValueError):
                        q = r = 0.0
                    if q <= 0 or r <= 0:
                        bad_line = (
                            line.get("stock_name")
                            or line.get("matched_item")
                            or line.get("description")
                            or "line"
                        )
                        break
                if bad_line is not None:
                    return await _terminal(
                        (
                            f"Line '{bad_line}' is missing quantity or rate — "
                            "fix it before writing."
                        ),
                        {"type": "voucher_error", "entry_id": entry.get("id")},
                    )

                try:
                    result = await _write_inventory_voucher(
                        writer, entry, reference, reference_date,
                    )
                except RuntimeError as e:
                    # A master-create failed/timed out (e.g. a blocking modal):
                    # return a clean voucher_error instead of hanging or 500ing.
                    logger.error("Inventory master pre-flight failed: %s", e)
                    return await _terminal(
                        str(e),
                        {"type": "voucher_error", "entry_id": entry.get("id")},
                    )
            elif voucher_type == "Purchase":
                result = await writer.create_purchase_voucher_ledger(
                    date=entry["date"],
                    party_ledger=entry["party_ledger"],
                    purchase_ledger=entry["debit_ledger"],
                    amount=entry["amount"],
                    narration=entry["narration"],
                    gst_entries=gst_entries,
                    bill_ref=bill_ref,
                    reference=reference,
                    reference_date=reference_date,
                )
            elif voucher_type == "Sales":
                result = await writer.create_sales_voucher_ledger(
                    date=entry["date"],
                    party_ledger=entry["party_ledger"],
                    sales_ledger=entry["credit_ledger"],
                    amount=entry["amount"],
                    narration=entry["narration"],
                    gst_entries=gst_entries,
                    bill_ref=bill_ref,
                    reference=reference,
                    reference_date=reference_date,
                )
            elif voucher_type == "Debit Note":
                # DN (purchase return): party on DEBIT, purchase-returns contra
                # on CREDIT. The party comes from party_ledger; the contra is the
                # OTHER leg (credit_ledger). See logs/manual_test_group_b_live.log.
                result = await writer.create_debit_note(
                    date=entry["date"],
                    party_ledger=entry["party_ledger"],
                    purchase_ledger=entry["credit_ledger"],
                    amount=entry["amount"],
                    narration=entry["narration"],
                    gst_entries=gst_entries,
                    bill_ref=bill_ref,
                    reference=reference,
                    reference_date=reference_date,
                )
            elif voucher_type == "Credit Note":
                # CN (sales return): party on CREDIT, sales-returns contra on
                # DEBIT. The party comes from party_ledger; the contra is the
                # OTHER leg (debit_ledger).
                result = await writer.create_credit_note(
                    date=entry["date"],
                    party_ledger=entry["party_ledger"],
                    sales_ledger=entry["debit_ledger"],
                    amount=entry["amount"],
                    narration=entry["narration"],
                    gst_entries=gst_entries,
                    bill_ref=bill_ref,
                    reference=reference,
                    reference_date=reference_date,
                )
            else:
                return await _terminal(
                    f"Unknown voucher type: {voucher_type}",
                    {"type": "voucher_error", "entry_id": entry.get("id")},
                )
        except ValidationError as e:
            return await _terminal(
                f"Validation failed: {'; '.join(e.errors)}",
                {"type": "voucher_error", "entry_id": entry.get("id")},
            )
        except KeyError as e:
            return await _terminal(
                f"Voucher entry is missing required field: {e}",
                {"type": "voucher_error", "entry_id": entry.get("id")},
            )

        if result["success"]:
            vch_id = result.get("last_vch_id") or ""
            success_message = (
                f"{voucher_type} voucher written to Tally successfully. "
                f"Voucher ID: {vch_id}"
            )
            success_data = {
                "type": "voucher_written",
                "entry_id": entry.get("id"),
                "tally_voucher_id": vch_id,
            }

            # Persist audit trail (DB mode only) — VoucherEntry linked to the
            # file. Requires a real conversation_id (non-nullable FK); the
            # frontend threads it through the request body. If absent (legacy
            # entries / smoke tests), skip the audit row rather than crash.
            if settings.db_mode and db is not None and conv_id:
                from backend.db.models import VoucherEntry as VoucherEntryDB
                ve = VoucherEntryDB(
                    file_id=entry.get("file_id") or None,
                    user_id=user_id,
                    workspace_id=request.workspace_id,
                    conversation_id=conv_id,
                    voucher_type=voucher_type,
                    voucher_data=entry,
                    status="written",
                    tally_response=result,
                    tally_voucher_number=str(vch_id),
                )
                db.add(ve)
                await _record_voucher_revision(
                    db,
                    workspace_id=request.workspace_id,
                    conversation_id=conv_id,
                    entry_id=entry.get("id"),
                    source="write",
                    voucher_data=entry,
                    file_id=entry.get("file_id"),
                )
                await _update_persisted_voucher_status(
                    db, conv_id, entry.get("id"), "written",
                )
                # Persist the success RESULT message in the SAME commit as the
                # status/audit changes so it survives reload and sorts after the
                # review card (per-row created_at default).
                await _persist_action_response(
                    db, conv_id, success_message, success_data,
                )
                await db.commit()

            return ChatResponse(
                message=success_message,
                data=success_data,
                session_id=session_id,
            )
        else:
            return await _terminal(
                f"Failed to write to Tally: {result.get('error_message', 'Unknown error')}",
                {"type": "voucher_error", "entry_id": entry.get("id")},
            )

    # Should be unreachable thanks to Literal[...] on VoucherActionRequest.action
    raise HTTPException(status_code=400, detail=f"Unknown action: {request.action}")


async def _chat_legacy_mode(
    request: ChatRequest,
    client: TallyClient,
    session_store: SessionStore,
) -> ChatResponse:
    """Original in-memory session flow — used when DATABASE_URL is not set."""
    session = session_store.get_or_create(
        session_id=request.session_id,
        company=request.company,
    )
    session_id = request.session_id or session.session_id

    with _tracer.start_as_current_span(
        "chat",
        attributes={
            "langfuse.session.id": session_id,
            "langfuse.user.id": session_id,
            "langfuse.trace.name": "chat",
            "user.query": request.message,
        },
    ) as span:
        if request.company:
            span.set_attribute("langfuse.trace.metadata.company", request.company)

        orchestrator = Orchestrator()
        result = await orchestrator.process_query(request.message, client, session)

        chart = None
        if result.get("chart"):
            chart = ChartSpec(**result["chart"])

        return ChatResponse(
            message=result["message"],
            data=result.get("data"),
            chart=chart,
            session_id=result["session_id"],
        )


async def _chat_db_mode(
    request: ChatRequest,
    client: TallyClient,
    user_id: str,
    db: AsyncSession | None = None,
) -> ChatResponse:
    """DB-backed conversation flow — used when DATABASE_URL is set."""
    from backend.agents.registry import get_agent
    from backend.db.models import Conversation, Message, UsageLog, Workspace
    from backend.utils.pricing import compute_cost

    if not request.workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required in DB mode")
    if not request.conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required in DB mode")
    if db is None:
        raise HTTPException(status_code=500, detail="Database session not available")

    # Load workspace
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == request.workspace_id,
            Workspace.user_id == user_id,
            Workspace.is_deleted.is_(False),
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Load conversation
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == request.conversation_id,
            Conversation.workspace_id == request.workspace_id,
            Conversation.user_id == user_id,
            Conversation.is_deleted.is_(False),
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Load prior messages from DB
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == request.conversation_id)
        .order_by(Message.created_at)
    )
    prior_messages = [
        {"role": m.role, "content": m.content}
        for m in result.scalars().all()
    ]

    # Persist user message
    user_msg = Message(
        conversation_id=request.conversation_id,
        role="user",
        content=request.message,
    )
    db.add(user_msg)
    await db.flush()

    # --- FX rate-override fast path (T5) ---
    # If a voucher entry is pending and this message is a rate override, recompute
    # the entry deterministically from its ORIGINAL foreign amounts and return an
    # updated review card. Never touches the Tally query agent.
    from backend.services.fx import (
        FxRecomputeError,
        classify_pending_message,
        recompute_entry_with_rate,
    )

    action, new_rate = classify_pending_message(request.pending_entry, request.message)

    # Try the recompute up front. If it can't run (e.g. the original foreign
    # amount is unknown), downgrade to a clarification rather than a silent ₹0
    # override (Finding 2).
    updated = None
    clarify_msg = (
        "I can update the conversion rate — what rate should I use? "
        "Reply with e.g. \"use rate 90\"."
    )
    if action == "override":
        try:
            updated = recompute_entry_with_rate(request.pending_entry, new_rate)
        except FxRecomputeError as e:
            action = "clarify"
            clarify_msg = (
                f"{e} Please re-upload the document so I can read the original "
                "amount and currency."
            )

    if action == "clarify":
        msg_text = clarify_msg
        assistant_msg = Message(
            conversation_id=request.conversation_id, role="assistant", content=msg_text,
        )
        db.add(assistant_msg)
        if conversation.title is None:
            conversation.title = request.message[:100]
        conversation.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return ChatResponse(message=msg_text, data=None, chart=None, session_id=str(conversation.id))

    if action == "override":
        review_data = {
            "type": "voucher_review",
            "entries": [updated],
        }
        # Re-include ledger lists if the pending entry already carried them
        # (frontend keeps its prior lists otherwise — see T5 note).
        for key in ("available_ledgers", "available_payment_ledgers"):
            if request.pending_entry.get(key):
                review_data[key] = request.pending_entry[key]
        rate_disp = (f"{new_rate:g}")
        msg_text = (
            f"Updated the conversion rate to {rate_disp}. "
            f"New amount: {format_inr(updated['amount'])}. "
            f"Review and click Write to Tally."
        )
        assistant_msg = Message(
            conversation_id=request.conversation_id,
            role="assistant",
            content=msg_text,
            data=review_data,
        )
        db.add(assistant_msg)
        if conversation.title is None:
            conversation.title = request.message[:100]
        conversation.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return ChatResponse(
            message=msg_text, data=review_data, chart=None, session_id=str(conversation.id),
        )

    # Build session context for orchestrator (bridge between DB and in-memory)
    session = SessionContext(
        session_id=str(conversation.id),
        company=workspace.name,
        max_messages=20,
    )
    for msg in prior_messages[-20:]:
        session.add_message(msg["role"], msg["content"])

    # Configure TallyClient from workspace
    ws_config = workspace.config or {}
    if ws_config.get("tally_host"):
        client = TallyClient(
            ws_config["tally_host"],
            ws_config.get("tally_port", 9000),
        )
        if ws_config.get("mock_mode"):
            client.mock_mode = True

    # Trace with Langfuse
    with _tracer.start_as_current_span(
        "chat",
        attributes={
            "langfuse.session.id": str(conversation.id),
            "langfuse.user.id": user_id,
            "langfuse.trace.name": "chat",
            "user.query": request.message,
            "workspace_id": str(workspace.id),
            "workspace_name": workspace.name,
            "agent_type": workspace.agent_type,
            "conversation_id": str(conversation.id),
        },
    ):
        start_time = time_module.time()

        # S1: Wire agent registry to chat — use workspace.agent_type
        agent_class = get_agent(workspace.agent_type)
        agent = agent_class()
        result = await agent.process_query(request.message, client, session)

        latency_ms = int((time_module.time() - start_time) * 1000)

    chart = None
    if result.get("chart"):
        chart = ChartSpec(**result["chart"])

    # Persist assistant message
    assistant_msg = Message(
        conversation_id=request.conversation_id,
        role="assistant",
        content=result["message"],
        data=result.get("data"),
        chart=result.get("chart"),
    )
    db.add(assistant_msg)
    await db.flush()

    # Log usage
    agent_calls = result.get("usage", [])
    if agent_calls:
        total_cost = compute_cost(agent_calls)
        usage_log = UsageLog(
            message_id=assistant_msg.id,
            user_id=user_id,
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            total_input_tokens=sum(c.get("input_tokens", 0) for c in agent_calls),
            total_output_tokens=sum(c.get("output_tokens", 0) for c in agent_calls),
            total_cost_usd=total_cost,
            model_primary=workspace.config.get("model", settings.CLAUDE_MODEL),
            latency_ms=latency_ms,
            agent_calls=agent_calls,
        )
        db.add(usage_log)

    # Auto-generate title from first message
    if conversation.title is None:
        conversation.title = request.message[:100]

    # S6: Bump conversation.updated_at on new message
    conversation.updated_at = datetime.now(timezone.utc)

    await db.commit()

    return ChatResponse(
        message=result["message"],
        data=result.get("data"),
        chart=chart,
        session_id=str(conversation.id),
    )
