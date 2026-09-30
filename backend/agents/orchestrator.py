"""Orchestrator — classifies user queries and routes to the appropriate agent.

The Orchestrator is the main entry point for the agent pipeline.  It uses
Claude to classify the user's natural-language query into a structured type
(e.g. simple_lookup, comparison, greeting) and then routes to the QueryAgent
for data fetching.

Exports:
    Orchestrator      — The main orchestrator class.
    anthropic_client  — Module-level AsyncAnthropic instance (patched in tests).
"""

from __future__ import annotations

import json
import re
import logging
from datetime import date
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

from backend.config import settings
from backend.agents.base import BaseAgent
from backend.agents.prompts import build_orchestrator_prompt
from backend.agents.query_agent import QueryAgent
from backend.agents.analysis_agent import AnalysisAgent
from backend.agents.chart_agent import ChartAgent
from backend.agents.context import SessionContext
from backend.agents.chart_advisor import get_chart_advice
from backend.agents.utils import parse_all_markdown_tables
from backend.tally_bridge.client import TallyClient
from backend.utils.date_utils import format_for_tally

# Patterns indicating user wants table-only output (no chart)
_TABLE_INTENT_PATTERNS = (
    "show as a table", "as a table", "in table format",
    "table only", "just a table", "only table", "no chart",
)


def _has_table_intent(user_message: str) -> bool:
    """Return True if the user message explicitly requests table-only output."""
    lower = user_message.lower()
    return any(p in lower for p in _TABLE_INTENT_PATTERNS)

# Module-level client — tests patch this object.
anthropic_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

GREETING_RESPONSE = (
    "Hello! I'm your TallyPrime assistant. Ask me about your accounting data "
    "— balances, sales, outstanding bills, profit & loss, and more."
)


