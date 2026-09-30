"""Tests for compute_invoice_gross — shared party-gross helper (Finding 1).

The inventory write path must set the New Ref bill-allocation amount (and the
posted party leg) to the SAME gross the stock builder computes from the item
tuples (Σ qty×rate + per-bucket GST), never to Vision's extracted total. This
helper is the single source of that calc; the builder and the bill allocation
must reconcile exactly.
"""
import pytest

from tally_bridge.import_builder import (
    build_create_purchase_voucher,
    compute_invoice_gross,
)


def test_gross_single_rate_intra():
    # 10 × 100 = 1000 base, 18% GST = 180 → 1180
    items = [("A4 Paper", 10.0, 100.0, "Purchase Accounts", "Nos", 18)]
    assert compute_invoice_gross(items, "intra") == 1180.0


def test_gross_single_rate_inter_equals_intra():
    items = [("A4 Paper", 10.0, 100.0, "Purchase Accounts", "Nos", 18)]
    assert compute_invoice_gross(items, "inter") == compute_invoice_gross(items, "intra")


def test_gross_multi_rate_bucketed():
    # 5 × 200 @18% = 1000 + 180 ; 2 × 50 @5% = 100 + 5  → 1285
    items = [
        ("Laptop", 5.0, 200.0, "Purchase Accounts", "Nos", 18),
        ("Paper", 2.0, 50.0, "Purchase Accounts", "Nos", 5),
    ]
    assert compute_invoice_gross(items, "intra") == 1285.0


def test_gross_zero_gst():
    items = [("Exempt Good", 3.0, 100.0, "Purchase Accounts", "Nos", 0)]
    assert compute_invoice_gross(items, "intra") == 300.0


def test_gross_matches_builder_party_leg():
    """The helper's gross must equal the AMOUNT the builder posts on the party
    leg (and the bill allocation), so the voucher balances exactly."""
    items = [
        ("Laptop", 5.0, 200.0, "Purchase Accounts", "Nos", 18),
        ("Paper", 2.0, 50.0, "Purchase Accounts", "Nos", 5),
    ]
    gross = compute_invoice_gross(items, "intra")
    xml = build_create_purchase_voucher(
        date="20260210", voucher_number="INV-1", party="Croma",
        items=items, narration="n", gst_mode="intra", company="Co",
        bill_allocations=[{"name": "INV-1", "type": "New Ref", "amount": gross}],
    )
    # The party LEDGERENTRY posts party_total = base + tax = gross.
    assert f"<AMOUNT>{gross:.2f}</AMOUNT>" in xml
