"""Probe 18's B part: a TB as-on a closed year's end (31-03-2023) against company B's dataset."""
from tally_bridge.client import TallyClient
from probes import p18_historical_reports as p18
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from probes.safety import educational_ignored_dates
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **{"opening_stock_row": True, **knobs})
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    await run_probe(p18.PROBE, labels=["B"], client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())
    return store.probe_entry(18)["parts"]["B"]


async def test_b_tb_as_on_a_closed_year_equals_the_dataset(tmp_path):
    books = _books()
    part = await _run(tmp_path, books)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    tb = part["observations"]["tb"]
    assert tb["mismatched"] == [] and tb["stock_bearing"]["Current Assets"]["reconciled"] is True
    assert part["fixtures"] == ["p18_B_tb_asof_2023-03-31.xml", "p18_B_ledger_list.xml", "p18_B_group_list.xml"]
    assert all(educational_ignored_dates(r) == [] for r in books.requests)


async def test_b_without_the_opening_stock_row_the_stock_group_does_not_reconcile(tmp_path):
    part = await _run(tmp_path, _books(opening_stock_row=False))
    assert part["outcome"] == "FAILED" and "Current Assets" in part["summary"]
    # I2: a stock-only unreconciled failure must NOT carry TB_IMPACT's "parity suspended" claim — every non-stock
    # primary group still matched, so only the stock-bearing group's own impact applies.
    assert part["spec_impact"] == p18.B_STOCK_UNRECONCILED_IMPACT
    assert "parity is suspended" not in part["spec_impact"]


async def test_b_a_lost_sale_shows_up_in_its_group(tmp_path):
    books = _books()

    def drop_first_fy22_sale(state):
        mid = next(m for m, v in state["vouchers"].items()
                   if v["vch_type"] == "Sales" and v["date"] < "20230331" and v["cancelled"] == "No"
                   and v["optional"] == "No")
        del state["vouchers"][mid]
    books.edit_state(drop_first_fy22_sale)
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "Sales Accounts" in part["summary"]
    assert part["spec_impact"] == p18.TB_IMPACT


async def test_b_an_extra_ledger_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["ledgers"].__setitem__("Hand Entered Ltd", {
        "parent": "Sundry Debtors", "email": "", "alter_id": 999, "guid": "x", "opening": "0.00"}))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "Hand Entered Ltd" in part["summary"]
