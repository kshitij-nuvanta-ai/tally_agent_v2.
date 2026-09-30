import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from probes.companies import COMPANIES, SEED_COMPANY
from probes.setup.import_xml import ImportResult, wrap_import
from probes.setup.writes import TallyWriter, WriteFailed, WriteRefused
from tests.probes.fake_books import FakeBooks, import_result, sync_client
from tests.probes.fakes import FakeTally, objects_xml

A = COMPANIES["A"]
B = COMPANIES["B"]


def _writer(books):
    said: list[str] = []
    return TallyWriter(sync_client(books.transport()), said.append), said


def _imports(books):
    return [r for r in books.requests if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in r]


def test_import_result_parses_the_live_response_shape():
    result = ImportResult.parse(import_result(created=1, last_vch_id="51"))
    assert (result.created, result.altered, result.deleted, result.errors, result.last_vch_id) == (1, 0, 0, 0, "51")
    assert result.clean
    assert not ImportResult.parse(import_result(errors=1, line_error="Voucher not found")).clean


def test_copied_import_envelope_escapes_and_checks_the_report():
    root = ET.fromstring(wrap_import("All Masters", "Sharma & Sons' Probe Traders", "<LEDGER/>"))
    assert root.find(".//SVCURRENTCOMPANY").text == "Sharma & Sons' Probe Traders"
    with pytest.raises(ValueError):
        wrap_import("Everything", A, "<LEDGER/>")


def test_payment_create_alter_delete_round_trip():
    books = FakeBooks(name=A)
    writer, _ = _writer(books)
    master_id = writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-throwaway 1")
    assert master_id == "51" and books.state["alt_vch"] == 51
    writer.alter_voucher_narration(A, master_id, "S0-throwaway 1 (altered)")
    assert books.state["vouchers"]["51"]["narration"] == "S0-throwaway 1 (altered)"
    writer.delete_voucher(A, master_id)
    assert books.state["vouchers"] == {} and books.state["alt_vch"] == 55


def test_payment_uses_the_verified_voucher_shape():
    books = FakeBooks(name=A)
    writer, _ = _writer(books)
    writer.delete_voucher(A, writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-t"))
    create, delete = _imports(books)
    for fragment in ("<ALLLEDGERENTRIES.LIST>", "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
                     "<DATE>20260331</DATE>", "<AMOUNT>-1.00</AMOUNT>", "<AMOUNT>1.00</AMOUNT>",
                     "<LEDGERNAME>Cash</LEDGERNAME>", "<REPORTNAME>Vouchers</REPORTNAME>"):
        assert fragment in create
    assert 'DATE="31-Mar-2026"' in delete and 'TAGNAME="Master ID" TAGVALUE="51"' in delete


def test_post_dated_payment_sends_the_flag_and_logs_the_read_back():
    books = FakeBooks(name=A)
    writer, said = _writer(books)
    writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-pd", post_dated=True)
    assert "<ISPOSTDATED>Yes</ISPOSTDATED>" in _imports(books)[0]
    assert books.state["vouchers"]["51"]["post_dated"] == "Yes"
    assert any("read-back IsPostDated" in line for line in said)


def test_post_dated_create_missing_from_the_readback_still_returns_the_master_id():
    """I3: a post-dated voucher can be created (Tally confirms LASTVCHID) but not show up in the Voucher collection
    read-back yet — that must not raise, and the operator can still delete it by the Master ID."""
    fake = FakeTally([A])

    def import_handler(body):
        return import_result(deleted=1, last_vch_id="99") if 'ACTION="Delete"' in body else \
            import_result(created=1, last_vch_id="99")

    fake.route("<TALLYREQUEST>Import Data</TALLYREQUEST>", import_handler)
    fake.route("S0OpVouchers", lambda body: objects_xml("VOUCHER", []))    # the voucher never shows up on read-back
    said: list[str] = []
    writer = TallyWriter(sync_client(fake.transport()), said.append)

    master_id = writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-pd-missing",
                                      post_dated=True)
    assert master_id == "99"
    assert any("post-dated voucher created (LASTVCHID 99) but not listed in the Voucher collection" in line
              for line in said)

    writer.delete_voucher(A, master_id)   # doesn't raise "still there": the read-back never shows it either way


def test_non_post_dated_create_missing_from_the_readback_still_raises():
    """The I3 relaxation is only for post_dated=True; an ordinary create with no read-back proof still fails."""
    fake = FakeTally([A])
    fake.route("<TALLYREQUEST>Import Data</TALLYREQUEST>", lambda body: import_result(created=1, last_vch_id="99"))
    fake.route("S0OpVouchers", lambda body: objects_xml("VOUCHER", []))
    writer = TallyWriter(sync_client(fake.transport()), lambda line: None)
    with pytest.raises(WriteFailed, match="not found on read-back"):
        writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-missing")


def test_writes_to_a_company_without_probe_are_refused_before_sending():
    books = FakeBooks(name="Bharat Traders Private Limited")
    writer, _ = _writer(books)
    before = len(books.requests)
    with pytest.raises(WriteRefused):
        writer.create_payment("Bharat Traders Private Limited", ledger="Electricity", amount=Decimal("1"),
                              narration="S0-x")
    with pytest.raises(WriteRefused):
        writer.delete_ledger("Bharat Traders Private Limited", "Cash")
    assert len(books.requests) == before


