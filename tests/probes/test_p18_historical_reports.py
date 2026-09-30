from decimal import Decimal
from pathlib import Path

from tally_bridge.sync_reports import parse_ledger_list
from probes import p18_historical_reports as p18
from probes.core import Outcome
from probes.runner import run_probe
from tests.probes.fakes import (ScriptedIO, a_tally, bills_xml, line, make_harness, objects_xml, ready_store,
                                   stock_summary_xml, tb_xml, vch, vouchers_xml)

LEDGERS = [
    {"Name": "Apex", "Parent": "Sundry Debtors", "OpeningBalance": ""},
    {"Name": "Samsung India Electronics", "Parent": "National Creditors", "OpeningBalance": ""},
    {"Name": "Cash", "Parent": "Cash-in-Hand", "OpeningBalance": "-1000.00"},
    {"Name": "Capital Account", "Parent": "Capital Account", "OpeningBalance": "1000.00"},
    {"Name": "Sales", "Parent": "Sales Accounts", "OpeningBalance": ""},
    {"Name": "Purchases", "Parent": "Purchase Accounts", "OpeningBalance": ""},
]
GROUPS = [
    {"Name": "Current Assets", "Parent": "Primary"}, {"Name": "Sundry Debtors", "Parent": "Current Assets"},
    {"Name": "Cash-in-Hand", "Parent": "Current Assets"}, {"Name": "Stock-in-Hand", "Parent": "Current Assets"},
    {"Name": "Current Liabilities", "Parent": "Primary"}, {"Name": "Sundry Creditors", "Parent": "Current Liabilities"},
    {"Name": "National Creditors", "Parent": "Sundry Creditors"}, {"Name": "Capital Account", "Parent": "Primary"},
    {"Name": "Sales Accounts", "Parent": "Primary"}, {"Name": "Purchase Accounts", "Parent": "Primary"},
]
MONITOR, TAB = "Samsung 24 inch Monitor", "Samsung Galaxy Tab A8"
VOUCHERS = vouchers_xml([
    vch({"DATE": "20250928", "VOUCHERTYPENAME": "Purchase"},
        lines=[line("Samsung India Electronics", "643100.00",
                    bills=[{"NAME": "P001", "BILLTYPE": "New Ref", "AMOUNT": "643100.00"}]),
               line("Purchases", "-643100.00")],
        inventory=[{"STOCKITEMNAME": MONITOR, "ISDEEMEDPOSITIVE": "Yes", "ACTUALQTY": "25 Nos", "AMOUNT": "-275000.00"}]),
    vch({"DATE": "20251001", "VOUCHERTYPENAME": "Sales"},
        lines=[line("Apex", "-110920.00", bills=[{"NAME": "S001", "BILLTYPE": "New Ref", "AMOUNT": "-110920.00"}]),
               line("Sales", "110920.00")],
        inventory=[{"STOCKITEMNAME": MONITOR, "ISDEEMEDPOSITIVE": "No", "ACTUALQTY": "5 Nos", "AMOUNT": "62500.00"}]),
    vch({"DATE": "20251020", "VOUCHERTYPENAME": "Receipt"},
        lines=[line("Cash", "-110920.00"),
               line("Apex", "110920.00", bills=[{"NAME": "S001", "BILLTYPE": "Agst Ref", "AMOUNT": "110920.00"}])]),
    # Ruling Q3: a voucher AFTER the as-on date (31-10-2025), so the as-on position and the full-period position
    # differ in this fake too — without it "the report ignored the date" can't be told apart from a correct report.
    vch({"DATE": "20251115", "VOUCHERTYPENAME": "Purchase"},
        lines=[line("Samsung India Electronics", "110000.00",
                    bills=[{"NAME": "P002", "BILLTYPE": "New Ref", "AMOUNT": "110000.00"}]),
               line("Purchases", "-110000.00")],
        inventory=[{"STOCKITEMNAME": MONITOR, "ISDEEMEDPOSITIVE": "Yes", "ACTUALQTY": "10 Nos", "AMOUNT": "-110000.00"}]),
])
STOCK_ITEMS = [
    {"Name": MONITOR, "Parent": "Electronics", "BaseUnits": "Nos", "OpeningBalance": "20 Nos"},
    {"Name": TAB, "Parent": "Electronics", "BaseUnits": "Nos", "OpeningBalance": "15 Nos"},
]


