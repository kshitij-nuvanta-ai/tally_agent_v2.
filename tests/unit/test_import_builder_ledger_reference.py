"""Unit tests — REFERENCE / REFERENCEDATE (supplier invoice no + date) threaded
through the ledger-based invoice builders and the payment builder (Phase 1 Part A).

The ledger builders (`build_create_purchase_voucher_ledger`,
`build_create_sales_voucher_ledger`, `build_create_debit_note`,
`build_create_credit_note`) and `build_create_payment_voucher` must accept
`reference`/`reference_date` and emit a `<REFERENCE>`/`<REFERENCEDATE>` block.
Asserts XML structure only — no live Tally calls.
"""
import xml.etree.ElementTree as ET

import pytest

from tally_bridge.import_builder import (
    build_create_credit_note,
    build_create_debit_note,
    build_create_payment_voucher,
    build_create_purchase_voucher_ledger,
    build_create_sales_voucher_ledger,
)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _purchase_ledger_xml(**kwargs):
    base = dict(
        date="20250928",
        party_ledger="Samsung India Electronics",
        purchase_ledger="Purchase - Electronics",
        amount=11800.0,
        narration="P001",
        company="X",
    )
    base.update(kwargs)
    return build_create_purchase_voucher_ledger(**base)


def _sales_ledger_xml(**kwargs):
    base = dict(
        date="20251001",
        party_ledger="Apex Technologies Pvt Ltd",
        sales_ledger="Sales - Electronics",
        amount=45000.0,
        narration="S001",
        company="X",
    )
    base.update(kwargs)
    return build_create_sales_voucher_ledger(**base)


def _debit_note_xml(**kwargs):
    base = dict(
        date="20250928",
        party_ledger="Samsung India Electronics",
        purchase_ledger="Purchase - Electronics",
        amount=1180.0,
        narration="DN001",
        company="X",
    )
    base.update(kwargs)
    return build_create_debit_note(**base)


def _credit_note_xml(**kwargs):
    base = dict(
        date="20251001",
        party_ledger="Apex Technologies Pvt Ltd",
        sales_ledger="Sales - Electronics",
        amount=4500.0,
        narration="CN001",
        company="X",
    )
    base.update(kwargs)
    return build_create_credit_note(**base)


def _payment_xml(**kwargs):
    base = dict(
        date="20250928",
        debit_ledger="Travel Expenses",
        credit_ledger="Cash",
        amount=500.0,
        narration="Cab fare",
        company="X",
    )
    base.update(kwargs)
    return build_create_payment_voucher(**base)


_BUILDERS = {
    "purchase_ledger": _purchase_ledger_xml,
    "sales_ledger": _sales_ledger_xml,
    "debit_note": _debit_note_xml,
    "credit_note": _credit_note_xml,
    "payment": _payment_xml,
}


@pytest.mark.parametrize("builder", _BUILDERS.values(), ids=list(_BUILDERS))
def test_reference_and_referencedate_both_present(builder):
    xml = builder(reference="CRO-2026-5678", reference_date="20260210")
    v = _root(xml).find(".//VOUCHER")
    refs = v.findall("REFERENCE")
    rdates = v.findall("REFERENCEDATE")
    assert len(refs) == 1 and refs[0].text == "CRO-2026-5678"
    assert len(rdates) == 1 and rdates[0].text == "20260210"


@pytest.mark.parametrize("builder", _BUILDERS.values(), ids=list(_BUILDERS))
def test_reference_omitted_emits_nothing(builder):
    xml = builder()
    v = _root(xml).find(".//VOUCHER")
    assert v.find("REFERENCE") is None
    assert v.find("REFERENCEDATE") is None


@pytest.mark.parametrize("builder", _BUILDERS.values(), ids=list(_BUILDERS))
def test_reference_only_no_date(builder):
    xml = builder(reference="INV-99")
    v = _root(xml).find(".//VOUCHER")
    assert v.findtext("REFERENCE") == "INV-99"
    assert v.find("REFERENCEDATE") is None


@pytest.mark.parametrize("builder", _BUILDERS.values(), ids=list(_BUILDERS))
def test_reference_date_invalid_format_raises(builder):
    with pytest.raises(ValueError, match="reference_date"):
        builder(reference="INV-99", reference_date="2026-02-10")