def test_seed_rename_is_the_only_exception():
    books = FakeBooks(name=SEED_COMPANY)
    writer, _ = _writer(books)
    with pytest.raises(WriteRefused):
        writer.rename_company(SEED_COMPANY, "Bharat Traders Copy")
    writer.rename_company(SEED_COMPANY, A)
    assert books.companies() == [A]
    assert "<COMPANY NAME=\"Bharat Traders Private Limited\" ACTION=\"Alter\"><NAME>" in _imports(books)[0]


def test_an_empty_value_alter_is_refused():
    writer, _ = _writer(FakeBooks(name=A))
    with pytest.raises(ValueError):
        writer.alter_ledger_email(A, "Electricity", "")


def test_ledger_create_alter_delete_and_rename():
    books = FakeBooks(name=A)
    writer, _ = _writer(books)
    writer.create_ledger(A, "S0 Probe Ledger", "Indirect Expenses")
    writer.alter_ledger_email(A, "S0 Probe Ledger", "s0probe-1@example.com")
    assert books.state["ledgers"]["S0 Probe Ledger"]["email"] == "s0probe-1@example.com"
    writer.delete_ledger(A, "S0 Probe Ledger")
    assert "S0 Probe Ledger" not in books.state["ledgers"]
    writer.rename_ledger(A, "Rajesh Computers", "Rajesh Computers S0")
    writer.rename_ledger(A, "Rajesh Computers S0", "Rajesh Computers")
    assert "Rajesh Computers" in books.state["ledgers"]
    assert books.state["alt_mst"] == 266 + 5


def test_duplicate_ledger_create_is_refused_before_sending():
    books = FakeBooks(name=A)
    writer, _ = _writer(books)
    with pytest.raises(WriteFailed, match="already exists"):
        writer.create_ledger(A, "Electricity", "Indirect Expenses")
    assert _imports(books) == []
    assert not books.popup


def test_altered_1_without_the_change_fails_the_write():
    fake = FakeTally([A])
    fake.route("<TALLYREQUEST>Import Data</TALLYREQUEST>", lambda body: import_result(altered=1))
    fake.route("S0OpLedger", lambda body: objects_xml("LEDGER", [
        {"Name": "S0 Probe Ledger", "Parent": "Indirect Expenses", "Email": "", "AlterID": "1"}]))
    writer = TallyWriter(sync_client(fake.transport()), lambda line: None)
    with pytest.raises(WriteFailed, match="not proof"):
        writer.alter_ledger_email(A, "S0 Probe Ledger", "s0probe-1@example.com")


def test_duplicate_stock_group_raises_the_popup():
    books = FakeBooks(name=A)
    writer, _ = _writer(books)
    assert writer.raise_duplicate_master_popup(A) == "timeout"
    assert books.popup
    with pytest.raises(WriteFailed):
        writer.company_names()


def test_licence_info_reads_mode_and_release():
    writer, _ = _writer(FakeBooks(name=A))
    info = writer.licence_info()
    assert info.educational is True and info.release == "7.0"
    assert _writer(FakeBooks(name=A, educational=False))[0].licence_info().educational is False


# --- master writers (company B) -------------------------------------------------------------------------------------

def test_creating_a_group_then_listing_it_round_trips():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_group(B, "National Creditors", "Sundry Creditors")
    assert writer.list_groups(B)["National Creditors"] == "Sundry Creditors"


def test_create_group_lists_before_creating_and_reads_back():
    books = FakeBooks(name=B)
    writer, said = _writer(books)
    writer.create_group(B, "Local Creditors", "Sundry Creditors")
    assert writer.list_groups(B)["Local Creditors"] == "Sundry Creditors"
    assert len(_imports(books)) == 1


def test_a_second_create_sends_nothing():
    books = FakeBooks(name=B)
    writer, said = _writer(books)
    writer.create_group(B, "Local Creditors", "Sundry Creditors")
    writer.create_group(B, "Local Creditors", "Sundry Creditors")
    assert len(_imports(books)) == 1                      # the second call never reached Tally
    assert any("already exists" in line for line in said)


def test_a_simple_unit_has_no_name_attribute_and_is_marked_simple():
    """Op 1 (live-verified, docs/tally-write-exploration-v4.md:35-39): no NAME attribute, ISSIMPLEUNIT=Yes."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_unit(B, "Nos")
    sent = _imports(books)[-1]
    assert '<UNIT ACTION="Create">' in sent
    assert 'NAME="Nos"' not in sent
    assert "<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>" in sent


def test_a_compound_unit_sends_its_first_unit_as_base_and_its_second_as_additional():
    """C31: live Tally answered BASEUNITS=Nos + ADDITIONALUNITS=Nos with "Next Unit already contains the First
    unit!". "Box of 10 Nos" is first unit Box (BASEUNITS) x CONVERSION 10 = second unit Nos (ADDITIONALUNITS)."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_unit(B, "Nos")
    writer.create_unit(B, "Box")
    writer.create_unit(B, "Box of 10 Nos", first_unit="Box", second_unit="Nos", conversion=10)
    sent = _imports(books)[-1]
    assert "<BASEUNITS>Box</BASEUNITS>" in sent
    assert "<ADDITIONALUNITS>Nos</ADDITIONALUNITS>" in sent
    assert "<CONVERSION>10</CONVERSION>" in sent
    assert "<ISSIMPLEUNIT>No</ISSIMPLEUNIT>" in sent
    assert "Box of 10 Nos" in writer.list_units(B)


