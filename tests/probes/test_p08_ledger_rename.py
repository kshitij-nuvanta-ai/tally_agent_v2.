import html
import re

from probes import p08_ledger_rename as p08
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, line, make_harness, objects_xml, ready_store, vch, vouchers_xml


def _fake(export_old_name=False, new_guid_on_rename=False, target_exists=False):
    fake, counters = a_tally()
    state = {"name": p08.LEDGER, "guid": "g-1-0000001e", "alter_id": 30}

    def ledger(body):
        match = re.search(r'\$Name = "([^"]*)"', body)
        wanted = html.unescape(match.group(1)) if match else ""
        rows = []
        if wanted == state["name"]:
            rows.append({"Name": state["name"], "GUID": state["guid"], "AlterID": str(state["alter_id"]),
                         "Parent": "South Zone Debtors"})
        if target_exists and wanted == p08.RENAMED:
            rows.append({"Name": p08.RENAMED, "GUID": "g-x", "AlterID": "1", "Parent": "South Zone Debtors"})
        return objects_xml("LEDGER", rows)

    def vouchers(body):
        name = p08.LEDGER if export_old_name else state["name"]
        return vouchers_xml([
            vch({"MASTERID": "6", "ALTERID": "106", "GUID": "g-v6", "PARTYLEDGERNAME": name, "DATE": "20251105"},
                lines=[line(name, "-30090.00"), line("Sales - Electronics", "30090.00")]),
            vch({"MASTERID": "13", "ALTERID": "113", "GUID": "g-v13", "PARTYLEDGERNAME": name, "DATE": "20260120"},
                lines=[line(name, "-164492.00"), line("Sales - Electronics", "164492.00")]),
            vch({"MASTERID": "8", "ALTERID": "108", "GUID": "g-v8", "PARTYLEDGERNAME": "Apex", "DATE": "20251125"},
                lines=[line("Apex", "-1.00"), line("Sales - Electronics", "1.00")]),
        ])

    fake.route("S0P08Ledger", ledger)
    fake.route("S0P08Vouchers", vouchers)

    def on_action(action):
        state["name"] = action.params["to"]
        state["alter_id"] += 1
        counters["AltMstId"] += 1
        if new_guid_on_rename:
            state["guid"] = "g-renamed"

    return fake, on_action


async def _run(tmp_path, **kw):
    fake, on_action = _fake(**kw)
    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p08.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(8)["parts"]["A"], io


async def test_rename_keeps_guid_and_old_vouchers_export_the_new_name(tmp_path):
    part, io = await _run(tmp_path)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["same_guid"] is True and obs["ledger_alterid_bumped"] is True
    assert obs["vouchers_export"] == "new name" and obs["voucher_alterids_changed"] is False
    assert [a.params for a in io.actions] == [
        {"company": "Bharat Traders Probe Copy", "from": p08.LEDGER, "to": p08.RENAMED},
        {"company": "Bharat Traders Probe Copy", "from": p08.RENAMED, "to": p08.LEDGER}]


async def test_old_vouchers_keeping_the_old_name_is_different(tmp_path):
    part, _ = await _run(tmp_path, export_old_name=True)
    assert part["outcome"] == "DIFFERENT"
    assert "old name" in part["summary"]


async def test_guid_changing_on_rename_fails(tmp_path):
    part, _ = await _run(tmp_path, new_guid_on_rename=True)
    assert part["outcome"] == "FAILED"
    assert "R9" in part["spec_impact"]


async def test_existing_target_name_blocks_before_any_change(tmp_path):
    part, io = await _run(tmp_path, target_exists=True)
    assert part["outcome"] == "BLOCKED"
    assert io.actions == []
