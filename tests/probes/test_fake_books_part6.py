from datetime import date, timedelta
from decimal import Decimal

from tally_bridge.envelopes import wrap_report
from tally_bridge.sync_reports import parse_bills
from tally_bridge.xml_utils import read_objects
from probes import p05_voucher_month_bounds as p05
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_TO, B_CUSTOM_VOUCHER_TYPE, bill_terms, credit_days,
                                      flagged_tags, item_specs, r9_candidate, stock_opening_at, written_vouchers)
from probes.companies import (COMPANIES, COMPANY_C_BOOKS_FROM, COMPANY_C_BOOKS_TO, COMPANY_C_LEDGER,
                                 COMPANY_C_VOUCHER_NARRATION)
from probes.reads import fill_month_request, master_request, parse_vouchers, qty_number, tally_date, voucher_request
from tests.probes.fake_books import FakeBooks, seed_company_b, seed_company_c, sync_client
from tests.probes.fakes import bills_xml

B, C = COMPANIES["B"], COMPANIES["C"]


def _post(books: FakeBooks, xml: str) -> str:
    with sync_client(books.transport()) as http:
        return http.post("/", content=xml.encode("utf-8")).text


def _b(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True, bills=True)
    return books


def test_credit_days_reads_tallys_text():
    assert credit_days("30 Days") == 30 and credit_days(" 45 Days ") == 45 and credit_days("1 Day") == 1
    assert credit_days("") is None and credit_days(None) is None and credit_days("30") is None


def test_the_flagged_pairs_and_the_written_usd_sales():
    cancelled, optional = flagged_tags("educational")
    assert cancelled == {201, 202} and optional == {301, 302}
    written = written_vouchers("educational")
    assert len(written) == 960 and 101 in written and 102 in written   # plan part 7: 101/102 written with forex (was C36-skipped)


def test_bill_terms_carry_bill_date_credit_period_and_due():
    terms = bill_terms("educational")
    inv = terms["Inv/203"]                       # live: p21_B_fy2022_month_02.xml, BILLDATE 20230201, "30 Days"
    assert inv.bill_date == date(2023, 2, 1) and inv.credit_days == 30 and inv.due == date(2023, 3, 3)
    opening = terms["Op/2022-001"]
    assert opening.credit_period is None and opening.due == opening.bill_date and not opening.flagged
    assert terms["Inv/301"].flagged                  # opened by optional voucher 301


def test_stock_opening_at_the_current_fy_is_the_31_march_2025_stock():
    # C46, live 2026-09-24 (p11_B_stock_openings.xml): 11 / 47 / 19 / 9 / 4.
    want = {"USB Cable Type-C": 11, "Wireless Mouse": 47, "A4 Paper Ream": 19, "Office Stapler": 9,
            "Whiteboard Marker Set": 4}
    for item, qty in want.items():
        assert stock_opening_at("educational", item, date(2025, 4, 1)) == Decimal(qty)
    assert stock_opening_at("educational", "USB Cable Type-C", date(2022, 4, 1)) == Decimal("120")


def test_r9_candidate_is_a_creditor_under_a_custom_group():
    name, parent, other = r9_candidate("educational")
    assert parent in ("National Creditors", "Local Creditors") and other == "Sundry Debtors" and name


def test_header_collection_lists_flags_and_honours_the_listed_knobs():
    xml = voucher_request("S0P03BVouchers", ["Narration", "IsCancelled", "IsOptional"], B,
                          from_date=B_BOOKS_FROM, to_date=B_BOOKS_TO)
    rows = read_objects(_post(_b(), xml), "VOUCHER", ["Narration", "IsCancelled", "IsOptional"])
    assert len(rows) == 960                            # plan part 7: 101/102 written with forex (was C36-skipped)
    assert sum(r["IsCancelled"] == "Yes" for r in rows) == 2 and sum(r["IsOptional"] == "Yes" for r in rows) == 2
    hidden = read_objects(_post(_b(optional_vouchers_listed=False), xml), "VOUCHER", ["Narration"])
    assert len(hidden) == 958
    header_only = read_objects(_post(_b(header_lists_flagged=False), xml), "VOUCHER", ["Narration"])
    assert len(header_only) == 956                     # review I3: the header read alone drops all four flagged


def test_month_export_carries_bill_date_and_credit_period():
    xml = fill_month_request(p05.svdates_template(), B, "01-06-2023", "02-06-2023")
    periods = {b.get("BILLCREDITPERIOD") for v in parse_vouchers(_post(_b(), xml)) for line in v["ledger_lines"]
               for b in line["bills"] if b.get("BILLTYPE") == "New Ref"}
    assert periods == {"30 Days", "45 Days"}
    silent = {b.get("BILLCREDITPERIOD") for v in parse_vouchers(_post(_b(bill_credit_period_exported=False), xml))
              for line in v["ledger_lines"] for b in line["bills"]}
    assert silent == {None}


def test_bills_receivable_due_is_bill_date_plus_credit_period():
    terms = bill_terms("educational")
    report = wrap_report("Bills Receivable", B_BOOKS_FROM, B_BOOKS_TO, B)
    bills = [b for b in parse_bills(_post(_b(), report)) if terms[b["bill_number"]].credit_days]
    assert bills and all(tally_date(b["due_date"]) == terms[b["bill_number"]].due for b in bills)
    flat = [b for b in parse_bills(_post(_b(bill_due_from_credit_period=False), report))
            if terms[b["bill_number"]].credit_days]
    assert flat and all(b["due_date"] == b["bill_date"] for b in flat)


