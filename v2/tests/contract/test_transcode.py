from decimal import Decimal
from pathlib import Path

import pytest

from v2.contract import transcode
from v2.contract.parse import amount

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _read(name: str) -> str:
    return (SYNC / name).read_text(encoding="utf-8")


def test_forex_sales_usd_voucher_is_verbatim():                        # spec §5.4, p22_B_forex_sales.xml
    vouchers = {v["data"]["guid"]: v["data"] for v in transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))}
    usd = vouchers["138b7373-753c-4dbe-aa63-b802035f0ba9-000003c1"]
    assert usd["masterid"] == " 961" and usd["alterid"] == " 965" and usd["date"] == "20220901"
    assert [(e["ledgername"], e["amount"]) for e in usd["ledger_entries"]] == [
        ("Gulf Office Supplies LLC (USD)", "-$448.44 @ ? 82.99/$ = -? 37216.04"),
        ("Export Sales", "$448.44 @ ? 82.99/$ = ? 37216.04")]


def test_inr_sale_example_matches_spec():                              # spec §5.4 first example
    v = next(v["data"] for v in transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))
             if v["data"]["guid"].endswith("-00000067"))
    assert v["ledger_entries"][0]["bill_allocations"] == [
        {"name": "Inv/103", "billtype": "New Ref", "billcreditperiod": "30 Days", "amount": "-16538.66",
         "billdate": "20220901"}]   # billdate: pre-flight T1 (spec §5.3 lists it; the §5.4 example is trimmed)
    assert v["inventory_entries"][0]["actualqty"] == " 17 Nos" and v["inventory_entries"][0]["rate"] == "824.46/Nos"
    assert sum(amount(e["amount"]).inr for e in v["ledger_entries"]) == Decimal("0.00")


@pytest.mark.parametrize("fixture", ["p06_A_vouchers_nested.xml", "p22_B_forex_sales.xml",
                                     "p21_B_fy2022_month_09.xml"])
def test_only_allledgerentries_become_ledger_entries(fixture):         # LESSONS rule 18, the 2x trap
    for v in transcode.vouchers_from_xml(_read(fixture)):
        assert "ledgerentries_list" not in v["data"]
        total = sum((amount(e["amount"]).inr for e in v["data"]["ledger_entries"]), Decimal("0"))
        assert total == Decimal("0.00"), v["data"]["guid"]


def test_p06_has_50_vouchers_all_balanced():
    assert len(transcode.vouchers_from_xml(_read("p06_A_vouchers_nested.xml"))) == 50


def test_cancelled_voucher_has_no_entries_and_empty_party():           # p03_B_flagged_month_2023_02.xml [S0-B:201]
    v = next(v["data"] for v in transcode.vouchers_from_xml(_read("p03_B_flagged_month_2023_02.xml"))
             if "[S0-B:201]" in v["data"].get("narration", ""))
    assert v["iscancelled"] == "Yes" and v.get("partyledgername", "") == "" and v["ledger_entries"] == []


def test_hindi_narration_round_trips():                                # p15_B_hindi_narration.xml
    texts = [v["data"].get("narration", "") for v in transcode.vouchers_from_xml(_read("p15_B_hindi_narration.xml"))]
    assert any(any("ऀ" <= ch <= "ॿ" for ch in t) for t in texts)


def test_compound_unit_quantity_verbatim():                            # p15_B_compound_unit_voucher.xml
    qtys = [i["actualqty"] for v in transcode.vouchers_from_xml(_read("p15_B_compound_unit_voucher.xml"))
            for i in v["data"].get("inventory_entries", []) if "actualqty" in i]
    assert "10 Box 0 Nos" in [q.strip() for q in qtys]


def test_group_master_keeps_reserved_prefix_verbatim():                # p25_A_groups.xml (D31 applies server-side)
    groups = {g["data"]["name"]: g["data"] for g in transcode.masters_from_xml(_read("p25_A_groups.xml"), "group")}
    assert groups["Current Assets"]["parent"].startswith("\u0004")
    assert groups["North Zone Debtors"]["parent"] == "Sundry Debtors"


def test_ledger_master_name_from_attribute_and_forex_balance():        # p22_B_usd_ledger.xml
    ledgers = transcode.masters_from_xml(_read("p22_B_usd_ledger.xml"), "ledger")
    usd = next(l["data"] for l in ledgers if l["data"]["name"] == "Gulf Office Supplies LLC (USD)")
    assert usd["currencyname"] == "$" and usd["closingbalance"] == "-$1609.71 @ ? 82.58/$ = -? 132929.85"


