import httpx

from probes import p00_environment
from probes.companies import COMPANIES, SEED_COMPANY
from probes.core import Outcome
from probes.runner import run_probe
from tests.probes.fakes import FakeTally, ScriptedIO, bills_xml, make_harness, objects_xml, tb_xml

A = COMPANIES["A"]
RECEIVABLE = bills_xml([("S015", "Apex Technologies Pvt Ltd", "-62800.00"), ("S010", "Rest", "-907737.00")])
PAYABLE = bills_xml([("P001", "Samsung India Electronics", "243100.00"), ("P004", "Rest", "1591042.00")])
TB = tb_xml([("Capital Account", "", "100000.00"), ("Current Assets", "-100000.00", "")])
DATA_FOLDER = "/Users/probe/TallyData/S0ProbeCopy"


def _fake(receivable=RECEIVABLE):
    fake = FakeTally([])
    fake.route("S0CompanyGuid", lambda body: objects_xml(
        "COMPANY", [{"Name": fake.companies[0], "GUID": "guid-seed"}] if fake.companies else []))
    fake.route("<ID>Bills Receivable</ID>", lambda body: receivable)
    fake.route("<ID>Bills Payable</ID>", lambda body: PAYABLE)
    fake.route("<ID>Trial Balance</ID>", lambda body: TB)
    return fake


def _io(fake, answers=(DATA_FOLDER, "TallyPrime 7.0", "Edit Log", "licensed")):
    def on_wait(instruction):
        if instruction.startswith("Restore"):
            fake.companies = [SEED_COMPANY]
        elif "rename it to" in instruction:
            fake.companies = [A]

    return ScriptedIO(answers=list(answers), on_wait=on_wait)


async def _run(tmp_path, fake, io, monkeypatch, wine="wine-9.0"):
    monkeypatch.setattr(p00_environment, "wine_version", lambda: wine)
    client, store, capture = make_harness(tmp_path, fake)
    outcome = await run_probe(p00_environment.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return outcome, store


async def test_happy_path_makes_company_a(tmp_path, monkeypatch):
    fake = _fake()
    outcome, store = await _run(tmp_path, fake, _io(fake), monkeypatch)
    part = store.probe_entry(0)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    assert store.environment["wine"] == "wine-9.0"
    assert store.environment["licence"] == "licensed"
    assert store.environment["company_a_tb_baseline"] == {"Capital Account": "100000.00", "Current Assets": "-100000.00"}
    assert part["observations"]["guid_changed_on_rename"] is False
    assert part["observations"]["data_folder"] == DATA_FOLDER
    assert part["fixtures"] == [
        "p00_A_company_list.xml", "p00_A_guid_before_rename.xml",
        "p00_A_anchors_bills_receivable.xml", "p00_A_anchors_bills_payable.xml", "p00_A_anchors_tb.xml",
        "p00_A_guid_after_rename.xml",
    ]


async def test_tally_unreachable_fails_with_q29_impact(tmp_path, monkeypatch):
    fake = _fake()
    fake.down = True
    outcome, store = await _run(tmp_path, fake, _io(fake), monkeypatch)
    part = store.probe_entry(0)["parts"]["A"]
    assert outcome is Outcome.FAILED
    assert "Q29" in part["spec_impact"]


async def test_timeout_on_first_request_blocks_with_popup_hint(tmp_path, monkeypatch):
    fake = _fake()
    fake.route("<ID>List of Companies</ID>", lambda body: httpx.ReadTimeout)
    outcome, store = await _run(tmp_path, fake, _io(fake), monkeypatch)
    part = store.probe_entry(0)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert part["spec_impact"] == ""
    assert "popup" in part["summary"].lower()


async def test_anchor_mismatch_blocks_before_rename_pause(tmp_path, monkeypatch):
    fake = _fake(receivable=bills_xml([("S015", "Apex", "-1.00")]))
    io = _io(fake)
    outcome, store = await _run(tmp_path, fake, io, monkeypatch)
    part = store.probe_entry(0)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert "Bills Receivable" in part["summary"]
    assert "company_a_tb_baseline" not in store.environment
    assert not any("rename it to" in w for w in io.waits)


async def test_wrong_company_after_restore_blocks(tmp_path, monkeypatch):
    fake = _fake()
    io = ScriptedIO(on_wait=lambda instruction: setattr(fake, "companies", ["Something Else"]))
    outcome, store = await _run(tmp_path, fake, io, monkeypatch)
    assert outcome is Outcome.BLOCKED
    assert SEED_COMPANY in store.probe_entry(0)["parts"]["A"]["summary"]


async def test_missing_wine_asks_for_version(tmp_path, monkeypatch):
    fake = _fake()
    io = _io(fake, answers=("wine-8.0 (typed)", DATA_FOLDER, "TallyPrime 7.0", "Edit Log", "licensed"))
    outcome, store = await _run(tmp_path, fake, io, monkeypatch, wine=None)
    assert outcome is Outcome.CONFIRMED
    assert store.environment["wine"] == "wine-8.0 (typed)"


async def test_edition_and_licence_are_case_insensitive_and_stored_canonical(tmp_path, monkeypatch):
    fake = _fake()
    io = _io(fake, answers=(DATA_FOLDER, "TallyPrime 7.0", "edit log", "LICENSED"))
    outcome, store = await _run(tmp_path, fake, io, monkeypatch)
    assert outcome is Outcome.CONFIRMED
    assert store.environment["edition"] == "Edit Log"
    assert store.environment["licence"] == "licensed"


async def test_invalid_edition_is_reasked_until_valid(tmp_path, monkeypatch):
    fake = _fake()
    io = _io(fake, answers=(DATA_FOLDER, "TallyPrime 7.0", "bogus", "Edit Log", "licensed"))
    outcome, store = await _run(tmp_path, fake, io, monkeypatch)
    assert outcome is Outcome.CONFIRMED, store.probe_entry(0)["parts"]["A"]["summary"]
    assert store.environment["edition"] == "Edit Log"
    edition_asks = [a for a in io.asks if "Edition" in a]
    assert len(edition_asks) == 2
    assert "isn't" in edition_asks[1] and "'bogus'" in edition_asks[1]
