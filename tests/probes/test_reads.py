from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tally_bridge.envelopes import wrap_report
from tally_bridge.sync_reports import parse_trial_balance
from probes.reads import (PROBE_POSTING_RULE, amount, ancestors, dmy, exploded_tb_rows, is_countable,
                             ledger_movements, master_request, opening_stock_row, parse_parents, parse_vouchers,
                             postings, primary_group_rows, primary_lines, qty_number, signed_ui_amount,
                             stock_bearing_groups, stock_rows_any_depth, tally_date, tb_rows_any_depth, top_group,
                             voucher_request)
from tests.probes.fakes import line, stock_summary_xml, vch, vouchers_xml

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"
C33_SNAPSHOT = SYNC / "c33_untyped_2026-09-23"   # 2026-09-23 p16/p17/p18 captures (C33)
SALES = vouchers_xml([
    vch({"DATE": "20251001", "VOUCHERTYPENAME": "Sales", "MASTERID": "7"},
        lines=[line("Apex", "-118.00", bills=[{"NAME": "S001", "BILLTYPE": "New Ref", "AMOUNT": "-118.00"}],
                    _list="LEDGERENTRIES.LIST"),
               line("CGST Output", "18.00", _list="LEDGERENTRIES.LIST")],
        inventory=[{"STOCKITEMNAME": "HP Laptop 15s", "ACTUALQTY": " 1 Nos", "ISDEEMEDPOSITIVE": "No", "AMOUNT": "100.00",
                    "accounting": [line("Sales - Electronics", "100.00")],
                    "batches": [{"GODOWNNAME": "Main Location", "AMOUNT": "100.00"}]}]),
])


def test_parse_vouchers_reads_header_lines_bills_inventory_and_skips_placeholders():
    [voucher] = parse_vouchers(SALES)                      # CMPINFO's <VOUCHER>0</VOUCHER> is skipped
    assert voucher["header"]["MASTERID"] == "7"
    assert [item["list"] for item in voucher["ledger_lines"]] == ["LEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"]
    assert voucher["ledger_lines"][0]["bills"] == [{"NAME": "S001", "BILLTYPE": "New Ref", "AMOUNT": "-118.00"}]
    assert voucher["ledger_lines"][1]["bills"] == []        # the empty placeholder list is ignored
    assert voucher["inventory"][0]["fields"]["ACTUALQTY"] == "1 Nos"
    assert voucher["inventory"][0]["accounting"][0]["amount"] == Decimal("100.00")
    assert voucher["inventory"][0]["batches"] == [{"GODOWNNAME": "Main Location", "AMOUNT": "100.00"}]


def test_posting_rules():
    [voucher] = parse_vouchers(SALES)
    assert postings(voucher, "default") == [("Apex", Decimal("-118.00")), ("CGST Output", Decimal("18.00")),
                                            ("Sales - Electronics", Decimal("100.00"))]
    assert postings(voucher, "all_only") == []
    assert sum(value for _, value in postings(voucher, "ledger_plus_alloc")) == 0
    [both] = parse_vouchers(vouchers_xml([vch({"DATE": "20251002"}, lines=[
        line("Cash", "-5.00"), line("Rent", "5.00"), line("Cash", "-5.00", _list="LEDGERENTRIES.LIST")])]))
    assert [name for name, _ in postings(both, "default")] == ["Cash", "Rent"]   # ALLLEDGERENTRIES wins, never both
    assert [item["fields"]["LEDGERNAME"] for item in primary_lines(both)] == ["Cash", "Rent"]


# --- 2026-09-23 live finding: inventory vouchers export the nominal ledger TWICE, so 'default' double-counts ---------


def test_the_default_posting_rule_is_all_only():
    assert PROBE_POSTING_RULE == "all_only"
    [voucher] = parse_vouchers(SALES)
    assert postings(voucher) == postings(voucher, "all_only")


def test_live_inventory_vouchers_balance_only_under_all_only():
    """Company A's 50 live vouchers: 'all_only' leaves 0 unbalanced, 'default' leaves exactly the 24 inventory ones."""
    vouchers = parse_vouchers((C33_SNAPSHOT / "p16_A_vouchers_fy.xml").read_text(encoding="utf-8"))
    assert len(vouchers) == 50 and all(is_countable(v) for v in vouchers)

    def unbalanced(rule):
        return [v for v in vouchers
                if sum((a for _, a in postings(v, rule) if a is not None), Decimal("0")) != Decimal("0")]

    inventory_vouchers = [v for v in vouchers if v["inventory"]]
    assert len(inventory_vouchers) == 24                      # 16 Sales + 8 Purchase
    assert unbalanced("default") == inventory_vouchers        # the artifact, not unbalanced books
    assert unbalanced(PROBE_POSTING_RULE) == []
    assert all(postings(v, PROBE_POSTING_RULE) for v in vouchers)   # no voucher loses its lines under all_only


