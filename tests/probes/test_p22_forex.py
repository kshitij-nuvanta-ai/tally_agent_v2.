import json
from decimal import Decimal
from pathlib import Path

from tally_bridge.client import TallyClient
from probes import p05_voucher_month_bounds as p05
from probes import p22_forex as p22
from probes import registry
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.company_b_view import USD_CURRENCY_SYMBOL, USD_EXPORT_PARTY, forex_vouchers
from probes.reads import parse_vouchers
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
SHAPE = Path(__file__).parent.parent / "fixtures" / "sync" / "forex_shape_2026-09-25_run2"   # plan part 7 Task 2


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books, *, with_probe_5=True):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-25T12:00:00+05:30")
    client, capture = TallyClient(transport=books.transport(), trust_env=False), Capture(tmp_path / "fixtures")
    if with_probe_5:
        await run_probe(p05.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    await run_probe(p22.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(22)["parts"]["B"]


def _edit_line(books, tag, ledger, **changes):
    def mutate(state):
        v = next(v for v in state["vouchers"].values() if v["narration"].startswith(f"[S0-B:{tag}]"))
        line = next(l for l in v["lines"] if l["ledger"] == ledger)
        line.update(changes)
    books.edit_state(mutate)


async def test_forex_sales_confirmed_when_the_amount_states_the_base(tmp_path):
    part = await _run(tmp_path, _books())                        # the live default: the full form, "? " prefix
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert sorted(obs["vouchers"]) == ["101", "102"]
    assert all(j["balanced"] and j["base_matches_dataset"] for j in obs["vouchers"].values())
    assert {l["route"] for j in obs["vouchers"].values() for l in j["lines"]} == {"stated"}
    assert all(l["parse_decimal_raises"] for j in obs["vouchers"].values() for l in j["lines"])   # expected (§11.2)
    assert [l["base"] for l in obs["vouchers"]["101"]["lines"]] == ["-37216.04", "37216.04"]
    assert part["fixtures"][-3:] == ["p22_B_forex_sales.xml", "p22_B_usd_ledger.xml", "p22_B_currencies.xml"]
    # C47 (live 2026-09-25): the USD party's closing exports as an expression — recorded, not judged.
    assert obs["usd_ledger"]["currency_name"] == "$" and obs["usd_ledger"]["closing_form"] == "expression"
    assert {c["Name"] for c in obs["currencies"]} == {"?", "$"}
    assert obs["extra_fields"] == []


async def test_base_only_by_rate_is_different(tmp_path):
    part = await _run(tmp_path, _books(forex_export_form="no_base"))
    assert part["outcome"] == "DIFFERENT" and "face × rate" in part["spec_impact"]


async def test_plain_amount_with_a_forex_field_is_confirmed(tmp_path):
    part = await _run(tmp_path, _books(forex_export_form="plain_plus_field"))
    assert part["outcome"] == "CONFIRMED"
    assert part["observations"]["vouchers"]["101"]["lines"][0]["route"] == "field"
    assert part["observations"]["extra_fields"] == ["FOREXAMOUNT"]


async def test_plain_inr_without_forex_blocks_as_c36_recurrence(tmp_path):
    part = await _run(tmp_path, _books(forex_storage="plain"))
    assert part["outcome"] == "BLOCKED" and "plain INR" in part["summary"]


async def test_unbalanced_forex_voucher_fails(tmp_path):
    books = _books()
    _edit_line(books, 101, "Export Sales", amount_text="$448.44 @ ? 82.99/$ = ? 37216.05")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "101" in part["summary"]


async def test_unparsed_amount_fails(tmp_path):
    books = _books()
    _edit_line(books, 102, USD_EXPORT_PARTY, amount_text="-$ ??? @ ? 82.58")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "102" in part["summary"]


async def test_base_differs_from_dataset_blocks_as_drift(tmp_path):
    books = _books()
    _edit_line(books, 101, USD_EXPORT_PARTY, amount_text="-$448.44 @ ? 83.00/$ = -? 37220.52")
    _edit_line(books, 101, "Export Sales", amount_text="$448.44 @ ? 83.00/$ = ? 37220.52")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "differs from the dataset" in part["summary"]


async def test_missing_usd_sale_blocks(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["vouchers"].pop(next(m for m, v in s["vouchers"].items()
                                                      if v["narration"].startswith("[S0-B:102]"))))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "102" in part["summary"] and "setup-b" in part["summary"]


async def test_untagged_voucher_in_the_window_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["vouchers"].__setitem__("99999", {**next(iter(s["vouchers"].values())),
                                                                   "narration": "stray", "date": "20220901"}))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "untagged" in part["summary"]


async def test_needs_probe_5(tmp_path):
    # Pre-flight F5: requires=(0, 5) makes the runner block before run_b, in its own words.
    part = await _run(tmp_path, _books(), with_probe_5=False)
    assert part["outcome"] == "BLOCKED" and "probe(s) 5" in part["summary"]


async def test_educational_window_is_c43_safe(tmp_path):
    books = _books()
    await _run(tmp_path, books)
    sent = [r for r in books.requests if "S0VoucherMonth" in r and '<SVFROMDATE TYPE="Date">01-09-2022' in r]
    assert sent and all('<SVTODATE TYPE="Date">02-09-2022</SVTODATE>' in r for r in sent)


async def test_usd_ledger_closing_expression_is_recorded_not_judged(tmp_path):
    part = await _run(tmp_path, _books())                        # C47: the live default
    assert part["outcome"] == "CONFIRMED"
    led = part["observations"]["usd_ledger"]
    assert led["closing_form"] == "expression" and led["currency_name"] == "$"
    assert "ClosingBalance exports as an expression" in part["summary"] and "parity" in part["spec_impact"]


async def test_master_reads_carry_no_period_variables(tmp_path):
    books = _books()
    await _run(tmp_path, books)
    masters = [r for r in books.requests if "S0P22Ledger" in r or "S0P22Currencies" in r]
    assert len(masters) == 2 and not any("SVFROMDATE" in r or "SVTODATE" in r for r in masters)   # rule 17


def test_forex_vouchers_view():
    specs = forex_vouchers("educational")
    assert sorted(specs) == [101, 102] and USD_CURRENCY_SYMBOL == "$"
    assert all(v.party == USD_EXPORT_PARTY for v in specs.values())


def test_registry_wires_probe_22_and_14_stays_last():
    assert registry.info(22).module == "probes.p22_forex" and registry.load_probe(22) is p22.PROBE
    assert p22.PROBE.requires == (0, 5) and p22.PROBE.educational_sensitive
    b = [step for step, label in registry.ALL_ORDER if label == "B"]
    assert b.index(22) < b.index(23) < b.index(25) < b.index(14) and b[-1] == 14


def _live(variant: str) -> dict:
    raw = (SHAPE / f"variant_{variant}.xml").read_text(encoding="utf-8")
    return next(v for v in parse_vouchers(raw) if v["header"]["NARRATION"] == f"S0-throwaway forex {variant}")


def test_judge_on_the_live_shape_capture():
    """The live throwaway (Task 2, V1b) judged by the probe's own code: its stated base is the sent ₹37,216.04, it
    balances, and each of its primary lines is counted once (live exports ALLLEDGERENTRIES and LEDGERENTRIES)."""
    chosen = json.loads((SHAPE / "summary.json").read_text(encoding="utf-8"))["chosen"]
    voucher = _live(chosen["variant"])
    spec = forex_vouchers("educational")[101]                    # same figures as the throwaway
    judged = p22.judge_voucher(voucher, spec, party_alias=voucher["header"].get("PARTYLEDGERNAME"))
    assert judged["balanced"] and judged["base_matches_dataset"] and judged["routes"] == ["stated"]
    assert [l["base"] for l in judged["lines"]] == ["-37216.04", "37216.04"]
    assert p22.verdict([judged])[0].value == "CONFIRMED"


def test_judge_on_the_live_plain_control_is_the_c36_block():
    """V0 (the same sale written plain INR, live): no forex anywhere — the C36 failure, BLOCKED, never CONFIRMED."""
    import pytest
    from probes.core import ProbeBlocked
    voucher = _live("V0")
    judged = p22.judge_voucher(voucher, forex_vouchers("educational")[101],
                               party_alias=voucher["header"].get("PARTYLEDGERNAME"))
    assert judged["routes"] == ["plain_no_forex"]
    with pytest.raises(ProbeBlocked, match="plain INR"):
        p22.verdict([judged])


def test_judge_keys_the_base_match_by_ledger():
    voucher = _live("V1b")
    spec = forex_vouchers("educational")[102]                    # other figures: ₹95,897.68
    judged = p22.judge_voucher(voucher, spec, party_alias=voucher["header"].get("PARTYLEDGERNAME"))
    assert judged["balanced"] and not judged["base_matches_dataset"]
    assert Decimal(judged["lines"][1]["base"]) == Decimal("37216.04")


# --- plan part 7 review M1 / M3 ----------------------------------------------------------------------------------------
async def test_rate_differs_from_dataset_blocks_as_drift(tmp_path):
    """M1: the right base but another rate — Tally didn't keep what the dataset says; drift (row 4), never CONFIRMED."""
    books = _books()
    _edit_line(books, 101, USD_EXPORT_PARTY, amount_text="-$448.44 @ ? 83.00/$ = -? 37216.04")
    _edit_line(books, 101, "Export Sales", amount_text="$448.44 @ ? 83.00/$ = ? 37216.04")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "differs from the dataset" in part["summary"] and "rate" in part["summary"]
    assert part["observations"]["vouchers"]["101"]["lines"][0]["rate_matches"] is False


async def test_face_or_currency_differs_from_dataset_blocks_as_drift(tmp_path):
    books = _books()
    _edit_line(books, 102, USD_EXPORT_PARTY, amount_text="-€1161.27 @ ? 82.58/€ = -? 95897.68")
    _edit_line(books, 102, "Export Sales", amount_text="€1161.28 @ ? 82.58/€ = ? 95897.68")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "102" in part["summary"]
    assert "currency" in part["summary"] and "face" in part["summary"]


async def test_an_extra_plain_line_is_its_own_finding_not_c36(tmp_path):
    """M3: a line Tally adds (e.g. rounding/exchange) is reported as an unexpected line, not as a plain-INR store."""
    books = _books()

    def add(state):
        v = next(v for v in state["vouchers"].values() if v["narration"].startswith("[S0-B:101]"))
        v["lines"].append({"ledger": "Bank Charges", "amount": "0.00", "deemed_positive": "No"})
    books.edit_state(add)
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "unexpected line" in part["summary"] and "Bank Charges" in part["summary"]
    assert "plain INR" not in part["summary"]
    assert part["observations"]["vouchers"]["101"]["unexpected_ledgers"] == ["Bank Charges"]


def test_the_live_capture_matches_face_rate_and_currency():
    voucher = _live("V1b")
    judged = p22.judge_voucher(voucher, forex_vouchers("educational")[101],
                               party_alias=voucher["header"].get("PARTYLEDGERNAME"))
    assert judged["forex_matches_dataset"] and judged["unexpected_ledgers"] == []
    assert all(l["fx_matches"] and l["rate_matches"] and l["currency_matches"] for l in judged["lines"])


async def test_a_plain_usd_ledger_closing_is_recorded_too(tmp_path):
    part = await _run(tmp_path, _books(forex_ledger_closing="plain"))     # candidate
    assert part["outcome"] == "CONFIRMED" and part["observations"]["usd_ledger"]["closing_form"] == "plain"
    assert "ClosingBalance exports as an expression" not in part["summary"]


# --- C47: the USD party is valued at the latest voucher rate — recorded, not judged ------------------------------------
async def test_the_usd_party_revalued_at_the_latest_rate_is_recorded_not_judged(tmp_path):
    part = await _run(tmp_path, _books())                        # live: -$1609.71 @ ? 82.58/$ = -? 132929.85
    assert part["outcome"] == "CONFIRMED"
    led = part["observations"]["usd_ledger"]
    assert led["closing_text"] == "-$1609.71 @ ? 82.58/$ = -? 132929.85"
    assert (led["revalued_at_latest_rate"], led["revaluation_difference"]) == (True, "183.87")
    assert (led["bases_total"], led["closing_base"]) == ("-133113.72", "-132929.85")
    assert "latest voucher rate" in part["summary"] and "183.87" in part["summary"]


async def test_a_usd_party_at_the_sum_of_bases_is_recorded_as_not_revalued(tmp_path):
    part = await _run(tmp_path, _books(forex_ledger_revaluation="bases"))     # candidate
    led = part["observations"]["usd_ledger"]
    assert part["outcome"] == "CONFIRMED"
    assert (led["revalued_at_latest_rate"], led["revaluation_difference"]) == (False, "0.00")
    assert "latest voucher rate" not in part["summary"]


async def test_a_plain_usd_party_closing_is_still_checked_for_revaluation(tmp_path):
    part = await _run(tmp_path, _books(forex_ledger_closing="plain"))
    led = part["observations"]["usd_ledger"]
    assert (led["closing_base"], led["revalued_at_latest_rate"], led["revaluation_difference"]) == (
        "-132929.85", True, "183.87")
