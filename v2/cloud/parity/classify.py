"""Parity cause classifier (S1 spec §10.7). Pure: turns unlabelled mismatch/missing ``Line``s into a cause plus a
remediation list, in the table's order. Lines that already carry a final cause from an earlier rung (``group_walk_wrong``
from rung 2; ``forex_face_mismatch`` / ``forex_revaluation_unexplained`` from ``forex.py``) are recognised by that
cause rather than re-derived, and forex causes are folded into ``forex_gap`` here (row 7).

Ruling F7: cancelled vouchers export **no** amounts at all (LESSONS rule 23) -- flipping a cancelled voucher's flag
therefore changes no ledger sum. ``ctx.flagged_amounts`` can only ever be populated from **optional** vouchers (the
only kind whose flag flip changes an exported total), even though the table still says "cancelled/optional".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from v2.cloud.parity.model import TOL, Line

_FOREX_CAUSES = frozenset({"forex_face_mismatch", "forex_revaluation_unexplained"})


@dataclass(frozen=True)
class Context:
    anchor_rows: dict[str, Decimal]
    flagged_amounts: dict[str, set[Decimal]]
    forex_guids: set[str]
    fy_start: date
    verified_edge: date
    month_ends_without_tb: list[date]


@dataclass(frozen=True)
class Remediation:
    id: str
    action: str
    params: dict


def _remediation(action: str, params: dict) -> Remediation:
    digest = hashlib.sha256((action + json.dumps(params, sort_keys=True, default=str)).encode()).hexdigest()
    return Remediation(id=digest[:16], action=action, params=params)


def classify(lines: list[Line], ctx: Context) -> tuple[list[Line], list[Remediation]]:
    result = list(lines)
    remediations: list[Remediation] = []

    def _set(i: int, cause: str) -> None:
        result[i] = replace(result[i], cause=cause)

    # Row 1: a TB row resolving to no live ledger -> masters_gap.
    masters_gap_idxs = [i for i, l in enumerate(result) if l.verdict == "missing_in_db"]
    if masters_gap_idxs:
        for i in masters_gap_idxs:
            _set(i, "masters_gap")
        remediations.append(_remediation("refetch_masters", {}))

    # Row 2: a live ledger absent from the capture -> ledger_deleted.
    ledger_deleted_idxs = [i for i, l in enumerate(result) if l.verdict == "missing_in_tally"]
    if ledger_deleted_idxs:
        for i in ledger_deleted_idxs:
            _set(i, "ledger_deleted")
        remediations.append(_remediation("reconcile_masters", {"master_type": "ledger"}))

    # Everything below only considers unlabelled ledger-scope mismatches (cause is still None).
    remaining = [i for i, l in enumerate(result)
                 if l.verdict == "mismatch" and l.scope == "ledger" and l.cause is None]

    # Row 3: diff equals one flagged (optional-voucher, ruling F7) amount on that ledger -> flag_filter_inverted.
    handled: set[int] = set()
    for i in remaining:
        l = result[i]
        if l.guid in ctx.flagged_amounts and l.diff is not None and l.diff in ctx.flagged_amounts[l.guid]:
            _set(i, "flag_filter_inverted")
            handled.add(i)
    remaining = [i for i in remaining if i not in handled]

    # Row 4 (ruling F5): a SET of >= 2 mismatching ledgers whose diffs net to 0 (+- tolerance), not just a pair --
    # a dropped GST sale moves party gross, Domestic Sales net, Output CGST and Output SGST (4 ledgers).
    if len(remaining) >= 2:
        total = sum((result[i].diff for i in remaining), Decimal("0"))
        if abs(total) <= TOL:
            for i in remaining:
                _set(i, "voucher_missed_or_duplicated")
            remediations.append(_remediation("month_bisect", {
                "fy_start": ctx.fy_start.isoformat(),
                "month_ends": [d.isoformat() for d in ctx.month_ends_without_tb],
            }))
            remaining = []

    # Row 5: >= 3 BS ledgers whose diff matches their own anchor row (a systematically wrong anchor) -> anchor_wrong.
    anchor_idxs = [i for i in remaining
                   if result[i].guid in ctx.anchor_rows and result[i].diff is not None
                   and abs(result[i].diff - ctx.anchor_rows[result[i].guid]) <= TOL]
    if len(anchor_idxs) >= 3:
        for i in anchor_idxs:
            _set(i, "anchor_wrong")
        remediations.append(_remediation("capture_snapshot", {
            "report_type": "trial_balance_ledgerwise",
            "as_on": (ctx.verified_edge - timedelta(days=1)).isoformat(),
        }))
        remediations.append(_remediation("refetch_masters", {}))
        remaining = [i for i in remaining if i not in anchor_idxs]

    # Row 6: one ledger differs -> ledger_gap. Controller ruling (review I4): this is also the fallback for any
    # mismatch no earlier signature consumed (>= 2 leftover diffs that neither net to zero nor match the
    # anchor-wrong pattern) -- classify never returns an unlabelled mismatch. Each ledger gets its own
    # refetch_ledger_vouchers remediation.
    for i in remaining:
        _set(i, "ledger_gap")
        remediations.append(_remediation("refetch_ledger_vouchers", {
            "ledger_guid": result[i].guid,
            "fy_start": ctx.fy_start.isoformat(),
        }))
    remaining = []

    # Row 7: forex face mismatch / unexplained revaluation (ruling F4 -- reachable only along the matching forex.py
    # path) -> forex_gap, refetch that ledger's vouchers.
    for i, l in enumerate(result):
        if l.cause in _FOREX_CAUSES:
            _set(i, "forex_gap")
            remediations.append(_remediation("refetch_ledger_vouchers", {
                "ledger_guid": l.guid,
                "fy_start": ctx.fy_start.isoformat(),
            }))

    # Row 8 (group_walk_wrong) and row 9 (stale_tally, a precondition failure not a Line) need no action here:
    # group_walk_wrong is already set by rung2 and carries no remediation.

    return result, remediations
