"""Tally duplicate-invoice check across the FY boundary (no Postgres needed).

With TYPED date vars (C33) the party-voucher window is binding, so the dedup
check must anchor it on the document date. Only the network edge is mocked:
the real dedup service → real ``get_party_vouchers`` → real builder → mock
Tally (which honours typed windows only) → real parser. The DB is a stub with
no prior written VoucherEntry rows (the B2(a) DB path is covered by the
DB-gated tests in test_dedup.py).
"""
from __future__ import annotations

import pytest

from backend.services.dedup import find_business_key_duplicate, find_duplicate
from backend.tally_bridge.client import TallyClient


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _EmptyDB:
    async def execute(self, _stmt):
        return _EmptyResult()


@pytest.fixture
async def client():
    c = TallyClient()
    c.mock_mode = True
    yield c
    await c.close()


async def test_prior_fy_invoice_reuploaded_in_new_fy_is_blocked(client):
    # INV-APX-2425-004 was entered in Tally on 15-05-2024 (FY 2024-25).
    dup = await find_business_key_duplicate(
        _EmptyDB(), client, workspace_id="ws",
        party_ledger="Apex Technologies Pvt Ltd",
        invoice_ref="INV-APX-2425-004", doc_date="2024-05-15",
    )
    assert dup is not None
    assert dup["voucher_no"] == "S2425-004"
    assert dup["reason"] == "same invoice no for party"


async def test_current_fy_invoice_still_detected(client):
    dup = await find_business_key_duplicate(
        _EmptyDB(), client, workspace_id="ws",
        party_ledger="Apex Technologies Pvt Ltd",
        invoice_ref="inv-apx-002 ", doc_date="2025-08-12",
    )
    assert dup is not None and dup["voucher_no"] == "7"


async def test_find_duplicate_threads_doc_date(client):
    dup = await find_duplicate(
        _EmptyDB(), client, workspace_id="ws", content_hash="",
        party_ledger="Apex Technologies Pvt Ltd",
        invoice_ref="INV-APX-2425-004", doc_date="20240515",
    )
    assert dup is not None and dup["voucher_no"] == "S2425-004"


async def test_unknown_ref_is_not_a_duplicate(client):
    dup = await find_business_key_duplicate(
        _EmptyDB(), client, workspace_id="ws",
        party_ledger="Apex Technologies Pvt Ltd",
        invoice_ref="INV-NEW-999", doc_date="2025-08-12",
    )
    assert dup is None
