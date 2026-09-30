"""FakeBooks: GROUP / UNIT / STOCKITEM masters, the S0B* read-back collections, voucher types, and the two
opt-in failure modes (drop_flags, fail_imports) that Tasks 6-7 need. Talks straight to the transport with
wrap_import / wrap_collection — no TallyWriter methods here (Task 4 supplies create_group etc.)."""
from __future__ import annotations

import httpx
import pytest

from tally_bridge.envelopes import wrap_collection
from tally_bridge.xml_utils import read_objects
from probes.setup.import_xml import ImportResult, esc, wrap_import
from decimal import Decimal

from tests.probes.fake_books import FakeBooks, deemed_positive_matches, sync_client

B = "Sharma & Sons' Probe Traders"


def _post(books: FakeBooks, xml: str) -> str:
    return sync_client(books.transport()).post("/", content=xml, timeout=5).text


def _create(tag: str, name: str, inner: str) -> str:
    return wrap_import("All Masters", B, f'<{tag} NAME="{esc(name)}" ACTION="Create">{inner}</{tag}>')


def _group_xml(name: str, parent: str) -> str:
    return _create("GROUP", name, f'<NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST><PARENT>{esc(parent)}</PARENT>')


def _unit_xml(name: str) -> str:
    return _create("UNIT", name, f'<NAME>{esc(name)}</NAME>')


def _item_xml(name: str, parent: str, base_units: str) -> str:
    return _create("STOCKITEM", name,
                    f'<NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST><PARENT>{esc(parent)}</PARENT>'
                    f'<BASEUNITS>{esc(base_units)}</BASEUNITS>')


def _list(books: FakeBooks, collection: str, object_type: str, fields: list[str]) -> list[dict[str, str]]:
    xml = wrap_collection(collection, object_type, fields, B)
    return read_objects(_post(books, xml), object_type, fields)


# --- GROUP -----------------------------------------------------------------------------------------------------
def test_creating_a_group_then_listing_it_round_trips():
    books = FakeBooks(name=B)
    result = ImportResult.parse(_post(books, _group_xml("National Creditors", "Sundry Creditors")))
    assert result.created == 1 and result.clean

    rows = _list(books, "S0BGroups", "Group", ["Name", "Parent"])
    assert {"Name": "National Creditors", "Parent": "Sundry Creditors"} in rows


def test_a_duplicate_group_create_raises_the_modal():
    books = FakeBooks(name=B)
    _post(books, _group_xml("National Creditors", "Sundry Creditors"))
    with pytest.raises(httpx.ReadTimeout):
        _post(books, _group_xml("National Creditors", "Sundry Creditors"))


# --- UNIT --------------------------------------------------------------------------------------------------------
def test_creating_a_unit_then_listing_it_round_trips():
    books = FakeBooks(name=B)
    result = ImportResult.parse(_post(books, _unit_xml("Nos")))
    assert result.created == 1 and result.clean

    rows = _list(books, "S0BUnits", "Unit", ["Name"])
    assert {"Name": "Nos"} in rows


def test_a_duplicate_unit_create_raises_the_modal():
    books = FakeBooks(name=B)
    _post(books, _unit_xml("Nos"))
    with pytest.raises(httpx.ReadTimeout):
        # straight to the transport: the writer's own list-before-create guard is Task 4's job
        sync_client(books.transport()).post("/", content=_unit_xml("Nos"), timeout=5)


# --- STOCKITEM -------------------------------------------------------------------------------------------------
def test_creating_a_stock_item_then_listing_it_round_trips():
    books = FakeBooks(name=B)
    result = ImportResult.parse(_post(books, _item_xml("Widget", "Electronics", "Nos")))
    assert result.created == 1 and result.clean

    rows = _list(books, "S0BItems", "StockItem", ["Name", "Parent", "BaseUnits"])
    assert {"Name": "Widget", "Parent": "Electronics", "BaseUnits": "Nos"} in rows