def test_bill_rows_without_dates_stay_byte_identical():
    books = FakeBooks(name=B)
    books.edit_state(lambda s: s["bills"].__setitem__("X/1", {"party": "P", "amount": "-5.00"}))
    report = wrap_report("Bills Receivable", "01-04-2025", "31-03-2026", B)
    assert _post(books, report) == bills_xml([("X/1", "P", "-5.00")])


def test_voucher_types_export_parent_and_reserved_name():
    fields = ["Name", "Parent", "ReservedName"]
    xml = master_request("S0P25BVoucherTypes", "VoucherType", fields, B)
    rows = {r["Name"]: r for r in read_objects(_post(_b(), xml), "VOUCHERTYPE", fields)}
    assert rows[B_CUSTOM_VOUCHER_TYPE]["Parent"] == "Sales" and rows[B_CUSTOM_VOUCHER_TYPE]["ReservedName"] == ""
    assert rows["Sales"]["Parent"] == "Sales" and rows["Sales"]["ReservedName"] == "Sales"   # live p25 A shape
    hidden = {r["Name"]: r for r in read_objects(_post(_b(voucher_type_parent_exported=False), xml),
                                                   "VOUCHERTYPE", fields)}
    assert hidden[B_CUSTOM_VOUCHER_TYPE]["Parent"] == ""


def test_duplicate_ledger_rows_are_exported():
    name, _parent, other = r9_candidate("educational")
    books = _b()
    books.edit_state(lambda s: s.setdefault("duplicate_ledgers", []).append({"name": name, "parent": other}))
    xml = master_request("S0P25BLedgers", "Ledger", ["Name", "Parent"], B)
    parents = sorted(r["Parent"] for r in read_objects(_post(books, xml), "LEDGER", ["Name", "Parent"])
                     if r["Name"] == name)
    assert len(parents) == 2 and other in parents


def test_current_period_stock_openings_replay_to_the_dataset():
    xml = master_request("S0P11Stock", "StockItem", ["Name", "OpeningBalance"], B)
    rows = read_objects(_post(_b(stock_opening_scope="current"), xml), "STOCKITEM", ["Name", "OpeningBalance"])
    assert {r["Name"] for r in rows} == set(item_specs("educational"))
    for row in rows:
        assert qty_number(row["OpeningBalance"]) == stock_opening_at("educational", row["Name"], date(2025, 4, 1))


def test_the_default_stock_opening_scope_follows_live_c46():
    """Review M4: live C46 (p11 B re-run) exports the current period's opening, so that is the fake's default; the
    "books" (books-beginning) shape is opt-in."""
    xml = master_request("S0P11Stock", "StockItem", ["Name", "OpeningBalance"], B)
    fields = ["Name", "OpeningBalance"]
    assert (read_objects(_post(_b(), xml), "STOCKITEM", fields)
            == read_objects(_post(_b(stock_opening_scope="current"), xml), "STOCKITEM", fields))
    assert (read_objects(_post(_b(), xml), "STOCKITEM", fields)
            != read_objects(_post(_b(stock_opening_scope="books"), xml), "STOCKITEM", fields))


def test_company_c_seed_answers_every_probe_24_read():
    books = FakeBooks(name=C, educational=True)
    seed_company_c(books)
    active = read_objects(_post(books, "<ENVELOPE><HEADER><ID>S0ActiveCompany</ID></HEADER></ENVELOPE>"),
                          "COMPANY", ["Name", "GUID"])
    assert [r["Name"] for r in active] == [C] and active[0]["GUID"]
    ledgers = read_objects(_post(books, master_request("S0P24Ledgers", "Ledger", ["Name"], C)), "LEDGER", ["Name"])
    assert COMPANY_C_LEDGER in {r["Name"] for r in ledgers}
    month = fill_month_request(p05.svdates_template(), C, COMPANY_C_BOOKS_FROM, COMPANY_C_BOOKS_TO)
    assert [v["header"]["NARRATION"] for v in parse_vouchers(_post(books, month))] == [COMPANY_C_VOUCHER_NARRATION]


def test_cancelled_vouchers_export_the_live_shape():
    # Ruling S2 (live p21_B_fy2022_month_02.xml): a cancelled voucher exports ISCANCELLED Yes, NO ledger or inventory
    # lines and an EMPTY PARTYLEDGERNAME -- in the month export and in the header collection alike.
    cancelled, _optional = flagged_tags("educational")
    written = written_vouchers("educational")
    days = sorted(written[tag].date for tag in cancelled)
    books = _b()
    month = fill_month_request(p05.svdates_template(), B, f"{days[0]:%d-%m-%Y}", f"{days[-1]:%d-%m-%Y}")
    exported = [v for v in parse_vouchers(_post(books, month)) if v["header"]["ISCANCELLED"] == "Yes"]
    assert len(exported) == len(cancelled) == 2
    for v in exported:
        assert v["ledger_lines"] == [] and v["inventory"] == [] and v["header"]["PARTYLEDGERNAME"] == ""
    live = [v for v in parse_vouchers(_post(books, month)) if v["header"]["ISCANCELLED"] == "No"]
    assert all(v["ledger_lines"] and v["header"]["PARTYLEDGERNAME"] for v in live)
    xml = voucher_request("S0P03BVouchers", ["Narration", "PartyLedgerName", "IsCancelled"], B,
                          from_date=B_BOOKS_FROM, to_date=B_BOOKS_TO)
    rows = read_objects(_post(books, xml), "VOUCHER", ["Narration", "PartyLedgerName", "IsCancelled"])
    assert {r["PartyLedgerName"] for r in rows if r["IsCancelled"] == "Yes"} == {""}
