import json
from datetime import datetime, timezone
from decimal import Decimal

from probes.core import Outcome, PartResult
from probes.results import ResultsStore

T1 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)


def _record(store, part, result, ran_at=T1, all_parts=("A", "B")):
    return store.record_part(16, part, result, all_parts=list(all_parts), fixtures=["p16_A_x.xml"],
                             manual_steps=[], ran_at=ran_at)


def test_partial_then_combined(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    assert _record(store, "A", PartResult(Outcome.CONFIRMED, "ok")) is Outcome.PARTIAL
    assert _record(store, "B", PartResult(Outcome.DIFFERENT, "diff", spec_impact="change")) is Outcome.DIFFERENT
    assert store.outcome(16) is Outcome.DIFFERENT
    reloaded = ResultsStore(tmp_path / "results.json")
    assert reloaded.probe_entry(16)["parts"]["B"]["spec_impact"] == "change"


def test_rerun_moves_previous_into_history(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    _record(store, "A", PartResult(Outcome.BLOCKED, "timeout"), all_parts=("A",))
    _record(store, "A", PartResult(Outcome.CONFIRMED, "ok"), ran_at=T2, all_parts=("A",))
    entry = store.probe_entry(16)
    assert entry["outcome"] == "CONFIRMED"
    assert [h["outcome"] for h in entry["history"]] == ["BLOCKED"]
    assert entry["history"][0]["part"] == "A"


def test_confirmed_requests_and_environment(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    assert store.confirmed("company_counters") is None
    store.confirm_request("company_counters", 1, "<X/>", fields=["GUID"])
    store.update_environment(licence="licensed")
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert data["confirmed_requests"]["company_counters"] == {"probe": 1, "xml_template": "<X/>", "fields": ["GUID"]}
    assert data["environment"] == {"licence": "licensed"}


def test_decimal_observations_serialise(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    _record(store, "A", PartResult(Outcome.CONFIRMED, "ok", observations={"total": Decimal("970537.00")}), all_parts=("A",))
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert data["probes"]["16"]["parts"]["A"]["observations"]["total"] == "970537.00"


def test_mixed_key_observations_do_not_crash(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    _record(store, "A", PartResult(Outcome.CONFIRMED, "ok", observations={1: "a", "b": 2}), all_parts=("A",))
    reloaded = ResultsStore(tmp_path / "results.json")
    assert reloaded.probe_entry(16)["parts"]["A"]["observations"] == {"1": "a", "b": 2}


def test_remaining_lists_parts_not_yet_recorded(tmp_path):
    store = ResultsStore(tmp_path / "results.json")
    _record(store, "A", PartResult(Outcome.CONFIRMED, "ok"))
    assert store.probe_entry(16)["remaining"] == ["B"]
    _record(store, "B", PartResult(Outcome.CONFIRMED, "ok"))
    assert store.probe_entry(16)["remaining"] == []