def test_a_duplicate_stock_item_create_raises_the_modal():
    books = FakeBooks(name=B)
    _post(books, _item_xml("Widget", "Electronics", "Nos"))
    with pytest.raises(httpx.ReadTimeout):
        _post(books, _item_xml("Widget", "Electronics", "Nos"))


# --- S0BLedgers / S0BVouchers reuse the existing ledger/voucher state ---------------------------------------------
def test_s0b_ledgers_returns_the_seeded_ledgers():
    books = FakeBooks(name=B)
    rows = _list(books, "S0BLedgers", "Ledger", ["Name", "Parent"])
    assert {"Name": "Cash", "Parent": "Cash-in-Hand"} in rows


def test_s0b_vouchers_returns_a_created_voucher():
    books = FakeBooks(name=B)
    inner = ('<VOUCHER VCHTYPE="Payment" ACTION="Create"><DATE>1-Apr-25</DATE>'
             "<NARRATION>Rent</NARRATION><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME></VOUCHER>")
    ImportResult.parse(_post(books, wrap_import("Vouchers", B, inner)))

    rows = _list(books, "S0BVouchers", "Voucher", ["MasterId", "Narration"])
    assert {"MasterId": "51", "Narration": "Rent"} in rows


# --- voucher types -----------------------------------------------------------------------------------------------
def test_voucher_types_are_seeded_and_listable():
    books = FakeBooks(name=B)
    rows = _list(books, "S0BVoucherTypes", "VoucherType", ["Name"])
    names = {row["Name"] for row in rows}
    assert names == {"Sales", "Purchase", "Receipt", "Payment", "Contra", "Journal"}


# --- drop_flags ---------------------------------------------------------------------------------------------------
def test_drop_flags_discards_iscancelled_and_isoptional_on_readback():
    books = FakeBooks(name=B, drop_flags=True)
    inner = ('<VOUCHER VCHTYPE="Payment" ACTION="Create"><DATE>1-Apr-25</DATE>'
             "<NARRATION>Rent</NARRATION><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>"
             "<ISCANCELLED>Yes</ISCANCELLED><ISOPTIONAL>Yes</ISOPTIONAL></VOUCHER>")
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, inner)))
    assert result.created == 1 and result.clean          # the import itself still "succeeds"

    rows = _list(books, "S0BVouchers", "Voucher", ["MasterId", "IsCancelled", "IsOptional"])
    row = next(r for r in rows if r["MasterId"] == "51")
    assert row["IsCancelled"] == "No"
    assert row["IsOptional"] == "No"


def test_without_drop_flags_iscancelled_sticks():
    books = FakeBooks(name=B)
    inner = ('<VOUCHER VCHTYPE="Payment" ACTION="Create"><DATE>1-Apr-25</DATE>'
             "<NARRATION>Rent</NARRATION><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>"
             "<ISCANCELLED>Yes</ISCANCELLED></VOUCHER>")
    ImportResult.parse(_post(books, wrap_import("Vouchers", B, inner)))

    rows = _list(books, "S0BVouchers", "Voucher", ["MasterId", "IsCancelled"])
    row = next(r for r in rows if r["MasterId"] == "51")
    assert row["IsCancelled"] == "Yes"


# --- fail_imports --------------------------------------------------------------------------------------------------
def test_fail_imports_fails_every_import():
    books = FakeBooks(name=B, fail_imports=True)
    result = ImportResult.parse(_post(books, _group_xml("National Creditors", "Sundry Creditors")))
    assert result.created == 0
    assert result.errors == 1
    assert result.line_error == "fake import failure"

    # nothing was actually stored
    assert _list(books, "S0BGroups", "Group", ["Name"]) == []