def test_a_compound_unit_whose_name_is_not_tallys_generated_name_is_refused_before_sending():
    """Tally names a compound unit "<first> of <conversion> <second>"; any other name could never be found on
    read-back, so it is refused up front."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="Box of 10 Nos"):
        writer.create_unit(B, "Carton", first_unit="Box", second_unit="Nos", conversion=10)
    assert _imports(books) == []


def test_the_fake_rejects_a_compound_unit_whose_two_units_are_the_same():
    """The live failure (logs/setup-b-live-2026-09-24.log), reproduced offline so the regression is caught here."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_unit(B, "Nos")
    inner = ('<UNIT ACTION="Create">\n  <NAME>Box of 10 Nos</NAME>\n  <BASEUNITS>Nos</BASEUNITS>\n'
             '  <ADDITIONALUNITS>Nos</ADDITIONALUNITS>\n  <CONVERSION>10</CONVERSION>\n  <ISSIMPLEUNIT>No</ISSIMPLEUNIT>\n'
             '</UNIT>')
    result = writer.import_("All Masters", B, inner)
    assert (result.created, result.exceptions) == (0, 1)
    assert result.line_error == "Next Unit already contains the First unit!"
    assert "Box of 10 Nos" not in writer.list_units(B)


def test_a_stock_item_without_an_hsn_is_created_gst_not_applicable():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_stock_item(B, "Whiteboard Marker", unit="Nos")
    sent = _imports(books)[-1]
    assert "<GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>" in sent    # LESSONS §15 r12
    assert "HSNCODE" not in sent


def test_a_stock_item_with_an_hsn_carries_both_the_top_level_and_the_nested_copy():
    """Op 3 gotcha (docs/tally-write-exploration-v4.md:98): HSNCODE/HSN are duplicated at top level and inside
    HSNDETAILS.LIST — both are needed."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_stock_item(B, "Monitor 24in", unit="Nos", hsn="8528")
    sent = _imports(books)[-1]
    assert "<GSTAPPLICABLE>Applicable</GSTAPPLICABLE>" in sent
    assert "<GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>" in sent
    assert "<HSNCODE>8528</HSNCODE>\n  <HSN>8528</HSN>" in sent            # top-level copy
    assert "<HSNDETAILS.LIST><HSNCODE>8528</HSNCODE></HSNDETAILS.LIST>" in sent    # nested copy


def test_a_stock_item_with_an_opening_balance_carries_qty_rate_and_value():
    """Op 3 shapes (docs/tally-write-exploration-v4.md:90-92): OPENINGBALANCE/OPENINGRATE carry the unit name;
    the rate and value are money (2dp), the quantity is not. C39 (live 2026-09-24): OPENINGVALUE is SIGNED like a
    ledger opening (C30) — an asset's value is a debit, so it goes out NEGATIVE; +10200 landed on the Cr side."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_stock_item(B, "Monitor 24in", unit="TstN",
                             opening_qty=Decimal("5"), opening_rate=Decimal("11000"))
    sent = _imports(books)[-1]
    assert "<OPENINGBALANCE>5 TstN</OPENINGBALANCE>" in sent
    assert "<OPENINGRATE>11000.00/TstN</OPENINGRATE>" in sent
    assert "<OPENINGVALUE>-55000.00</OPENINGVALUE>" in sent


def test_a_party_ledger_carries_bill_wise_and_a_valid_gstin():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_party_ledger(B, "Pune Traders", parent="Local Creditors", bill_wise=True,
                               gstin="27AAAPL1234C1ZV")
    sent = _imports(books)[-1]
    assert "<ISBILLWISEON>Yes</ISBILLWISEON>" in sent
    assert "<PARTYGSTIN>27AAAPL1234C1ZV</PARTYGSTIN>" in sent


def test_a_negative_dataset_opening_is_sent_signed_on_the_wire():
    """C30 (overturns C21/F11): Tally reads OPENINGBALANCE's sign — negative = Dr, positive = Cr — it does NOT
    infer the side from the parent group. Live evidence: backend/tally_bridge/import_builder.py's abs() landed
    company A's HDFC −5,00,000 / SBI −2,00,000 as CREDITS
    (tests/fixtures/sync/c33_untyped_2026-09-23/p18_A_ledger_list.xml shows them positive, like Capital Account). So the dataset's debit-negative value goes on the wire as is."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_party_ledger(B, "Kolhapur Retail Mart", parent="Sundry Debtors", bill_wise=False,
                               opening=Decimal("-45000.00"))
    sent = _imports(books)[-1]
    assert "<OPENINGBALANCE>-45000.00</OPENINGBALANCE>" in sent


def test_a_positive_dataset_opening_is_sent_as_a_positive_credit():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_party_ledger(B, "Capital Account", parent="Capital Account", bill_wise=False,
                               opening=Decimal("1000000.00"))
    assert "<OPENINGBALANCE>1000000.00</OPENINGBALANCE>" in _imports(books)[-1]


def _tb_row(books, group):
    return next(row for row in books._trial_balance_rows(books.state) if row[0] == group)


def test_a_debit_opening_lands_on_the_debit_side_of_the_fake_trial_balance():
    """The fake must read the wire sign the way Tally does (negative = Dr), so the balance tests mean something."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_party_ledger(B, "Kolhapur Retail Mart", parent="Sundry Debtors", bill_wise=False,
                               opening=Decimal("-45000.00"))
    assert _tb_row(books, "Sundry Debtors")[1:] == ("-45000.00", "")


