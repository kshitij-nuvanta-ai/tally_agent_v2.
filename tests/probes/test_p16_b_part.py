"""Probe 16's B part: is Ledger.OpeningBalance books- or FY-scoped, and does a typed SVTODATE give as-on closings?"""
from decimal import Decimal
from pathlib import Path

from tally_bridge.client import TallyClient
from probes import p16_ledger_closing_balance as p16
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from probes.safety import educational_ignored_dates
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _books(**knobs) -> FakeBooks:
    # C47: the USD party's balances export in the live expression form (FakeBooks' default) — parse_ledger_list reads
    # it at its stated INR base since the forex-parser change, so every test here sees the live form.
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books, *, allow_risky=False):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    await run_probe(p16.PROBE, labels=["B"], client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=ScriptedIO(), allow_risky=allow_risky)
    return store.probe_entry(16)["parts"]["B"]


async def test_b_as_on_ignored_is_different_and_the_scope_is_recorded(tmp_path):
    books = _books()
    part = await _run(tmp_path, books)
    assert part["outcome"] == "DIFFERENT", part["summary"]
    obs = part["observations"]
    assert obs["opening_scope"]["verdict"] == "books" and obs["opening_scope"]["telling"] > 0
    # Ruling Q9: the FY 2024-25 read's period was ignored here, so the spec's read can't answer the scope question;
    # the current-FY read stands in, and says so.
    assert obs["opening_scope"]["source"] == "current_fy_read" and "not honoured" in obs["opening_scope"]["why"]
    assert obs["closing_now"]["mismatches"] == {}
    assert obs["as_on_2023"]["state"] == "ignored" and obs["as_on_fy2024"]["state"] == "ignored"
    assert part["fixtures"] == ["p16_B_groups.xml", "p16_B_ledgers.xml", "p16_B_ledgers_fy2024.xml",
                                "p16_B_ledgers_asof_2023-03-31.xml"]
    assert not any("<SVFROMDATE" in r for r in books.requests if "S0P16BLedgers" in r)
    assert all(educational_ignored_dates(r) == [] for r in books.requests)


async def test_b_as_on_honoured_with_books_scope_is_confirmed(tmp_path):
    part = await _run(tmp_path, _books(ledger_svtodate_honoured=True))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["as_on_2023"]["state"] == "works"
    assert "books-start opening" in part["spec_impact"] and "without probe 17" in part["spec_impact"]


async def test_b_fy_scoped_opening_is_a_finding_not_a_failure(tmp_path):
    part = await _run(tmp_path, _books(ledger_svtodate_honoured=True, ledger_opening_scope="fy"))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["opening_scope"]["verdict"] == "fy"
    assert "FY holding" in part["spec_impact"]


async def test_b_a_closing_balance_that_is_not_the_datasets_fails(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["ledgers"]["HDFC Bank Current A/c"].__setitem__("opening", "-868000.00"))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "HDFC Bank Current A/c" in part["summary"]


async def test_b_an_extra_ledger_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["ledgers"].__setitem__("Hand Entered Ltd", {
        "parent": "Sundry Debtors", "email": "", "alter_id": 999, "guid": "x", "opening": "0.00"}))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "Hand Entered Ltd" in part["summary"] and "setup-b" in part["summary"]


async def test_b_opt_in_svfromdate_wedge_is_failed_and_reported(tmp_path):
    part = await _run(tmp_path, _books(), allow_risky=True)
    assert part["outcome"] == "FAILED" and part["summary"].startswith(p16.WEDGE_SUMMARY)
    assert part["observations"]["as_on_svfromdate"]["wedged"] is True


# --- Ruling Q9: scope is judged from the typed read INSIDE FY 2024-25 (spec §7 probe 16 B) ------------------------


async def test_b_scope_is_judged_from_the_fy2024_read_when_its_period_is_honoured(tmp_path):
    books = _books(ledger_svtodate_honoured=True, ledger_opening_scope="fy")
    part = await _run(tmp_path, books)
    scope = part["observations"]["opening_scope"]
    assert scope["source"] == "fy2024_read" and scope["verdict"] == "fy"
    assert scope["supporting"] == {"fy2024_read": "fy", "current_fy_read": "fy"} and scope["reads_agree"] is True
    fy_read = next(r for r in books.requests if "S0P16BLedgers" in r and "SVTODATE" in r and "31-03-2025" in r)
    assert '<SVTODATE TYPE="Date">31-03-2025</SVTODATE>' in fy_read            # typed, inside FY 2024-25


