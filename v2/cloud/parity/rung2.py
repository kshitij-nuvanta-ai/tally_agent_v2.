"""Rung 2 -- group level, with ledger resolution (S1 spec §10.6, D29, LESSONS rule 19). Pure.

- Nominal ledgers, per ledger, from the ledger-level TB: Σ lines in [FY(as_on).start, as_on] (``fy_total``;
  nominal ledgers restart each FY) vs the row (absent -> 0.00); unresolved rows -> ``missing_in_db``.
- Reserved primary groups, from the group TB's **first-occurrence** rows (company A has a *ledger* also called
  ``Capital Account`` right after the group row). A balance-sheet group's computed figure is Σ its ledgers'
  rung-1 / forex ``our`` + Σ accepted forex ``unrealised`` (D4) + the same TB's ``Opening Stock`` row if it is the
  stock-bearing group. A nominal group is Σ its nominal ledgers' ``fy_total``. A reserved group absent from the TB
  (Tally omits zero rows) is compared against 0.00.
- A group mismatch whose ledgers all match -> cause ``group_walk_wrong`` (engineering flag).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from v2.cloud.parity.anchors import require_ledgerwise
from v2.cloud.parity.model import (BS_NATURES, NOMINAL_NATURES, ZERO, LedgerIn, Line, Sums, TbRow, compare,
                                   is_pl_account)
from v2.contract.tally_rules import PRIMARY_NATURE

_MATCHED = frozenset({"match", "match_revalued"})


def primary_group_rows(rows: list[TbRow]) -> dict[str, Decimal]:
    """First occurrence of each reserved primary-group name, in document order."""
    out: dict[str, Decimal] = {}
    for r in rows:
        if r.name in PRIMARY_NATURE and r.name not in out:
            out[r.name] = r.amount
    return out


def rung2(ledgers: list[LedgerIn], rung1_lines: list[Line], forex_lines: list[Line], group_rows: dict[str, Decimal],
          opening_stock: Decimal | None, stock_bearing_primary: str, nominal_tb: dict[str, Decimal], sums: Sums,
          unresolved_nominal: list[str], tol: Decimal, *, ledgerwise_flags: Mapping[str, str] | None,
          group_anchors: Mapping[str, Decimal] | None = None,
          group_unrealised: Mapping[str, Decimal] | None = None) -> list[Line]:
    """``nominal_tb`` must come from a ledger-level TB: ``ledgerwise_flags`` is that snapshot's ``request_flags``
    and anything but ``ISLEDGERWISE = Yes`` raises ``ValueError("not a ledger-level TB")``.

    **Group-anchor route** (controller ruling, 10c carry; §10.4 "rung 2 carries the check at group level", §15.5
    row 12). When a balance-sheet group has members with no ledger-level anchor (rung-1 ``no_ledger_anchor``) and
    ``group_anchors`` is given -- the group TB as-on E−1, first-occurrence rows, already net of that TB's Opening
    Stock and of our books_from day-one lines (the caller's job) -- the group is compared as
    ``anchor_g + Σ countable lines of its live ledgers in [E, as_on] + unrealised_g + Opening Stock`` (§10.6). A group
    absent from the anchor TB anchors at 0.00 (Tally omits zero rows). ``group_unrealised[g]`` is the forex
    revaluation attributable to g; a group holding forex ledgers with no such entry can't be split and stays
    ``not_applicable`` (``forex_unsplit``) rather than be compared off by the revaluation."""
    require_ledgerwise(ledgerwise_flags)
    lines: list[Line] = []

    nominal = [l for l in ledgers if l.nature in NOMINAL_NATURES and not is_pl_account(l)]
    nominal_line: dict[str, Line] = {}
    for l in nominal:
        our = sums.fy_total.get(l.guid, ZERO)
        tally = nominal_tb.get(l.guid, Decimal("0.00"))
        line = Line("ledger", l.guid, l.name, our, tally, tally - our, compare(our, tally, tol), None)
        nominal_line[l.guid] = line
        lines.append(line)
    seen: set[str] = set()
    for name in unresolved_nominal:
        if name not in seen:
            seen.add(name)
            lines.append(Line("ledger", None, name, None, None, None, "missing_in_db", None))

    bs_line = {l.guid: l for l in rung1_lines if l.guid is not None}
    bs_line.update({l.guid: l for l in forex_lines})

    members: dict[str, list[LedgerIn]] = {}
    for l in ledgers:
        if l.primary_group in PRIMARY_NATURE and not is_pl_account(l):
            members.setdefault(l.primary_group, []).append(l)

    for group in [*group_rows, *(g for g in members if g not in group_rows)]:
        nature = PRIMARY_NATURE[group]
        tally = group_rows.get(group, Decimal("0.00"))
        own = members.get(group, [])
        if nature in BS_NATURES:
            ledger_lines = [bs_line.get(l.guid) for l in own]
            if any(x is None or x.our is None for x in ledger_lines):
                if group_anchors is not None:
                    if any(l.is_forex for l in own) and group not in (group_unrealised or {}):
                        lines.append(Line("group", None, group, None, tally, None, "not_applicable", "forex_unsplit"))
                        continue
                    our = group_anchors.get(group, ZERO) + sum((sums.total.get(l.guid, ZERO) for l in own), ZERO)
                    our += (group_unrealised or {}).get(group, ZERO)
                    if group == stock_bearing_primary and opening_stock is not None:
                        our += opening_stock
                    lines.append(Line("group", None, group, our, tally, tally - our, compare(our, tally, tol), None))
                    continue
                cause = next((x.cause for x in ledger_lines if x is not None and x.our is None), "no_ledger_line")
                lines.append(Line("group", None, group, None, tally, None, "not_applicable", cause))
                continue
            our = sum((x.our for x in ledger_lines), ZERO)
            our += sum((x.unrealised for x in ledger_lines if x.verdict == "match_revalued" and x.unrealised),
                       ZERO)
            if group == stock_bearing_primary and opening_stock is not None:
                our += opening_stock
        else:
            ledger_lines = [nominal_line[l.guid] for l in own]
            our = sum((x.our for x in ledger_lines), ZERO)
        verdict = compare(our, tally, tol)
        cause = "group_walk_wrong" if verdict == "mismatch" and all(x.verdict in _MATCHED for x in ledger_lines) \
            else None
        lines.append(Line("group", None, group, our, tally, tally - our, verdict, cause))
    return lines
