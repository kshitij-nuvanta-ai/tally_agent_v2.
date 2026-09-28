"""v2 cloud ORM models — S1 spec §4 (migration ``v2_001``).

All 21 v2 tables share ``Base.metadata`` (this module's own, distinct from the current app's
``backend.db.models.Base``). ``current.py``'s ``users_table`` / ``workspaces_table`` are read-only reflections
in the same metadata so v2 tables' FKs resolve, but carry ``info={"v2_readonly": True}`` so the v2 Alembic chain
never emits DDL for them (see ``alembic/env.py``'s ``include_object``).
"""
from __future__ import annotations

from . import current  # noqa: F401  (registers users_table/workspaces_table for FK resolution)
from .base import Base
from .bookkeeping import (
    AgentDevice,
    SyncBatch,
    SyncCommand,
    SyncFyCoverage,
    SyncQuarantine,
    SyncRun,
    SyncWorkspace,
)
from .masters import (
    TallyCurrency,
    TallyGroup,
    TallyLedger,
    TallyStockGroup,
    TallyStockItem,
    TallyUnit,
    TallyVoucherType,
)
from .parity import ParityLine, ParityRun
from .snapshots import TallyReportSnapshot
from .vouchers import (
    TallyBillAllocation,
    TallyVoucher,
    TallyVoucherInventoryLine,
    TallyVoucherLedgerLine,
)

__all__ = [
    "Base",
    "V2_TABLES",
    "SyncWorkspace",
    "AgentDevice",
    "SyncRun",
    "SyncBatch",
    "SyncQuarantine",
    "SyncCommand",
    "SyncFyCoverage",
    "TallyCurrency",
    "TallyGroup",
    "TallyVoucherType",
    "TallyLedger",
    "TallyStockGroup",
    "TallyUnit",
    "TallyStockItem",
    "TallyVoucher",
    "TallyVoucherLedgerLine",
    "TallyVoucherInventoryLine",
    "TallyBillAllocation",
    "TallyReportSnapshot",
    "ParityRun",
    "ParityLine",
]

# FK-safe DROP order (children first). `python -m v2.cloud purge` (Task 11) and the DB test harness
# (v2/tests/cloud/conftest.py) both rely on this ordering.
V2_TABLES: tuple[str, ...] = (
    "parity_lines",
    "tally_bill_allocations",
    "tally_voucher_inventory_lines",
    "tally_voucher_ledger_lines",
    "parity_runs",
    "tally_vouchers",
    "sync_batches",
    "sync_runs",
    "sync_workspaces",
    "agent_devices",
    "sync_commands",
    "sync_quarantine",
    "sync_fy_coverage",
    "tally_currencies",
    "tally_groups",
    "tally_voucher_types",
    "tally_ledgers",
    "tally_stock_groups",
    "tally_units",
    "tally_stock_items",
    "tally_report_snapshots",
)