def test_the_fake_reads_the_wire_sign_not_the_parent_groups_nature():
    """A positive OPENINGBALANCE under a debit-nature group (an overdraft under Bank Accounts) is a CREDIT in
    Tally — the fake must not re-sign it from the group (the disproven Op 5 / C21 rule)."""
    books = FakeBooks(name=B)
    xml = wrap_import("All Masters", B, '<LEDGER NAME="HDFC OD" ACTION="Create">\n  <NAME.LIST><NAME>HDFC OD</NAME>'
                      '</NAME.LIST>\n  <PARENT>Bank Accounts</PARENT>\n  <OPENINGBALANCE>25000.00</OPENINGBALANCE>'
                      '\n</LEDGER>')
    sync_client(books.transport()).post("/", content=xml.encode("utf-8"))
    assert _tb_row(books, "Bank Accounts")[1:] == ("", "25000.00")


@pytest.mark.parametrize("name, parent, opening, match", [
    ("HDFC OD A/c", "Bank Accounts", Decimal("25000.00"), "contra-natural"),     # a CREDIT under a debit group
    ("Advance from Kolhapur", "Sundry Debtors", Decimal("1000.00"), "contra-natural"),  # a debtor in credit
    ("Drawings", "Capital Account", Decimal("-5000.00"), "contra-natural"),      # a DEBIT under a credit group
    ("Pune Traders", "Local Creditors", Decimal("1.00"), "nature is unknown"),   # m3: natural credit, custom group
])
def test_a_contra_natural_or_unclassifiable_opening_is_refused_before_anything_is_sent(name, parent, opening, match):
    """M1, kept as a DATASET sanity check after C30: the wire is now signed, so a contra-natural opening would
    land where its sign says — but in this dataset one is far likelier a sign slip than a real overdraft, so it is
    still refused, as is an opening under a group whose nature this module cannot name. m3: each case matches
    its own branch's message, so the custom-group case cannot pass via the contra-natural branch."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match=match):
        writer.create_party_ledger(B, name, parent=parent, bill_wise=False, opening=opening)
    assert books.requests == []


@pytest.mark.parametrize("opening", [Decimal("-18000.00"), Decimal("18000.00")])
def test_a_duties_and_taxes_opening_may_sit_on_either_side(opening):
    """m1: an Input GST ledger carries a DEBIT opening (ITC carried forward), Output GST a credit — both normal."""
    from probes.setup.writes import check_opening_side

    check_opening_side("Input CGST", "Duties & Taxes", opening)


def test_every_company_b_opening_matches_its_parent_groups_nature():
    """M1: the loader's own dataset must never trip the contra-natural refusal."""
    from probes.setup.company_b_data import generate
    from probes.setup.writes import check_opening_side

    for ledger in generate().ledgers:
        if ledger.opening is not None:
            check_opening_side(ledger.name, ledger.parent, ledger.opening)


def test_the_non_billwise_debtor_is_written_bill_wise_off():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_party_ledger(B, "Kolhapur Retail Mart", parent="Sundry Debtors", bill_wise=False)
    assert "<ISBILLWISEON>No</ISBILLWISEON>" in _imports(books)[-1]


def test_a_write_to_a_company_without_probe_in_the_name_is_refused():
    books = FakeBooks(name="Sharma & Sons Traders")
    writer, _ = _writer(books)
    with pytest.raises(WriteRefused):
        writer.create_group("Sharma & Sons Traders", "Local Creditors", "Sundry Creditors")
    assert _imports(books) == []