# --- I5: the fake sees ISDEEMEDPOSITIVE, and rejects a flag that disagrees with its amount sign ----------------------
def _voucher_xml(entries: str, inventory: str = "") -> str:
    return ('<VOUCHER VCHTYPE="Receipt" ACTION="Create"><DATE>1-Apr-25</DATE>'
            "<NARRATION>[S0-B:1] x</NARRATION><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>"
            f"{entries}{inventory}</VOUCHER>")


def _entry(ledger: str, flag: str, amount: str) -> str:
    return (f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{esc(ledger)}</LEDGERNAME>"
            f"<ISDEEMEDPOSITIVE>{flag}</ISDEEMEDPOSITIVE><AMOUNT>{amount}</AMOUNT></ALLLEDGERENTRIES.LIST>")


def test_the_fake_stores_isdeemedpositive_on_every_line():
    """I5: the fake used to compute balances from `line["amount"]` alone and never stored the flag, so the one
    field whose wrong value is the documented cause of EXCEPTIONS=1 was invisible to every assertion."""
    books = FakeBooks(name=B)
    _post(books, wrap_import("Vouchers", B, _voucher_xml(
        _entry("Cash", "Yes", "-1000.00") + _entry("Pune Traders", "No", "1000.00"))))
    lines = books.state["vouchers"]["51"]["lines"]
    assert [(l["ledger"], l["deemed_positive"], l["amount"]) for l in lines] == [
        ("Cash", "Yes", "-1000.00"), ("Pune Traders", "No", "1000.00")]


def test_the_fake_rejects_a_line_whose_flag_disagrees_with_its_amount_sign():
    """Op 6/7/8: Yes always carries a NEGATIVE amount, No a POSITIVE one. Every other permutation the live
    exploration tried answered EXCEPTIONS=1 with no LINEERROR (v4 doc, cross-cutting finding 2) — the fake now
    approximates that acceptance rule instead of echoing whatever it is handed."""
    books = FakeBooks(name=B)
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, _voucher_xml(
        _entry("Cash", "No", "-1000.00") + _entry("Pune Traders", "Yes", "1000.00")))))
    assert result.exceptions == 1 and result.created == 0 and not result.clean
    assert books.state["vouchers"] == {}          # and nothing was stored


def test_the_fake_checks_the_inventory_row_and_its_accounting_allocation_too():
    """Where Task 6's I1 defect actually lived: the ledger lines were right and the ALLINVENTORYENTRIES block
    carried No against a negative amount."""
    books = FakeBooks(name=B)
    inventory = ('<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>A4 Paper</STOCKITEMNAME>'
                 "<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>-1000.00</AMOUNT>"
                 "<ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Purchase</LEDGERNAME>"
                 "<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-1000.00</AMOUNT>"
                 "</ACCOUNTINGALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>")
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, _voucher_xml(
        _entry("Mumbai Supplies", "No", "1180.00") + _entry("Input CGST", "Yes", "-180.00"), inventory))))
    assert result.exceptions == 1 and books.state["vouchers"] == {}


def test_a_zero_amount_pins_no_side_and_is_accepted_either_way():
    assert deemed_positive_matches("Yes", Decimal("0.00"))
    assert deemed_positive_matches("No", Decimal("0.00"))
    assert deemed_positive_matches("Yes", Decimal("-1.00")) and not deemed_positive_matches("Yes", Decimal("1.00"))
    assert deemed_positive_matches("No", Decimal("1.00")) and not deemed_positive_matches("No", Decimal("-1.00"))


# --- C32: Tally counts ACCOUNTINGALLOCATIONS toward the voucher total, so a nominal line + allocation double-counts ---
def _invoice_xml(entries: str, inventory: str) -> str:
    return ('<VOUCHER VCHTYPE="Sales" ACTION="Create"><DATE>20220401</DATE>'
            "<NARRATION>[S0-B:1] Sale</NARRATION><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>"
            "<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW><ISINVOICE>Yes</ISINVOICE>"
            f"{entries}{inventory}</VOUCHER>")


