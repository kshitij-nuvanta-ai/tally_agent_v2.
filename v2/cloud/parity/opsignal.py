"""Ops signal (S1 spec §10.10, decision 14). One structured log line per run, logger ``v2.ops.integrity``:
``{workspace_id, run_id, rung, status, cause_counts, mismatch_count, max_abs_diff_bucket}`` -- decision 14: counts
and causes only, **never** names, GUIDs or exact amounts. ``quarantine_event`` (carried over from task 8c's D12)
is the same discipline for a quarantined batch: counts by code only.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Mapping

from v2.cloud.parity.model import PROBLEM_VERDICTS, Line

_LOGGER_NAME = "v2.ops.integrity"

_BUCKETS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("1000"), "<₹1k"),
    (Decimal("100000"), "<₹1L"),
    (Decimal("10000000"), "<₹1Cr"),
)


def bucket(d: Decimal) -> str:
    """One of ``<₹1k | <₹1L | <₹1Cr | ≥₹1Cr`` -- never the exact amount (decision 14)."""
    magnitude = abs(d)
    for threshold, label in _BUCKETS:
        if magnitude < threshold:
            return label
    return "≥₹1Cr"


def integrity_event(ws_id: str, run_id: str, rung: int, status: str, lines: list[Line]) -> dict:
    problems = [l for l in lines if l.verdict in PROBLEM_VERDICTS]
    cause_counts: dict[str, int] = {}
    for l in problems:
        if l.cause:
            cause_counts[l.cause] = cause_counts.get(l.cause, 0) + 1
    # Review I3 / controller ruling: only `mismatch` verdicts feed the magnitude. `match_revalued` forex lines
    # carry their revaluation in `diff` even on a clean run (D4/C47) and would otherwise report a false magnitude.
    diffs = [abs(l.diff) for l in lines if l.verdict == "mismatch" and l.diff is not None]
    # Rereview 1 / controller ruling: zero mismatches is not the same as a real sub-₹1k mismatch -- §10.10 names no
    # zero/none label among <₹1k | <₹1L | <₹1Cr | ≥₹1Cr, so a clean run reports None (JSON null) rather than going
    # through bucket() at all.
    max_abs_diff_bucket = bucket(max(diffs)) if diffs else None
    return {
        "workspace_id": ws_id,
        "run_id": run_id,
        "rung": rung,
        "status": status,
        "cause_counts": cause_counts,
        "mismatch_count": len(problems),
        "max_abs_diff_bucket": max_abs_diff_bucket,
    }


def quarantine_event(workspace_id: str, counts_by_code: Mapping[str, int]) -> dict:
    """Counts-only event for a batch that quarantined objects (D12, carried from task 8c): no master names, GUIDs,
    amounts or narration -- only the deterministic reject codes and how many of each."""
    return {"workspace_id": workspace_id, "event": "quarantine", "counts_by_code": dict(counts_by_code)}


def emit(event: dict) -> None:
    logging.getLogger(_LOGGER_NAME).info(json.dumps(event))