def test_a_sales_voucher_uses_the_verified_sign_convention():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Sales", date="20230601", narration="[S0-B:7] sale", party="Pune Traders",
        lines=[("Pune Traders", Decimal("-11800.00"), True), ("Sales", Decimal("10000.00"), False),
               ("Output CGST", Decimal("900.00"), False), ("Output SGST", Decimal("900.00"), False)],
        inventory=[("A4 Paper", "Nos", Decimal("10"), Decimal("1000.00"), Decimal("10000.00"))],
        bills=[("B/7", "New Ref", Decimal("11800.00"), "30 Days")])
    sent = _imports(books)[-1]
    assert "<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>" in sent          # party
    assert "<AMOUNT>-11800.00</AMOUNT>" in sent
    assert "<ALLINVENTORYENTRIES.LIST>" in sent                         # r15: goods via the stock grid
    assert "<BILLCREDITPERIOD>30 Days</BILLCREDITPERIOD>" in sent
    # F1 fix: invoice-mode (Sales/Purchase) uses the unprefixed tag, verified by Op 6 + import_builder.py, never the
    # ALL-prefixed one — match the opening bracket so this can't be satisfied by a substring of ALLLEDGERENTRIES.LIST.
    assert "<LEDGERENTRIES.LIST>" in sent
    assert "<ALLLEDGERENTRIES.LIST>" not in sent
    assert "<RATE>1000.00/Nos</RATE>" in sent
    assert "<ACTUALQTY>10 Nos</ACTUALQTY>" in sent
    # I1's sibling half: Op 6 goods out — ISDEEMEDPOSITIVE=No against a POSITIVE amount, on the inventory row
    # and its ACCOUNTINGALLOCATIONS child alike.
    inventory_block = sent.split("<ALLINVENTORYENTRIES.LIST>")[1].split("</ALLINVENTORYENTRIES.LIST>")[0]
    assert inventory_block.count("<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>") == 2
    assert "<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>" not in inventory_block
    assert inventory_block.count("<AMOUNT>10000.00</AMOUNT>") == 2


def test_a_purchase_inverts_the_signs():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Purchase", date="20230601", narration="[S0-B:9] buy", party="Mumbai Supplies",
        lines=[("Mumbai Supplies", Decimal("5900.00"), False), ("Purchase", Decimal("-5000.00"), True),
               ("Input CGST", Decimal("-450.00"), True), ("Input SGST", Decimal("-450.00"), True)])
    sent = _imports(books)[-1]
    assert "<LEDGERENTRIES.LIST>" in sent
    assert "<ALLLEDGERENTRIES.LIST>" not in sent
    party_block = sent.split("<LEDGERENTRIES.LIST>")[1]
    assert "<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>" in party_block
    assert "<AMOUNT>5900.00</AMOUNT>" in party_block


def test_a_receipt_still_uses_all_ledger_entries():
    """Pins the split from the other side: non-invoice vch_types (Op 8/9, create_payment) keep the ALL-prefixed tag."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Receipt", date="20230601", narration="[S0-B:50] receipt", party="Pune Traders",
        lines=[("Cash", Decimal("-1000.00"), True), ("Pune Traders", Decimal("1000.00"), False)])
    sent = _imports(books)[-1]
    assert "<ALLLEDGERENTRIES.LIST>" in sent
    # M3: this fixture used to be the MIRROR of Op 8 (cash No/+, party Yes/-). Nothing here asserted signs, so
    # it passed — while encoding the wrong convention as a fixture. Op 8's own line: "Cash debit (Yes/-),
    # party credit (No/+)" (docs/tally-write-exploration-v4.md).
    cash_block = sent.split("<ALLLEDGERENTRIES.LIST>")[1]
    assert "<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>" in cash_block and "<AMOUNT>-1000.00</AMOUNT>" in cash_block
    assert "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>" in sent
    assert "<ISINVOICE>" not in sent


def test_a_custom_purchase_subtype_carrying_inventory_is_accepted():
    """F6: 'Purchase - GST' is parented to Purchase, exactly analogous to the dataset's 'Sales - GST' — the
    inventory-placement guard must accept it (not just is_invoice_type's ledger-tag choice) and emit invoice-mode
    XML, matching the sibling Sales-prefixed case.

    I1 (final review): this test used to assert only that `<ALLINVENTORYENTRIES.LIST>` was PRESENT, so it passed
    while the block inside carried ISDEEMEDPOSITIVE=No against a negative AMOUNT — the mismatched permutation Op
    7 records as EXCEPTIONS=1 (the deemed flag was decided by `vch_type == "Purchase"`, exact equality, while the
    ledger tag three lines above used `startswith`). The flag inside the block is now asserted."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Purchase - GST", date="20230601", narration="[S0-B:60] buy", party="Mumbai Supplies",
        lines=[("Mumbai Supplies", Decimal("1180.00"), False), ("Purchase", Decimal("-1000.00"), True),
               ("Input CGST", Decimal("-90.00"), True), ("Input SGST", Decimal("-90.00"), True)],
        inventory=[("A4 Paper", "Nos", Decimal("1"), Decimal("1000.00"), Decimal("-1000.00"))])
    sent = _imports(books)[-1]
    assert "<LEDGERENTRIES.LIST>" in sent
    assert "<ALLLEDGERENTRIES.LIST>" not in sent
    assert "<ISINVOICE>Yes</ISINVOICE>" in sent
    assert "<ALLINVENTORYENTRIES.LIST>" in sent
    inventory_block = sent.split("<ALLINVENTORYENTRIES.LIST>")[1].split("</ALLINVENTORYENTRIES.LIST>")[0]
    # Op 7: goods in — ISDEEMEDPOSITIVE=Yes against a NEGATIVE amount, on the inventory row AND on its
    # ACCOUNTINGALLOCATIONS child (both flags come from the same expression, so both are pinned here).
    assert inventory_block.count("<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>") == 2
    assert "<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>" not in inventory_block
    assert inventory_block.count("<AMOUNT>-1000.00</AMOUNT>") == 2