def _invoice_entry(ledger: str, flag: str, amount: str) -> str:
    return (f"<LEDGERENTRIES.LIST><LEDGERNAME>{esc(ledger)}</LEDGERNAME>"
            f"<ISDEEMEDPOSITIVE>{flag}</ISDEEMEDPOSITIVE><AMOUNT>{amount}</AMOUNT></LEDGERENTRIES.LIST>")


def _tag1_sale_entries_and_inventory(*, with_nominal_line: bool) -> tuple[str, str]:
    """Voucher [S0-B:1] of company_b_data.generate("educational") — the live C32 voucher (logs/debug-vch1-*.log)."""
    from probes.setup.company_b_data import generate
    v = next(v for v in generate("educational").vouchers if v.tag == 1)
    assert v.inventory and v.lines[1].ledger == "Domestic Sales"
    entries = "".join(_invoice_entry(l.ledger, "Yes" if l.deemed_positive else "No", f"{l.amount:.2f}")
                      for i, l in enumerate(v.lines) if with_nominal_line or i != 1)
    inv = v.inventory[0]
    inventory = (f"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{esc(inv.item)}</STOCKITEMNAME>"
                 f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>{inv.amount:.2f}</AMOUNT>"
                 "<ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Domestic Sales</LEDGERNAME>"
                 f"<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>{inv.amount:.2f}</AMOUNT>"
                 "</ACCOUNTINGALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>")
    return entries, inventory


def test_the_fake_rejects_the_old_c32_shape_nominal_line_plus_allocation():
    """C32 (live 2026-09-24, logs/debug-vch1-*.log): the nominal Sales ledger as a LEDGERENTRIES line AND in the
    inventory row's ACCOUNTINGALLOCATIONS — Tally counts the goods twice and answers EXCEPTIONS=1, no LINEERROR."""
    books = FakeBooks(name=B)
    entries, inventory = _tag1_sale_entries_and_inventory(with_nominal_line=True)
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, _invoice_xml(entries, inventory))))
    assert result.exceptions == 1 and result.created == 0 and result.line_error == ""
    assert books.state["vouchers"] == {}


def test_the_fake_accepts_the_c32_shape_and_books_the_nominal_ledger_from_the_allocation():
    """The live-accepted shape (CREATED=1): no nominal LEDGERENTRIES line. The allocation still posts to the
    nominal ledger, so the fake's Trial Balance must see Domestic Sales — otherwise balances drift silently."""
    books = FakeBooks(name=B)
    entries, inventory = _tag1_sale_entries_and_inventory(with_nominal_line=False)
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, _invoice_xml(entries, inventory))))
    assert result.created == 1 and result.clean
    lines = books.state["vouchers"]["51"]["lines"]
    total = sum(Decimal(l["amount"]) for l in lines)
    assert total == Decimal("0.00")
    assert [l["amount"] for l in lines if l["ledger"] == "Domestic Sales"] == ["8357.40"]


def test_the_fake_rejects_an_unbalanced_accounting_voucher():
    books = FakeBooks(name=B)
    result = ImportResult.parse(_post(books, wrap_import("Vouchers", B, _voucher_xml(
        _entry("Cash", "Yes", "-1000.00") + _entry("Pune Traders", "No", "900.00")))))
    assert result.exceptions == 1 and books.state["vouchers"] == {}


# --- C33: the fake honours a period variable only when it is TYPE="Date", like live Tally ----------------------------
from tally_bridge.envelopes import wrap_report
from tests.probes.fake_books import CURRENT_PERIOD, requested_period

_OLD_RECEIPT = ('<VOUCHER VCHTYPE="Receipt" ACTION="Create"><DATE>20220401</DATE>'
                "<NARRATION>[S0-B:1] old</NARRATION><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>"
                + _entry("Cash", "Yes", "-100.00") + _entry("Pune Traders", "No", "100.00") + "</VOUCHER>")


