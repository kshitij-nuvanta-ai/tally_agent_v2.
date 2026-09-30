"""Wire kinds, required keys, error-code sets and the request/response bodies (S1 spec §5.3, §7, §11).

Field names copy the §7 JSON examples one for one. Tally-form dates (`books_from` "20220401", `as_on_date`
"31-03-2023") stay text and are parsed by contract.parse on the server; ISO dates and timestamps are typed.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MASTER_KINDS = ("currency", "group", "voucher_type", "unit", "stock_group", "ledger", "stock_item")   # §12 step 6 order
BALANCE_KINDS = ("ledger_balance", "stock_balance")
ALL_KINDS = MASTER_KINDS + BALANCE_KINDS + ("voucher",)
REQUIRED_KEYS = {
    "currency": ("guid", "alterid", "name", "expandedsymbol"),
    "group": ("guid", "alterid", "name", "parent"),
    "voucher_type": ("guid", "alterid", "name", "parent"),
    "ledger": ("guid", "alterid", "name", "parent"),
    "stock_group": ("guid", "alterid", "name", "parent"),
    "unit": ("guid", "alterid", "name"),
    "stock_item": ("guid", "alterid", "name", "parent", "baseunits"),
    "ledger_balance": ("guid", "name", "closingbalance", "captured_at"),
    "stock_balance": ("guid", "name", "closingvalue", "captured_at"),
    "voucher": ("guid", "masterid", "alterid", "date", "vouchertypename", "iscancelled", "isoptional",
                "ispostdated", "ledger_entries"),
}
LINE_REQUIRED = ("ledgername", "amount", "isdeemedpositive")
INVENTORY_REQUIRED = ("stockitemname", "amount")
DETERMINISTIC_CODES = frozenset({"unbalanced_voucher", "unparseable_amount", "forex_base_missing", "invalid_date",
                                 "invalid_logical", "missing_field", "duplicate_posting_list", "unknown_kind"})
RETRYABLE_CODES = frozenset({"missing_master", "ambiguous_master"})


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Counters(_Body):
    alt_vch_id: int
    alt_mst_id: int


# --- §7.9 batches ----------------------------------------------------------------------------------------------------
class WireObject(_Body):
    kind: str                 # not a Literal: an unknown kind is a per-object `unknown_kind`, not a 422 on the body
    data: dict[str, Any]


class QuarantineEntry(_Body):                                         # plan A7
    kind: str
    guid: str
    code: str
    voucher_date: str | None = None


class Chunk(_Body):
    from_: date = Field(alias="from")
    to: date


class BatchRequest(_Body):
    batch_id: str
    run_id: str
    company_guid: str
    chunk: Chunk | None = None
    objects: list[WireObject]
    quarantine: list[QuarantineEntry] = []


class BatchCounts(_Body):
    inserted: int = 0
    updated: int = 0
    skipped_older: int = 0
    balances_applied: int = 0
    balances_stale: int = 0


class BatchWarning(_Body):
    index: int
    code: str


class RereadLedger(_Body):
    guid: str
    name: str


class BatchResponse(_Body):
    batch_id: str
    status: Literal["accepted", "rejected"]
    replayed: bool = False
    counts: BatchCounts
    warnings: list[BatchWarning] = []
    reread_ledgers: list[RereadLedger] = []


# --- §7.12 snapshots -------------------------------------------------------------------------------------------------
class SnapshotRequest(_Body):
    report_type: str
    from_date: str
    as_on_date: str
    request_flags: dict[str, str] = {}
    purpose: str
    captured_at: datetime
    counters: Counters
    cells: list[dict[str, str]]


# --- §7.6 heartbeat --------------------------------------------------------------------------------------------------
class SeenCompany(_Body):
    guid: str
    name: str


class HeartbeatRequest(_Body):
    agent_version: str
    tally_version: str | None = None
    tally_status: Literal["closed", "port_closed", "popup_blocked", "no_company", "other_company",
                          "other_company_same_name", "ours"]
    seen_company: SeenCompany | None = None
    counters: Counters | None = None
    last_error_code: str | None = None
    breaker: str
    outbox_depth: int
    pc_clock: datetime
    acked_commands: list[str] = []


# --- §7.8 runs -------------------------------------------------------------------------------------------------------
class RunCreate(_Body):
    kind: Literal["first_sync", "incremental", "backfill", "full_resync"]
    scope: dict[str, Any] | None = None
    command_id: str | None = None
    counters_at_start: Counters
    progress_total: int | None = None


class RunPatch(_Body):
    status: Literal["running", "completed", "failed", "interrupted"]
    progress_done: int | None = None
    progress_total: int | None = None
    batches_declared: int | None = None
    cursor_after: Counters | None = None


# --- §7.10 coverage --------------------------------------------------------------------------------------------------
class CoveragePatch(_Body):
    fy_start: date
    month: str | None = None                                           # "YYYY-MM"
    run_id: str | None = None
    action: Literal["add_fy"] | None = None


# --- §7.11 reconcile -------------------------------------------------------------------------------------------------
class ReconcileScope(_Body):
    kind: Literal["vouchers", "masters"]
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    master_type: str | None = None


class PresentObject(_Body):
    guid: str
    alter_id: int


class ReconcileRequest(_Body):
    run_id: str
    scope: ReconcileScope
    present: list[PresentObject]
    present_count: int
    confirm_large: bool = False


# --- §7.14 parity ----------------------------------------------------------------------------------------------------
class ParityRequest(_Body):
    scope: str
    as_on_date: str
    capture_started_at: datetime
    counters_before: Counters
    counters_after: Counters
    remediation_done: list[str] = []
    fy_start: str | None = None          # scope = bisect only (§10.9; the month_bisect remediation's `fy_start`)


# --- §7.1, §7.5, §7.15 auth / bind / relink / web commands ----------------------------------------------------------
class LoginRequest(_Body):
    email: str
    password: str
    device_name: str
    agent_version: str


class BindRequest(_Body):
    workspace_id: str
    company_guid: str
    company_name: str
    books_from: str
    base_currency_name: str | None = None
    takeover: bool = False


class RelinkRequest(_Body):
    new_company_guid: str
    company_name: str
    password: str


class WebCommand(_Body):
    type: Literal["recheck_now", "confirm_resync", "confirm_relink"]
    scope: Literal["company", "fy"] | None = None
    fy_start: date | None = None
    password: str | None = None
