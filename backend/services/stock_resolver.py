"""Resolve extracted line items against the workspace's existing stock items.

Invoice entry Phase 2 (inventory). For a goods invoice (Purchase/Sales with
quantity-bearing lines) this fetches the company's stock items once, then for
each line builds a deterministic "resolved" dict that the review card renders
and ``voucher_action`` consumes to build inventory item tuples.

Matching is deterministic (and unit-tested): normalise both sides (strip +
casefold), try an exact match first, then a token-overlap/contains heuristic
above a threshold. No match → ``create_new=True`` and the description becomes
the new stock item's name.
"""
from __future__ import annotations

from decimal import Decimal

from tally_bridge.queries.masters import list_stock_items

DEFAULT_UNIT = "Nos"
# Minimum Jaccard token-overlap (or contains) to accept a fuzzy match.
_MATCH_THRESHOLD = 0.5


def _normalise(text: str) -> str:
    return " ".join((text or "").strip().casefold().split())


def _tokens(text: str) -> set[str]:
    return set(_normalise(text).split())


def _best_match(description: str, existing: list[str]) -> str | None:
    """Return the best-matching existing stock-item name, or None.

    Exact (normalised) match first; otherwise the highest token-overlap
    (Jaccard) or containment score, accepted only above ``_MATCH_THRESHOLD``.
    """
    norm_desc = _normalise(description)
    if not norm_desc:
        return None

    # Exact normalised match.
    for name in existing:
        if _normalise(name) == norm_desc:
            return name

    desc_tokens = _tokens(description)
    if not desc_tokens:
        return None

    best_name: str | None = None
    best_score = 0.0
    for name in existing:
        name_tokens = _tokens(name)
        if not name_tokens:
            continue
        overlap = desc_tokens & name_tokens
        if not overlap:
            continue
        union = desc_tokens | name_tokens
        jaccard = len(overlap) / len(union)
        # Containment: one description's tokens fully contained in the other.
        contains = len(overlap) / min(len(desc_tokens), len(name_tokens))
        score = max(jaccard, contains)
        if score > best_score:
            best_score = score
            best_name = name

    if best_name is not None and best_score >= _MATCH_THRESHOLD:
        return best_name
    return None


def _to_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _doc_gst_rate(doc) -> float:
    """Combined GST rate from the document's GST breakdown (CGST+SGST or IGST)."""
    gst = getattr(doc, "gst", None)
    if not gst:
        return 0.0
    cgst = _to_float(getattr(gst, "cgst_rate", None))
    sgst = _to_float(getattr(gst, "sgst_rate", None))
    igst = _to_float(getattr(gst, "igst_rate", None))
    if igst:
        return igst
    return cgst + sgst


def dropped_unquantified_descriptions(doc) -> list[str]:
    """Descriptions of line items that ``resolve_line_items`` would drop.

    A qty-null line is skipped by the resolver (it signals a non-inventory
    line). On an invoice that DID route to inventory, those dropped lines mean
    the stock grid would under-post the invoice. The inventory write path uses
    this list to block the write and tell the user which lines need a quantity.
    """
    return [
        (line.description or "")
        for line in (doc.line_items or [])
        if line.quantity is None
    ]


async def resolve_line_items(
    client,
    doc,
    *,
    direction: str,
    default_group: str,
) -> list[dict]:
    """Resolve a document's quantity-bearing line items to stock-item dicts.

    Args:
        client: TallyClient (read-only — fetches existing stock items).
        doc: ExtractedDocument with ``line_items``.
        direction: "purchase" or "sales" (reserved for future use / parity).
        default_group: stock group for newly-created items (e.g. "Primary").

    Returns one dict per quantity-bearing line:
        {description, qty, rate, unit, gst_rate, amount, matched_item,
         create_new, stock_name, stock_group, hsn}

    Lines with no quantity are skipped (they signal a non-inventory document).
    """
    existing_items = await list_stock_items(client)
    existing_names = [item.name for item in existing_items]
    fallback_gst = _doc_gst_rate(doc)

    resolved: list[dict] = []
    for line in doc.line_items or []:
        if line.quantity is None:
            continue  # non-inventory line — skip

        description = line.description or ""
        matched = _best_match(description, existing_names)

        line_gst = getattr(line, "gst_rate", None)
        gst_rate = _to_float(line_gst) if line_gst is not None else fallback_gst

        unit = (line.unit or "").strip() or DEFAULT_UNIT
        hsn = (getattr(line, "hsn", None) or getattr(doc, "hsn", None) or "")

        resolved.append({
            "description": description,
            "qty": _to_float(line.quantity),
            "rate": _to_float(line.rate),
            "unit": unit,
            "gst_rate": gst_rate,
            "amount": _to_float(line.amount),
            "matched_item": matched,
            "create_new": matched is None,
            "stock_name": matched or description,
            "stock_group": default_group,
            "hsn": str(hsn),
        })

    return resolved
