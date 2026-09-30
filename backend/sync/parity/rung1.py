"""Rung 1 -- ledger level, balance-sheet ledgers (S1 spec §10.4). Pure.

``computed_L = anchor_L(E) + Σ countable lines of L in [E, as_on]`` vs Tally's figure (the mirrored closing; or,
when that is ``None`` -- a ``""`` CLOSINGBALANCE (ruling F14) or ``balance_source = needs_tb`` (D3) -- the
ledger-level TB row as-on ``as_on``, where a ledger absent from that TB is 0.00).

Order of the per-ledger checks: not in this capture -> ``missing_in_tally``; ``Profit & Loss A/c`` -> D11; group
nature NULL -> ``unclassified_group``; nominal -> ``nominal`` (never ``match``); forex -> left to
``forex.forex_lines`` (no line here); no ledger-level anchor -> ``no_ledger_anchor`` (never ``match``).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from backend.sync.parity.anchors import require_ledgerwise
from backend.sync.parity.model import NOMINAL_NATURES, ZERO, LedgerIn, Line, Sums, compare, is_pl_account


def _na(l: LedgerIn, cause: str) -> Line:
    return Line("ledger", l.guid, l.name, None, None, None, "not_applicable", cause)


def tally_figure(l: LedgerIn, ledgerwise_tb: dict[str, Decimal]) -> Decimal:
    if l.balance_source == "tally" and l.mirrored_closing is not None:
        return l.mirrored_closing
    return ledgerwise_tb.get(l.guid, Decimal("0.00"))


def computed(l: LedgerIn, anchors: dict[str, Decimal], sums: Sums) -> Decimal:
    return anchors.get(l.guid, ZERO) + sums.total.get(l.guid, ZERO)


def rung1(ledgers: list[LedgerIn], anchors: dict[str, Decimal] | None, sums: Sums,
          ledgerwise_tb: dict[str, Decimal], unresolved_tb_names: list[str], tol: Decimal, *,
          ledgerwise_flags: Mapping[str, str] | None) -> list[Line]:
    """``ledgerwise_tb`` must come from a ledger-level TB: ``ledgerwise_flags`` is that snapshot's ``request_flags``
    and anything but ``ISLEDGERWISE = Yes`` raises ``ValueError("not a ledger-level TB")`` (10c carry)."""
    require_ledgerwise(ledgerwise_flags)
    lines: list[Line] = []
    for l in ledgers:
        if not l.in_capture:
            our = computed(l, anchors, sums) if anchors is not None else None
            lines.append(Line("ledger", l.guid, l.name, our, None, None, "missing_in_tally", None))
        elif is_pl_account(l):
            lines.append(_na(l, "pl_account"))
        elif l.nature is None:
            lines.append(_na(l, "unclassified_group"))
        elif l.nature in NOMINAL_NATURES:
            lines.append(_na(l, "nominal"))
        elif l.is_forex:
            continue                                     # §10.5
        elif anchors is None:
            lines.append(_na(l, "no_ledger_anchor"))
        else:
            our, tally = computed(l, anchors, sums), tally_figure(l, ledgerwise_tb)
            lines.append(Line("ledger", l.guid, l.name, our, tally, tally - our, compare(our, tally, tol), None))
    seen: set[str] = set()
    for name in unresolved_tb_names:
        if name not in seen:
            seen.add(name)
            lines.append(Line("ledger", None, name, None, None, None, "missing_in_db", None))
    return lines
