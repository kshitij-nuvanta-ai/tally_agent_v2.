from decimal import Decimal
from pathlib import Path

import httpx

from tally_bridge.sync_reports import parse_trial_balance
from probes import p17_ledger_level_tb as p17
from probes.core import Outcome
from probes.runner import run_probe
from tests.probes.fakes import (COMPANY_A, ScriptedIO, a_tally, company_list_xml, make_harness, objects_xml,
                                   ready_store, tb_xml)

BASELINE = {"Capital Account": "1000.00", "Current Liabilities": "1834142.00", "Current Assets": "-1835142.00"}
LEDGERS = [{"Name": "Capital Account", "Parent": "Capital Account"},
           {"Name": "HP India Sales Pvt Ltd", "Parent": "Sundry Creditors"}, {"Name": "Cash", "Parent": "Cash-in-Hand"}]
GROUPS = [{"Name": "Capital Account", "Parent": "Primary"}, {"Name": "Current Liabilities", "Parent": "Primary"},
          {"Name": "Sundry Creditors", "Parent": "Current Liabilities"}, {"Name": "Current Assets", "Parent": "Primary"},
          {"Name": "Cash-in-Hand", "Parent": "Current Assets"}, {"Name": "Stock-in-Hand", "Parent": "Current Assets"}]
GROUP_TB = tb_xml([("Capital Account", "", "1000.00"), ("Current Liabilities", "", "1834142.00"),
                   ("Current Assets", "-1835142.00", "")])


def _exploded(hp="1834142.00"):
    return tb_xml([("Capital Account", "", "1000.00"), ("Capital Account", "", "1000.00"),
                   ("Current Liabilities", "", "1834142.00"), ("Sundry Creditors", "", "1834142.00"),
                   ("HP India Sales Pvt Ltd", "", hp), ("Current Assets", "-1835142.00", ""),
                   ("Cash-in-Hand", "-1835142.00", ""), ("Cash", "-1835142.00", "")])


def _explode_on_flag(hp="1834142.00"):
    return lambda body: _exploded(hp) if "<EXPLODEFLAG>Yes</EXPLODEFLAG>" in body else GROUP_TB


def _fake(tb_handler):
    fake, _ = a_tally()
    fake.route("S0P17Ledgers", lambda body: objects_xml("LEDGER", LEDGERS))
    fake.route("S0P17Groups", lambda body: objects_xml("GROUP", GROUPS))
    fake.route("<ID>Trial Balance</ID>", tb_handler)
    return fake