def _vouchers_in(books: FakeBooks, static_vars: dict[str, str], *, untyped: bool = False) -> list[str]:
    xml = wrap_collection("S0BVouchers", "Voucher", ["MasterId"], B, static_vars=static_vars)
    if untyped:
        xml = xml.replace(' TYPE="Date"', "")
    return [r["MasterId"] for r in read_objects(_post(books, xml), "VOUCHER", ["MasterId"])]


@pytest.mark.parametrize("from_text,to_text", [("01-04-2022", "31-03-2023"), ("20220401", "20230331"),
                                               ("1-Apr-2022", "31-Mar-2023")])
def test_a_typed_period_is_honoured_in_every_live_format(from_text, to_text):
    books = FakeBooks(name=B)
    assert ImportResult.parse(_post(books, wrap_import("Vouchers", B, _OLD_RECEIPT))).created == 1
    assert _vouchers_in(books, {"SVFROMDATE": from_text, "SVTODATE": to_text}) == ["51"]


def test_an_untyped_period_is_silently_replaced_by_the_current_period():
    """C33 (live 2026-09-24): untyped SVFROMDATE/SVTODATE come back with a healthy answer for the company's
    CURRENT period (1-Apr-2025..31-Mar-2026) — a 2022 voucher is simply not there."""
    books = FakeBooks(name=B)
    _post(books, wrap_import("Vouchers", B, _OLD_RECEIPT))
    assert _vouchers_in(books, {"SVFROMDATE": "01-04-2022", "SVTODATE": "31-03-2023"}, untyped=True) == []
    assert requested_period('<SVFROMDATE>01-04-2022</SVFROMDATE>') == CURRENT_PERIOD == ("20250401", "20260331")


def test_the_current_period_is_configurable():
    books = FakeBooks(name=B, current_period=("20220401", "20230331"))
    _post(books, wrap_import("Vouchers", B, _OLD_RECEIPT))
    assert _vouchers_in(books, {"SVFROMDATE": "01-04-2022", "SVTODATE": "31-03-2023"}, untyped=True) == ["51"]


def test_a_trial_balance_closes_as_on_the_typed_svtodate_only():
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["ledgers"].update({"Pune Traders": {"parent": "Sundry Debtors", "email": "",
                                                                      "alter_id": 1, "guid": "g"}}))
    _post(books, wrap_import("Vouchers", B, _OLD_RECEIPT))
    typed = _post(books, wrap_report("Trial Balance", "01-04-2021", "31-03-2022", B))
    untyped = _post(books, wrap_report("Trial Balance", "01-04-2021", "31-03-2022", B).replace(' TYPE="Date"', ""))
    assert "100.00" not in typed            # as on 31-03-2022 the 1-Apr-2022 receipt hasn't happened yet
    assert "100.00" in untyped              # untyped → current period (to 31-03-2026), which includes it


# --- C34: the fake files a bill by the SIGN of its amount, as live Tally did -----------------------------------------
from tally_bridge.sync_reports import parse_bills  # noqa: E402


def _sale_with_bill(bill_amount: str, tag: int = 1) -> str:
    bill = (f"<BILLALLOCATIONS.LIST><NAME>Inv/{tag}</NAME><BILLTYPE>New Ref</BILLTYPE>"
            f"<AMOUNT>{bill_amount}</AMOUNT></BILLALLOCATIONS.LIST>")
    return ('<VOUCHER VCHTYPE="Sales" ACTION="Create"><DATE>20250601</DATE>'
            f"<NARRATION>[S0-B:{tag}] Sale</NARRATION><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>"
            "<LEDGERENTRIES.LIST><LEDGERNAME>Pune Traders</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>"
            f"<AMOUNT>-9861.74</AMOUNT>{bill}</LEDGERENTRIES.LIST>"
            + _invoice_entry("Export Sales", "No", "9861.74") + "</VOUCHER>")