def test_inventory_without_a_second_line_is_refused_before_anything_is_sent():
    """F7: `lines` needs a party line and a nominal ledger line for the ACCOUNTINGALLOCATIONS.LIST convention to
    be meaningful at all — a single-line `lines` with inventory is refused rather than silently misallocating."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="nominal ledger"):
        writer.create_b_voucher(
            B, vch_type="Sales", date="20230601", narration="[S0-B:61] x", party="P",
            lines=[("P", Decimal("0.00"), True)],
            inventory=[("A4 Paper", "Nos", Decimal("1"), Decimal("0.00"), Decimal("0.00"))])
    assert _imports(books) == []


def test_an_unbalanced_voucher_is_refused_before_anything_is_sent():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="does not balance"):
        writer.create_b_voucher(B, vch_type="Sales", date="20230601", narration="[S0-B:1] x", party="P",
                                lines=[("P", Decimal("-100.00"), True), ("Sales", Decimal("90.00"), False)])
    assert _imports(books) == []



# --- C32: an inventory invoice carries the nominal ledger ONLY in ACCOUNTINGALLOCATIONS ----------------------------
def _ledger_entry_names(sent: str) -> list[str]:
    root = ET.fromstring(sent)
    return [e.findtext("LEDGERNAME") for tag in ("LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST")
            for e in root.iter(tag)]


def test_an_inventory_sale_does_not_emit_the_nominal_ledger_line():
    """C32 (live 2026-09-24, logs/debug-vch1-*.log): the nominal ledger as a LEDGERENTRIES line AND in each
    inventory row's ACCOUNTINGALLOCATIONS made Tally count the goods twice (EXCEPTIONS=1). Removing only the
    nominal line gave CREATED=1 — which is also what production build_create_sales_voucher emits."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Sales", date="20230601", narration="[S0-B:7] sale", party="Pune Traders",
        lines=[("Pune Traders", Decimal("-11800.00"), True), ("Sales", Decimal("10000.00"), False),
               ("Output CGST", Decimal("900.00"), False), ("Output SGST", Decimal("900.00"), False)],
        inventory=[("A4 Paper", "Nos", Decimal("6"), Decimal("1000.00"), Decimal("6000.00")),
                   ("Pen", "Nos", Decimal("4"), Decimal("1000.00"), Decimal("4000.00"))])
    sent = _imports(books)[-1]
    assert _ledger_entry_names(sent) == ["Pune Traders", "Output CGST", "Output SGST"]
    allocations = [(a.findtext("LEDGERNAME"), a.findtext("AMOUNT"))
                   for a in ET.fromstring(sent).iter("ACCOUNTINGALLOCATIONS.LIST")]
    assert allocations == [("Sales", "6000.00"), ("Sales", "4000.00")]


def test_an_inventory_purchase_does_not_emit_the_nominal_ledger_line():
    """Same rule under Op 7's inverted signs (production build_create_purchase_voucher: party No/+, GST Yes/−,
    inventory + allocation Yes/−goods, no nominal LEDGERENTRIES line)."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Purchase - GST", date="20230601", narration="[S0-B:60] buy", party="Mumbai Supplies",
        lines=[("Mumbai Supplies", Decimal("1180.00"), False), ("Purchase", Decimal("-1000.00"), True),
               ("Input CGST", Decimal("-90.00"), True), ("Input SGST", Decimal("-90.00"), True)],
        inventory=[("A4 Paper", "Nos", Decimal("1"), Decimal("1000.00"), Decimal("-1000.00"))])
    sent = _imports(books)[-1]
    assert _ledger_entry_names(sent) == ["Mumbai Supplies", "Input CGST", "Input SGST"]
    assert books.state["vouchers"]["51"]  # the fake (which now balances allocations) accepted it


@pytest.mark.parametrize("inventory_amount", [Decimal("9000.00"), Decimal("-10000.00")])
def test_inventory_allocations_that_disagree_with_the_nominal_line_are_refused(inventory_amount):
    """The nominal line is no longer sent, so its amount must be carried EXACTLY (sign-aware) by the inventory
    rows' allocations — otherwise the voucher Tally computes would not balance, even though `lines` does."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="inventory"):
        writer.create_b_voucher(
            B, vch_type="Sales", date="20230601", narration="[S0-B:7] sale", party="Pune Traders",
            lines=[("Pune Traders", Decimal("-11800.00"), True), ("Sales", Decimal("10000.00"), False),
                   ("Output CGST", Decimal("900.00"), False), ("Output SGST", Decimal("900.00"), False)],
            inventory=[("A4 Paper", "Nos", Decimal("10"), Decimal("1000.00"), inventory_amount)])
    assert _imports(books) == []


def test_a_non_inventory_sale_still_emits_every_line():
    """A sale with no stock rows has nowhere else to carry its nominal ledger — unchanged by C32. (Not the USD export:
    that one goes out with forex since plan part 7 — test_company_b.py — and this asserts ledger names only.)"""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(
        B, vch_type="Sales", date="20230601", narration="[S0-B:5] service sale", party="Pune Traders",
        lines=[("Pune Traders", Decimal("-5000.00"), True), ("Domestic Sales", Decimal("5000.00"), False)])
    assert _ledger_entry_names(_imports(books)[-1]) == ["Pune Traders", "Domestic Sales"]