def test_ledger_full_carries_guid_and_alterid():                       # p04_A_ledger_full.xml
    ledgers = transcode.masters_from_xml(_read("p04_A_ledger_full.xml"), "ledger")
    assert len(ledgers) == 35 and all(l["data"]["guid"] and l["data"]["alterid"] for l in ledgers)


def test_counters_verbatim():                                          # p01_A_counters_baseline.xml
    c = transcode.counters_from_xml(_read("p01_A_counters_baseline.xml"))
    assert (c["altvchid"], c["altmstid"], c["booksfrom"], c["guid"]) == (
        " 50", " 265", "20250401", "710de34a-3661-4a7b-8148-c2206c3b3e17")


def test_tb_cells_pair_name_with_amounts_and_keep_synthetic_rows():    # p18_B_tb_asof_2023-03-31.xml
    cells = transcode.report_cells(_read("p18_B_tb_asof_2023-03-31.xml"), "trial_balance")
    names = [c["dspdispname"] for c in cells]
    assert names[:2] == ["Capital Account", "Capital Account"]
    assert {"Opening Stock", "Unadjusted Forex Gain/Loss"} <= set(names)
    forex = next(c for c in cells if c["dspdispname"] == "Unadjusted Forex Gain/Loss")
    assert forex == {"dspdispname": "Unadjusted Forex Gain/Loss", "dspcldramta": "-183.87", "dspclcramta": ""}


@pytest.mark.parametrize("fixture, report_type, keys", [
    ("p12_A_stock_summary_today.xml", "stock_summary", {"dspdispname", "dspclqty", "dspclrate", "dspclamta"}),
    ("p23_B_bills_receivable_due.xml", "bills_receivable",
     {"billdate", "billref", "billparty", "billcl", "billdue", "billoverdue"}),
    ("p12_A_bs_today.xml", "balance_sheet", {"dspdispname", "bssubamt", "bsmainamt"}),
    ("p12_A_pl_fy2025.xml", "profit_and_loss", {"dspdispname", "plsubamt", "bsmainamt"}),
])
def test_report_cell_keys_per_type(fixture, report_type, keys):        # spec §7.12
    cells = transcode.report_cells(_read(fixture), report_type)
    assert cells and all(set(c) <= keys for c in cells)


@pytest.mark.parametrize("fixture", ["p10_A_no_company_report.xml", "p10_A_no_company_collection.xml"])
def test_error_envelope_raises(fixture):
    with pytest.raises(transcode.TallyErrorEnvelope):
        transcode.report_cells(_read(fixture), "trial_balance")
    with pytest.raises(transcode.TallyErrorEnvelope):
        transcode.vouchers_from_xml(_read(fixture))


def test_voucher_forex_line_amount_and_empty_placeholders_skipped():    # p22_B_forex_sales.xml [S0-B:101]
    v = next(v["data"] for v in transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))
             if v["data"]["guid"].endswith("-000003c1"))
    assert all(not k.endswith(".list") for k in v)                     # list placeholders never become fields
    assert "bill_allocations" not in v["ledger_entries"][1]            # Export Sales: empty BILLALLOCATIONS.LIST
    assert v["iscancelled"] == "No" and v["isoptional"] == "No" and v["ispostdated"] == "No"


def test_inventory_entry_carries_batch_and_accounting_allocations():   # p06_A_vouchers_nested.xml
    entries = [i for v in transcode.vouchers_from_xml(_read("p06_A_vouchers_nested.xml"))
               for i in v["data"].get("inventory_entries", [])]
    assert entries and all(i["batch_allocations"] and i["accounting_allocations"] for i in entries)
    assert all("ledgername" in a and "amount" in a for i in entries for a in i["accounting_allocations"])


def test_reserved_prefix_kept_as_u0004_in_voucher_leaf():              # F16, p22_B_forex_sales.xml
    v = transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))[0]["data"]
    assert v["inventory_entries"][0]["gstovrdnisrevchargeappl"] == "\u0004 Not Applicable"


def test_reserved_prefix_placeholder_never_leaks():                    # F16: the placeholder maps back everywhere
    groups = transcode.masters_from_xml(_read("p25_A_groups.xml"), "group")
    texts = [value for g in groups for value in g["data"].values()]
    assert not any(transcode._PLACEHOLDER in t for t in texts)
    with pytest.raises(ValueError):
        transcode.masters_from_xml(f"<ENVELOPE><GROUP NAME='x'>{transcode._PLACEHOLDER}</GROUP></ENVELOPE>", "group")


