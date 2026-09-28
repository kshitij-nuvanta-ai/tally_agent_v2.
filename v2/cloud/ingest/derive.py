"""Ingest step 10: derivations at ingest -- `nature` / `primary_group`, voucher-type `base_type`, `is_forex`
(S1 spec §4.4, §12 step 10, probe 25). Pure: no DB, no HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass

from v2.contract.tally_rules import PRIMARY_NATURE, PRIMARY_PARENT, RESERVED_VOUCHER_TYPES


@dataclass(frozen=True)
class Derived:
    primary_group: str | None
    nature: str | None
    warning: str | None


def nature_walk(group: str, parents: dict[str, str]) -> Derived:
    """Walk `parents` up from `group` until the parent is Primary (D31 already stripped `&#4; Primary` to
    `"Primary"` by the time it's in this dict) -- the group itself is then a reserved primary group -- and map
    it through `PRIMARY_NATURE`. Handles custom sub-groups of any depth (`National Creditors -> Sundry Creditors
    -> Current Liabilities -> liabilities`). A cycle, or a primary group not in the map, yields
    `warning = "unmapped_primary"` and `nature = None` (parity then treats the group's ledgers as
    `not_applicable` / `unclassified_group`, never `match` -- §4.4)."""
    seen: list[str] = []
    name = group
    while True:
        if name in seen:
            return Derived(None, None, "unmapped_primary")
        seen.append(name)
        parent = parents.get(name)
        if parent is None or parent in ("", PRIMARY_PARENT):
            nature = PRIMARY_NATURE.get(name)
            return Derived(name if nature else None, nature, None if nature else "unmapped_primary")
        name = parent


def base_type_walk(vtype: str, parents: dict[str, str], reserved: dict[str, str | None]) -> str | None:
    """Walk the voucher-type `parents` chain until: the object's own exported `RESERVEDNAME` (`reserved[name]`)
    is non-empty (a direct signal from Tally that this name already IS a reserved type); or `name` is one of the
    24 reserved types (probe 25's `base_types` observation, `RESERVED_VOUCHER_TYPES`) -- a reserved type is its
    own parent, so this also covers the self-parent case (`Contra -> Contra`); or `name == parent` (any other
    self-parent, defensive); else follow `parent`. Unresolvable (chain runs out, or a cycle) -> `None` +
    warning (recorded by the caller)."""
    seen: list[str] = []
    name = vtype
    while True:
        if name in seen:
            return None
        seen.append(name)
        own_reserved = reserved.get(name)
        if own_reserved:
            return own_reserved
        if name in RESERVED_VOUCHER_TYPES:
            return name
        parent = parents.get(name)
        if parent is None:
            return None
        if parent == name:
            return name
        name = parent


def is_forex_ledger(currency_name: str | None, base_currency_name: str | None, ever_expression: bool) -> bool:
    """D30: a forex ledger is one whose `CurrencyName` is not the base currency, **or** one that ever carried an
    expression amount. No `CurrencyName` at all (`None`) defaults to the base currency (not forex) unless an
    expression amount was seen."""
    if ever_expression:
        return True
    if currency_name is None:
        return False
    return currency_name != base_currency_name


def is_base_currency(expanded_symbol: str) -> bool:
    """D30: the base currency is the currency master whose `ExpandedSymbol` is `INR` (its `NAME` exports as `?`,
    LESSONS rule 28c -- so identity is by `ExpandedSymbol`, never `NAME`)."""
    return expanded_symbol == "INR"


def descendants(name: str, parents: dict[str, str]) -> set[str]:
    """Every key in `parents` whose parent chain passes through `name` (excluding `name` itself) -- used to
    re-derive a moved master's descendants in the same transaction (§12 step 10). Cycle-safe: a chain that loops
    without ever reaching `name` contributes nothing."""
    result: set[str] = set()
    for candidate in parents:
        if candidate == name:
            continue
        seen: set[str] = set()
        current = candidate
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
            if current == name:
                result.add(candidate)
                break
    return result
