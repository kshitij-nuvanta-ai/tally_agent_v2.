from decimal import Decimal
from pathlib import Path

from tally_bridge.client import TallyClient
from probes import p11_openings as p11
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import FakeTally, ScriptedIO, objects_xml, ready_store

B = COMPANIES["B"]


BOOKS_SCOPE = {"stock_opening_scope": "books"}   # review M4: the pre-C46 hypothesis, opt-in (the default is C46's)


def _books(**knobs) -> FakeBooks:
    # C47 review I2: the USD party's FY-scoped OpeningBalance exports in the live expression form (FakeBooks'
    # default); parse_ledger_list reads it at its stated INR base since the forex-parser change.
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    await run_probe(p11.PROBE, labels=None, client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())
    return store.probe_entry(11)["parts"]["B"]


async def test_openings_straight_from_the_masters_are_confirmed(tmp_path):
    part = await _run(tmp_path, _books(**BOOKS_SCOPE))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["ledgers"]["mismatched"] == {} and obs["opening_bill"]["candidate"]["magnitude_match"] is True
    assert obs["stock"]["A4 Paper Ream"]["qty_ok"] and obs["stock"]["USB Cable Type-C"]["value_ok"]
    assert part["fixtures"] == ["p11_B_ledger_openings.xml", "p11_B_opening_bills.xml", "p11_B_stock_openings.xml"]


async def test_opening_bill_only_in_the_report_is_different(tmp_path):
    part = await _run(tmp_path, _books(ledger_opening_bills_exported=False))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["opening_bill"]["report"]["found"] is True
    assert "p11_B_opening_bills_report.xml" in part["fixtures"]
    assert "Bills Receivable" in part["spec_impact"]


async def test_fy_scoped_ledger_openings_are_different_and_point_at_probe_16(tmp_path):
    part = await _run(tmp_path, _books(ledger_opening_scope="fy"))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    obs = part["observations"]["ledgers"]
    assert obs["mismatched"] and set(obs["as_current_fy"]) == set(obs["mismatched"])
    assert "probe 16 B" in part["spec_impact"]


async def test_stock_value_exported_positive_is_different(tmp_path):
    books = _books(**BOOKS_SCOPE)
    books.edit_state(lambda s: [i.__setitem__("opening_value", i["opening_value"].lstrip("-"))
                                for i in s["items"].values()])
    part = await _run(tmp_path, books)
    assert part["outcome"] == "DIFFERENT" and part["observations"]["stock"]["USB Cable Type-C"]["value_sign_flipped"]


async def test_wrong_stock_quantity_fails(tmp_path):
    books = _books(**BOOKS_SCOPE)
    books.edit_state(lambda s: s["items"]["USB Cable Type-C"].__setitem__("opening_qty", "100 Nos"))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "USB Cable Type-C" in part["summary"]


async def test_a_missing_ledger_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["ledgers"].pop("Satara Packaging Co"))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "Satara Packaging Co" in part["summary"]


SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "sync" / "c46_p11_live_2026-09-24"


async def test_stock_openings_of_the_current_period_are_different_under_c46(tmp_path):
    part = await _run(tmp_path, _books(stock_opening_scope="current"))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    obs = part["observations"]
    assert obs["stock_scope"]["scope"] == "current_period" and obs["sub_verdicts"]["stock"] == "DIFFERENT"
    assert obs["sub_verdicts"]["ledgers"] == "CONFIRMED" and "C46" in part["spec_impact"]


async def test_every_half_is_named_in_the_summary(tmp_path):
    part = await _run(tmp_path, _books(ledger_opening_scope="fy", stock_opening_scope="current"))
    assert part["outcome"] == "DIFFERENT"
    assert all(label in part["summary"] for label in ("ledgers:", "opening bill:", "stock:"))
    assert "probe 16 B" in part["spec_impact"] and "C46" in part["spec_impact"]


async def test_a_mixed_stock_scope_fails(tmp_path):
    books = _books(**BOOKS_SCOPE)
    books.edit_state(lambda s: s["items"]["USB Cable Type-C"].__setitem__("opening_qty", "11 Nos"))  # its 31-03-2025 qty
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and part["observations"]["stock_scope"]["scope"] == "mixed"


