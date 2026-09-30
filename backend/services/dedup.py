"""Duplicate-invoice detection (Phase 1 Part B).

Two independent, workspace-scoped checks. If either fires, the caller blocks the
write (hard block, no override):

- B1 exact-file: another UploadedFile in the same workspace already has the same
  sha256 content hash → reason "same file".
- B2 business-key (party ledger + supplier-invoice-no), only when an invoice ref
  was extracted:
    (a) a prior written VoucherEntry in the workspace with the same party +
        reference, or
    (b) Tally itself (authoritative) — the party's vouchers whose REFERENCE
        matches the extracted invoice no (catches invoices entered outside the
        app). Tally errors are swallowed (treated as "no match") so a transient
        Tally failure never crashes an upload — B1 + DB B2 still apply.

All queries are scoped to ``workspace_id``.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from backend.db.models import UploadedFile, VoucherEntry
from backend.tally_bridge.queries.vouchers import get_party_vouchers

logger = logging.getLogger(__name__)

# Voucher types whose REFERENCE field can carry a supplier invoice no.
_PARTY_VOUCHER_TYPES = ["Purchase", "Sales", "Debit Note", "Credit Note", "Payment"]


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _norm(s: str | None) -> str:
    """Normalize a dedup key: strip surrounding whitespace + casefold.

    OCR/Vision artifacts ("INV-100 ", "inv-100\\n") must not cause silent
    misses. Applied to BOTH sides of the party + reference comparison.
    """
    return (s or "").strip().casefold()


async def find_business_key_duplicate(
    db,
    client,
    *,
    workspace_id: str,
    party_ledger: str | None,
    invoice_ref: str | None,
    company: str | None = None,
    doc_date: str | None = None,
) -> dict | None:
    """B2 business-key (party ledger + supplier invoice no) duplicate check.

    Re-usable, content-hash-free check shared by the upload path (via
    ``find_duplicate``) and the write-time re-check in ``voucher_action``.
    Returns a ``{"voucher_no", "date", "reason"}`` descriptor if the
    party + reference already exist (DB VoucherEntry or Tally), else None.

    No invoice ref → no business-key block (consistent with upload-time
    B2-skip). All matching is normalized (strip + casefold).

    ``doc_date`` (the document's own date, any of YYYY-MM-DD / YYYYMMDD /
    DD-MM-YYYY) anchors the Tally lookup window — see
    ``queries.vouchers.party_voucher_window`` (C33: typed date vars make the
    window binding, so it must follow the document, not a fixed FY).
    """
    if not invoice_ref or not _norm(invoice_ref):
        return None

    ref_n = _norm(invoice_ref)
    party_n = _norm(party_ledger)

    # (a) DB: prior written VoucherEntry with same party + reference.
    rows = (await db.execute(
        select(VoucherEntry).where(
            VoucherEntry.workspace_id == workspace_id,
            VoucherEntry.status == "written",
        )
    )).scalars().all()
    for ve in rows:
        data = ve.voucher_data or {}
        if (_norm(data.get("party_ledger")) == party_n
                and _norm(data.get("reference")) == ref_n):
            return {
                "voucher_no": ve.tally_voucher_number or "",
                "date": (data.get("date") or ""),
                "reason": "same invoice no for party",
            }

    # (b) Tally (authoritative). Errors → treat as "no Tally match".
    if party_ledger:
        try:
            vouchers = await get_party_vouchers(
                client, party_ledger, _PARTY_VOUCHER_TYPES, company=company,
                anchor_date=doc_date,
            )
        except Exception as exc:  # noqa: BLE001 — never crash on Tally errors
            logger.warning("Tally dedup query failed for %r: %s", party_ledger, exc)
            vouchers = []
        for v in vouchers:
            if v.get("reference") and _norm(v.get("reference")) == ref_n:
                return {
                    "voucher_no": v.get("voucher_number") or "",
                    "date": v.get("date") or "",
                    "reason": "same invoice no for party",
                }

    return None


async def find_duplicate(
    db,
    client,
    *,
    workspace_id: str,
    content_hash: str,
    party_ledger: str | None,
    invoice_ref: str | None,
    company: str | None = None,
    exclude_file_id: str | None = None,
    doc_date: str | None = None,
) -> dict | None:
    """Return a ``{"voucher_no", "date", "reason"}`` descriptor if the upload is a
    duplicate, else None.

    ``exclude_file_id`` lets the caller exclude the just-persisted UploadedFile
    row (its own hash) from the B1 self-match.
    """
    # --- B1: exact-file ---
    if content_hash:
        stmt = select(UploadedFile).where(
            UploadedFile.workspace_id == workspace_id,
            UploadedFile.content_hash == content_hash,
        )
        if exclude_file_id:
            stmt = stmt.where(UploadedFile.id != exclude_file_id)
        match = (await db.execute(stmt)).scalars().first()
        if match is not None:
            # Surface the written voucher linked to that file, if any.
            ve = (await db.execute(
                select(VoucherEntry).where(
                    VoucherEntry.file_id == match.id,
                    VoucherEntry.status == "written",
                )
            )).scalars().first()
            return {
                "voucher_no": (ve.tally_voucher_number if ve else "") or "",
                "date": match.created_at.isoformat() if match.created_at else "",
                "reason": "same file",
            }

    # --- B2: business key (party + invoice ref). Skip if no invoice ref. ---
    return await find_business_key_duplicate(
        db, client,
        workspace_id=workspace_id,
        party_ledger=party_ledger,
        invoice_ref=invoice_ref,
        company=company,
        doc_date=doc_date,
    )
