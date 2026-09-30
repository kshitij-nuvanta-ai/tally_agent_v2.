import httpx

from agent.tally.client import TallyClient
from probes import p01_company_counters as p01
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.core import Outcome
from probes.operator.auto import build_auto_operator
from probes.operator.tally_control import TallyProcess
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import OWN_COMMAND, FakeBooks, FakeRunner, seed_state, tmp_config
from tests.probes.fakes import FakeTally, ScriptedIO, make_harness, mark_done, objects_xml

A = COMPANIES["A"]


def _state(expense="Bank Charges"):
    return {"AltVchId": 100, "AltMstId": 50, "ledger": 7, "expense": expense}


def _fake(state, include_counters=True, ledgers=None, throwaway_exists=False, last_voucher_date="20260301"):
    fake = FakeTally([A])

    def counters(body):
        row = {"Name": A, "GUID": "g-1", "BooksFrom": "20250401", "LastVoucherDate": last_voucher_date, "AlterID": "1"}
        if include_counters:
            row.update(AltVchId=str(state["AltVchId"]), AltMstId=str(state["AltMstId"]))
        return objects_xml("COMPANY", [row])

    def one_ledger(body):
        if p01.THROWAWAY_LEDGER in body:
            rows = [{"Name": p01.THROWAWAY_LEDGER, "GUID": "l-9", "AlterID": "9"}] if throwaway_exists else []
            return objects_xml("LEDGER", rows)
        return objects_xml("LEDGER", [{"Name": state["expense"], "GUID": "l-1", "AlterID": str(state["ledger"])}])

    fake.route("S0CompanyCounters", counters)
    fake.route("S0LedgerList", lambda body: objects_xml("LEDGER", ledgers or [
        {"Name": "Cash", "Parent": "Cash-in-Hand"}, {"Name": "Bank Charges", "Parent": "Indirect Expenses"}]))
    fake.route("S0OneLedger", one_ledger)
    return fake


def _io(state, skip=(), voucher_moves_ledger=False):
    fields = {"create_voucher": "AltVchId", "alter_voucher": "AltVchId", "delete_voucher": "AltVchId",
              "create_ledger": "AltMstId", "alter_ledger": "AltMstId", "delete_ledger": "AltMstId"}
    alters = {"n": 0}

    def on_action(action):
        step = action.kind
        if step == "alter_ledger":
            alters["n"] += 1
            step = "alter_ledger" if alters["n"] == 1 else "alter_ledger_again"
        if step in skip:
            return
        state[fields[action.kind]] += 1
        if action.kind == "create_voucher" and voucher_moves_ledger:
            state["ledger"] += 1

    return ScriptedIO(on_action=on_action)


