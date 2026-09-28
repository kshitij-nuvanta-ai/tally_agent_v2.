"""Ingest step 8: name -> GUID resolution (S1 spec §4.4, §12 steps 6/8, D13, D14, D31).

Pure: no DB, no HTTP. Resolution is per master **kind** -- a group and a ledger may share a name (company A's
`Capital Account`, D13) -- and only among the workspace's **live** masters of that type; a deleted master never
resolves. Every name compared here goes through `parse.name` (the D31 leading-U+0004 strip) on **both** sides of
the match: the incoming query name and the names indexed from the caller's rows, per the Task 1 carry -- so a
reserved-prefixed stored name (`"\\u0004 Primary"`) still resolves under its clean form `"Primary"`, and a
reserved-prefixed query resolves against a clean stored name too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from v2.cloud.ingest.parsed import PMaster, PVoucher
from v2.contract.parse import name as parse_name

# S1 spec §12 step 6: currencies -> groups -> voucher types -> units -> stock groups -> ledgers -> stock items
# -> balances -> vouchers.
_KIND_ORDER: dict[str, int] = {
    "currency": 0, "group": 1, "voucher_type": 2, "unit": 3, "stock_group": 4, "ledger": 5, "stock_item": 6,
    "ledger_balance": 7, "stock_balance": 7, "voucher": 8,
}


class ResolveError(ValueError):
    """Raised by `NameIndex.resolve` when a name doesn't resolve to exactly one live master of that kind
    (§11: `missing_master` / `ambiguous_master`, both retryable -- never quarantined)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class NameIndex:
    """A name -> GUID lookup per master kind, built once per batch (§12 "resolution uses one name -> GUID map per
    kind loaded once per batch"). `_by_name` holds only **live** guids; `_name_of` tracks each live guid's current
    (cleaned) name so `add`/`rename` can move it without leaving the old name pointing at a stale GUID."""

    _by_name: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    _name_of: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str, str, bool]]) -> "NameIndex":
        """`rows`: `(kind, guid, name, is_deleted)`, typically the workspace's stored masters. Deleted rows are
        never indexed -- they can never resolve, and can never block a live master of the same name."""
        idx = cls()
        for kind, guid, name, is_deleted in rows:
            if is_deleted:
                continue
            idx._insert(kind, guid, name)
        return idx

    def _insert(self, kind: str, guid: str, name: str) -> None:
        cleaned = parse_name(name)
        key = (kind, guid)
        old = self._name_of.get(key)
        if old is not None:
            self._by_name[(kind, old)].discard(guid)
        self._name_of[key] = cleaned
        self._by_name.setdefault((kind, cleaned), set()).add(guid)

    def add(self, kind: str, guid: str, name: str) -> None:
        """A master created earlier in this same batch (not yet in the DB) -- now resolvable as live."""
        self._insert(kind, guid, name)

    def rename(self, kind: str, guid: str, new_name: str) -> None:
        """A master renamed earlier in this same batch -- the old name no longer resolves to it (§12 step 9:
        "renames keep the GUID")."""
        self._insert(kind, guid, new_name)

    def resolve(self, kind: str, name: str) -> str:
        cleaned = parse_name(name)
        guids = self._by_name.get((kind, cleaned), set())
        if not guids:
            raise ResolveError("missing_master")
        if len(guids) > 1:
            raise ResolveError("ambiguous_master")
        return next(iter(guids))


def _kind_of(obj) -> str:
    return "voucher" if isinstance(obj, PVoucher) else obj.kind


def _alter_id_of(obj) -> int:
    # Balances (PBalance) carry no alter_id (they're keyed by captured_at instead); 0 keeps them grouped together
    # and lets Python's stable sort preserve the caller's original relative order among them.
    return obj.alter_id if isinstance(obj, (PMaster, PVoucher)) else 0


def order_objects(parsed: list) -> list:
    """S1 spec §12 step 6: currencies -> groups -> voucher types -> units -> stock groups -> ledgers -> stock
    items -> balances -> vouchers; masters within a kind by `alter_id` ascending; vouchers last."""
    return sorted(parsed, key=lambda obj: (_KIND_ORDER[_kind_of(obj)], _alter_id_of(obj)))


def guid_prefix_warning(company_guid: str, obj_guid: str) -> bool:
    """D14: every S0 GUID starts with its company's GUID (`138b7373-...-000003c1`); a mismatch is a **warning**
    only (imported vouchers and split companies can legitimately carry another company's prefix) -- never a
    hard reject."""
    return not obj_guid.startswith(company_guid)
