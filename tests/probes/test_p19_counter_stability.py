from probes import p19_counter_stability as p19
from probes.runner import run_probe
from tests.probes.fakes import COMPANY_A, FakeTally, ScriptedIO, make_harness, objects_xml, ready_store, tb_xml


def _fake(state, drift=False):
    fake = FakeTally([COMPANY_A])

    def counters(body):
        if drift:
            state["AltMstId"] += 1
        return objects_xml("COMPANY", [{"Name": COMPANY_A, "GUID": "g-1", "AltVchId": str(state["AltVchId"]),
                                        "AltMstId": str(state["AltMstId"]), "BooksFrom": "20250401",
                                        "LastVoucherDate": "20260301", "AlterID": "1"}])

    fake.route("S0CompanyCounters", counters)
    fake.route("S0P19Ledgers", lambda body: objects_xml("LEDGER", [{"Name": "Cash", "ClosingBalance": "-900.00"}]))
    fake.route("<ID>Trial Balance</ID>", lambda body: tb_xml([("Capital Account", "", "1000.00")]))
    return fake


def _io(state, voucher_moves=True, view_moves=False):
    def on_action(action):
        if action.kind == "create_voucher" and voucher_moves:
            state["AltVchId"] += 1
        elif action.kind == "delete_voucher":
            state["AltVchId"] += 3
        elif action.kind == "view_report" and view_moves:
            state["AltMstId"] += 1
    return ScriptedIO(on_action=on_action)


async def _run(tmp_path, fake, io):
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p19.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(19)["parts"]["A"]


def _state():
    return {"AltVchId": 50, "AltMstId": 266}


async def test_quiet_captures_stable_and_mid_capture_entry_detected(tmp_path):
    state = _state()
    io = _io(state)
    part = await _run(tmp_path, _fake(state), io)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["quiet_stable"] is True
    assert part["observations"]["mid_capture_entry_detected"] is True
    assert [a.kind for a in io.actions] == ["view_report", "create_voucher", "delete_voucher"]
    assert len(part["fixtures"]) == 3 * 4 + 1 + 4
    assert "p19_A_capture_quiet_3_counters_end.xml" in part["fixtures"]
    assert "p19_A_after_ui_view.xml" in part["fixtures"]
    assert "p19_A_capture_moving_tb.xml" in part["fixtures"]


async def test_viewing_a_report_that_moves_counters_is_different(tmp_path):
    state = _state()
    part = await _run(tmp_path, _fake(state), _io(state, view_moves=True))
    assert part["outcome"] == "DIFFERENT"
    assert "retry policy" in part["spec_impact"]
    assert part["summary"] == "A report view moved the counters"
    assert "XML report export" not in part["spec_impact"]


async def test_mid_capture_entry_not_seen_fails(tmp_path):
    state = _state()
    part = await _run(tmp_path, _fake(state), _io(state, voucher_moves=False))
    assert part["outcome"] == "FAILED"
    assert "can't see" in part["spec_impact"]


async def test_counters_moving_with_no_activity_fails(tmp_path):
    state = _state()
    part = await _run(tmp_path, _fake(state, drift=True), _io(state))
    assert part["outcome"] == "FAILED"
    assert "nobody" in part["summary"]


# --- M3: auto mode doesn't claim the UI report view was exercised --------------------------------------------------


async def test_auto_mode_records_the_ui_view_as_not_exercised(tmp_path):
    state = _state()

    def on_action(action):
        if action.kind == "create_voucher":
            state["AltVchId"] += 1
        elif action.kind == "delete_voucher":
            state["AltVchId"] += 3
        # view_report never moves the counters here — auto mode uses an XML export, not the UI

    io = ScriptedIO(on_action=on_action, run_mode="auto")
    part = await _run(tmp_path, _fake(state), io)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["ui_view"] == "not exercised (auto mode: XML export)"
    assert "wasn't exercised (auto mode used an XML export instead)" in part["summary"]
    assert "a report view doesn't move the counters" not in part["summary"]


async def test_auto_mode_does_not_claim_a_ui_report_view_on_the_different_path(tmp_path):
    """M3/M5: the DIFFERENT branch must use the same auto-mode wording as the CONFIRMED one — auto mode's "view"
    is an XML export, so "a report view moves the counters" would be a claim the run never tested."""
    state = _state()

    def on_action(action):
        if action.kind == "create_voucher":
            state["AltVchId"] += 1
        elif action.kind == "delete_voucher":
            state["AltVchId"] += 3
        elif action.kind == "view_report":
            state["AltMstId"] += 1

    part = await _run(tmp_path, _fake(state), ScriptedIO(on_action=on_action, run_mode="auto"))
    assert part["outcome"] == "DIFFERENT"
    assert part["summary"] == ("An XML report export (auto mode reads no UI, so a UI report view is still untested) "
                               "moved the counters")
    assert part["spec_impact"].startswith("An XML report export (auto mode reads no UI, so a UI report view is still "
                                          "untested) moves the counters")
    assert "A report view moves the counters" not in part["spec_impact"]
    assert "retry policy" in part["spec_impact"]
