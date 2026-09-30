import copy
import logging
from decimal import Decimal
from pathlib import Path

import pytest

import backend.sync.ingest.validate as validate_mod
from backend.sync.ingest.validate import parse_objects
from backend.sync.parity.rung0 import voucher_balances
from contract import transcode

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _vouchers(name):
    return transcode.vouchers_from_xml((SYNC / name).read_text(encoding="utf-8"))


def _codes(errors):
    return sorted({(e.index, e.code) for e in errors})


def test_all_b_month_09_vouchers_parse_and_balance():
    parsed, errors, _ = parse_objects(_vouchers("p21_B_fy2022_month_09.xml"))
    assert errors == [] and all(voucher_balances(v) for v in parsed)


def test_usd_sale_lines_carry_fx_and_stated_base():
    parsed, errors, _ = parse_objects(_vouchers("p22_B_forex_sales.xml"))
    usd = next(v for v in parsed if v.guid.endswith("-000003c1"))
    assert usd.has_forex and [(l.amount.inr, l.amount.fx_amount) for l in usd.lines] == [
        (Decimal("-37216.04"), Decimal("-448.44")), (Decimal("37216.04"), Decimal("448.44"))]


def test_cancelled_voucher_without_lines_or_party_is_valid():
    parsed, errors, _ = parse_objects(_vouchers("p03_B_flagged_month_2023_02.xml"))
    assert errors == []
    cancelled = [v for v in parsed if v.is_cancelled]
    assert cancelled and all(v.lines == [] and v.party_ledger_name == "" for v in cancelled)


def test_missing_required_key_is_missing_field():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:2])
    del objs[1]["data"]["alterid"]
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(1, "missing_field")] and "alterid" in errors[0].detail


def test_unbalanced_voucher_by_one_paisa():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-16538.67"
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "unbalanced_voucher")]


def test_forex_line_without_base_is_forex_base_missing():
    objs = copy.deepcopy([v for v in _vouchers("p22_B_forex_sales.xml") if v["data"]["guid"].endswith("-000003c1")])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-$448.44 @ ? 82.99/$"
    _, errors, _ = parse_objects(objs)
    assert (0, "forex_base_missing") in _codes(errors)


@pytest.mark.parametrize("field, value, code", [
    ("date", "2022-13-40", "invalid_date"), ("iscancelled", "Maybe", "invalid_logical"),
])
def test_bad_header_values(field, value, code):
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"][field] = value
    _, errors, _ = parse_objects(objs)
    assert (0, code) in _codes(errors)


def test_bad_line_amount_is_unparseable_amount():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][1]["amount"] = "14,0l5.82"
    _, errors, _ = parse_objects(objs)
    assert (0, "unparseable_amount") in _codes(errors)


def test_ledgerentries_list_key_is_duplicate_posting_list():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledgerentries_list"] = objs[0]["data"]["ledger_entries"]
    _, errors, _ = parse_objects(objs)
    assert (0, "duplicate_posting_list") in _codes(errors)


def test_unknown_kind():
    _, errors, _ = parse_objects([{"kind": "godown", "data": {"guid": "g"}}])
    assert _codes(errors) == [(0, "unknown_kind")]


def test_errors_are_collected_not_first_only():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:3])
    del objs[0]["data"]["date"]
    objs[2]["data"]["iscancelled"] = "x"
    _, errors, _ = parse_objects(objs)
    assert {e.index for e in errors} == {0, 2}


def test_sign_disagreeing_with_isdeemedpositive_is_warning_only():   # D23
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][1]["isdeemedpositive"] = "Yes"      # credit amount flagged deemed-positive
    parsed, errors, warnings = parse_objects(objs)
    assert errors == [] and (0, "sign_vs_deemed_positive") in {(w.index, w.code) for w in warnings}


def test_error_detail_never_contains_amounts_or_names():              # decision 14
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-16538.67"
    _, errors, _ = parse_objects(objs)
    assert "16538" not in errors[0].detail and "Indore" not in errors[0].detail


def test_masters_parse_with_reserved_prefix_cleaned():
    groups = transcode.masters_from_xml((SYNC / "p25_A_groups.xml").read_text(encoding="utf-8"), "group")
    for g in groups:                         # p25 lacks GUID/AlterID (fixture gap A4): add test identities
        g["data"].setdefault("guid", "t-" + g["data"]["name"])
        g["data"].setdefault("alterid", " 1")
    parsed, errors, _ = parse_objects(groups)
    assert errors == []
    assert next(p for p in parsed if p.name == "Current Assets").parent == "Primary"


def test_ledger_balance_expression_parsed():                           # p22_B_usd_ledger.xml
    obj = {"kind": "ledger_balance", "data": {"guid": "g1", "name": "Gulf Office Supplies LLC (USD)",
           "closingbalance": "-$1609.71 @ ? 82.58/$ = -? 132929.85", "captured_at": "2026-09-25T16:52:49+05:30"}}
    (p,), errors, _ = parse_objects([obj])
    assert errors == [] and p.closing.inr == Decimal("-132929.85") and p.closing.fx_amount == Decimal("-1609.71")


def test_ledger_balance_expression_without_base_is_accepted_as_needs_tb():   # D3 ledger rule
    obj = {"kind": "ledger", "data": {"guid": "g1", "alterid": " 3", "name": "USD Party", "parent": "Sundry Debtors",
           "closingbalance": "-$1609.71 @ ? 82.58/$", "captured_at": "2026-09-25T16:52:49+05:30"}}
    (p,), errors, _ = parse_objects([obj])
    assert errors == [] and p.fields["closingbalance"].inr is None and p.fields["closingbalance"].stated is False


# --- Fix round 1 (review task-8a-review.md: Important #1, #2, #3) -------------------------------------------


