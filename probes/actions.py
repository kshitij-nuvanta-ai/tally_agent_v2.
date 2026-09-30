"""What a pause or an ask asks for, in machine-readable form (S0-D9, spec §5.8).

A probe passes an Action with every `ctx.pause` / `ctx.ask`. The console shows only the instruction text; the
automated operator (`probes/operator/`) dispatches on `Action.kind` and never parses the English text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PAUSE_KINDS: frozenset[str] = frozenset({
    "tally_running", "restore_seed", "rename_company", "close_all_companies", "open_company",
    "create_voucher", "alter_voucher", "delete_voucher",
    "create_ledger", "alter_ledger", "delete_ledger", "rename_ledger",
    "view_report", "raise_popup", "dismiss_popup", "quit_tally", "backup_company", "restore_company",
})
ASK_KINDS: frozenset[str] = frozenset({
    "wine_version", "data_folder", "tally_version", "edition", "licence", "expense_ledger", "ui_closing_balance",
})


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if self.kind not in PAUSE_KINDS | ASK_KINDS:
            raise ValueError(f"Unknown action kind: {self.kind!r}")