async def _run(tmp_path, fake, io):
    client, store, capture = make_harness(tmp_path, fake)
    mark_done(store, 0, part="A")
    outcome = await run_probe(p01.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return outcome, store


async def test_expected_counters_confirmed_and_request_stored(tmp_path):
    state = _state()
    outcome, store = await _run(tmp_path, _fake(state), _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    confirmed = store.confirmed("company_counters")
    assert confirmed["xml_template"] == p01.COUNTERS_REQUEST
    assert confirmed["fields"] == ["GUID", "AltVchId", "AltMstId", "BooksFrom", "LastVoucherDate", "AlterID"]
    assert part["observations"]["ledger"] == "Bank Charges"
    assert list(part["observations"]["matrix"]) == ["create_voucher", "alter_voucher", "delete_voucher",
                                                     "create_ledger", "alter_ledger", "alter_ledger_again",
                                                     "delete_ledger"]
    assert part["observations"]["ledger_alterid_moved_on_voucher_entry"] is False
    assert "p01_A_counters_after_alter_ledger_again.xml" in part["fixtures"]
    assert "p01_A_throwaway_ledger_check.xml" in part["fixtures"]


async def test_master_steps_run_on_the_throwaway_ledger_only(tmp_path):
    state = _state()
    io = _io(state)
    await _run(tmp_path, _fake(state), io)
    kinds = [a.kind for a in io.actions]
    assert kinds == ["create_voucher", "alter_voucher", "delete_voucher", "create_ledger", "alter_ledger",
                     "alter_ledger", "delete_ledger"]
    masters = [a for a in io.actions if a.kind.endswith("_ledger")]
    assert {a.params["name"] for a in masters} == {p01.THROWAWAY_LEDGER}
    emails = [a.params["email"] for a in masters if a.kind == "alter_ledger"]
    assert emails == list(p01.EMAILS) and all(emails)            # never an empty-value alter
    assert io.actions[0].params["ledger"] == "Bank Charges"      # the voucher uses the existing expense ledger


async def test_counter_not_moving_is_different(tmp_path):
    state = _state()
    outcome, store = await _run(tmp_path, _fake(state), _io(state, skip=("delete_voucher",)))
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.DIFFERENT
    assert "AltVchId did not move on delete_voucher" in part["summary"]
    assert "R6" in part["spec_impact"]


async def test_second_alter_not_moving_is_different(tmp_path):
    state = _state()
    outcome, store = await _run(tmp_path, _fake(state), _io(state, skip=("alter_ledger_again",)))
    assert outcome is Outcome.DIFFERENT
    assert "AltMstId did not move on alter_ledger_again" in store.probe_entry(1)["parts"]["A"]["summary"]


async def test_voucher_moving_ledger_alterid_is_different(tmp_path):
    state = _state()
    outcome, store = await _run(tmp_path, _fake(state), _io(state, voucher_moves_ledger=True))
    assert outcome is Outcome.DIFFERENT
    assert "ledger's own AlterID" in store.probe_entry(1)["parts"]["A"]["summary"]


async def test_missing_counters_fail_and_nothing_confirmed(tmp_path):
    state = _state()
    outcome, store = await _run(tmp_path, _fake(state, include_counters=False), _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.FAILED
    assert "rolling re-pull" in part["spec_impact"]
    assert store.confirmed("company_counters") is None


async def test_fallback_ledger_used_when_no_bank_charges(tmp_path):
    state = _state(expense="Electricity")
    fake = _fake(state, ledgers=[{"Name": "Cash", "Parent": "Cash-in-Hand"},
                                 {"Name": "Electricity", "Parent": "Indirect Expenses"},
                                 {"Name": "Salaries", "Parent": "Indirect Expenses"}])
    outcome, store = await _run(tmp_path, fake, _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    assert part["observations"]["ledger"] == "Electricity"


async def test_without_probe0_confirmed_blocks(tmp_path):
    state = _state()
    client, store, capture = make_harness(tmp_path, _fake(state))
    outcome = await run_probe(p01.PROBE, labels=None, client=client, store=store, capture=capture, io=_io(state))
    assert outcome is Outcome.BLOCKED
    assert "Run probe(s) 0 first." in store.probe_entry(1)["parts"]["A"]["summary"]


async def test_existing_throwaway_ledger_blocks_before_any_change(tmp_path):
    state = _state()
    io = _io(state)
    outcome, store = await _run(tmp_path, _fake(state, throwaway_exists=True), io)
    assert outcome is Outcome.BLOCKED
    assert "already exists" in store.probe_entry(1)["parts"]["A"]["summary"]
    assert io.actions == []


async def test_cleanup_note_pending_when_blocked_after_create_ledger(tmp_path):
    state = _state()
    fake = _fake(state)
    calls = {"n": 0}
    others = [(marker, handler) for marker, handler in fake.routes if marker != "S0CompanyCounters"]

    def counters(body):
        calls["n"] += 1
        if calls["n"] == 6:   # candidates, baseline, 3 voucher steps, then the read after create_ledger
            return httpx.ReadTimeout
        row = {"Name": A, "GUID": "g-1", "BooksFrom": "20250401", "LastVoucherDate": "20260301", "AlterID": "1",
               "AltVchId": str(state["AltVchId"]), "AltMstId": str(state["AltMstId"])}
        return objects_xml("COMPANY", [row])

    fake.routes = [("S0CompanyCounters", counters), *others]
    outcome, store = await _run(tmp_path, fake, _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert part["observations"]["cleanup_needed"] == [p01.DELETE_LEDGER_NOTE]


async def test_auto_operator_runs_probe_1_and_leaves_company_a_as_it_was(tmp_path):
    books = FakeBooks(name=A)
    op = build_auto_operator(config=tmp_config(tmp_path), transport=books.transport(),
                             runner=FakeRunner(books, [TallyProcess(8, OWN_COMMAND)]), echo=lambda line: None)
    store = ResultsStore(tmp_path / "r.json")
    mark_done(store, 0)
    outcome = await run_probe(p01.PROBE, labels=None, client=TallyClient(transport=books.transport()), store=store,
                              capture=Capture(tmp_path / "fixtures"), io=op)
    part = store.probe_entry(1)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    assert part["observations"]["run_mode"] == "auto"
    assert part["observations"]["ledger"] == "Electricity"
    assert books.state["ledgers"] == seed_state(A)["ledgers"]     # nothing left behind: no leftover EMAIL, no ledger
    assert books.state["vouchers"] == {}


# --- M6: probe 16's F2-date discriminator comes from this baseline, taken before the throwaway voucher ---------------


async def test_last_voucher_date_baseline_is_recorded_before_the_throwaway_voucher(tmp_path):
    state = _state()
    _, store = await _run(tmp_path, _fake(state), _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    assert part["observations"][p01.LAST_VOUCHER_DATE_BASELINE] == {
        "value": "20260301", "available": True, "note": ""}
    # It is the pre-throwaway reading, not the value the matrix ends on.
    assert part["observations"]["matrix"]["create_voucher"]["values"]["LastVoucherDate"] == "20260301"


async def test_absent_last_voucher_date_is_recorded_as_not_available(tmp_path):
    """The Company collection exports nothing for LastVoucherDate → the field is dropped from the confirmed
    request, so the baseline must say "not available" instead of an empty string that reads as a safe date."""
    state = _state()
    _, store = await _run(tmp_path, _fake(state, last_voucher_date=""), _io(state))
    part = store.probe_entry(1)["parts"]["A"]
    baseline = part["observations"][p01.LAST_VOUCHER_DATE_BASELINE]
    assert baseline["value"] == "" and baseline["available"] is False
    assert "not available" in baseline["note"]