def test_judged_scope_prefers_the_fy2024_read_and_reports_a_disagreement():
    books_scope = {"verdict": "books", "telling": 5, "as_books": 5, "as_fy": 0, "mismatched": []}
    fy_scope = {"verdict": "fy", "telling": 5, "as_books": 0, "as_fy": 5, "mismatched": []}
    judged = p16.judged_scope({"state": "works"}, fy_scope, books_scope)
    assert judged["verdict"] == "fy" and judged["source"] == "fy2024_read" and judged["reads_agree"] is False
    fallback = p16.judged_scope({"state": "ignored"}, fy_scope, books_scope)
    assert fallback["verdict"] == "books" and fallback["source"] == "current_fy_read"


async def test_b_scope_reads_that_disagree_are_different(tmp_path, monkeypatch):
    """D11: a books-vs-FY contradiction between the FY 2024-25 read and the current-FY read is reported, not buried."""
    real = p16.opening_scope
    calls = []

    def flip_the_second(opening, books, fy):              # the FY 2024-25 read is judged second
        result = real(opening, books, fy)
        calls.append(result["verdict"])
        return {**result, "verdict": "fy"} if len(calls) == 2 else result
    monkeypatch.setattr(p16, "opening_scope", flip_the_second)
    part = await _run(tmp_path, _books(ledger_svtodate_honoured=True))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert "disagree" in part["summary"]


async def test_b_the_live_forex_closing_reads_at_its_revalued_base(tmp_path):
    """C47 (live 2026-09-25): after the plan-part-7 load the USD party's ClosingBalance is `-$1609.71 @ ? 82.58/$ =
    -? 132929.85`. parse_ledger_list now takes the stated base, so 16 B no longer BLOCKs. Part 1 §6 Rung 1 "Forex
    ledgers": Tally's ClosingBalance is the ledger revalued at its LATEST voucher rate, and S1 reads it from Tally — so
    16 B expects the revalued figure (the dataset's C47-aware balance), not Σ the vouchers' INR bases (-133113.72)."""
    books = _books(forex_ledger_closing="expression")
    part = await _run(tmp_path, books)
    plain = await _run(tmp_path / "plain", _books(forex_ledger_closing="plain"))
    assert part["outcome"] == plain["outcome"] == "DIFFERENT", part["summary"]
    assert part["summary"] == plain["summary"] and "AmountParseError" not in part["summary"]
    assert part["observations"]["closing_now"] == {"compared": 20, "mismatches": {}}
    captured = next((tmp_path / "fixtures").rglob("p16_B_ledgers.xml")).read_text(encoding="utf-8")
    assert "-$1609.71 @ ? 82.58/$ = -? 132929.85" in captured


def test_b_expects_the_revalued_usd_closing_not_the_sum_of_bases():
    """The comparison 16 B makes for the USD party, on the live bytes (p22_B_usd_ledger.xml): Tally's figure equals the
    dataset's balance at the current period's end, which carries the C47 revaluation (−₹183.87 vs the bases)."""
    from probes.company_b_view import B_CURRENT_PERIOD, ledger_balances_at
    from probes.setup.company_b_data import USD_EXPORT_PARTY
    live = p16._balances((FIXTURES / "p22_B_usd_ledger.xml").read_text(encoding="utf-8"))
    want = ledger_balances_at("educational", B_CURRENT_PERIOD[1])
    assert want[USD_EXPORT_PARTY] == Decimal("-132929.85")
    assert p16.compare_closing(live, [USD_EXPORT_PARTY], want) == {"compared": 1, "mismatches": {}}
    assert p16.compare_closing(live, [USD_EXPORT_PARTY], {USD_EXPORT_PARTY: Decimal("-133113.72")})["mismatches"]


async def test_b_the_live_forex_opening_reads_at_its_base_too(tmp_path):
    """C47 review I2: the USD party's FY-scoped OpeningBalance is the same expression (p22_B_usd_ledger.xml:52). Read at
    its base it is the current FY's (revalued) opening, so the live combination judges exactly as the plain form."""
    live = dict(ledger_opening_scope="fy", forex_ledger_opening="expression", forex_ledger_closing="expression")
    part = await _run(tmp_path, _books(**live))
    plain = await _run(tmp_path / "plain", _books(ledger_opening_scope="fy", forex_ledger_opening="plain",
                                                  forex_ledger_closing="plain"))
    assert part["outcome"] == plain["outcome"] == "DIFFERENT", part["summary"]
    assert part["summary"] == plain["summary"] and "AmountParseError" not in part["summary"]
    assert part["observations"]["opening_scope"] == plain["observations"]["opening_scope"]
    assert part["observations"]["opening_scope"]["verdict"] == "fy"
    honoured = await _run(tmp_path / "honoured", _books(**live, ledger_svtodate_honoured=True))
    assert honoured["outcome"] == "CONFIRMED", honoured["summary"]
    assert honoured["observations"]["opening_scope"]["source"] == "fy2024_read"