def _tb(capital="1000.00", current_assets="-111920.00", opening_stock=None):
    """An EXPLODEFLAG Trial Balance as-on the date: primary-group rows plus, optionally, the synthetic
    `Opening Stock` row Tally prints under the stock-bearing group."""
    rows = [("Capital Account", "", capital), ("Current Liabilities", "", "643100.00"),
            ("Current Assets", current_assets, "")]
    if opening_stock is not None:
        rows.append(("Opening Stock", opening_stock, ""))
    return tb_xml(rows + [("Sales Accounts", "", "110920.00"), ("Purchase Accounts", "-643100.00", "")])


# Monitor as-on 31-10-2025: 20 opening + 25 bought 28-09 - 5 sold 01-10 = 40; the FULL period adds 10 bought 15-11 = 50.
AS_ON_MONITOR, FULL_PERIOD_MONITOR = "40 Nos", "50 Nos"
PAYABLE_AS_ON = [("P001", "Samsung India Electronics", "643100.00")]
PAYABLE_FULL_PERIOD = PAYABLE_AS_ON + [("P002", "Samsung India Electronics", "110000.00")]


def _fake(tb=None, monitor_qty=AS_ON_MONITOR, payable=None):
    fake, _ = a_tally()
    fake.route("S0P18Ledgers", lambda body: objects_xml("LEDGER", LEDGERS))
    fake.route("S0P18Groups", lambda body: objects_xml("GROUP", GROUPS))
    fake.route("S0P18Vouchers", lambda body: VOUCHERS)
    fake.route("S0P18Stock", lambda body: objects_xml("STOCKITEM", STOCK_ITEMS))
    fake.route("<ID>Trial Balance</ID>", lambda body: tb or _tb())
    fake.route("<ID>Bills Receivable</ID>", lambda body: "<ENVELOPE></ENVELOPE>")
    fake.route("<ID>Bills Payable</ID>", lambda body: bills_xml(payable or PAYABLE_AS_ON))
    fake.route("<ID>Stock Summary</ID>", lambda body: stock_summary_xml([
        ("Electronics", f"{15 + int(monitor_qty.split()[0])} Nos", "", "697500.00"),
        (MONITOR, monitor_qty, "11000.00/Nos", "495000.00"), (TAB, "15 Nos", "13500.00/Nos", "202500.00")]))
    return fake