def _bills(books: FakeBooks, report: str) -> list[str]:
    return [b.get("bill_number") for b in parse_bills(_post(books, wrap_report(report, "31-03-2026", "31-03-2026", B)))]


def test_the_old_c34_shape_files_a_sales_bill_under_payables():
    """C34 (live UI 2026-09-24): [S0-B:1] sent BILLALLOCATIONS +9861.74 under a −9861.74 party line; Tally
    accepted it and filed Inv/1 under Bills PAYABLE (Bill collection ClosingBalance 9861.74)."""
    books = FakeBooks(name=B)
    assert ImportResult.parse(_post(books, wrap_import("Vouchers", B, _sale_with_bill("9861.74")))).created == 1
    assert _bills(books, "Bills Payable") == ["Inv/1"]
    assert _bills(books, "Bills Receivable") == []


def test_a_negative_sales_bill_is_a_receivable():
    books = FakeBooks(name=B)
    _post(books, wrap_import("Vouchers", B, _sale_with_bill("-9861.74")))
    assert _bills(books, "Bills Receivable") == ["Inv/1"]
    assert _bills(books, "Bills Payable") == []


def test_an_agst_ref_receipt_knocks_the_receivable_off():
    """The fake's knock-off mechanics on hand-built XML only. Whether the DATASET's receipts actually settle its
    receivables (review #5: none did) is test_company_b.py::test_a_full_load_leaves_exactly_the_bills_the_dataset_
    expects_open, which the fake can now fail (C35 refusals)."""
    books = FakeBooks(name=B)
    _post(books, wrap_import("Vouchers", B, _sale_with_bill("-9861.74")))
    receipt = ('<VOUCHER VCHTYPE="Receipt" ACTION="Create"><DATE>20250701</DATE>'
               "<NARRATION>[S0-B:2] r</NARRATION><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>"
               + _entry("Cash", "Yes", "-9861.74")
               + "<ALLLEDGERENTRIES.LIST><LEDGERNAME>Pune Traders</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
               "<AMOUNT>9861.74</AMOUNT><BILLALLOCATIONS.LIST><NAME>Inv/1</NAME><BILLTYPE>Agst Ref</BILLTYPE>"
               "<AMOUNT>9861.74</AMOUNT></BILLALLOCATIONS.LIST></ALLLEDGERENTRIES.LIST></VOUCHER>")
    assert ImportResult.parse(_post(books, wrap_import("Vouchers", B, receipt))).created == 1
    assert _bills(books, "Bills Receivable") == [] and _bills(books, "Bills Payable") == []


# --- C35 (review #1/#5): the fake refuses a bill shape Tally would reject or mis-book, instead of opening a bill -----
def _receipt_with_bill(name: str, amount: str, *, bill_type: str = "Agst Ref", bill_amount: str | None = None) -> str:
    return ('<VOUCHER VCHTYPE="Receipt" ACTION="Create"><DATE>20250701</DATE>'
            "<NARRATION>[S0-B:2] r</NARRATION><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>"
            + _entry("Cash", "Yes", f"-{amount}")
            + "<ALLLEDGERENTRIES.LIST><LEDGERNAME>Pune Traders</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
            f"<AMOUNT>{amount}</AMOUNT><BILLALLOCATIONS.LIST><NAME>{name}</NAME><BILLTYPE>{bill_type}</BILLTYPE>"
            f"<AMOUNT>{bill_amount or amount}</AMOUNT></BILLALLOCATIONS.LIST></ALLLEDGERENTRIES.LIST></VOUCHER>")


def _import(books: FakeBooks, voucher: str) -> ImportResult:
    return ImportResult.parse(_post(books, wrap_import("Vouchers", B, voucher)))


