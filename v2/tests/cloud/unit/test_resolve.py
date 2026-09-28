"""Tests for S1 task 8b name -> GUID resolution (spec §4.4, §12 steps 6/8, D13, D14, D31). Pure -- no DB, no HTTP."""
import pytest

from v2.cloud.ingest.parsed import PMaster
from v2.cloud.ingest.resolve import NameIndex, ResolveError, guid_prefix_warning, order_objects


def test_group_and_ledger_may_share_a_name():                          # company A "Capital Account"
    idx = NameIndex.from_rows([("group", "g1", "Capital Account", False), ("ledger", "l1", "Capital Account", False)])
    assert idx.resolve("group", "Capital Account") == "g1" and idx.resolve("ledger", "Capital Account") == "l1"


def test_missing_and_ambiguous():
    idx = NameIndex.from_rows([("ledger", "l1", "Cash", False), ("ledger", "l2", "Cash", False),
                               ("ledger", "l3", "Old", True)])
    with pytest.raises(ResolveError) as e:
        idx.resolve("ledger", "Cash")
    assert e.value.code == "ambiguous_master"
    with pytest.raises(ResolveError) as e:
        idx.resolve("ledger", "Old")                                   # deleted rows never resolve
    assert e.value.code == "missing_master"


def test_rename_in_batch_moves_the_name():
    idx = NameIndex.from_rows([("ledger", "l1", "Old Name", False)])
    idx.rename("ledger", "l1", "New Name")
    assert idx.resolve("ledger", "New Name") == "l1"
    with pytest.raises(ResolveError):
        idx.resolve("ledger", "Old Name")


def test_names_are_exact_code_points():                                # probe 15
    idx = NameIndex.from_rows([("ledger", "h1", "शर्मा ट्रेडर्स", False)])
    assert idx.resolve("ledger", "शर्मा ट्रेडर्स") == "h1"


def test_guid_prefix_warning():
    c = "138b7373-753c-4dbe-aa63-b802035f0ba9"
    assert not guid_prefix_warning(c, c + "-000003c1") and guid_prefix_warning(c, "710de34a-x-00000001")


def test_add_is_a_live_master_of_this_batch():
    idx = NameIndex.from_rows([])
    idx.add("stock_item", "s1", "Widget")
    assert idx.resolve("stock_item", "Widget") == "s1"


def test_reserved_prefixed_name_matches_stripped_stored_name():
    # Task 1 carry: parse.name's U+0004 strip applies to BOTH the incoming query name and the names indexed from
    # rows, so a reserved-prefixed stored name still resolves under its clean form, and vice versa.
    idx = NameIndex.from_rows([("group", "g1", "\u0004 Primary", False)])
    assert idx.resolve("group", "Primary") == "g1"
    idx2 = NameIndex.from_rows([("group", "g2", "Primary", False)])
    assert idx2.resolve("group", "\u0004   Primary") == "g2"


def test_deleted_master_does_not_shadow_a_live_one_of_the_same_name():
    idx = NameIndex.from_rows([("ledger", "l1", "Cash", True), ("ledger", "l2", "Cash", False)])
    assert idx.resolve("ledger", "Cash") == "l2"


def _master(kind, guid, alter_id, name="x", parent=None):
    return PMaster(index=0, kind=kind, guid=guid, alter_id=alter_id, name=name, parent=parent, fields={}, raw={})


def test_order_objects_kind_order_then_alter_id_then_vouchers_last():
    from v2.cloud.ingest.parsed import PVoucher

    voucher = PVoucher(index=0, guid="v1", master_id="m1", alter_id=1, date=None, effective_date=None,
                        voucher_type_name="Sales", voucher_number="", reference="", party_ledger_name="",
                        narration="", is_cancelled=False, is_optional=False, is_post_dated=False, is_invoice=None,
                        lines=[], inventory=[], has_forex=False, raw={})
    ledger_2 = _master("ledger", "l2", 2)
    ledger_1 = _master("ledger", "l1", 1)
    group_1 = _master("group", "g1", 5)
    currency_1 = _master("currency", "c1", 3)

    ordered = order_objects([voucher, ledger_2, ledger_1, group_1, currency_1])
    assert ordered == [currency_1, group_1, ledger_1, ledger_2, voucher]


def test_order_objects_stock_group_before_ledger_stock_item_after():
    stock_group = _master("stock_group", "sg1", 1)
    ledger = _master("ledger", "l1", 1)
    stock_item = _master("stock_item", "si1", 1)
    unit = _master("unit", "u1", 1)
    voucher_type = _master("voucher_type", "vt1", 1)
    ordered = order_objects([stock_item, ledger, voucher_type, stock_group, unit])
    assert [o.kind for o in ordered] == ["voucher_type", "unit", "stock_group", "ledger", "stock_item"]