async def _run(tmp_path, fake):
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    outcome = await run_probe(p18.PROBE, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    return outcome, store.probe_entry(18)["parts"]["A"]


async def test_history_reads_match_the_lines(tmp_path):
    outcome, part = await _run(tmp_path, _fake())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert outcome is Outcome.PARTIAL                       # the B part comes in plan part 3
    assert part["observations"]["sub_verdicts"] == {"tb": "CONFIRMED", "bills_stock": "CONFIRMED"}
    assert part["observations"]["bills"]["receivable"]["weak"] is True
    assert part["observations"]["stock"]["item_rows"] == 2
    assert "p18_A_stock_summary_asof_2025-10-31.xml" in part["fixtures"]
    assert "Bills Receivable check is vacuous" in part["summary"]   # M12


async def test_tb_right_but_stock_wrong_is_different_not_failed(tmp_path):
    """Two independent sub-verdicts: a correct as-on TB must not be dragged down by reports that ignore the date."""
    _, part = await _run(tmp_path, _fake(monitor_qty=FULL_PERIOD_MONITOR))
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["sub_verdicts"] == {"tb": "CONFIRMED", "bills_stock": "DIFFERENT"}
    assert part["summary"].startswith("TB as-on 31-10-2025 is correct history;")
    assert "month-end comparison" in part["spec_impact"]
    assert "IS history" in part["spec_impact"] and "R30 needs no suspension" in part["spec_impact"]
    assert "isn't history" not in part["spec_impact"]
    assert "IGNORE the as-on date" in part["spec_impact"]


async def test_a_stock_summary_at_the_full_period_position_is_evidence_the_date_was_ignored(tmp_path):
    """Ruling Q3: the 'report ignored the date' branch, exercised — the fake holds a voucher after the as-on date."""
    _, part = await _run(tmp_path, _fake(monitor_qty=FULL_PERIOD_MONITOR))
    assert part["observations"]["stock"]["matches_full_period"] is True
    assert "stock match the FULL period (31-03-2026) instead, i.e. the report ignored the date" in part["summary"]


async def test_bills_at_the_full_period_position_are_evidence_the_date_was_ignored(tmp_path):
    _, part = await _run(tmp_path, _fake(payable=PAYABLE_FULL_PERIOD))
    payable = part["observations"]["bills"]["payable"]
    assert part["outcome"] == "DIFFERENT" and payable["match"] is False and payable["matches_full_period"] is True
    assert "bills payable match the FULL period" in part["summary"] and "IGNORE the as-on date" in part["spec_impact"]


async def test_a_stock_summary_that_is_neither_as_on_nor_full_period_is_not_called_ignored(tmp_path):
    """45 Nos is neither 40 (as-on) nor 50 (full period): DIFFERENT, but the evidence doesn't say the date was ignored,
    so the verdict must not claim it did."""
    _, part = await _run(tmp_path, _fake(monitor_qty="45 Nos"))
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["stock"]["matches_full_period"] is False
    assert "ignored the date" not in part["summary"] and "IGNORE the as-on date" not in part["spec_impact"]
    assert "neither the as-on nor the full-period position" in part["spec_impact"]


async def test_tb_history_wrong_fails_with_r30(tmp_path):
    _, part = await _run(tmp_path, _fake(tb=_tb(capital="2000.00")))
    assert part["outcome"] == "FAILED"
    assert part["observations"]["sub_verdicts"]["tb"] == "FAILED"
    assert "R30" in part["spec_impact"]


async def test_stock_bearing_group_alone_does_not_fail_the_tb(tmp_path):
    _, part = await _run(tmp_path, _fake(tb=_tb(current_assets="-5.00")))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    entry = part["observations"]["tb"]["stock_bearing"]["Current Assets"]
    assert entry["match"] is False and entry["reconciled"] is False and entry["opening_stock"] is None


async def test_the_stock_bearing_group_reconciles_against_the_tbs_own_opening_stock_row(tmp_path):
    """Same response, same rule as probe 16: TB row = ledger rollup + the TB's own `Opening Stock` row."""
    fake = _fake(tb=_tb(current_assets="-111925.00", opening_stock="-5.00"))
    _, part = await _run(tmp_path, fake)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    entry = part["observations"]["tb"]["stock_bearing"]["Current Assets"]
    assert entry["gap"] == Decimal("-5.00") and entry["opening_stock"] == Decimal("-5.00")
    assert entry["reconciled"] is True
    assert part["observations"]["tb_opening_stock_row"]["present"] is True
    tb_reads = [r for r in fake.probe_requests() if "<ID>Trial Balance</ID>" in r]
    assert tb_reads and all("<EXPLODEFLAG>Yes</EXPLODEFLAG>" in r for r in tb_reads)


# --- 2026-09-23 live findings: the as-on TB IS history; Bills and Stock Summary IGNORE the as-on date ----------------

# The 2026-09-23 run (untyped dates, C33), moved here before plan part 5 re-runs these probes (Task 1).
SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "sync" / "c33_untyped_2026-09-23"


def _live(name):
    return (SNAPSHOT / name).read_text(encoding="utf-8")


def test_the_live_as_on_tb_is_correct_history_under_all_only():
    """Every non-stock group reconciles to the paisa; under the old 'default' rule Sales/Purchase were 2x out."""
    vouchers = p18.parse_vouchers(_live("p18_A_vouchers_to_2025-10-31.xml"))
    ledgers = {row["name"]: row for row in parse_ledger_list(_live("p18_A_ledger_list.xml"))}
    groups = p18.parse_parents(_live("p18_A_group_list.xml"))
    rows = p18.exploded_tb_rows(_live("p18_A_tb_asof_2025-10-31.xml"))
    tb_rows = {name: row["closing_balance"] for name, row in p18.primary_group_rows(rows).items()}
    verdict = p18.tb_verdict(tb_rows, ledgers, groups, vouchers, p18.dmy(p18.TB_AS_ON))
    assert verdict["verdict"] == "CONFIRMED", verdict["mismatched"]
    assert verdict["groups"]["Sales Accounts"]["computed"] == Decimal("544000.00")
    assert verdict["groups"]["Purchase Accounts"]["computed"] == Decimal("-1157000.00")
    # The stock-bearing group's whole gap is the Opening Stock figure probe 17 saw in the exploded TB.
    assert verdict["stock_bearing"]["Current Assets"]["gap"] == Decimal("1855800.00")
    # Under the old 'default' posting rule the nominal ledgers came out at exactly 2x and nothing reconciled.
    doubled = p18.ledger_movements(vouchers, up_to=p18.dmy(p18.TB_AS_ON), rule="default")
    assert sum(v for k, v in doubled.items() if k.startswith("Sales")) == 2 * Decimal("544000.00")
    assert sum(v for k, v in doubled.items() if k.startswith("Purchase")) == 2 * Decimal("-1157000.00")


# The as-on date the 2026-09-23 bills/stock captures were SENT with (Ruling Q2: pinned here, not p18.BILLS_AS_ON —
# the probe's constant moved to 31-10-2025 in plan part 5, and this test must describe the capture it reads).
SNAPSHOT_AS_ON = p18.dmy("30-09-2025")


def test_the_live_bills_and_stock_reports_ignore_the_as_on_date_and_return_the_current_position():
    """2026-09-23 capture — 30-09-2025 was a C43-ignored date, so this pins what that run SAW, not a Tally rule."""
    vouchers = p18.parse_vouchers(_live("p18_A_vouchers_to_2025-10-31.xml"))
    ledgers = {row["name"]: row for row in parse_ledger_list(_live("p18_A_ledger_list.xml"))}
    groups = p18.parse_parents(_live("p18_A_group_list.xml"))
    as_on, period_end = SNAPSHOT_AS_ON, p18.dmy(p18.A_FY_TO)
    for side, fixture, anchor in (("receivable", "p18_A_bills_receivable_asof_2025-09-30.xml", Decimal("970537.00")),
                                  ("payable", "p18_A_bills_payable_asof_2025-09-30.xml", Decimal("1834142.00"))):
        entry = p18._bills_side(_live(fixture), p18.pending_bills(vouchers, ledgers, groups, as_on, side),
                                p18.pending_bills(vouchers, ledgers, groups, period_end, side))
        assert entry["match"] is False                      # not the as-on position
        assert entry["matches_full_period"] is True         # the full-period position, i.e. the date was ignored
        assert sum(entry["reported"].values()) == anchor    # and equal to the period-end seed anchor
    items = p18.read_objects(_live("p18_A_stock_item_openings.xml"), "STOCKITEM", p18.STOCK_FIELDS)
    rows = p18.stock_rows_any_depth(_live("p18_A_stock_summary_asof_2025-09-30.xml"))
    assert p18.stock_verdict(rows, items, vouchers, as_on)["verdict"] == "DIFFERENT"
    full = p18.stock_verdict(rows, items, vouchers, period_end)
    assert full["verdict"] == "CONFIRMED" and len(full["compared"]) == 14


async def test_every_date_p18_sends_is_one_educational_tally_honours(tmp_path):
    """C43: 30-09-2025 (the 2026-09-23 bills/stock date) is ignored by an Educational Tally; the guard would block."""
    from probes.safety import educational_ignored_dates
    fake = _fake()
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, licence="educational")
    await run_probe(p18.PROBE, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    part = store.probe_entry(18)["parts"]["A"]
    assert part["outcome"] != "BLOCKED", part["summary"]
    assert all(educational_ignored_dates(body) == [] for body in fake.probe_requests())
    assert "p18_A_bills_receivable_asof_2025-10-31.xml" in part["fixtures"]


async def test_vouchers_are_read_for_the_whole_fy_typed_and_filtered_in_python(tmp_path):
    """C33: the old `vouchers_to_2025-10-31` read only reached the full FY because Tally ignored its untyped to-date;
    typed, it would stop at October and the 'matches the FULL period' evidence would be computed from October."""
    fake = _fake()
    _, part = await _run(tmp_path, fake)
    body = next(r for r in fake.probe_requests() if "S0P18Vouchers" in r)
    assert '<SVFROMDATE TYPE="Date">01-04-2025</SVFROMDATE>' in body
    assert '<SVTODATE TYPE="Date">31-03-2026</SVTODATE>' in body
    assert "p18_A_vouchers_fy.xml" in part["fixtures"]