def test_the_fake_refuses_an_agst_ref_to_a_bill_that_was_never_opened():
    """Review #1: 288 of 288 dataset Agst Refs named a bill no voucher opened, and the fake silently opened a
    fresh (wrong-side) bill for each — so nothing offline could notice."""
    books = FakeBooks(name=B)
    result = _import(books, _receipt_with_bill("Inv/14", "5000.00"))
    assert result.exceptions == 1 and result.created == 0
    assert books.state["vouchers"] == {} and books.state["bills"] == {}


def test_the_fake_refuses_an_agst_ref_that_would_over_settle_its_bill():
    books = FakeBooks(name=B)
    _import(books, _sale_with_bill("-9861.74"))
    result = _import(books, _receipt_with_bill("Inv/1", "9861.75"))
    assert result.exceptions == 1 and result.created == 0
    assert books.state["bills"]["Inv/1"]["amount"] == "-9861.74"          # untouched


def test_the_fake_refuses_an_agst_ref_to_another_partys_bill():
    books = FakeBooks(name=B)
    _import(books, _sale_with_bill("-9861.74"))
    other = _receipt_with_bill("Inv/1", "100.00").replace("Pune Traders", "Nagpur Traders")
    assert _import(books, other).exceptions == 1


def test_the_fake_refuses_bills_that_do_not_add_up_to_the_party_line():
    books = FakeBooks(name=B)
    result = _import(books, _sale_with_bill("-9000.00"))                  # party line is -9861.74
    assert result.exceptions == 1 and books.state["bills"] == {}


def test_a_part_payment_leaves_the_rest_of_the_bill_open():
    books = FakeBooks(name=B)
    _import(books, _sale_with_bill("-9861.74"))
    assert _import(books, _receipt_with_bill("Inv/1", "4000.00")).created == 1
    assert books.state["bills"]["Inv/1"]["amount"] == "-5861.74"
    assert _bills(books, "Bills Receivable") == ["Inv/1"]


def test_an_on_account_receipt_opens_no_named_bill():
    books = FakeBooks(name=B)
    assert _import(books, _receipt_with_bill("On Account", "5000.00", bill_type="On Account")).created == 1
    assert books.state["bills"] == {}


# --- C39 (live 2026-09-24, logs/stock-opening-live-check-2026-09-24.log): stock OPENINGVALUE is booked by its sign --
from probes.reads import exploded_tb_rows, primary_group_rows  # noqa: E402


def _current_assets(books: FakeBooks) -> Decimal:
    rows = primary_group_rows(exploded_tb_rows(_post(books, wrap_report("Trial Balance", "01-04-2022", "31-03-2026", B))))
    return rows["Current Assets"]["closing_balance"]


def _stock_item_with_value(value: str) -> str:
    return _create("STOCKITEM", "USB Cable Type-C",
                   "<NAME.LIST><NAME>USB Cable Type-C</NAME></NAME.LIST><PARENT></PARENT><BASEUNITS>Nos</BASEUNITS>"
                   f"<OPENINGBALANCE>120 Nos</OPENINGBALANCE><OPENINGRATE>85.00/Nos</OPENINGRATE>"
                   f"<OPENINGVALUE>{value}</OPENINGVALUE>")


def test_a_positive_stock_opening_value_lands_on_the_credit_side_as_live():
    """Live: OPENINGVALUE +10200.00 was stored +10200 and the TB put it on the Cr side of Current Assets."""
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["units"].update({"Nos": {"base": "Nos", "additional": None, "conversion": None}}))
    assert ImportResult.parse(_post(books, _stock_item_with_value("10200.00"))).created == 1
    assert _current_assets(books) == Decimal("10200.00")          # positive = Cr (tb_xml's DSPCLCRAMTA)


def test_a_negative_stock_opening_value_is_a_debit_in_current_assets():
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["units"].update({"Nos": {"base": "Nos", "additional": None, "conversion": None}}))
    _post(books, _stock_item_with_value("-10200.00"))
    assert _current_assets(books) == Decimal("-10200.00")


