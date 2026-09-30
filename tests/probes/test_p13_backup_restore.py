import copy

from probes import p13_backup_restore as p13
from probes.runner import run_probe
from tests.probes.fakes import COMPANY_A, ScriptedIO, a_tally, make_harness, objects_xml, ready_store


def _fake(new_guid=False, restore_works=True):
    fake, counters = a_tally({"AltVchId": 50, "AltMstId": 266, "GUID": "g-1"})
    state = {"vouchers": [{"GUID": f"g-1-{i:08x}", "MasterId": str(i), "Narration": f"seed {i}"} for i in range(1, 8)],
             "snapshot": None}
    fake.route("S0P13Vouchers", lambda body: objects_xml("VOUCHER", state["vouchers"]))

    def on_action(action):
        if action.kind == "backup_company":
            state["snapshot"] = (copy.deepcopy(state["vouchers"]), dict(counters))
        elif action.kind == "create_voucher":
            state["vouchers"].append({"GUID": "g-1-00000033", "MasterId": "51", "Narration": action.params["narration"]})
            counters["AltVchId"] += 1
        elif action.kind == "restore_company" and restore_works:
            vouchers, saved = state["snapshot"]
            state["vouchers"] = vouchers
            counters.update(saved)
            if new_guid:
                counters["GUID"] = "g-2"

    return fake, on_action


async def _run(tmp_path, run_mode="manual", **kw):
    fake, on_action = _fake(**kw)
    io = ScriptedIO(on_action=on_action, run_mode=run_mode)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p13.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(13)["parts"]["A"], io


async def test_restore_keeps_identity_and_rolls_counters_back(tmp_path):
    part, io = await _run(tmp_path)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["same_guid"] is True and obs["counters_back_below"] is True and obs["throwaway_gone"] is True
    assert obs["restore_method"] == "Tally Backup / Restore screens (manual)"
    assert [a.kind for a in io.actions] == ["backup_company", "create_voucher", "restore_company"]
    assert part["fixtures"] == ["p13_A_before_backup.xml", "p13_A_before_backup_counters.xml",
                                "p13_A_after_throwaway.xml", "p13_A_after_restore.xml",
                                "p13_A_after_restore_counters.xml"]


async def test_auto_mode_notes_the_file_level_restore(tmp_path):
    part, _ = await _run(tmp_path, run_mode="auto")
    assert part["outcome"] == "CONFIRMED"
    assert p13.AUTO_LIMIT_NOTE in part["summary"]
    assert part["observations"]["restore_method"].startswith("file copy")


async def test_new_guid_after_restore_is_different(tmp_path):
    part, _ = await _run(tmp_path, new_guid=True)
    assert part["outcome"] == "DIFFERENT"
    assert "Q25" in part["spec_impact"]


async def test_restore_that_does_not_happen_blocks_with_cleanup(tmp_path):
    part, _ = await _run(tmp_path, restore_works=False)
    assert part["outcome"] == "BLOCKED"
    assert part["observations"]["cleanup_needed"] == [p13.CLEANUP_NOTE]


# --- M2: empty-sample guards ------------------------------------------------------------------------------------


async def test_no_numeric_master_ids_to_sample_blocks(tmp_path):
    fake, on_action = _fake()
    fake.routes = [(m, h) for m, h in fake.routes if m != "S0P13Vouchers"]
    fake.route("S0P13Vouchers", lambda body: objects_xml("VOUCHER", []))
    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p13.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    part = store.probe_entry(13)["parts"]["A"]
    assert part["outcome"] == "BLOCKED"
    assert part["summary"] == "No vouchers with a numeric MasterID to sample"
    assert io.actions == []


async def test_nothing_read_back_after_restore_blocks(tmp_path):
    fake, on_action_base = _fake()

    def on_action(action):
        on_action_base(action)
        if action.kind == "restore_company":
            fake.routes = [(m, h) for m, h in fake.routes if m != "S0P13Vouchers"]
            fake.route("S0P13Vouchers", lambda body: objects_xml("VOUCHER", []))

    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p13.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    part = store.probe_entry(13)["parts"]["A"]
    assert part["outcome"] == "BLOCKED"
    assert part["summary"] == "Nothing read back after the restore"


# --- M2 (retro): the counters must be seen to rise before "fall back" is judged -----------------------------------


async def test_counters_not_risen_is_blocked_not_different(tmp_path):
    fake_, counters = a_tally({"AltVchId": 50, "AltMstId": 266, "GUID": "g-1"})
    state = {"vouchers": [{"GUID": f"g-1-{i:08x}", "MasterId": str(i), "Narration": f"seed {i}"} for i in range(1, 8)],
             "snapshot": None}
    fake_.route("S0P13Vouchers", lambda body: objects_xml("VOUCHER", state["vouchers"]))

    def act(action):
        if action.kind == "backup_company":
            state["snapshot"] = (copy.deepcopy(state["vouchers"]), dict(counters))
        elif action.kind == "create_voucher":
            state["vouchers"].append({"GUID": "g-1-00000033", "MasterId": "51", "Narration": action.params["narration"]})
        elif action.kind == "restore_company":
            state["vouchers"], saved = state["snapshot"]
            counters.update(saved)

    io = ScriptedIO(on_action=act)
    client, store, capture = make_harness(tmp_path, fake_)
    ready_store(store)
    await run_probe(p13.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    part = store.probe_entry(13)["parts"]["A"]
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "did not rise" in part["summary"]


async def test_missing_altvchid_is_blocked_not_different(tmp_path):
    fake, on_action = _fake()
    fake.routes = [(m, h) for m, h in fake.routes if m != "S0CompanyCounters"]
    fake.route("S0CompanyCounters", lambda body: objects_xml("COMPANY", [{
        "Name": COMPANY_A, "GUID": "g-1", "AltMstId": "266", "BooksFrom": "20250401", "LastVoucherDate": "20260301",
        "AlterID": "266"}]))
    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p13.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    part = store.probe_entry(13)["parts"]["A"]
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "unreadable" in part["summary"] or "did not rise" in part["summary"]