class Orchestrator(BaseAgent):
    """Classifies user queries and routes to the appropriate agent.

    Flow:
        1. Classify the user's query via Claude.
        2. Route based on classification:
           - greeting -> canned greeting
           - clarification_needed -> return the clarification question
           - anything else -> QueryAgent
    """

    def __init__(self) -> None:
        self.query_agent = QueryAgent()
        self.analysis_agent = AnalysisAgent()
        self.chart_agent = ChartAgent()

    # --- BaseAgent interface implementation (with backward compatibility) ---
    async def process_query(
        self,
        message: str,
        workspace_config: dict[str, Any] | Any | None = None,
        workspace_memory: dict[str, Any] | Any | None = None,
        conversation_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Process a query with support for both signatures.

        Can be called as:
        1. BaseAgent interface: process_query(message, workspace_config, workspace_memory, conversation_messages, **kwargs)
        2. Legacy interface: process_query(user_message, client, session)

        Args:
            message: The user's query text.
            workspace_config: Either a dict (BaseAgent mode) or TallyClient (legacy mode).
            workspace_memory: Either a dict (BaseAgent mode) or SessionContext (legacy mode).
            conversation_messages: List of dicts (BaseAgent mode) or None (legacy mode).
            **kwargs: Additional params.

        Returns:
            Response dict with keys: query_type, message, data, chart, session_id.
        """
        # Detect which signature was used by checking if workspace_config has async methods (TallyClient-like)
        # or if it's a dict (BaseAgent mode)
        if workspace_config is not None and not isinstance(workspace_config, dict) and isinstance(workspace_memory, SessionContext):
            # Legacy mode: process_query(user_message, client, session)
            client = workspace_config
            session = workspace_memory
            return await self._execute_internal(message, client, session)

        # BaseAgent mode: process_query(message, workspace_config, workspace_memory, conversation_messages, **kwargs)
        if not isinstance(workspace_config, dict):
            workspace_config = {}
        if workspace_memory is None:
            workspace_memory = {}
        if conversation_messages is None:
            conversation_messages = []

        # Extract Tally client from kwargs or create one from config
        client = kwargs.get("client")
        if client is None:
            from backend.tally_bridge.client import TallyClient
            host = workspace_config.get("TALLY_HOST", "localhost")
            port = workspace_config.get("TALLY_PORT", 9000)
            client = TallyClient(host=host, port=port)

        # Reconstruct session from conversation_messages or create new one
        session = SessionContext(company=workspace_config.get("company", ""))
        for msg in conversation_messages:
            session.add_message(msg.get("role", "user"), msg.get("content", ""))

        # Delegate to the internal execution method
        return await self._execute_internal(message, client, session)

    async def process_file_upload(
        self,
        file_path: str,
        filename: str,
        mime_type: str,
        user_message: str,
        client,  # TallyClient
        session,  # SessionContext
        file_id: str,
        db=None,  # AsyncSession | None (DB mode only)
        user_id: str | None = None,
        workspace_id: str | None = None,
        conversation_id: str | None = None,
        company: str | None = None,
    ) -> dict:
        """Process an uploaded file for data entry.

        Pipeline: parse document → fetch Tally ledgers → route by doc_type →
        build the matching voucher → return review card data. For Debit/Credit
        Notes, the party's prior Purchase/Sales vouchers are fetched to offer an
        "Against Invoice" reference.

        The actual Tally write happens later when the user clicks "Write to Tally"
        (via /chat/voucher-action endpoint).

        In DB mode (``db`` provided) an ``UploadedFile`` audit row is persisted and
        its id becomes the review card's ``file_id``.

        Returns a dict with keys: message, data (ChatResponse-compatible).
        """
        import base64
        import os
        import uuid as uuid_mod

        from backend.services.document_parser import (
            build_vision_prompt,
            detect_file_type,
            parse_vision_response,
            validate_extracted_amounts,
        )
        from backend.services.ledger_mapper import LedgerMapper
        from backend.services.voucher_builder import (
            build_credit_note_data,
            build_debit_note_data,
            build_payment_voucher_data,
            build_purchase_voucher_data,
            build_sales_voucher_data,
        )
        from backend.tally_bridge.queries.vouchers import get_party_vouchers
        from backend.tally_bridge.request_builder import (
            build_list_groups,
            build_list_ledgers,
        )
        from backend.tally_bridge.response_parser import (
            parse_groups,
            parse_ledger_list,
        )

        # 1. Parse the document
        file_type = detect_file_type(filename, mime_type)
        if file_type != "vision":
            return {
                "message": (
                    f"File type '{file_type}' not yet supported in B1a. "
                    "Please upload an image or PDF of an expense receipt."
                ),
                "data": None,
            }

        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        image_data = base64.b64encode(raw_bytes).decode("utf-8")

        # Map MIME type for Claude Vision
        media_type = mime_type or "image/jpeg"
        if media_type == "image/heic":
            media_type = "image/jpeg"  # Claude doesn't support HEIC directly

        if media_type == "application/pdf":
            # PDF support via document block
            content_block = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": image_data,
                },
            }
        else:
            content_block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            }

        response = await anthropic_client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    content_block,
                    {"type": "text", "text": build_vision_prompt(company)},
                ],
            }],
        )
        extracted_text = response.content[0].text
        try:
            extracted = parse_vision_response(extracted_text)
        except Exception as e:
            return {
                "message": f"Could not parse document: {e}. Raw response:\n{extracted_text[:500]}",
                "data": None,
            }

        # 2. Validate amounts (warnings, not blocking)
        warnings = validate_extracted_amounts(extracted)

        # 2b. Persist the uploaded-file audit row (DB mode only). The DB id then
        # becomes the review card's file_id so a later write can link back.
        from backend.services.dedup import sha256_bytes
        content_hash = sha256_bytes(raw_bytes)
        if db is not None and user_id and workspace_id and conversation_id:
            from backend.db.models import UploadedFile
            uploaded = UploadedFile(
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                filename=filename,
                mime_type=mime_type or "application/octet-stream",
                file_size=os.path.getsize(file_path),
                storage_path=file_path,
                extracted_data=_extracted_to_jsonable(extracted),
                content_hash=content_hash,
                status="extracted",
            )
            db.add(uploaded)
            await db.flush()
            file_id = str(uploaded.id)

        # 3. Fetch Tally ledgers for mapping
        ledger_xml = await client.post_xml(build_list_ledgers())
        tally_ledgers = parse_ledger_list(ledger_xml)
        ledger_names = [l["name"] for l in tally_ledgers]

        # Fetch the Tally group tree so ledger selection can be HIERARCHY-AWARE:
        # real party ledgers commonly sit under custom sub-groups of Sundry
        # Creditors/Debtors (e.g. "National Creditors" → "Sundry Creditors").
        # An exact parent_group match would exclude those parties → they get
        # wrongly flagged is_new_ledger and blocked on write. Build a
        # child→parent map (lowercased) and walk lineage to a requested root.
        # Defensive: any failure / empty read falls back to exact-match.
        group_parent: dict[str, str] = {}
        try:
            tally_groups = parse_groups(await client.post_xml(build_list_groups()))
            for g in tally_groups:
                name = (g.get("name") or "").strip().lower()
                parent = (g.get("parent") or "").strip().lower()
                if name:
                    group_parent[name] = parent
        except Exception:
            group_parent = {}

        def _roots_to(group: str, wanted: set[str]) -> bool:
            """True if ``group`` is one of ``wanted`` or its lineage reaches one."""
            cur = (group or "").lower()
            seen: set[str] = set()
            while cur and cur not in seen:
                if cur in wanted:
                    return True
                seen.add(cur)
                cur = group_parent.get(cur, "")
            return False

        def _ledgers_in(*groups: str) -> list[str]:
            wanted = {g.lower() for g in groups}
            return [
                l["name"] for l in tally_ledgers
                if _roots_to(l.get("parent_group", ""), wanted)
            ]

        payment_ledgers = _ledgers_in("cash-in-hand", "bank accounts", "bank occ a/c")
        if not payment_ledgers:
            payment_ledgers = ["Cash"]
        supplier_ledgers = _ledgers_in("sundry creditors")
        customer_ledgers = _ledgers_in("sundry debtors")
        purchase_ledgers = _ledgers_in("purchase accounts") or ["Purchase Accounts"]
        sales_ledgers = _ledgers_in("sales accounts") or ["Sales Accounts"]

        # 4. Route by doc_type → build the matching voucher.
        mapper = LedgerMapper()
        doc_type = extracted.doc_type
        party_vouchers_list: list[dict] = []

        # Resolve GST ledgers from the workspace's Tally ledgers (Duties & Taxes)
        # so a GST invoice posts the Input/Output GST leg separately rather than
        # folding it into the contra. Missing ledgers warn + fall back to no leg.
        gst_warnings: list[str] = []

        # Track the contra (purchase/sales) ledger chosen for goods invoices so
        # the inventory path can default each line's ledger to it.
        contra_ledger: str | None = None
        if doc_type in ("purchase", "debit_note"):
            party_name = extracted.party_name or "Unknown Supplier"
            mapping = await mapper.find_mapping(
                party_name, doc_type, tally_ledgers=supplier_ledgers or ledger_names,
            )
            purchase_ledger = purchase_ledgers[0]
            contra_ledger = purchase_ledger
            gst_ledgers, gst_warnings = _resolve_gst_ledgers(
                tally_ledgers, "input", extracted,
            )
            if doc_type == "debit_note":
                party_vouchers_list = await get_party_vouchers(
                    client, party_name, ["Purchase"],
                    anchor_date=extracted.date,
                )
                voucher = build_debit_note_data(
                    doc=extracted, party_ledger=party_name,
                    purchase_ledger=purchase_ledger,
                    gst_ledgers=gst_ledgers,
                    original_ref=extracted.original_invoice_ref,
                )
            else:
                voucher = build_purchase_voucher_data(
                    doc=extracted, party_ledger=party_name,
                    purchase_ledger=purchase_ledger,
                    gst_ledgers=gst_ledgers,
                )
        elif doc_type in ("sales", "credit_note"):
            party_name = extracted.party_name or "Unknown Customer"
            mapping = await mapper.find_mapping(
                party_name, doc_type, tally_ledgers=customer_ledgers or ledger_names,
            )
            sales_ledger = sales_ledgers[0]
            contra_ledger = sales_ledger
            gst_ledgers, gst_warnings = _resolve_gst_ledgers(
                tally_ledgers, "output", extracted,
            )
            if doc_type == "credit_note":
                party_vouchers_list = await get_party_vouchers(
                    client, party_name, ["Sales"],
                    anchor_date=extracted.date,
                )
                voucher = build_credit_note_data(
                    doc=extracted, party_ledger=party_name,
                    sales_ledger=sales_ledger,
                    gst_ledgers=gst_ledgers,
                    original_ref=extracted.original_invoice_ref,
                )
            else:
                voucher = build_sales_voucher_data(
                    doc=extracted, party_ledger=party_name,
                    sales_ledger=sales_ledger,
                    gst_ledgers=gst_ledgers,
                )
        else:
            # payment (and any unknown type) → expense + payment ledger.
            mapping = await mapper.find_mapping(
                extracted.party_name or extracted.vendor_name or "Expense",
                "Payment", tally_ledgers=ledger_names,
            )
            voucher = build_payment_voucher_data(
                doc=extracted, expense_ledger=mapping.ledger_name,
                payment_ledger=payment_ledgers[0],
            )

        # 4b. "Against Invoice" options for DN/CN — surface each prior voucher's
        # number/date/amount for the edit-form dropdown (Task 13).
        against_invoice_options = [
            {
                "voucher_number": v.get("voucher_number") or v.get("reference") or "",
                "date": v.get("date", ""),
                "amount": v.get("amount"),
                "reference": v.get("reference") or v.get("voucher_number") or "",
            }
            for v in party_vouchers_list
        ]

        # 4c. Surface any GST-ledger-not-found warnings on the review entry.
        if gst_warnings:
            warnings = warnings + gst_warnings

        # 5. FX rate warnings (T6). Non-INR docs whose rate came from a default
        # or fallback need a "verify" warning; a missing rate blocks the write.
        cur = voucher.original_currency.strip().upper()
        if voucher.rate_source in ("default", "fallback"):
            warnings = warnings + [
                f"Used default {cur}→INR rate {voucher.fx_rate} — "
                f"verify or reply 'use rate <n>'."
            ]
        elif voucher.rate_source == "none":
            warnings = warnings + [
                f"No conversion rate for {cur} — reply 'use rate <n>' to set it "
                f"(entry can't be written yet)."
            ]

        # 5b. Supplier invoice no + date (Phase 1 Part A). The reference is the
        # document's OWN invoice number (invoice_number), for ALL voucher types.
        # For DN/CN the against-bill reference (bill_reference) comes separately
        # from original_invoice_ref (handled in the voucher builders above).
        # reference_date is the doc date as YYYYMMDD (Tally import format).
        # Only convert a YYYY-MM-DD doc date.
        reference = extracted.invoice_number or None
        reference_date: str | None = None
        if extracted.date:
            digits = extracted.date.replace("-", "")
            if len(digits) == 8 and digits.isdigit():
                reference_date = digits

        # 5c. Duplicate detection (Phase 1 Part B). Hard block on upload — runs
        # only in DB mode (needs the persisted UploadedFile + VoucherEntry rows).
        # No invoice number → B2 skipped (B1 still applies); add a soft note.
        duplicate_of: dict | None = None
        dedup_party = voucher.party_ledger or extracted.party_name or extracted.vendor_name
        if db is not None and workspace_id:
            from backend.services.dedup import find_duplicate
            duplicate_of = await find_duplicate(
                db, client,
                workspace_id=workspace_id,
                content_hash=content_hash,
                party_ledger=dedup_party,
                invoice_ref=reference,
                company=getattr(session, "company", None),
                exclude_file_id=file_id,
                doc_date=extracted.date,
            )
        if not reference:
            warnings = warnings + [
                "No invoice number found — duplicate check limited to exact file."
            ]

        # 5d. Goods detection (inventory Phase 2). For Purchase/Sales, if ANY
        # line item carries a quantity, the document is itemised goods → resolve
        # each line against the company's existing stock items and surface the
        # inventory fields on the entry. The accounting-only voucher (amount/GST
        # /party leg) computed above is preserved; the inventory path layers on
        # top. No-qty docs (services/expenses) keep the accounting-only path.
        is_inventory = False
        resolved_lines: list[dict] = []
        available_stock_items: list[str] = []
        unquantified_descriptions: list[str] = []
        default_stock_group = settings.DEFAULT_STOCK_GROUP
        if doc_type in ("purchase", "sales"):
            has_qty = any(
                li.quantity is not None for li in (extracted.line_items or [])
            )
            if has_qty:
                from backend.services import stock_resolver
                from backend.services.stock_resolver import (
                    dropped_unquantified_descriptions,
                    resolve_line_items,
                )

                stock_items = await stock_resolver.list_stock_items(client)
                available_stock_items = [li.name for li in stock_items]
                resolved_lines = await resolve_line_items(
                    client, extracted,
                    direction=doc_type,
                    default_group=default_stock_group,
                )
                # Default each line's posting ledger to the chosen contra ledger.
                for line in resolved_lines:
                    line["ledger"] = contra_ledger
                is_inventory = bool(resolved_lines)
                # Finding 3: record any qty-null lines the resolver dropped. A
                # mixed invoice (some lines without a qty) would under-post the
                # stock grid; voucher_action blocks the write until the user
                # adds quantities for these descriptions.
                unquantified_descriptions = dropped_unquantified_descriptions(
                    extracted
                )

        # 6. Assemble review card response
        entry_id = str(uuid_mod.uuid4())
        review_data = {
            "type": "voucher_review",
            "file_id": file_id,
            "entries": [{
                "id": entry_id,
                # Hard-link to the original upload: file_id is the uploaded_files
                # row id (reassigned to str(uploaded.id) in DB mode above). MUST
                # live on the entry itself — not just review_data — so the live
                # card, the persisted Message entry, the VoucherEntry audit row
                # and the version trail are all joinable to uploaded_files.id.
                "file_id": file_id,
                "voucher_type": voucher.voucher_type,
                "date": voucher.date,
                "vendor_name": extracted.party_name or extracted.vendor_name,
                "party_name": extracted.party_name or extracted.vendor_name,
                "amount": float(voucher.amount),
                "debit_ledger": voucher.debit_ledger,
                "credit_ledger": voucher.credit_ledger,
                "narration": voucher.narration,
                "gst_entries": voucher.gst_entries,
                "reference": reference,
                "reference_date": reference_date,
                "duplicate_of": duplicate_of,
                "status": "duplicate" if duplicate_of else "draft",
                "warnings": warnings,
                "is_new_ledger": mapping.is_new_ledger,
                "suggested_parent": mapping.suggested_parent,
                # Group B additions
                "party_ledger": voucher.party_ledger,
                "is_party_ledger": voucher.is_party_ledger,
                "bill_reference": voucher.bill_reference,
                "bill_type": voucher.bill_type,
                "against_invoice_options": against_invoice_options,
                "party_vouchers": party_vouchers_list,
                # FX fields (T6) — originals are the foreign-currency amounts so a
                # later chat rate-override can recompute exactly (incl. from a
                # no-rate state). amount/gst_entries above are already INR.
                "original_currency": voucher.original_currency,
                "original_amount": float(voucher.original_amount),
                "fx_rate": float(voucher.fx_rate),
                "inr_amount": float(voucher.amount),
                "original_gst_entries": voucher.original_gst_entries,
                # Inventory line items (Phase 2). is_inventory gates the
                # stock-grid write in voucher_action; non-goods entries omit
                # these (accounting-only path).
                "is_inventory": is_inventory,
                "line_items": resolved_lines,
                "available_stock_items": available_stock_items,
                "default_stock_group": default_stock_group,
                "has_unquantified_lines": bool(unquantified_descriptions),
                "unquantified_descriptions": unquantified_descriptions,
            }],
            "available_ledgers": ledger_names,
            "available_payment_ledgers": payment_ledgers,
            "available_supplier_ledgers": supplier_ledgers,
            "available_customer_ledgers": customer_ledgers,
        }

        type_labels = {
            "Payment": "payment", "Purchase": "purchase invoice",
            "Sales": "sales invoice", "Debit Note": "debit note",
            "Credit Note": "credit note",
        }
        type_label = type_labels.get(voucher.voucher_type, "entry")
        party_display = extracted.party_name or extracted.vendor_name or "Unknown"

        message = (
            f"I've extracted the {type_label} details for **{party_display}**.\n\n"
            f"Please review the entry below and click **Write to Tally** to create "
            f"the {voucher.voucher_type.lower()} voucher, or **Edit Entry** to make corrections."
        )
        if duplicate_of:
            vno = duplicate_of.get("voucher_no")
            dref = f" (voucher #{vno})" if vno else ""
            message += (
                f"\n\n🚫 **Duplicate detected — not written.** "
                f"{duplicate_of.get('reason', 'duplicate')}{dref}. "
                f"This entry is blocked; you can discard it or edit the invoice "
                f"number if it was mis-read."
            )
        if warnings:
            message += "\n\n⚠️ " + " | ".join(warnings)

        return {"message": message, "data": review_data}

    async def _execute_internal(
        self,
        user_message: str,
        client: TallyClient,
        session: SessionContext,
    ) -> dict[str, Any]:
        """Internal query execution (called by process_query).

        Returns:
            {
                "query_type": str,
                "message": str,
                "data": dict | None,
                "chart": None,
                "session_id": str,
                "usage": list[dict],
            }
        """
        usage_records: list[dict[str, Any]] = []

        classification = await self._classify(user_message, session)
        query_type = classification.get("query_type", "simple_lookup")
        requires_chart = classification.get("requires_chart", False)

        # Capture classifier usage
        if "usage" in classification:
            usage_records.append(classification["usage"])

        # Respect explicit table-only intent from user message
        if _has_table_intent(user_message):
            requires_chart = False
            logger.info("Orchestrator — user requested table-only, suppressing chart")
        elif not requires_chart and query_type in ("trend", "comparison", "top_n", "aggregation"):
            requires_chart = True
            logger.info("Orchestrator — auto-enabling chart for query_type=%s", query_type)

        logger.info(
            "Orchestrator — classified %r as query_type=%s, requires_chart=%s",
            user_message[:80], query_type, requires_chart,
        )

        # --- Greeting ---
        if query_type == "greeting":
            session.add_message("user", user_message)
            session.add_message("assistant", GREETING_RESPONSE)
            return {
                "query_type": "greeting",
                "message": GREETING_RESPONSE,
                "data": None,
                "chart": None,
                "session_id": session.session_id,
                "usage": usage_records,
            }

        # --- Clarification needed ---
        if query_type == "clarification_needed":
            clarification = classification.get(
                "clarification_question",
                "Could you please provide more details about what you'd like to know?",
            )
            session.add_message("user", user_message)
            session.add_message("assistant", clarification)
            return {
                "query_type": "clarification_needed",
                "message": clarification,
                "data": None,
                "chart": None,
                "session_id": session.session_id,
                "usage": usage_records,
            }

        # --- All other types: route to QueryAgent ---
        logger.info("Orchestrator — routing to QueryAgent")
        agent_result = await self.query_agent.execute(user_message, client, session)
        tool_results = agent_result.get("tool_results", [])
        all_data = _extract_all_data(tool_results)
        raw_data = all_data[-1] if all_data else None
        raw_tally_data, computed_data = _separate_tool_results(tool_results)

        # Capture QueryAgent usage
        if agent_result.get("usage"):
            usage_records.extend(agent_result["usage"])

        logger.info(
            "Orchestrator — QueryAgent returned %d tool call(s), %d data set(s) "
            "(%d raw, %d computed)",
            len(tool_results), len(all_data), len(raw_tally_data), len(computed_data),
        )

        # --- Analysis Agent (unconditional for all non-greeting/non-clarification queries) ---
        # NOTE: Session messages are added AFTER AnalysisAgent runs, not before.
        # QueryAgent no longer touches session — this prevents the AnalysisAgent from
        # seeing the current turn's user query + QueryAgent text as a "prior turn",
        # which caused it to say "same question as prior turn" or reconcile against
        # QueryAgent's incorrect intermediate numbers.
        message = agent_result["message"]
        data = raw_data

        logger.info("Orchestrator — routing to AnalysisAgent (query_type=%s)", query_type)
        analysis_result = await self.analysis_agent.execute(
            raw_tally_data, computed_data, user_message, query_type,
            session=session,
        )
        message = analysis_result["message"]
        data = analysis_result.get("data", raw_data)

        # Capture AnalysisAgent usage
        if analysis_result.get("usage"):
            usage_records.extend(analysis_result["usage"])

        logger.info(
            "Orchestrator — AnalysisAgent returned %d tool call(s), chart_suggestion=%s",
            len(analysis_result.get("tool_results", [])),
            analysis_result.get("chart_suggestion"),
        )

        # Add the final user query + analysis result to session AFTER both agents complete.
        session.add_message("user", user_message)
        session.add_message("assistant", message)

        # --- Chart Agent (when chart is needed) ---
        chart = None
        if settings.CHARTS_ENABLED and requires_chart:
            message_text = analysis_result.get("message", "")
            chart_suggestion = analysis_result.get("chart_suggestion")
            chart_title = analysis_result.get("chart_title")

            if chart_suggestion != "table_only":
                all_tables = parse_all_markdown_tables(message_text)
                logger.info("Chart pipeline — found %d tables, suggestion=%s", len(all_tables), chart_suggestion)

                if all_tables:
                    # Try Haiku chart advisor first
                    advice, advisor_usage = await get_chart_advice(
                        all_tables,
                        user_message,
                        chart_suggestion=chart_suggestion,
                    )
                    if advisor_usage:
                        usage_records.append(advisor_usage)
                    logger.info("Chart advisor returned: %s", advice)

                    if advice and advice.get("chart_type") != "table_only":
                        # Use advisor's table and column selections
                        selected_table = all_tables[advice["table_index"]]

                        # Filter table to only advisor-selected columns
                        filtered = _filter_table_by_advice(selected_table, advice)
                        logger.info(
                            "Chart filter — headers=%s, rows=%d",
                            filtered["headers"] if filtered else None,
                            len(filtered["rows"]) if filtered else 0,
                        )
                        if filtered:
                            chart = self.chart_agent.execute(
                                filtered,
                                query_type,
                                chart_suggestion=advice.get("chart_type", chart_suggestion),
                                chart_title=advice.get("chart_title", chart_title),
                            )

                    if chart is None:
                        logger.info("Chart pipeline — no chart produced (advisor=%s)", "failed" if advice is None else "no suitable columns")
            else:
                logger.info("Chart pipeline — skipped (chart_suggestion=table_only)")

        # Final data from analysis result
        final_data = data

        return {
            "query_type": query_type,
            "message": message,
            "data": final_data,
            "chart": chart,
            "session_id": session.session_id,
            "usage": usage_records,
        }

    async def _classify(self, user_message: str, session: SessionContext | None = None) -> dict:
        """Use Claude to classify the user's query into a structured type.

        Returns a dict with at least ``query_type``.  Falls back to
        ``{"query_type": "simple_lookup", "requires_chart": False}`` when
        the response cannot be parsed as JSON.
        """
        current_date = format_for_tally(date.today())
        system_prompt = build_orchestrator_prompt(current_date)

        # Build messages with conversation context for better classification
        messages: list[dict[str, Any]] = []
        if session and session.messages:
            # Last 4 messages for context
            recent = session.messages[-4:]
            for msg in recent:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        try:
            response = await anthropic_client.messages.create(
                model=settings.CLAUDE_CLASSIFIER_MODEL,
                max_tokens=512,
                system=system_prompt,
                messages=messages,
            )
        except anthropic.APIError:
            return {"query_type": "simple_lookup", "requires_chart": False}

        # Extract text from response
        text = ""
        for block in response.content:
            if block.type == "text":
                text = block.text
                break

        logger.info(
            "Orchestrator classifier — input=%d, output=%d tokens",
            response.usage.input_tokens, response.usage.output_tokens,
        )

        classifier_usage = {
            "agent": "classifier",
            "model": settings.CLAUDE_CLASSIFIER_MODEL,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

        try:
            result = json.loads(_strip_markdown_fences(text))
            result["usage"] = classifier_usage
            return result
        except (json.JSONDecodeError, TypeError):
            logger.warning("Classification fallback: could not parse Claude response as JSON. Raw text: %s", text)
            return {"query_type": "simple_lookup", "requires_chart": False, "usage": classifier_usage}


def _filter_table_by_advice(table: dict, advice: dict) -> dict | None:
    """Filter table columns to only those selected by the chart advisor.

    Returns a new {headers, rows} dict with only the columns named in
    advice["x_column"], advice["y_columns"], and advice["secondary_y_columns"].
    Returns None if the x_column is not found or fewer than 2 columns survive.
    """
    headers = table["headers"]
    rows = table["rows"]

    x_col = advice.get("x_column", "")
    if not x_col or x_col not in headers:
        return None

    # Build ordered list of column indices: x first, then y, then secondary_y
    col_indices: list[int] = []

    col_indices.append(headers.index(x_col))

    for col in advice.get("y_columns", []):
        if col in headers:
            idx = headers.index(col)
            if idx not in col_indices:
                col_indices.append(idx)

    for col in advice.get("secondary_y_columns", []):
        if col in headers:
            idx = headers.index(col)
            if idx not in col_indices:
                col_indices.append(idx)

    if len(col_indices) < 2:  # Need at least x + 1 y column
        return None

    new_headers = [headers[i] for i in col_indices]
    new_rows = []
    for row in rows:
        new_row = [row[i] if i < len(row) else "" for i in col_indices]
        new_rows.append(new_row)

    return {"headers": new_headers, "rows": new_rows}


# Canonical GST ledger labels per component, keyed by the voucher_builder's
# expected dict keys (cgst_input/.../igst_output). The "tokens" are matched
# case-insensitively and order-independently against each Duties & Taxes ledger
# name, so both "CGST Input" and "Input CGST" resolve to cgst_input.
_GST_COMPONENT_TOKENS = {
    "cgst": ("cgst",),
    "sgst": ("sgst",),
    "igst": ("igst",),
}


def _resolve_gst_ledgers(
    tally_ledgers: list[dict],
    direction: str,
    doc,
) -> tuple[dict[str, str], list[str]]:
    """Resolve GST ledger names from the workspace's Tally ledgers.

    Matches ledgers under "Duties & Taxes" (case-insensitive) to the GST
    components present on ``doc`` for the given ``direction`` ("input" for
    Purchase/Debit Note, "output" for Sales/Credit Note). Returns a dict keyed
    by the voucher_builder's expected keys (``cgst_input``/``sgst_input``/
    ``igst_input`` or the ``*_output`` variants) plus a list of warnings for any
    component the document HAS but no matching ledger was found.

    Matching is order-independent ("Input CGST" resolves the same as
    "CGST Input") and requires the direction word ("input"/"output") to be
    present in the ledger name so the Input/Output ledgers aren't confused.
    """
    gst_ledgers: dict[str, str] = {}
    warnings: list[str] = []

    gst = getattr(doc, "gst", None)
    if not gst:
        return gst_ledgers, warnings

    # Candidate GST ledgers: only those under Duties & Taxes.
    duties = [
        l for l in tally_ledgers
        if (l.get("parent_group", "") or "").strip().lower() == "duties & taxes"
    ]

    amounts = {
        "cgst": getattr(gst, "cgst_amount", None),
        "sgst": getattr(gst, "sgst_amount", None),
        "igst": getattr(gst, "igst_amount", None),
    }

    for component, tokens in _GST_COMPONENT_TOKENS.items():
        amount = amounts[component]
        if not amount:
            continue  # component not on the document → nothing to map
        key = f"{component}_{direction}"
        match = None
        for ledger in duties:
            name = ledger["name"]
            lname = name.lower()
            if direction in lname and all(t in lname for t in tokens):
                match = name
                break
        if match:
            gst_ledgers[key] = match
        else:
            label = f"{component.upper()} {direction.capitalize()}"
            warnings.append(
                f"GST ledger '{label}' not found — GST not posted separately"
            )

    return gst_ledgers, warnings


def _extracted_to_jsonable(extracted) -> dict:
    """Convert an ExtractedDocument to a JSON-serialisable dict for JSONB storage.

    Decimals → float, nested dataclasses (line items, gst) → dicts. Anything that
    still isn't JSON-safe is coerced to ``str`` as a last resort.
    """
    import dataclasses
    from decimal import Decimal

    def _conv(value):
        if isinstance(value, Decimal):
            return float(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {k: _conv(v) for k, v in dataclasses.asdict(value).items()}
        if isinstance(value, dict):
            return {k: _conv(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_conv(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    try:
        return _conv(extracted)
    except Exception:  # noqa: BLE001 - audit metadata must never break the upload
        return {"doc_type": getattr(extracted, "doc_type", None)}


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) from text."""
    stripped = text.strip()
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


