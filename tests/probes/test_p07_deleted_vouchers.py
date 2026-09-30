from probes import p07_deleted_vouchers as p07
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, make_harness, objects_xml, ready_store


def _fake(reuse_guid=False, reuse_master_id=False, linger=None):
    fake, counters = a_tally()
    state = {"rows": [], "next": 51, "last": None, "by_ref": {}}
    fake.route("S0P07Vouchers", lambda body: objects_xml("VOUCHER", state["rows"]))

    def on_action(action):
        ref = action.params["ref"]
        if action.kind == "create_voucher":
            last = state["last"]
            master_id = last["MasterId"] if (reuse_guid or reuse_master_id) and last else str(state["next"])
            guid = last["GUID"] if reuse_guid and last else f"g-1-{state['next']:08x}"
            row = {"GUID": guid, "MasterId": master_id, "AlterID": str(100 + state["next"]),
                   "Narration": action.params["narration"], "Date": "20260331", "IsDeleted": "No"}
            state["next"] += 1
            state["rows"].append(row)
            state["by_ref"][ref] = row
            counters["AltVchId"] += 1
        else:
            row = state["by_ref"][ref]
            state["last"] = dict(row)
            counters["AltVchId"] += 3
            if linger == "tombstone":
                row["IsDeleted"] = "Yes"
            elif linger is None:
                state["rows"].remove(row)

    return fake, on_action


async def _run(tmp_path, **kw):
    fake, on_action = _fake(**kw)
    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p07.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(7)["parts"]["A"], io


async def test_deleted_voucher_vanishes_and_ids_are_not_reused(tmp_path):
    part, io = await _run(tmp_path)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert [a.kind for a in io.actions] == ["create_voucher", "delete_voucher", "create_voucher", "delete_voucher"]
    assert part["observations"]["counters_moved_on_delete"] is True
    assert part["fixtures"] == ["p07_A_throwaway_created.xml", "p07_A_after_delete.xml", "p07_A_second_throwaway.xml",
                                "p07_A_after_second_delete.xml"]


async def test_reused_guid_fails(tmp_path):
    part, _ = await _run(tmp_path, reuse_guid=True)
    assert part["outcome"] == "FAILED"
    assert "GUID" in part["summary"] and "R7" in part["spec_impact"]


async def test_tombstone_is_different(tmp_path):
    part, _ = await _run(tmp_path, linger="tombstone")
    assert part["outcome"] == "DIFFERENT"
    assert "tombstone" in part["spec_impact"]


async def test_voucher_lingering_without_a_flag_fails(tmp_path):
    part, _ = await _run(tmp_path, linger="plain")
    assert part["outcome"] == "FAILED"
    assert "lingers" in part["summary"]


async def test_reused_master_id_with_a_new_guid_is_different(tmp_path):
    part, _ = await _run(tmp_path, reuse_master_id=True)
    assert part["outcome"] == "DIFFERENT"
    assert "MasterID" in part["summary"]