# --- C34: each BILLALLOCATIONS AMOUNT carries the sign of the party line it nests under ------------------------------
def _bill_amounts(sent: str) -> list[tuple[str, str]]:
    return [(b.findtext("NAME"), b.findtext("AMOUNT")) for b in ET.fromstring(sent).iter("BILLALLOCATIONS.LIST")]


@pytest.mark.parametrize("vch_type,party_amount,other,bill,expected", [
    # Op 6 sale: party Dr (Yes/−) → the bill is a receivable, −
    ("Sales", Decimal("-1180.00"), ("Export Sales", Decimal("1180.00"), False), ("Inv/1", "New Ref"), "-1180.00"),
    # Op 7 purchase: party Cr (No/+) → a payable, +
    ("Purchase", Decimal("1180.00"), ("Local Purchases", Decimal("-1180.00"), True), ("Pur/2", "New Ref"), "1180.00"),
    # Op 8 receipt: party Cr (No/+) → Agst Ref +, knocking off the − receivable
    ("Receipt", Decimal("500.00"), ("Cash", Decimal("-500.00"), True), ("Inv/1", "Agst Ref"), "500.00"),
    # payment: party Dr (Yes/−) → Agst Ref −, knocking off the + payable
    ("Payment", Decimal("-500.00"), ("Cash", Decimal("500.00"), False), ("Pur/2", "Agst Ref"), "-500.00"),
])
def test_a_bill_allocation_mirrors_the_party_line_sign(vch_type, party_amount, other, bill, expected):
    """C34 (live UI 2026-09-24): [S0-B:1]'s party line was −9,861.74 but its bill went +9,861.74 and Tally filed
    Inv/1 under Bills PAYABLE. Production _render_bill_allocations mirrors the party sign; so does this now.
    BillSpec amounts are magnitudes — the writer applies the sign."""
    books = FakeBooks(name=B)
    if bill[1] == "Agst Ref":            # C35: the fake refuses an Agst Ref to a bill that was never opened
        opened = f"{-Decimal(expected):.2f}"
        books.edit_state(lambda s: s["bills"].update({bill[0]: {"party": "Pune Traders", "amount": opened}}))
    writer, _ = _writer(books)
    writer.create_b_voucher(B, vch_type=vch_type, date="20230601", narration="[S0-B:9] x", party="Pune Traders",
                            lines=[("Pune Traders", party_amount, party_amount < 0), other],
                            bills=[(bill[0], bill[1], abs(party_amount), None)])
    assert _bill_amounts(_imports(books)[-1]) == [(bill[0], expected)]


def test_a_negative_bill_magnitude_is_refused_before_anything_is_sent():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="magnitude"):
        writer.create_b_voucher(B, vch_type="Sales", date="20230601", narration="[S0-B:9] x", party="Pune Traders",
                                lines=[("Pune Traders", Decimal("-100.00"), True), ("Export Sales", Decimal("100.00"), False)],
                                bills=[("Inv/9", "New Ref", Decimal("-100.00"), None)])
    assert _imports(books) == []


def test_bills_that_do_not_add_up_to_the_party_line_are_refused():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="bill"):
        writer.create_b_voucher(B, vch_type="Sales", date="20230601", narration="[S0-B:9] x", party="Pune Traders",
                                lines=[("Pune Traders", Decimal("-100.00"), True), ("Export Sales", Decimal("100.00"), False)],
                                bills=[("Inv/9", "New Ref", Decimal("60.00"), None), ("Inv/10", "New Ref", Decimal("30.00"), None)])
    assert _imports(books) == []


def test_split_bills_that_add_up_are_each_signed():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(B, vch_type="Sales", date="20230601", narration="[S0-B:9] x", party="Pune Traders",
                            lines=[("Pune Traders", Decimal("-100.00"), True), ("Export Sales", Decimal("100.00"), False)],
                            bills=[("Inv/9", "New Ref", Decimal("60.00"), None), ("Inv/10", "New Ref", Decimal("40.00"), None)])
    assert _bill_amounts(_imports(books)[-1]) == [("Inv/9", "-60.00"), ("Inv/10", "-40.00")]


def test_dataset_voucher_1_files_its_bill_as_a_receivable():
    """The live C34 voucher, [S0-B:1] of generate("educational"), end to end through the writer and the fake."""
    from probes.setup.company_b_data import generate
    v = next(v for v in generate("educational").vouchers if v.tag == 1)
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_b_voucher(B, vch_type=v.vch_type, date=v.date.strftime("%Y%m%d"), narration=v.narration,
                            party=v.party, lines=[(l.ledger, l.amount, l.deemed_positive) for l in v.lines],
                            inventory=[(i.item, "Nos", i.qty, i.rate, i.amount) for i in v.inventory],
                            bills=[(b.name, b.bill_type, b.amount, b.credit_period) for b in v.bills])
    party_amount = v.lines[0].amount
    assert party_amount < 0
    assert _bill_amounts(_imports(books)[-1]) == [("Inv/1", f"{party_amount:.2f}")]
    assert books.state["bills"] == {"Inv/1": {"party": v.party, "amount": f"{party_amount:.2f}"}}