async def test_the_2026_09_24_live_capture_relabels_to_different_under_c46(tmp_path, monkeypatch):
    """Plan part 6 re-run rule: the new rule on the exact bytes the old rule judged FAILED (stock only)."""
    # Plan part 7 (pre-flight F1): this capture predates the USD export party the part-7 loader adds, so it is judged
    # against the ledgers company B had on 2026-09-24 — without that one ledger, which live B did not have yet.
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    real_specs = p11.ledger_specs
    monkeypatch.setattr(p11, "ledger_specs", lambda licence: {n: spec for n, spec in real_specs(licence).items()
                                                              if n != USD_EXPORT_PARTY})
    fake = FakeTally([B])
    fake.route("S0CompanyCounters", lambda body: objects_xml("COMPANY", [{
        "Name": B, "GUID": "g-b", "AltVchId": "1", "AltMstId": "1", "BooksFrom": "20220401",
        "LastVoucherDate": "20260331", "AlterID": "1"}]))
    for marker, name in (("S0P11PartyBills", "p11_B_opening_bills.xml"), ("S0P11Ledgers", "p11_B_ledger_openings.xml"),
                         ("S0P11Stock", "p11_B_stock_openings.xml")):
        data = (SNAPSHOT / name).read_bytes()
        fake.route(marker, lambda body, data=data: data)
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    await run_probe(p11.PROBE, labels=None, client=TallyClient(transport=fake.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=ScriptedIO())
    part = store.probe_entry(11)["parts"]["B"]
    assert part["outcome"] == "DIFFERENT", part["summary"]
    obs = part["observations"]
    assert obs["sub_verdicts"] == {"ledgers": "DIFFERENT", "opening_bill": "CONFIRMED", "stock": "DIFFERENT"}
    assert len(obs["ledgers"]["as_current_fy"]) == 14
    assert obs["stock_scope"]["scope"] == "current_period" and len(obs["stock_scope"]["as_current_period"]) == 5


async def test_current_period_stock_says_rate_and_value_are_recorded_not_judged(tmp_path):
    """Review M5: under C46 only the quantity decides the stock half; the summary says rate/value are record-only."""
    part = await _run(tmp_path, _books(stock_opening_scope="current"))
    assert "rate/value recorded, not judged" in part["summary"]
    assert "USB Cable Type-C" in part["observations"]["stock"]


async def test_books_scope_stock_judges_rate_and_value(tmp_path):
    part = await _run(tmp_path, _books(**BOOKS_SCOPE))
    assert "not judged" not in part["summary"]


async def test_b_the_live_forex_opening_reads_at_its_base(tmp_path):
    """C47 review I2 (live 2026-09-25, p22_B_usd_ledger.xml:52): with FY-scoped openings (live, probe 11 B) the USD
    party's OpeningBalance is `-$1609.71 @ ? 82.58/$ = -? 132929.85`. parse_ledger_list now takes the stated base, so
    11 B no longer BLOCKs: the USD party (books-start opening 0, its forex sales are before the current FY) reads as the
    current FY's revalued opening −₹1,32,929.85 and joins the other as_current_fy ledgers — the ledger half stays
    DIFFERENT (FY-scoped), exactly as with the plain form."""
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    part = await _run(tmp_path, _books(ledger_opening_scope="fy", forex_ledger_opening="expression"))
    plain = await _run(tmp_path / "plain", _books(ledger_opening_scope="fy", forex_ledger_opening="plain"))
    assert part["outcome"] == plain["outcome"] == "DIFFERENT", part["summary"]
    assert part["summary"] == plain["summary"] and "AmountParseError" not in part["summary"]
    ledgers = part["observations"]["ledgers"]
    assert ledgers == plain["observations"]["ledgers"]
    assert ledgers["mismatched"][USD_EXPORT_PARTY] == {"tally": Decimal("-132929.85"), "setup": Decimal("0"),
                                                       "current_fy_opening": Decimal("-132929.85")}
    assert USD_EXPORT_PARTY in ledgers["as_current_fy"]
    assert part["observations"]["sub_verdicts"]["ledgers"] == "DIFFERENT"
