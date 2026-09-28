"""Lazy maintenance (S1 spec D20, §7.6): each heartbeat runs one bounded slice of raw purge / parity retention /
batch-log retention / storage estimate. Task 6 only records that a slice ran; Task 11 fills in the actual work
(raw purge, parity retention, batch-log retention, storage estimate) behind this same ``run_slice`` call so the
heartbeat hook never has to change.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock
from v2.cloud.config import V2Settings
from v2.cloud.models import SyncWorkspace


@dataclass
class SliceReport:
    """Final shape (controller ruling F18) so Task 11 only has to fill in the numbers, never change the type."""

    ran: bool = False
    raw_nulled: int = 0
    parity_runs_pruned: int = 0
    parity_lines_pruned: int = 0
    batches_pruned: int = 0
    storage_estimate_bytes: int = 0
    storage_alert: bool = False
    elapsed_s: float = 0.0


async def run_slice(
    session: AsyncSession, sw: SyncWorkspace, settings: V2Settings, clock: Clock
) -> SliceReport:
    """Task 6: a no-op slice that only records it ran. Task 11 replaces the body with the bounded work (§4.9
    raw purge, parity/batch-log retention, storage estimate refresh) under the same ``V2_MAINTENANCE_SLICE_*``
    budgets — the heartbeat call site (``state.heartbeat``) never needs to change."""
    return SliceReport(ran=True)