def test_bill_allocation_amount_without_stated_base_is_forex_base_missing():           # I1 (D3, brief rule 3)
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["bill_allocations"][0]["amount"] = "-$1.00 @ ? 82.99/$"
    _, errors, _ = parse_objects(objs)
    assert (0, "forex_base_missing") in _codes(errors)


def test_inventory_amount_without_stated_base_is_forex_base_missing():                 # I1 (D3, brief rule 3)
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["inventory_entries"][0]["amount"] = "-$1.00 @ ? 82.99/$"
    _, errors, _ = parse_objects(objs)
    assert (0, "forex_base_missing") in _codes(errors)


def test_bill_allocation_amount_with_stated_base_is_accepted():                        # I1, positive case
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["bill_allocations"][0]["amount"] = "-$1.00 @ ? 82.99/$ = -? 16538.66"
    (v,), errors, _ = parse_objects(objs)
    assert errors == []
    assert v.lines[0].bills[0].amount.inr == Decimal("-16538.66") and v.lines[0].bills[0].amount.stated is True


def test_stock_item_master_quantity_fields_are_not_parsed_as_money():                  # I2 (§5.3)
    items = transcode.masters_from_xml((SYNC / "p18_A_stock_item_openings.xml").read_text(encoding="utf-8"),
                                        "stock_item")
    for i in items:                                    # p18 lacks GUID/AlterID (fixture gap A4, as in p25 above)
        i["data"].setdefault("guid", "t-" + i["data"]["name"])
        i["data"].setdefault("alterid", " 1")
    parsed, errors, _ = parse_objects(items)
    assert errors == []
    paper = next(p for p in parsed if p.name == "A4 Paper Ream 500 sheets")
    assert paper.fields["openingbalance"] == Decimal("200")


def test_stock_item_compound_unit_opening_quantity_parses_leading_number():            # I2, C40 compound unit
    (item,) = transcode.masters_from_xml((SYNC / "p15_B_compound_unit_item.xml").read_text(encoding="utf-8"),
                                          "stock_item")
    item["data"]["guid"], item["data"]["alterid"] = "t-compound", " 1"
    assert item["data"]["openingbalance"] == " 19 Box 0 Nos"           # real capture, unmutated
    (p,), errors, _ = parse_objects([item])
    assert errors == [] and p.fields["openingbalance"] == Decimal("19")


def test_stock_item_closingvalue_still_parses_as_money():                              # I2, money field untouched
    obj = {"kind": "stock_item", "data": {"guid": "g1", "alterid": " 1", "name": "Widget", "parent": "Electronics",
           "baseunits": "Nos", "closingbalance": " 17 Nos", "closingvalue": "14015.82"}}
    (p,), errors, _ = parse_objects([obj])
    assert errors == []
    assert p.fields["closingbalance"] == Decimal("17") and p.fields["closingvalue"].inr == Decimal("14015.82")


def test_int_alterid_is_invalid_field_type_not_a_crash():                              # I3
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["alterid"] = 5
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "invalid_field_type")] and errors[0].detail == "alterid"


def test_int_date_is_invalid_field_type_not_a_crash():                                 # I3
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["date"] = 20220901
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "invalid_field_type")] and errors[0].detail == "date"


def test_list_instead_of_dict_data_is_invalid_field_type_not_a_crash():                # I3
    _, errors, _ = parse_objects([{"kind": "voucher", "data": ["not", "a", "dict"]}])
    assert _codes(errors) == [(0, "invalid_field_type")] and errors[0].detail == "data"


def test_non_dict_object_is_invalid_field_type_not_a_crash():                          # I3
    _, errors, _ = parse_objects(["not even an object"])
    assert _codes(errors) == [(0, "invalid_field_type")] and errors[0].detail == "object"


def test_null_field_does_not_crash_and_is_collected():                                 # I3
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["iscancelled"] = None
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "invalid_logical")] and errors[0].detail == "iscancelled"


def test_errors_still_collected_across_batch_with_a_typed_object_mixed_in():           # I3, no batch-wide crash
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:3])
    objs[1]["data"]["alterid"] = 5
    _, errors, _ = parse_objects(objs)
    assert {e.index for e in errors} == {1}


# --- Fix round 2 (task-8a-review.md re-review: a genuinely unexpected exception must not read as a deliberate
# typed-field rejection) ------------------------------------------------------------------------------------


def test_unexpected_exception_gets_its_own_code_is_logged_by_class_only_and_the_batch_survives(monkeypatch, caplog):
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:3])
    target_guid = objs[1]["data"]["guid"]

    def _boom(v):                                       # stands in for a genuine programming bug deep in a helper
        if v.guid == target_guid:
            raise RuntimeError("boom")
        return True                                      # a stub for the other two objects -- not re-testing rung0

    monkeypatch.setattr(validate_mod, "voucher_balances", _boom)
    with caplog.at_level(logging.WARNING, logger="backend.sync.ingest.validate"):
        parsed, errors, _ = parse_objects(objs)

    assert _codes(errors) == [(1, "unexpected_parse_error")]
    assert errors[0].detail == "object"
    assert {p.index for p in parsed} == {0, 2}           # the other objects in the batch still parsed

    [record] = [r for r in caplog.records if r.getMessage() == "v2.ingest.unexpected_parse_error"]
    assert record.exception_class == "RuntimeError" and record.kind == "voucher" and record.index == 1
    logged = repr(record.__dict__)
    assert target_guid not in logged and "Indore" not in logged and "16538" not in logged      # decision 14


def test_typed_field_guard_still_wins_over_the_unexpected_exception_safety_net():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["alterid"] = 5
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "invalid_field_type")]                                        # not unexpected_parse_error