def test_live_movements_reproduce_tallys_own_as_on_tb_only_under_all_only():
    """The 2x is measurable against Tally: as-on 31-10-2025 the TB says Purchase -11,57,000 / Sales +5,44,000."""
    vouchers = parse_vouchers((C33_SNAPSHOT / "p18_A_vouchers_to_2025-10-31.xml").read_text(encoding="utf-8"))
    tb = {row["account_name"]: row["closing_balance"]
          for row in parse_trial_balance((C33_SNAPSHOT / "p18_A_tb_asof_2025-10-31.xml").read_text(encoding="utf-8"))}
    as_on = dmy("31-10-2025")

    def total(prefix, rule):
        moves = ledger_movements(vouchers, up_to=as_on, rule=rule)
        return sum((v for k, v in moves.items() if k.startswith(prefix)), Decimal("0"))

    assert total("Purchase", PROBE_POSTING_RULE) == tb["Purchase Accounts"] == Decimal("-1157000.00")
    assert total("Sales", PROBE_POSTING_RULE) == tb["Sales Accounts"] == Decimal("544000.00")
    assert total("Purchase", "default") == 2 * tb["Purchase Accounts"]          # exactly double
    assert total("Sales", "default") == 2 * tb["Sales Accounts"]
    assert ledger_movements(vouchers, up_to=as_on) == ledger_movements(vouchers, up_to=as_on, rule="all_only")


# --- 2026-09-23 live finding: the TB's stock-bearing group carries a synthetic non-ledger 'Opening Stock' row -------


def test_the_exploded_tb_carries_a_synthetic_opening_stock_row_no_ledger_holds():
    rows = exploded_tb_rows((C33_SNAPSHOT / "p17_A_tb_exploded_explodeflag.xml").read_text(encoding="utf-8"))
    stock = opening_stock_row(rows)
    assert stock is not None and stock["closing_balance"] == Decimal("1855800.00")
    ledgers = parse_parents((C33_SNAPSHOT / "p16_A_ledgers.xml").read_text(encoding="utf-8"), "LEDGER")
    assert "Opening Stock" not in ledgers                     # it is a report row, not a ledger
    assert opening_stock_row(exploded_tb_rows((SYNC / "p00_A_anchors_tb.xml").read_text(encoding="utf-8"))) is None


def test_primary_group_rows_take_the_group_row_not_the_like_named_ledger():
    """Company A has a LEDGER called "Capital Account" too; the group row is the FIRST of the two."""
    rows = exploded_tb_rows((C33_SNAPSHOT / "p17_A_tb_exploded_explodeflag.xml").read_text(encoding="utf-8"))
    assert [row["account_name"] for row in rows[:2]] == ["Capital Account", "Capital Account"]
    groups = primary_group_rows(rows)
    assert groups["Capital Account"] is rows[0]
    assert set(groups) == {"Capital Account", "Current Liabilities", "Current Assets", "Sales Accounts",
                           "Purchase Accounts", "Indirect Expenses"}
    assert groups["Current Assets"]["closing_balance"] == Decimal("2605093.00")
    plain = primary_group_rows(exploded_tb_rows((SYNC / "p00_A_anchors_tb.xml").read_text(encoding="utf-8")))
    assert {k: v["closing_balance"] for k, v in plain.items()} == {
        k: v["closing_balance"] for k, v in groups.items()}    # EXPLODEFLAG leaves the group rows untouched


