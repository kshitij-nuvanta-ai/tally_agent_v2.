from probes import p02_active_company_guid as p02
from probes.companies import COMPANIES
from probes.core import Outcome
from probes.runner import run_probe
from tests.probes.fakes import FakeTally, ScriptedIO, make_harness, mark_done, objects_xml

A = COMPANIES["A"]
EMPTY = "<ENVELOPE></ENVELOPE>"


def _row(fake, extra=None, ignore_state=False):
    def handler(body):
        if not fake.companies and not ignore_state:
            return objects_xml("COMPANY", [])
        return objects_xml("COMPANY", [{"Name": A, "GUID": "g-1", **(extra or {})}])
    return handler


def _io(fake):
    def on_wait(instruction):
        if instruction.startswith("Close every company"):
            fake.companies = []
        elif instruction.startswith("Open company"):
            fake.companies = [A]
    return ScriptedIO(on_wait=on_wait)


async def _run(tmp_path, fake):
    client, store, capture = make_harness(tmp_path, fake)
    mark_done(store, 0, part="A")
    outcome = await run_probe(p02.PROBE, labels=None, client=client, store=store, capture=capture, io=_io(fake))
    return outcome, store


async def test_cheapest_candidate_confirmed(tmp_path):
    fake = FakeTally([A])
    fake.route("S0ActiveCompany", _row(fake))
    fake.route("<TYPE>Object</TYPE>", _row(fake, extra={"Padding": "x" * 300}))
    fake.route("S0CompanyListGuid", _row(fake))
    outcome, store = await _run(tmp_path, fake)
    part = store.probe_entry(2)["parts"]["A"]
    assert outcome is Outcome.CONFIRMED, part["summary"]
    assert store.confirmed("active_company")["candidate"] == "a"
    assert part["observations"]["no_company_distinguishable"] is True
    assert "p02_A_active_a_no_company.xml" in part["fixtures"]


async def test_only_full_list_works_is_different(tmp_path):
    fake = FakeTally([A])
    fake.route("S0ActiveCompany", lambda body: EMPTY)
    fake.route("<TYPE>Object</TYPE>", lambda body: EMPTY)
    fake.route("S0CompanyListGuid", _row(fake))
    outcome, store = await _run(tmp_path, fake)
    assert outcome is Outcome.DIFFERENT
    assert store.confirmed("active_company")["candidate"] == "c"
    assert "company list" in store.probe_entry(2)["parts"]["A"]["spec_impact"]


async def test_no_candidate_works_fails(tmp_path):
    fake = FakeTally([A])
    outcome, store = await _run(tmp_path, fake)
    assert outcome is Outcome.FAILED
    assert store.confirmed("active_company") is None


async def test_guid_still_returned_with_no_company_is_different(tmp_path):
    fake = FakeTally([A])
    fake.route("S0ActiveCompany", _row(fake, ignore_state=True))
    outcome, store = await _run(tmp_path, fake)
    part = store.probe_entry(2)["parts"]["A"]
    assert outcome is Outcome.DIFFERENT
    assert part["observations"]["no_company_distinguishable"] is False


async def test_without_probe0_confirmed_blocks(tmp_path):
    fake = FakeTally([A])
    client, store, capture = make_harness(tmp_path, fake)
    outcome = await run_probe(p02.PROBE, labels=None, client=client, store=store, capture=capture, io=_io(fake))
    assert outcome is Outcome.BLOCKED
    assert "Run probe(s) 0 first." in store.probe_entry(2)["parts"]["A"]["summary"]


async def test_reopening_wrong_company_blocks_naming_both(tmp_path):
    fake = FakeTally([A])
    fake.route("S0ActiveCompany", _row(fake))
    fake.route("<TYPE>Object</TYPE>", _row(fake, extra={"Padding": "x" * 300}))
    fake.route("S0CompanyListGuid", _row(fake))

    def on_wait(instruction):
        if instruction.startswith("Close every company"):
            fake.companies = []
        elif instruction.startswith("Open company"):
            fake.companies = ["Some Other Co"]

    client, store, capture = make_harness(tmp_path, fake)
    mark_done(store, 0, part="A")
    outcome = await run_probe(p02.PROBE, labels=None, client=client, store=store, capture=capture,
                              io=ScriptedIO(on_wait=on_wait))
    part = store.probe_entry(2)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert A in part["summary"] and "Some Other Co" in part["summary"]
    assert part["observations"]["cleanup_needed"] == [f"Open company {A!r} again (only that one)"]