# --- C40 (live 2026-09-24): an opening quantity in a compound unit's full name is silently dropped ---------------------
def _a4_paper(qty: str, rate: str) -> str:
    return _create("STOCKITEM", "A4 Paper Ream",
                   "<NAME.LIST><NAME>A4 Paper Ream</NAME></NAME.LIST><PARENT></PARENT>"
                   f"<BASEUNITS>Box of 10 Nos</BASEUNITS><OPENINGBALANCE>{qty}</OPENINGBALANCE>"
                   f"<OPENINGRATE>{rate}</OPENINGRATE><OPENINGVALUE>-14250.00</OPENINGVALUE>")


def _with_box_units(books: FakeBooks) -> None:
    books.edit_state(lambda s: s["units"].update({
        "Nos": {"base": "Nos", "additional": None, "conversion": None},
        "Box": {"base": "Box", "additional": None, "conversion": None},
        "Box of 10 Nos": {"base": "Box", "additional": "Nos", "conversion": "10"}}))


def test_an_opening_in_the_compound_units_full_name_is_accepted_but_stores_nothing():
    books = FakeBooks(name=B)
    _with_box_units(books)
    result = ImportResult.parse(_post(books, _a4_paper("15 Box of 10 Nos", "950.00/Box of 10 Nos")))
    assert result.created == 1                                    # live: created=1 ...
    item = books.state["items"]["A4 Paper Ream"]
    assert item["opening_qty"] == "" and Decimal(item["opening_value"]) == 0   # ... and no opening at all


def test_an_opening_in_the_compound_units_first_unit_is_kept():
    books = FakeBooks(name=B)
    _with_box_units(books)
    _post(books, _a4_paper("15 Box", "950.00/Box"))
    item = books.state["items"]["A4 Paper Ream"]
    assert item["opening_qty"] == "15 Box" and item["opening_value"] == "-14250.00"


# --- C42 (live 2026-09-24, logs/setup-b-live-2026-09-24-run4.log): flagged vouchers post nothing ---------------------
def _debtors(books: FakeBooks) -> Decimal:
    rows = exploded_tb_rows(_post(books, wrap_report("Trial Balance", "01-04-2022", "31-03-2026", B)))
    return next((r["closing_balance"] for r in rows if r["account_name"] == "Sundry Debtors"), Decimal("0.00"))


def _books_with_pune_traders() -> FakeBooks:
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["ledgers"].update({"Pune Traders": {"parent": "Sundry Debtors", "email": "",
                                                                      "alter_id": 1, "guid": "g"}}))
    return books


@pytest.mark.parametrize("field", ["cancelled", "optional"])
def test_a_voucher_the_operator_flags_after_its_create_moves_no_balance_and_opens_no_bill(field):
    """Live run 4: Sundry Debtors fell short of the everything-posts figure by EXACTLY the party amounts of the
    two cancelled (flagged by hand in the UI after the create) and two optional sales — Tally leaves both out of
    balances, and a flagged voucher's bill is in neither bills report."""
    books = _books_with_pune_traders()
    _import(books, _sale_with_bill("-9861.74"))
    assert _debtors(books) == Decimal("-9861.74") and _bills(books, "Bills Receivable") == ["Inv/1"]

    def flag(s):
        for v in s["vouchers"].values():
            v[field] = "Yes"
    books.edit_state(flag)
    assert _debtors(books) == Decimal("0.00")
    assert _bills(books, "Bills Receivable") == [] and _bills(books, "Bills Payable") == []
    assert books.posted_bills() == {}


def test_an_optional_voucher_sent_with_isoptional_posts_nothing():
    books = _books_with_pune_traders()
    assert _import(books, _sale_with_bill("-9861.74").replace(
        "<VOUCHERTYPENAME>", "<ISOPTIONAL>Yes</ISOPTIONAL><VOUCHERTYPENAME>")).created == 1
    assert _debtors(books) == Decimal("0.00") and _bills(books, "Bills Receivable") == []