# --- C37 / review #7-#8: every pre-send check is a pure function the loader can run over the whole dataset ---------
from probes.setup.writes import validate_b_voucher  # noqa: E402

_SALE_LINES = [("Pune Traders", Decimal("-1180.00"), True), ("Domestic Sales", Decimal("1000.00"), False),
               ("Output CGST", Decimal("90.00"), False), ("Output SGST", Decimal("90.00"), False)]
_SALE_ROW = [("Wireless Mouse", "Nos", Decimal("2"), Decimal("500.00"), Decimal("1000.00"))]


def _validate(**overrides):
    kwargs = dict(vch_type="Sales", narration="[S0-B:9] x", party="Pune Traders", lines=_SALE_LINES,
                  inventory=_SALE_ROW, bills=[("Inv/9", "New Ref", Decimal("1180.00"), "30 Days")])
    kwargs.update(overrides)
    return validate_b_voucher(**kwargs)


def test_validate_b_voucher_accepts_a_good_sale_and_drops_the_nominal_line():
    checked = _validate()
    assert [ledger for ledger, _, _ in checked.sent_lines] == ["Pune Traders", "Output CGST", "Output SGST"]
    assert checked.signed_bills == [("Inv/9", "New Ref", Decimal("-1180.00"), "30 Days")]
    assert checked.nominal_ledger == "Domestic Sales"


@pytest.mark.parametrize("overrides,message", [
    (dict(lines=[("Pune Traders", Decimal("-1180.00"), True), ("Domestic Sales", Decimal("1000.00"), False)]),
     "does not balance"),
    (dict(inventory=[("Wireless Mouse", "Nos", Decimal("2"), Decimal("400.00"), Decimal("800.00"))]),
     "inventory allocations"),
    (dict(bills=[("Inv/9", "New Ref", Decimal("-1180.00"), None)]), "magnitude"),
    (dict(bills=[("Inv/9", "New Ref", Decimal("1000.00"), None)]), "bill allocations total"),
    (dict(party="Nagpur Traders", inventory=()), "exactly one"),
    (dict(vch_type="Receipt"), "Sales/Purchase only"),
])
def test_validate_b_voucher_refuses_each_bad_shape(overrides, message):
    with pytest.raises(ValueError, match=message):
        _validate(**overrides)


def test_the_dropped_nominal_line_must_carry_the_inventory_rows_flag():
    """Review #8: the nominal line is never sent (C32), so its ISDEEMEDPOSITIVE was never checked. A sale's
    inventory rows go out No/+goods; a nominal line flagged Yes (or signed −) says the caller meant something else."""
    wrong_flag = [_SALE_LINES[0], ("Domestic Sales", Decimal("1000.00"), True), *_SALE_LINES[2:]]
    with pytest.raises(ValueError, match="ISDEEMEDPOSITIVE"):
        _validate(lines=wrong_flag)
    mirrored_sale = [("Pune Traders", Decimal("1180.00"), False), ("Domestic Sales", Decimal("-1000.00"), True),
                     ("Output CGST", Decimal("-90.00"), True), ("Output SGST", Decimal("-90.00"), True)]
    with pytest.raises(ValueError, match="ISDEEMEDPOSITIVE"):
        _validate(lines=mirrored_sale, bills=(),
                  inventory=[("Wireless Mouse", "Nos", Decimal("2"), Decimal("500.00"), Decimal("-1000.00"))])


def test_create_b_voucher_runs_the_same_validation_before_sending():
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    with pytest.raises(ValueError, match="ISDEEMEDPOSITIVE"):
        writer.create_b_voucher(B, vch_type="Sales", date="20230601", narration="[S0-B:9] x", party="Pune Traders",
                                lines=[_SALE_LINES[0], ("Domestic Sales", Decimal("1000.00"), True), *_SALE_LINES[2:]],
                                inventory=_SALE_ROW)
    assert _imports(books) == []


# --- C40 (live 2026-09-24): a compound-unit item's quantities are in the compound's FIRST unit ------------------------
def test_a_compound_unit_items_opening_is_sent_in_its_first_unit():
    """Live: OPENINGBALANCE "15 Box of 10 Nos" / OPENINGRATE "950.00/Box of 10 Nos" answered created=1 and stored NO
    opening; "15 Box" / "950.00/Box" read back " 15 Box 0 Nos", "950.00/Box", −14250.00. BASEUNITS stays the
    compound's name."""
    books = FakeBooks(name=B)
    writer, _ = _writer(books)
    writer.create_stock_item(B, "A4 Paper Ream", unit="Box of 10 Nos", qty_unit="Box", hsn="4802",
                             opening_qty=Decimal("15"), opening_rate=Decimal("950.00"))
    sent = _imports(books)[-1]
    assert "<BASEUNITS>Box of 10 Nos</BASEUNITS>" in sent
    assert "<OPENINGBALANCE>15 Box</OPENINGBALANCE>" in sent
    assert "<OPENINGRATE>950.00/Box</OPENINGRATE>" in sent
    assert "<OPENINGVALUE>-14250.00</OPENINGVALUE>" in sent