def test_stock_group_master_primary_parent_and_empty_collection():     # s1_A_stock_groups.xml, s1_B_stock_groups.xml
    groups = transcode.masters_from_xml(_read("s1_A_stock_groups.xml"), "stock_group")
    assert [g["data"]["parent"] for g in groups] == ["\u0004 Primary"] * 3
    assert all(g["kind"] == "stock_group" and g["data"]["guid"] for g in groups)
    assert transcode.masters_from_xml(_read("s1_B_stock_groups.xml"), "stock_group") == []


def test_currency_master_base_symbol_is_literal_question_mark():       # s1_A_currencies.xml (LESSONS rule 28c)
    (cur,) = transcode.masters_from_xml(_read("s1_A_currencies.xml"), "currency")
    assert cur["data"]["name"] == "?" and cur["data"]["expandedsymbol"] == "INR" and cur["data"]["alterid"] == " 265"


def test_unknown_master_kind_raises():
    with pytest.raises(ValueError):
        transcode.masters_from_xml(_read("p25_A_groups.xml"), "godown")


def test_ledger_balances_from_g5_keep_empty_closing_verbatim():        # s1_B_ledgers_touched.xml (G5, F14, F23)
    balances = transcode.balances_from_xml(_read("s1_B_ledgers_touched.xml"), "2026-09-25T18:43:45+05:30")
    by_name = {b["data"]["name"]: b["data"] for b in balances}
    assert all(b["kind"] == "ledger_balance" for b in balances) and len(balances) == 4
    assert by_name["Export Sales"] == {
        "guid": "138b7373-753c-4dbe-aa63-b802035f0ba9-000000e8", "name": "Export Sales", "closingbalance": "",
        "openingbalance": "0.00", "captured_at": "2026-09-25T18:43:45+05:30"}
    assert amount(by_name["Export Sales"]["closingbalance"]) is None          # "" -> None, never zero (F14)
    usd = by_name["Gulf Office Supplies LLC (USD)"]
    assert amount(usd["closingbalance"]).inr == Decimal("-132929.85")


def test_counters_of_an_empty_company_list_are_empty():                # p24_C_security_login_pending_active_company.xml
    assert transcode.counters_from_xml(_read("p24_C_security_login_pending_active_company.xml")) == {}


def test_counters_name_and_guid_only_read():                           # p02_A_active_a.xml
    assert transcode.counters_from_xml(_read("p02_A_active_a.xml")) == {
        "guid": "710de34a-3661-4a7b-8148-c2206c3b3e17", "name": "Bharat Traders Probe Copy"}


def test_counters_refuse_a_multi_company_answer():
    xml = "<ENVELOPE><COMPANY><NAME>A</NAME></COMPANY><COMPANY><NAME>B</NAME></COMPANY></ENVELOPE>"
    with pytest.raises(ValueError):
        transcode.counters_from_xml(xml)


def test_errormsg_envelope_raises():                                    # p02_A_active_b_no_company.xml
    with pytest.raises(transcode.TallyErrorEnvelope):
        transcode.counters_from_xml(_read("p02_A_active_b_no_company.xml"))


def test_report_cells_refuse_unknown_type_and_foreign_keys():
    with pytest.raises(ValueError):
        transcode.report_cells(_read("p18_B_tb_asof_2023-03-31.xml"), "day_book")
    with pytest.raises(ValueError):                                     # a stock summary fed as a TB
        transcode.report_cells(_read("p12_A_stock_summary_today.xml"), "trial_balance")


def test_bills_cells_row_per_billfixed():                               # p23_B_bills_receivable_due.xml
    first = transcode.report_cells(_read("p23_B_bills_receivable_due.xml"), "bills_receivable")[0]
    assert first == {"billdate": "31-Mar-23", "billref": "Inv/226", "billparty": "शर्मा ट्रेडर्स", "billcl": "-309.21",
                     "billdue": "30-Apr-23", "billoverdue": "1066"}


