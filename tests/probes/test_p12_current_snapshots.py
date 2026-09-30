from probes import p12_current_snapshots as p12
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, bills_xml, make_harness, ready_store, stock_summary_xml, tb_xml

BASELINE = {"Capital Account": "750000.00", "Current Assets": "-750000.00"}
TB = tb_xml([("Capital Account", "", "750000.00"), ("Current Assets", "-750000.00", "")])
BS = ("<ENVELOPE><BSNAME><DSPACCNAME><DSPDISPNAME>Capital Account</DSPDISPNAME></DSPACCNAME></BSNAME>"
      "<BSAMT><BSSUBAMT></BSSUBAMT><BSMAINAMT>750000.00</BSMAINAMT></BSAMT></ENVELOPE>")
PL = ("<ENVELOPE><DSPACCNAME><DSPDISPNAME>Sales Accounts</DSPDISPNAME></DSPACCNAME>"
      "<PLAMT><PLSUBAMT></PLSUBAMT><BSMAINAMT>2057650.00</BSMAINAMT></PLAMT></ENVELOPE>")
RECEIVABLE = bills_xml([("S015", "Apex Technologies Pvt Ltd", "-62800.00"), ("S010", "Rest", "-907737.00")])
PAYABLE = bills_xml([("P001", "Samsung India Electronics", "243100.00"), ("P004", "Rest", "1591042.00")])


def _fake(tb=TB, bs=BS, receivable=RECEIVABLE):
    fake, _ = a_tally()
    fake.route("<ID>Trial Balance</ID>", lambda body: tb)
    fake.route("<ID>Balance Sheet</ID>", lambda body: bs)
    fake.route("<ID>Profit and Loss</ID>", lambda body: PL)
    fake.route("<ID>Stock Summary</ID>", lambda body: stock_summary_xml([("Electronics", "60 Nos", "", "697500.00")]))
    fake.route("<ID>Bills Receivable</ID>", lambda body: receivable)
    fake.route("<ID>Bills Payable</ID>", lambda body: PAYABLE)
    return fake


async def _run(tmp_path, fake):
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, baseline=BASELINE)
    await run_probe(p12.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(12)["parts"]["A"]


async def test_six_snapshots_parse_and_bills_match_the_anchors(tmp_path):
    part = await _run(tmp_path, _fake())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["tb_equals_baseline"] is True
    assert part["fixtures"] == ["p12_A_tb_today.xml", "p12_A_bs_today.xml", "p12_A_pl_fy2025.xml",
                                "p12_A_stock_summary_today.xml", "p12_A_bills_receivable_today.xml",
                                "p12_A_bills_payable_today.xml"]


async def test_tb_amounts_that_are_not_plain_numbers_fail(tmp_path):
    part = await _run(tmp_path, _fake(tb=tb_xml([("Capital Account", "", "7,50,000.00 Cr")])))
    assert part["outcome"] == "FAILED"
    assert "plain numbers" in part["summary"]


async def test_a_report_answering_with_an_error_fails(tmp_path):
    part = await _run(tmp_path, _fake(bs="<ENVELOPE><LINEERROR>Report not found</LINEERROR></ENVELOPE>"))
    assert part["outcome"] == "FAILED"
    assert "Balance Sheet" in part["summary"]


async def test_bills_not_matching_the_anchors_block(tmp_path):
    part = await _run(tmp_path, _fake(receivable=bills_xml([("S015", "Apex", "-1.00")])))
    assert part["outcome"] == "BLOCKED"
    assert "reset-a" in part["summary"]