def test_ledger_movements_skip_cancelled_optional_and_post_dated_and_respect_dates():
    vouchers = parse_vouchers(vouchers_xml([
        vch({"DATE": "20251001"}, lines=[line("Cash", "-10.00"), line("Rent", "10.00")]),
        vch({"DATE": "20251101"}, lines=[line("Cash", "-1.00"), line("Rent", "1.00")]),
        vch({"DATE": "20251005", "ISCANCELLED": "Yes"}, lines=[line("Cash", "-100.00"), line("Rent", "100.00")]),
        vch({"DATE": "20251006", "ISOPTIONAL": "Yes"}, lines=[line("Cash", "-100.00"), line("Rent", "100.00")]),
        vch({"DATE": "20260331", "ISPOSTDATED": "Yes"}, lines=[line("Cash", "-7.00"), line("Rent", "7.00")]),
    ]))
    assert ledger_movements(vouchers) == {"Cash": Decimal("-11.00"), "Rent": Decimal("11.00")}
    assert ledger_movements(vouchers, up_to=date(2025, 10, 31))["Cash"] == Decimal("-10.00")
    assert ledger_movements(vouchers, before=date(2025, 10, 1)) == {}
    assert ledger_movements(vouchers, include_post_dated=True)["Cash"] == Decimal("-18.00")


def test_dates_quantities_and_ui_amounts():
    assert tally_date("20251001") == date(2025, 10, 1)
    assert tally_date("1-Oct-25") == date(2025, 10, 1)
    assert tally_date("") is None
    assert dmy("31-10-2025") == date(2025, 10, 31)
    assert qty_number(" 25 Nos") == Decimal("25")
    assert qty_number("-2.0000 NOS") == Decimal("-2")
    assert qty_number("") is None
    assert signed_ui_amount("9,70,537.00 Dr") == Decimal("-970537.00")
    assert signed_ui_amount("1,000 Cr") == Decimal("1000")
    assert signed_ui_amount("-5") == Decimal("-5")
    assert signed_ui_amount("  ") is None
    assert amount("$100 @ ₹83/$ = ₹8300") is None             # a forex expression reads as missing, never zero


def test_group_walk_on_the_live_ledger_list():
    ledgers = parse_parents((SYNC / "p01_A_ledger_list.xml").read_text(encoding="utf-8"), "LEDGER")
    assert len(ledgers) == 35
    assert ledgers["Profit & Loss A/c"] == "Primary"          # exported as '&#4; Primary', sanitised
    assert ledgers["CGST Input"] == "Duties & Taxes"
    groups = {"North Zone Debtors": "Sundry Debtors", "Sundry Debtors": "Current Assets", "Current Assets": "Primary",
              "Duties & Taxes": "Current Liabilities", "Current Liabilities": ""}
    assert ancestors(ledgers["Apex Technologies Pvt Ltd"], groups) == ["North Zone Debtors", "Sundry Debtors",
                                                                        "Current Assets"]
    assert top_group(ledgers["CGST Input"], groups) == "Current Liabilities"
    assert top_group("Unknown Group", groups) == "Unknown Group"


