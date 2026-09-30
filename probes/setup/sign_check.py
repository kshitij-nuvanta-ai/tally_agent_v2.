"""One-shot live check for Ruling C30 (2026-09-24): does Tally read OPENINGBALANCE's SIGN (negative = Dr)?

Not wired into the CLI — the main agent runs it by hand against a Probe company. Each form creates one throwaway
debtor through the real `create_party_ledger`, reads its OpeningBalance back raw, and deletes it (the v4 "Ledger
delete" shape, `TallyWriter.delete_ledger`, with its own read-back — LESSONS §15).

**Which form discriminates (C38, review 2026-09-24 #6):** `run` sends −1.00 under Sundry Debtors, a debit-natured
group. Under C30 (the sign is read) that is Dr 1 → exported "-1.00"; under C21 (the side is inferred from the
group) it is ALSO Dr 1 → "-1.00". So `run` cannot tell them apart — neither hypothesis predicts +1.00 there.
`run_positive` sends +1.00 under the same group: C30 predicts "1.00" (Cr, contra-natural but as sent), C21 predicts
"-1.00" (Dr, the group's side). `positive_verdict` reads that answer. C30's standing evidence meanwhile is company
A's abs()'d HDFC/SBI openings landing as credits under Bank Accounts — a real discriminator.
"""
from __future__ import annotations

from decimal import Decimal

from agent.tally.envelopes import formula_string, wrap_collection
from agent.tally.xml_utils import read_objects
from probes.setup.writes import TallyWriter, WriteFailed

LEDGER = "ZZ Sign Check Debtor"
PARENT = "Sundry Debtors"
OPENING = Decimal("-1.00")
POSITIVE_OPENING = Decimal("1.00")
FIELDS = ["Name", "Parent", "OpeningBalance"]


def _read_opening(writer: TallyWriter, company: str) -> str:
    xml = wrap_collection("S0SignCheckOpening", "Ledger", FIELDS, company,
                          filters=[("S0SignCheckOnly", f"$Name = {formula_string(LEDGER)}")])
    rows = [r for r in read_objects(writer.post(xml), "LEDGER", FIELDS) if r["Name"] == LEDGER]
    if not rows:
        raise WriteFailed(f"Ledger {LEDGER!r} not found on read-back")
    return rows[0]["OpeningBalance"]


def run(writer: TallyWriter, company: str, *, opening: Decimal = OPENING) -> str:
    """Create -> read OpeningBalance -> delete. Returns the raw read-back string. The ledger is deleted even if the
    read-back fails; a leftover from an aborted run is refused (create_party_ledger would skip it silently and the
    OLD opening would be read back). With the default −1.00 the answer is non-discriminating (module docstring)."""
    if writer.ledger(company, LEDGER) is not None:
        raise WriteFailed(f"Ledger {LEDGER!r} already exists — delete it first (a leftover from an earlier run)")
    writer.create_party_ledger(company, LEDGER, parent=PARENT, bill_wise=False, opening=opening,
                               allow_contra_natural=opening > 0)
    try:
        raw = _read_opening(writer, company)
        writer.say(f"sign check: sent OPENINGBALANCE {opening:.2f} under {PARENT}; Tally reads back {raw!r}")
        return raw
    finally:
        writer.delete_ledger(company, LEDGER)


def run_positive(writer: TallyWriter, company: str) -> str:
    """The DISCRIMINATING form (C38): +1.00 under Sundry Debtors. Read the answer with `positive_verdict`."""
    return run(writer, company, opening=POSITIVE_OPENING)


def positive_verdict(raw: str) -> str:
    """What `run_positive`'s raw read-back supports: "C30 …" if Tally kept the sent credit (+1.00), "C21 …" if it
    moved the opening to the group's debit side (−1.00), "neither …" for anything else."""
    text = (raw or "").strip()
    try:
        value = Decimal(text)
    except ArithmeticError:
        value = None
    if value == POSITIVE_OPENING:
        return f"C30 — Tally kept the sent sign: +1.00 under {PARENT} reads back {raw!r} (Cr)"
    if value == -POSITIVE_OPENING:
        return f"C21 — Tally inferred the side from {PARENT}: +1.00 sent, {raw!r} (Dr) read back"
    return f"neither — +1.00 sent under {PARENT}, unexpected read-back {raw!r}; record it and investigate"
