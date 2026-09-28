"""Rung 0 of the parity engine (S1 spec §10.3, applied at ingest per §12 step 7): per-voucher double-entry check.

Sigma `line.amount.inr` over a voucher's ledger_entries must be exactly `Decimal("0.00")`, on the INR base -- forex
lines carry their converted INR amount (spec §5.4), so this needs no special forex handling. An empty line list (a
cancelled voucher, spec §5.4) sums to zero and balances trivially.
"""
from __future__ import annotations

from decimal import Decimal

from v2.cloud.ingest.parsed import PVoucher

_ZERO = Decimal("0.00")


def voucher_balances(v: PVoucher) -> bool:
    return sum((line.amount.inr for line in v.lines), _ZERO) == _ZERO
