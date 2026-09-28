"""Escalation ladder, per workspace (S1 spec §10.8, `ladder` column, §15.5). Pure state transition: ``step`` takes
the previous ladder dict and this run's outcome and returns the next ladder dict. It never starts anything --
decision 12: a resync is only ever *offered* (``resync_offered_fy`` set); nothing runs until a human confirms it
out of band, and that confirmation reaches ``step`` only as the ``confirmed_fy_resync_completed`` flag on the
*next* call. Aborted / discarded runs never call ``step`` at all (§10.8's last bullet) -- that is the caller's
responsibility, not this module's.

The ladder dict has exactly four keys: ``state`` (``ok`` | ``suspect`` | ``alert`` | ``hard_alert``),
``heal_attempts`` (int), ``resync_offered_fy`` (``date | None``), ``pending_remediation_ids`` (the remediation ids
issued on this call, so the *next* call can check whether the caller's ``remediation_done`` covers them).
"""
from __future__ import annotations

from datetime import date


def step(prev: dict, *, had_mismatch: bool, remediation_done: list[str], issued_ids: list[str],
         confirmed_fy_resync_completed: bool, fy_for_offer: date) -> dict:
    if not had_mismatch:
        return {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": []}

    prev_state = prev.get("state", "ok")
    prev_pending = prev.get("pending_remediation_ids", [])
    heal_attempts = prev.get("heal_attempts", 0)
    resync_offered_fy = prev.get("resync_offered_fy")

    if confirmed_fy_resync_completed:
        # A confirmed single-FY resync completed and the very next run still mismatches -> hard_alert + (the
        # caller's) engineering-flag ops signal. The ladder itself never triggered that resync (decision 12).
        return {"state": "hard_alert", "heal_attempts": heal_attempts, "resync_offered_fy": resync_offered_fy,
                "pending_remediation_ids": list(issued_ids)}

    if prev_state == "ok":
        return {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": None,
                "pending_remediation_ids": list(issued_ids)}

    remediation_confirmed = bool(prev_pending) and all(pid in remediation_done for pid in prev_pending)
    if not remediation_confirmed:
        # No proof the previously issued remediation ran: keep the state, reissue the same remediation.
        return {"state": prev_state, "heal_attempts": heal_attempts, "resync_offered_fy": resync_offered_fy,
                "pending_remediation_ids": list(issued_ids)}

    heal_attempts += 1
    if heal_attempts >= 2:
        resync_offered_fy = fy_for_offer   # offered only -- decision 12; nothing starts here.
    return {"state": "alert", "heal_attempts": heal_attempts, "resync_offered_fy": resync_offered_fy,
            "pending_remediation_ids": list(issued_ids)}


def visible_state(ladder: dict) -> str:
    """``suspect`` is invisible to the web (§10.8) -- it reads as ``ok`` until it either clears or escalates."""
    state = ladder["state"]
    return "ok" if state == "suspect" else state
