"""Sync ORM models — S1 spec §4 (migration ``006_sync_tables``, formerly ``v2_001``).

The 21 sync tables are declared on the one declarative ``Base`` (``backend.db.models.Base``, v2 merge M4), next to
the 9 app tables, so their foreign keys point at the real ``users`` / ``workspaces`` tables. ``backend/db/__init__``
imports this package, so ``Base.metadata`` always holds all 30 tables.
"""
from __future__ import annotations

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
    "SYNC_TABLES",
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

# The 21 sync tables in FK-safe DELETE/DROP order (children first). `python -m backend.sync purge` (Task 11), the
# storage estimate (`backend/sync/maintenance.py`) and the DB test harness (tests/sync/conftest.py) rely on it.
SYNC_TABLES: tuple[str, ...] = (
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