def test_tb_rows_any_depth_on_the_live_tb():
    rows = tb_rows_any_depth((SYNC / "p00_A_anchors_tb.xml").read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["Capital Account", "Current Liabilities", "Current Assets",
                                              "Sales Accounts", "Purchase Accounts", "Indirect Expenses"]
    assert rows[2]["debit"] == Decimal("885263.00") and rows[2]["credit"] == Decimal("1719830.00")
    assert rows[1]["closing"] == Decimal("1757357.00")
    assert sum(row["closing"] for row in rows) == Decimal("3305800.00")    # live: the rows don't net to 0


def test_tb_and_stock_rows_any_depth_read_nested_rows():
    nested = ("<ENVELOPE><DSPACCNAME><DSPDISPNAME>Current Liabilities</DSPDISPNAME></DSPACCNAME><DSPACCINFO>"
              "<DSPCLCRAMT><DSPCLCRAMTA>10.00</DSPCLCRAMTA></DSPCLCRAMT><DSPEXPLOSION><DSPACCNAME>"
              "<DSPDISPNAME>HP India Sales Pvt Ltd</DSPDISPNAME></DSPACCNAME><DSPACCINFO><DSPCLCRAMT>"
              "<DSPCLCRAMTA>10.00</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO></DSPEXPLOSION></DSPACCINFO></ENVELOPE>")
    assert [(r["name"], r["closing"]) for r in tb_rows_any_depth(nested)] == [
        ("Current Liabilities", Decimal("10.00")), ("HP India Sales Pvt Ltd", Decimal("10.00"))]
    stock = stock_rows_any_depth(stock_summary_xml([("Electronics", "60 Nos", "", "600000.00"),
                                                    ("Samsung 24 inch Monitor", "45 Nos", "11000.00/Nos", "495000.00")]))
    assert [(r["name"], r["qty"], r["value"]) for r in stock] == [
        ("Electronics", Decimal("60"), Decimal("600000.00")),
        ("Samsung 24 inch Monitor", Decimal("45"), Decimal("495000.00"))]


def test_stock_bearing_groups():
    assert stock_bearing_groups({"Stock-in-Hand": "Current Assets", "Current Assets": "Primary"}) == {"Current Assets"}
    assert stock_bearing_groups({}) == {"Current Assets"}


# --- 2026-09-23 live finding: period variables must never reach a master collection ----------------------------------


@pytest.mark.parametrize("static_vars", [{"SVFROMDATE": "01-10-2025"}, {"SVTODATE": "31-10-2025"},
                                         {"SVFROMDATE": "01-10-2025", "SVTODATE": "31-10-2025"}])
def test_master_request_refuses_period_variables(static_vars):
    """SVFROMDATE freezes Tally's XML server behind a modal; SVTODATE is silently ignored and returns today's
    balances with a healthy 200 (live 2026-09-23, Ledger collection). The builder refuses both."""
    with pytest.raises(ValueError) as exc:
        master_request("S0Test", "Ledger", ["Name"], "Co", static_vars=static_vars)
    message = str(exc.value)
    assert "Ledger collection" in message and "wrap_report" in message
    for name in static_vars:
        assert name in message


def test_master_request_allows_period_variables_only_when_explicitly_allowed():
    xml = master_request("S0Test", "Ledger", ["Name"], "Co", static_vars={"SVFROMDATE": "01-10-2025"},
                         allow_period_vars=True)
    assert '<SVFROMDATE TYPE="Date">01-10-2025</SVFROMDATE>' in xml


def test_master_request_without_period_variables_is_unchanged():
    assert "SVFROMDATE" not in master_request("S0Test", "Ledger", ["Name"], "Co")


def test_voucher_request_and_wrap_report_still_send_both_period_variables():
    """Both are proven fine live and both legitimately need the variables — the guard must not touch them."""
    voucher = voucher_request("S0Test", ["Date"], "Co")
    assert '<SVFROMDATE TYPE="Date">01-04-2025</SVFROMDATE>' in voucher and '<SVTODATE TYPE="Date">31-03-2026</SVTODATE>' in voucher
    report = wrap_report("Trial Balance", "01-04-2025", "31-10-2025", "Co")
    assert '<SVFROMDATE TYPE="Date">01-04-2025</SVFROMDATE>' in report and '<SVTODATE TYPE="Date">31-10-2025</SVTODATE>' in report


def test_fill_month_request_escapes_the_company_and_fills_typed_dates():
    import xml.etree.ElementTree as ET
    from tally_bridge.envelopes import COMPANY_PLACEHOLDER
    from probes.reads import (FROM_PLACEHOLDER, TO_PLACEHOLDER, VOUCHER_MONTH_FIELDS, fill_month_request,
                                 voucher_request)
    template = voucher_request("S0VoucherMonth", VOUCHER_MONTH_FIELDS, COMPANY_PLACEHOLDER,
                               from_date=FROM_PLACEHOLDER, to_date=TO_PLACEHOLDER)
    xml = fill_month_request(template, "Sharma & Sons' Probe Traders", "01-06-2023", "30-06-2023")
    assert "<SVCurrentCompany>Sharma &amp; Sons&apos; Probe Traders</SVCurrentCompany>" in xml
    assert '<SVFROMDATE TYPE="Date">01-06-2023</SVFROMDATE>' in xml
    assert '<SVTODATE TYPE="Date">30-06-2023</SVTODATE>' in xml
    assert "__" not in xml
    assert ET.fromstring(xml).find(".//SVCurrentCompany").text == "Sharma & Sons' Probe Traders"


def test_untyped_period_vars_strips_only_the_date_type():
    from tally_bridge.envelopes import wrap_report
    from probes.reads import untyped_period_vars
    typed = wrap_report("Trial Balance", "01-04-2022", "30-06-2023", "B", extra_vars={"EXPLODEFLAG": "Yes"})
    untyped = untyped_period_vars(typed)
    assert "<SVFROMDATE>01-04-2022</SVFROMDATE>" in untyped
    assert "<SVTODATE>30-06-2023</SVTODATE>" in untyped
    assert 'TYPE="Date"' not in untyped
    assert untyped.replace("<SVFROMDATE>", '<SVFROMDATE TYPE="Date">').replace(
        "<SVTODATE>", '<SVTODATE TYPE="Date">') == typed