# Spec §13.1, expanded from its globs (p10_* error shapes excluded: test_error_envelope_raises), plus the S1 task-0
# captures under their landed names (pre-flight F1). p02/p24 company reads are counters-type reads (pre-flight T1).
S13_CAPTURES = (
    ("p06_A_vouchers_nested.xml", "vouchers", None),
    ("p16_A_vouchers_fy.xml", "vouchers", None),
    ("p18_A_vouchers_fy.xml", "vouchers", None),
    ("p16_A_post_dated_voucher.xml", "vouchers", None),
    ("p16_A_future_voucher.xml", "vouchers", None),
    ("p21_B_fy2022_month_01.xml", "vouchers", None),
    ("p21_B_fy2022_month_02.xml", "vouchers", None),
    ("p21_B_fy2022_month_03.xml", "vouchers", None),
    ("p21_B_fy2022_month_04.xml", "vouchers", None),
    ("p21_B_fy2022_month_05.xml", "vouchers", None),
    ("p21_B_fy2022_month_06.xml", "vouchers", None),
    ("p21_B_fy2022_month_07.xml", "vouchers", None),
    ("p21_B_fy2022_month_08.xml", "vouchers", None),
    ("p21_B_fy2022_month_09.xml", "vouchers", None),
    ("p21_B_fy2022_month_10.xml", "vouchers", None),
    ("p21_B_fy2022_month_11.xml", "vouchers", None),
    ("p21_B_fy2022_month_12.xml", "vouchers", None),
    ("p22_B_forex_sales.xml", "vouchers", None),
    ("p03_B_flagged_month_2023_02.xml", "vouchers", None),
    ("p03_B_flagged_month_2023_07.xml", "vouchers", None),
    ("p03_B_vouchers_flags.xml", "vouchers", None),
    ("p15_B_hindi_narration.xml", "vouchers", None),
    ("p15_B_compound_unit_voucher.xml", "vouchers", None),
    ("p07_A_throwaway_created.xml", "vouchers", None),
    ("p07_A_after_delete.xml", "vouchers", None),
    ("p08_A_rename_before.xml", "masters", "ledger"),
    ("p08_A_rename_before_vouchers.xml", "vouchers", None),
    ("p08_A_rename_after.xml", "masters", "ledger"),
    ("p08_A_rename_after_vouchers.xml", "vouchers", None),
    ("p23_B_bills_credit_period.xml", "vouchers", None),
    ("p25_A_groups.xml", "masters", "group"),
    ("p04_A_group_full.xml", "masters", "group"),
    ("p16_A_groups.xml", "masters", "group"),
    ("p18_B_group_list.xml", "masters", "group"),
    ("p25_A_voucher_types.xml", "masters", "voucher_type"),
    ("p25_B_voucher_types.xml", "masters", "voucher_type"),
    ("p16_A_ledgers.xml", "masters", "ledger"),
    ("p04_A_ledger_full.xml", "masters", "ledger"),
    ("p06_A_ledger_guids.xml", "masters", "ledger"),
    ("p18_B_ledger_list.xml", "masters", "ledger"),
    ("p22_B_usd_ledger.xml", "masters", "ledger"),
    ("p23_A_gst_ledgers.xml", "masters", "ledger"),
    ("p22_B_currencies.xml", "masters", "currency"),
    ("p04_A_stockitem_full.xml", "masters", "stock_item"),
    ("p15_B_compound_unit_item.xml", "masters", "stock_item"),
    ("p15_B_hindi_ledger.xml", "masters", "ledger"),
    ("p25_B_ledgers_after_duplicate_attempt.xml", "masters", "ledger"),
    ("p01_A_counters_baseline.xml", "counters", None),
    ("p01_A_counters_candidates.xml", "counters", None),
    ("p01_A_counters_after_alter_ledger.xml", "counters", None),
    ("p01_A_counters_after_alter_ledger_again.xml", "counters", None),
    ("p01_A_counters_after_alter_voucher.xml", "counters", None),
    ("p01_A_counters_after_create_ledger.xml", "counters", None),
    ("p01_A_counters_after_create_voucher.xml", "counters", None),
    ("p01_A_counters_after_delete_ledger.xml", "counters", None),
    ("p01_A_counters_after_delete_voucher.xml", "counters", None),
    ("p01_A_counters_after_revert_ledger.xml", "counters", None),
    ("p13_A_before_backup_counters.xml", "counters", None),
    ("p13_A_after_restore_counters.xml", "counters", None),
    ("p02_A_active_a.xml", "counters", None),
    ("p02_A_active_b.xml", "counters", None),
    ("p02_A_active_a_no_company.xml", "counters", None),
    ("p24_C_security_login_pending_active_company.xml", "counters", None),
    ("p24_C_security_login_pending_company_list.xml", "counters", None),
    ("p24_C_vault_prompt_pending_active_company.xml", "counters", None),
    ("p24_C_vault_prompt_pending_company_list.xml", "counters", None),
    ("p19_A_capture_quiet_1_counters_start.xml", "counters", None),
    ("p19_A_capture_quiet_1_counters_end.xml", "counters", None),
    ("p19_A_capture_quiet_2_counters_start.xml", "counters", None),
    ("p19_A_capture_quiet_2_counters_end.xml", "counters", None),
    ("p19_A_capture_quiet_3_counters_start.xml", "counters", None),
    ("p19_A_capture_quiet_3_counters_end.xml", "counters", None),
    ("p19_A_capture_moving_counters_start.xml", "counters", None),
    ("p19_A_capture_moving_counters_end.xml", "counters", None),
    ("p16_A_tb_fy_end.xml", "report", "trial_balance"),
    ("p12_A_tb_today.xml", "report", "trial_balance"),
    ("p17_A_tb_exploded_isledgerwise.xml", "report", "trial_balance_ledgerwise"),
    ("p17_A_tb_exploded_explodeflag.xml", "report", "trial_balance"),
    ("p17_A_tb_exploded_explodealllevels.xml", "report", "trial_balance"),
    ("p18_A_tb_asof_2025-10-31.xml", "report", "trial_balance"),
    ("p18_B_tb_asof_2023-03-31.xml", "report", "trial_balance"),
    ("p12_A_bills_payable_today.xml", "report", "bills_payable"),
    ("p12_A_bills_receivable_today.xml", "report", "bills_receivable"),
    ("p12_A_bs_today.xml", "report", "balance_sheet"),
    ("p12_A_pl_fy2025.xml", "report", "profit_and_loss"),
    ("p12_A_stock_summary_today.xml", "report", "stock_summary"),
    ("p18_A_bills_payable_asof_2025-10-31.xml", "report", "bills_payable"),
    ("p18_A_bills_receivable_asof_2025-10-31.xml", "report", "bills_receivable"),
    ("p18_A_stock_summary_asof_2025-10-31.xml", "report", "stock_summary"),
    ("p23_B_bills_receivable_due.xml", "report", "bills_receivable"),
    ("p15_B_stock_summary.xml", "report", "stock_summary"),
    ("s1_A_tb_ledger_asof_2025-04-01.xml", "report", "trial_balance_ledgerwise"),
    ("s1_B_tb_ledger_2025-04-01_2026-03-31.xml", "report", "trial_balance_ledgerwise"),
    ("s1_B_tb_ledger_asof_2023-03-31.xml", "report", "trial_balance_ledgerwise"),
    ("s1_B_tb_group_asof_2026-03-31.xml", "report", "trial_balance"),
    ("s1_A_voucher_types.xml", "masters", "voucher_type"),
    ("s1_A_currencies.xml", "masters", "currency"),
    ("s1_A_stock_groups.xml", "masters", "stock_group"),
    ("s1_A_units.xml", "masters", "unit"),
    ("s1_B_voucher_types.xml", "masters", "voucher_type"),
    ("s1_B_currencies.xml", "masters", "currency"),
    ("s1_B_stock_groups.xml", "masters", "stock_group"),
    ("s1_B_units.xml", "masters", "unit"),
    ("s1_B_usd_ledger.xml", "masters", "ledger"),
    ("s1_B_ledgers_touched.xml", "balances", "ledger_balance"),
    ("s1_A_counters.xml", "counters", None),
    ("s1_B_counters.xml", "counters", None),
)


@pytest.mark.parametrize("fixture, fn, arg", S13_CAPTURES)
def test_every_s13_capture_transcodes(fixture, fn, arg):
    raw = _read(fixture)
    if fn == "vouchers":
        out = transcode.vouchers_from_xml(raw)
        assert all(o["kind"] == "voucher" and o["data"]["guid"] for o in out)
    elif fn == "masters":
        out = transcode.masters_from_xml(raw, arg)
        assert all(o["kind"] == arg and o["data"]["name"] for o in out)
    elif fn == "balances":
        out = transcode.balances_from_xml(raw, "2026-09-25T18:43:45+05:30")
        assert out and all(o["kind"] == arg for o in out)
    elif fn == "report":
        out = transcode.report_cells(raw, arg)
        assert out and all(c.get("dspdispname") or c.get("billref") is not None for c in out)
    else:
        out = transcode.counters_from_xml(raw)
        assert set(out) <= {"guid", "name", "altvchid", "altmstid", "booksfrom"}
