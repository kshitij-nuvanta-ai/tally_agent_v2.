from tally_bridge.client import TallyClient
from probes import p25_masters_classification as p25
from probes.capture import Capture
from probes.company_b_view import B_CUSTOM_VOUCHER_TYPE, r9_candidate
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
NAME, PARENT, OTHER = r9_candidate("educational")


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books, io):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    await run_probe(p25.PROBE, labels=["B"], client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=io)
    return store.probe_entry(25)["parts"]["B"]


async def test_custom_type_walks_to_sales_and_the_duplicate_is_refused(tmp_path):
    """Ruling S6: a base type found only by the Parent walk is DIFFERENT (spec §7 probe 25; 25 A's live verdict)."""
    io = ScriptedIO(answers=["Duplicate Entry!"])
    part = await _run(tmp_path, _books(), io)
    assert part["outcome"] == "DIFFERENT", part["summary"]
    obs = part["observations"]
    assert obs["voucher_type"]["resolved_by"] == "Parent walk" and obs["r9"]["verdict"] == "refused"
    assert obs["sub_verdicts"] == {"voucher_type": "DIFFERENT", "duplicate_name": "CONFIRMED"}
    assert "Parent chain" in part["spec_impact"] and p25.R9_REFUSED_IMPACT in part["spec_impact"]
    assert part["spec_impact"].index("Parent chain") < part["spec_impact"].index("R9")   # worst half first
    assert NAME in io.asks[0] and OTHER in io.asks[0]
    assert part["fixtures"] == ["p25_B_voucher_types.xml", "p25_B_ledgers_after_duplicate_attempt.xml"]


async def test_a_custom_type_with_its_base_in_reserved_name_is_confirmed(tmp_path):
    books = _books()
    books.edit_state(lambda s: s.setdefault("voucher_type_reserved", {}).__setitem__(B_CUSTOM_VOUCHER_TYPE, "Sales"))
    part = await _run(tmp_path, books, ScriptedIO(answers=["Duplicate Entry!"]))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["voucher_type"]["resolved_by"] == "ReservedName"
    assert "ReservedName" in part["spec_impact"] and "R9" in part["spec_impact"]


async def test_a_custom_type_without_a_parent_fails(tmp_path):
    part = await _run(tmp_path, _books(voucher_type_parent_exported=False), ScriptedIO(answers=["Duplicate Entry!"]))
    assert part["outcome"] == "FAILED" and "R16" in part["spec_impact"]


async def test_a_saved_duplicate_is_different_and_names_the_cleanup(tmp_path):
    books = _books()
    books.edit_state(lambda s: s.setdefault("duplicate_ledgers", []).append({"name": NAME, "parent": OTHER}))
    part = await _run(tmp_path, books, ScriptedIO(answers=["saved"]))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert "CLEANUP" in part["summary"] and part["observations"]["r9"]["answer_agrees"] is True
    assert "GUID" in part["spec_impact"]


async def test_the_read_back_wins_over_the_operators_answer(tmp_path):
    part = await _run(tmp_path, _books(), ScriptedIO(answers=["saved"]))
    assert part["observations"]["sub_verdicts"]["duplicate_name"] == "CONFIRMED"
    assert part["observations"]["r9"]["verdict"] == "refused" and part["observations"]["r9"]["answer_agrees"] is False


async def test_non_interactive_leaves_r9_unmeasured(tmp_path):
    part = await _run(tmp_path, _books(), ScriptedIO(interactive=False))
    assert part["outcome"] == "DIFFERENT"                       # the Parent walk (S6); R9 not measured
    assert part["observations"]["r9"]["status"].startswith("not attempted")
    assert part["fixtures"] == ["p25_B_voucher_types.xml"]


async def test_auto_mode_never_attempts_the_duplicate(tmp_path):
    io = ScriptedIO(run_mode="auto")
    part = await _run(tmp_path, _books(), io)
    assert "auto mode" in part["observations"]["r9"]["status"] and io.asks == []


async def test_skip_leaves_r9_unmeasured(tmp_path):
    part = await _run(tmp_path, _books(), ScriptedIO(answers=["skip"]))
    assert part["outcome"] == "DIFFERENT" and part["observations"]["r9"]["status"] == "skipped by the operator"


async def test_a_missing_custom_voucher_type_blocks(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["voucherTypes"].remove(B_CUSTOM_VOUCHER_TYPE))
    part = await _run(tmp_path, books, ScriptedIO(answers=["Duplicate Entry!"]))
    assert part["outcome"] == "BLOCKED" and B_CUSTOM_VOUCHER_TYPE in part["summary"]


async def test_an_empty_r9_answer_is_asked_again(tmp_path):
    """Review M6: Enter alone is not TallyPrime's message — the question is asked once more."""
    io = ScriptedIO(answers=["", "Duplicate Entry!"])
    part = await _run(tmp_path, _books(), io)
    r9 = part["observations"]["r9"]
    assert len(io.asks) == 2 and r9["verdict"] == "refused"
    assert r9["tally_message"] == "Duplicate Entry!" and r9["answer_agrees"] is True


async def test_two_empty_r9_answers_leave_r9_unmeasured(tmp_path):
    """Review M6: with no answer there is no evidence an attempt was made, so a single ledger read back is not a
    CONFIRMED refusal, and nothing is recorded as an agreeing Tally message."""
    part = await _run(tmp_path, _books(), ScriptedIO(answers=["", "  "]))
    obs = part["observations"]
    assert "verdict" not in obs["r9"] and "answer_agrees" not in obs["r9"] and "tally_message" not in obs["r9"]
    assert obs["r9"]["status"].startswith("not measured")
    assert obs["sub_verdicts"]["duplicate_name"].startswith("not measured")
    assert part["outcome"] == "DIFFERENT" and "R9" not in part["spec_impact"]


async def test_two_empty_r9_answers_still_catch_a_saved_duplicate(tmp_path):
    books = _books()
    books.edit_state(lambda s: s.setdefault("duplicate_ledgers", []).append({"name": NAME, "parent": OTHER}))
    part = await _run(tmp_path, books, ScriptedIO(answers=["", ""]))
    r9 = part["observations"]["r9"]
    assert r9["verdict"] == "accepted" and r9["tally_message"] is None and r9["answer_agrees"] is None
    assert "CLEANUP" in part["summary"]
