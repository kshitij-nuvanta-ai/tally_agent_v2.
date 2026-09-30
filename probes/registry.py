"""Every S0 probe (built or not), and the run orders from the S0 spec §6 with the §4.2 anchors steps."""
from __future__ import annotations

import importlib
from dataclasses import dataclass

from probes.core import Probe

ANCHORS_BEFORE_PARITY = "anchors:before_parity"
ANCHORS_AFTER_A = "anchors:after_a_batch"
OrderStep = tuple[int | str, str]


@dataclass(frozen=True)
class ProbeInfo:
    id: int
    name: str
    companies: str          # "A", "A+B", "—" for deferred
    tier: str               # "B", "C", "B+C"
    deferred: bool = False
    module: str | None = None


PROBES: tuple[ProbeInfo, ...] = (
    ProbeInfo(0, "environment", "A", "B", module="probes.p00_environment"),
    ProbeInfo(1, "company_counters", "A", "B", module="probes.p01_company_counters"),
    ProbeInfo(2, "active_company_guid", "A", "B", module="probes.p02_active_company_guid"),
    ProbeInfo(3, "voucher_ids_flags", "A+B", "B", module="probes.p03_voucher_ids_flags"),
    ProbeInfo(4, "alterid_filter", "A", "B", module="probes.p04_alterid_filter"),
    ProbeInfo(5, "voucher_month_bounds", "B", "B", module="probes.p05_voucher_month_bounds"),
    ProbeInfo(6, "nested_lines_ledger_guid", "A", "B", module="probes.p06_nested_lines_ledger_guid"),
    ProbeInfo(7, "deleted_vouchers", "A", "B", module="probes.p07_deleted_vouchers"),
    ProbeInfo(8, "ledger_rename", "A", "B", module="probes.p08_ledger_rename"),
    ProbeInfo(9, "chunk_latency", "—", "C", deferred=True),
    ProbeInfo(10, "error_shapes", "A", "B", module="probes.p10_error_shapes"),
    ProbeInfo(11, "openings", "B", "B", module="probes.p11_openings"),
    ProbeInfo(12, "current_snapshots", "A", "B", module="probes.p12_current_snapshots"),
    ProbeInfo(13, "backup_restore", "A", "B", module="probes.p13_backup_restore"),
    ProbeInfo(14, "special_char_company", "B", "B", module="probes.p14_special_char_company"),
    ProbeInfo(15, "unicode_compound_units", "B", "B", module="probes.p15_unicode_compound_units"),
    ProbeInfo(16, "ledger_closing_balance", "A+B", "B", module="probes.p16_ledger_closing_balance"),
    ProbeInfo(17, "ledger_level_tb", "A", "B", module="probes.p17_ledger_level_tb"),
    ProbeInfo(18, "historical_reports", "A+B", "B", module="probes.p18_historical_reports"),
    ProbeInfo(19, "counter_stability", "A", "B", module="probes.p19_counter_stability"),
    ProbeInfo(20, "parity_cost", "—", "C", deferred=True),
    ProbeInfo(21, "full_history_reach", "B", "B+C", module="probes.p21_full_history_reach"),
    ProbeInfo(22, "forex", "B", "B", module="probes.p22_forex"),   # plan part 7 (C36 lifted)
    ProbeInfo(23, "gst_due_dates", "A+B", "B", module="probes.p23_gst_due_dates"),
    ProbeInfo(24, "secured_company", "C", "B", module="probes.p24_secured_company"),
    ProbeInfo(25, "masters_classification", "A+B", "B", module="probes.p25_masters_classification"),
)

FIRST_ORDER: list[OrderStep] = [
    (0, "A"), (2, "A"), (1, "A"),
    (ANCHORS_BEFORE_PARITY, "A"), (16, "A"), (17, "A"), (18, "A"), (ANCHORS_AFTER_A, "A"),
    (5, "B"), (21, "B"), (16, "B"), (18, "B"),
]

ALL_ORDER: list[OrderStep] = [
    (0, "A"), (2, "A"), (1, "A"),
    (ANCHORS_BEFORE_PARITY, "A"), (16, "A"), (17, "A"), (18, "A"), (19, "A"),
    (3, "A"), (4, "A"), (6, "A"), (12, "A"), (23, "A"), (25, "A"),
    (7, "A"), (8, "A"), (10, "A"), (13, "A"), (ANCHORS_AFTER_A, "A"),
    # 5 before 21: probe 21 fetches with probe 5's confirmed request (S0-D7); spec §6 "21 first" = first after 5.
    # 14 LAST in company B (Ruling Q6, spec §6 Changed 2026-09-24): it deliberately sends malformed XML that may
    # leave Tally behind a popup, so nothing else in B runs after it.
    (5, "B"), (21, "B"), (16, "B"), (18, "B"), (3, "B"), (11, "B"), (15, "B"), (22, "B"), (23, "B"), (25, "B"), (14, "B"),
    (24, "C"),
]


def is_anchor_step(step: int | str) -> bool:
    return isinstance(step, str) and step.startswith("anchors:")


def info(probe_id: int) -> ProbeInfo:
    for item in PROBES:
        if item.id == probe_id:
            return item
    raise KeyError(f"No probe {probe_id}")


def load_probe(probe_id: int) -> Probe | None:
    item = info(probe_id)
    if item.module is None:
        return None
    return importlib.import_module(item.module).PROBE