# Tool names from ANALYSIS_TOOLS (pre-computed by QueryAgent)
_COMPUTED_TOOL_NAMES = {
    "compute_totals", "compute_trend", "compute_period_comparison",
    "compute_percentage_change", "sort_by_field",
    "code_execution",
}


def _separate_tool_results(
    tool_results: list[dict],
) -> tuple[list, list]:
    """Separate tool results into raw Tally data and pre-computed analysis.

    Returns:
        (raw_data_list, computed_data_list) — each entry is the tool's data payload.
        ReportResponse-style dicts (headers/rows) in raw data are converted to list[dict].
        Computed data is kept as-is (may be headers/rows or list[dict]).
    """
    raw: list = []
    computed: list = []

    for tr in tool_results:
        result = tr.get("result", {})
        if result.get("success") is not True or result.get("data") is None:
            continue

        tool_name = tr.get("tool_name", "")
        if not tool_name:
            logger.warning("Tool result missing tool_name key — treating as raw data: %s", tr)
        data = result["data"]

        if tool_name in _COMPUTED_TOOL_NAMES:
            computed.append(data)
        else:
            # Convert ReportResponse-style dicts to list[dict] for raw data
            if isinstance(data, dict) and "headers" in data and "rows" in data:
                data = [dict(zip(data["headers"], row)) for row in data["rows"]]
            raw.append(data)

    return raw, computed


def _extract_all_data(tool_results: list[dict]) -> list[dict]:
    """Extract all successful tool result data entries.

    Returns a list of data dicts from tool results where ``success=True``
    and ``data`` is present.  This enables comparison queries that need
    multiple datasets (e.g. Q1 vs Q2 fetched via separate tool calls).

    ReportResponse-style dicts (with ``headers`` and ``rows`` keys) are
    automatically converted to ``list[dict]`` for uniform downstream handling.
    """
    data_list = []
    for tr in tool_results:
        result = tr.get("result", {})
        if result.get("success") is True and result.get("data") is not None:
            data = result["data"]

            # Handle ReportResponse-style dicts (headers/rows)
            if isinstance(data, dict) and "headers" in data and "rows" in data:
                rows_as_dicts = []
                for row in data["rows"]:
                    # zip truncates to shortest; safe because headers define the schema
                    rows_as_dicts.append(dict(zip(data["headers"], row)))
                data = rows_as_dicts

            data_list.append(data)
    return data_list