async def _run(tmp_path, fake):
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, baseline=BASELINE)
    outcome = await run_probe(p17.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return outcome, store, store.probe_entry(17)["parts"]["A"]


async def test_explodeflag_gives_ledger_rows_and_is_confirmed(tmp_path):
    outcome, store, part = await _run(tmp_path, _fake(_explode_on_flag()))
    assert outcome is Outcome.CONFIRMED, part["summary"]
    candidates = part["observations"]["candidates"]
    assert part["observations"]["working_variable"] == "explodeflag"
    assert candidates["explodeflag"]["ledger_rows"] == 3
    assert candidates["explodeflag"]["stock_bearing_skipped"] == ["Current Assets"]
    assert candidates["isledgerwise"]["ledger_rows"] == 0
    assert store.confirmed("ledger_level_tb")["candidate"] == "explodeflag"
    assert "__COMPANY__" in store.confirmed("ledger_level_tb")["xml_template"]
    assert len([f for f in part["fixtures"] if f.startswith("p17_A_tb_exploded_")]) == len(p17.EXPLODE_CANDIDATES)


async def test_no_candidate_gives_ledger_rows_fails(tmp_path):
    outcome, store, part = await _run(tmp_path, _fake(lambda body: GROUP_TB))
    assert outcome is Outcome.FAILED
    assert "rung 2 stays group-level" in part["spec_impact"]
    assert store.confirmed("ledger_level_tb") is None


async def test_ledger_rows_that_do_not_sum_are_different(tmp_path):
    outcome, _, part = await _run(tmp_path, _fake(_explode_on_flag(hp="1834000.00")))
    assert outcome is Outcome.DIFFERENT
    assert "Current Liabilities" in part["observations"]["candidates"]["explodeflag"]["mismatches"]


async def test_a_candidate_that_hangs_tally_fails_and_stops(tmp_path):
    hung = {"yes": False}

    def tb(body):
        if "<EXPLODEFLAG>Yes</EXPLODEFLAG>" in body:
            hung["yes"] = True
            return httpx.ReadTimeout
        return GROUP_TB

    fake = _fake(tb)
    fake.route("<ID>List of Companies</ID>",
               lambda body: httpx.ReadTimeout if hung["yes"] else company_list_xml([COMPANY_A]))
    outcome, _, part = await _run(tmp_path, fake)
    assert outcome is Outcome.FAILED
    assert "unresponsive" in part["summary"] and "hang Tally" in part["spec_impact"]
    assert list(part["observations"]["candidates"]) == ["explodeflag"]


# --- 2026-09-23 live finding: ISLEDGERWISE=Yes IS a working ledger-level TB -------------------------------------------

# The 2026-09-23 run (untyped dates, C33), moved here before plan part 5 re-runs these probes (Task 1).
SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "sync" / "c33_untyped_2026-09-23"


def _live(name):
    return (SNAPSHOT / name).read_text(encoding="utf-8")


LIVE_BASELINE = {row["account_name"]: str(row["closing_balance"])
                 for row in parse_trial_balance((SNAPSHOT.parent / "p00_A_anchors_tb.xml").read_text(encoding="utf-8"))}
LIVE_LEDGERS = p17.parse_parents(_live("p17_A_ledger_list.xml"), "LEDGER")
LIVE_GROUPS = p17.parse_parents(_live("p17_A_group_list.xml"))


def _live_evaluate(candidate):
    return p17.evaluate(_live(f"p17_A_tb_exploded_{candidate}.xml"), LIVE_LEDGERS, LIVE_GROUPS, LIVE_BASELINE)


def test_isledgerwise_is_a_working_ledger_level_tb_on_the_live_response():
    """The live response carries NO group rows, and company A has a ledger named "Capital Account". Discriminating
    rows by name consumed that ledger as the group total and produced the only mismatch the probe ever recorded."""
    entry = _live_evaluate("isledgerwise")
    assert entry["emits_group_rows"] is False
    assert entry["ledger_rows"] == 29 and entry["rows"] == 30
    assert "Capital Account" in LIVE_LEDGERS                 # the ledger that used to be swallowed
    assert entry["mismatches"] == {}
    assert entry["sums_match"] is True
    assert entry["non_ledger_rows"] == ["Opening Stock"]      # caveat 1: one synthetic, non-ledger row
    assert entry["stock_bearing_skipped"] == ["Current Assets"]


def test_explodeflag_really_does_stop_at_the_second_group_level():
    """F3 stays a genuine finding: a failing candidate must not be massaged into a pass."""
    assert _live_evaluate("explodeflag")["mismatches"]["Current Liabilities"] == {
        "group_row": Decimal("1757357.00"), "ledger_sum": Decimal("0")}
    all_levels = _live_evaluate("explodealllevels")
    assert all_levels["emits_group_rows"] is True
    # Duties & Taxes (one level down) IS exploded; the 5 creditors under the custom sub-groups never appear.
    assert all_levels["mismatches"]["Current Liabilities"] == {"group_row": Decimal("1757357.00"),
                                                              "ledger_sum": Decimal("-76785.00")}
    assert all_levels["sums_match"] is False
    for unrecognised in ("svexplodeflag", "ledgerwise"):
        assert _live_evaluate(unrecognised)["ledger_rows"] == 0      # not recognised: the plain TB comes back


async def test_the_live_candidates_make_probe17_confirmed_on_isledgerwise(tmp_path):
    def tb(body):
        for key, variables in p17.EXPLODE_CANDIDATES.items():
            if all(f"<{name}>{value}</{name}>" in body for name, value in variables.items()):
                match = key
        return _live(f"p17_A_tb_exploded_{match}.xml")

    fake, _ = a_tally()
    fake.route("S0P17Ledgers", lambda body: _live("p17_A_ledger_list.xml"))
    fake.route("S0P17Groups", lambda body: _live("p17_A_group_list.xml"))
    fake.route("<ID>Trial Balance</ID>", tb)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, baseline=LIVE_BASELINE)
    outcome = await run_probe(p17.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    part = store.probe_entry(17)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    assert part["observations"]["working_variable"] == "isledgerwise"
    assert "29 ledger rows" in part["summary"]
    assert store.confirmed("ledger_level_tb")["candidate"] == "isledgerwise"
    assert "<ISLEDGERWISE>Yes</ISLEDGERWISE>" in store.confirmed("ledger_level_tb")["xml_template"]
    impact = part["spec_impact"]
    assert "Rung 2 CAN compare at ledger level" in impact and "stay at group level" in impact
    assert "Caveat 1" in impact and "Opening Stock" in impact
    assert "Caveat 2" in impact and "EXPLODEFLAG" in impact and "second group level" in impact
    assert "custom sub-group" in impact


async def test_every_date_p17_sends_is_honoured_and_the_confirmed_template_is_typed(tmp_path):
    from probes.safety import educational_ignored_dates
    fake = _fake(_explode_on_flag())
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, baseline=BASELINE, licence="educational")
    await run_probe(p17.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    assert store.probe_entry(17)["parts"]["A"]["outcome"] == "CONFIRMED"
    assert all(educational_ignored_dates(body) == [] for body in fake.probe_requests())
    template = store.confirmed("ledger_level_tb")["xml_template"]
    assert '<SVFROMDATE TYPE="Date">01-04-2025</SVFROMDATE>' in template
