"""Forex ledgers -- C47 (S1 spec §10.5, D4, D30). Pure. The only multiplication in S1 is the self-check below.

Tally values a forex ledger at face total × its latest voucher rate; our lines carry each voucher's stated INR base.
The difference is expected; the engine proves it is a revaluation, never models it:

(a) face check -- only when Tally's figure states a face value AND the as-on date lies in Tally's current period
    (ruling F3; the master OpeningBalance is the CURRENT-FY opening, C46): face totals match to 0.01 and
    face × rate = base ± 0.01.
(b) set rule -- Σ over forex ledgers of (Tally − ours) must equal minus the TB's ``Unadjusted Forex Gain/Loss`` row.

"Current period" (ruling F25) is the FY of the mirrored ledger masters -- Tally's current period, e.g. FY 2025-26 for
the S0 companies -- NOT the IST-current FY. Callers pass it as ``tally_current_fy_end``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from v2.cloud.clock import fy_start_of
from v2.cloud.parity.model import BS_NATURES, ZERO, LedgerIn, Line, Sums, is_pl_account

FACE_TOL = Decimal("0.01")


def face_applies(as_on: date, tally_current_fy_end: date) -> bool:
    """Ruling F3: the master's face fields describe Tally's current period only."""
    return fy_start_of(as_on) == fy_start_of(tally_current_fy_end)


def scope_face(l: LedgerIn, as_on: date, tally_current_fy_end: date) -> LedgerIn:
    """Drop the face fields when ``as_on`` is outside Tally's current period (F3): (a) then can't run and
    §10.5(b) carries the ledger. Never pair a current-FY master's opening face with another FY's lines."""
    if face_applies(as_on, tally_current_fy_end):
        return l
    return replace(l, opening_fx=None, closing_fx=None, closing_fx_rate=None)


def face_check(l: LedgerIn, sums: Sums) -> tuple[bool, str | None]:
    if not sums.face_complete.get(l.guid, True):
        return False, "forex_face_incomplete"
    if l.closing_fx is None or l.closing_fx_rate is None or l.mirrored_closing is None or l.opening_fx is None:
        return False, "forex_face_incomplete"
    face_ours = l.opening_fx + sums.face.get(l.guid, ZERO)
    if abs(face_ours - l.closing_fx) > FACE_TOL:
        return False, "forex_face_mismatch"
    if abs(l.closing_fx * l.closing_fx_rate - l.mirrored_closing) > FACE_TOL:
        return False, "forex_face_mismatch"
    return True, None


def set_rule(raw_diffs: dict[str, Decimal], unadjusted: Decimal | None, tol: Decimal) -> bool:
    return abs(sum(raw_diffs.values(), ZERO) + (unadjusted or ZERO)) <= tol


def forex_lines(forex_ledgers: list[LedgerIn], anchors: dict[str, Decimal] | None, sums: Sums,
                tb_by_guid: dict[str, Decimal], unadjusted: Decimal | None,
                tol: Decimal) -> tuple[list[Line], Decimal]:
    """Lines for the balance-sheet forex ledgers, and ``forex_unrealised_total`` (Σ accepted ``unrealised``).

    Only live-in-capture, balance-sheet, non-``Profit & Loss A/c`` ledgers are forex lines; rung 1 reports the rest
    (``missing_in_tally``, ``pl_account`` -- company B's P&L A/c master carries an expression balance, so D30 marks
    it forex -- ``unclassified_group``). Tally's figure is the ledger-level TB row; a ledger absent from it falls
    back to the mirrored closing, else 0.00 (§10.4)."""
    ledgers = [l for l in forex_ledgers if l.in_capture and l.nature in BS_NATURES and not is_pl_account(l)]
    if anchors is None:
        return [Line("ledger", l.guid, l.name, None, None, None, "not_applicable", "no_ledger_anchor")
                for l in ledgers], ZERO

    ours = {l.guid: anchors.get(l.guid, ZERO) + sums.total.get(l.guid, ZERO) for l in ledgers}
    tally = {l.guid: tb_by_guid[l.guid] if l.guid in tb_by_guid
             else (l.mirrored_closing if l.mirrored_closing is not None else Decimal("0.00")) for l in ledgers}
    raw = {g: tally[g] - ours[g] for g in ours}
    set_ok = set_rule(raw, unadjusted, tol)

    lines: list[Line] = []
    total = ZERO
    for l in ledgers:
        g = l.guid
        face_ok, cause = face_check(l, sums)
        our_fx = (l.opening_fx + sums.face.get(g, ZERO)
                  if l.opening_fx is not None and sums.face_complete.get(g, True) else None)
        base = dict(scope="ledger", guid=g, name=l.name, our=ours[g], tally=tally[g], diff=raw[g],
                    our_fx=our_fx, tally_fx=l.closing_fx)
        if cause == "forex_face_mismatch":
            lines.append(Line(**base, verdict="mismatch", cause=cause))
        elif set_ok:
            lines.append(Line(**base, verdict="match_revalued", cause=None, unrealised=raw[g]))
            total += raw[g]
        elif raw[g] != 0:
            lines.append(Line(**base, verdict="mismatch", cause="forex_revaluation_unexplained"))
        elif face_ok:
            lines.append(Line(**base, verdict="match", cause=None))
        else:
            lines.append(Line(**base, verdict="not_applicable", cause=cause))
    return lines, total
