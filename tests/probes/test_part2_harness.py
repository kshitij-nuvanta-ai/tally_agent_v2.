"""Plan part 2 harness changes: actions on pause/ask, planned parts, unbuilt parts, run_mode, anchors steps."""
from datetime import datetime
from decimal import Decimal

import pytest

from probes import p00_environment
from probes import p02_active_company_guid as p02
from probes import runner as runner_module
from probes.actions import ASK_KINDS, PAUSE_KINDS, Action
from probes.companies import COMPANIES, SEED_COMPANY
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.registry import ALL_ORDER, ANCHORS_AFTER_A, ANCHORS_BEFORE_PARITY, FIRST_ORDER
from probes.report import render_report
from probes.results import ResultsStore
from probes.runner import run_order, run_probe
from tests.probes.fakes import FakeTally, ScriptedIO, bills_xml, make_harness, mark_done, objects_xml, tb_xml

A = COMPANIES["A"]
RECEIVABLE = bills_xml([("S015", "Apex Technologies Pvt Ltd", "-62800.00"), ("S010", "Rest", "-907737.00")])
PAYABLE = bills_xml([("P001", "Samsung India Electronics", "243100.00"), ("P004", "Rest", "1591042.00")])
TB = tb_xml([("Capital Account", "", "100000.00"), ("Current Assets", "-100000.00", "")])
BASELINE = {"Capital Account": "100000.00", "Current Assets": "-100000.00"}


def _probe(parts, **kw):
    kw.setdefault("id", 99)
    return Probe(name="fake", question="?", feeds=(), parts=parts, **kw)


async def _ok(ctx):
    return PartResult(Outcome.CONFIRMED, "ok")


def test_action_kinds_are_validated():
    assert Action("open_company", {"label": "A"}).params == {"label": "A"}
    assert Action("close_all_companies") == Action("close_all_companies")
    with pytest.raises(ValueError):
        Action("make_coffee")
    assert not PAUSE_KINDS & ASK_KINDS


async def test_pause_and_ask_pass_actions_and_log_their_kind(tmp_path):
    async def part(ctx):
        ctx.pause("Create it", Action("create_voucher", {"ref": "r"}))
        ctx.observe("typed", ctx.ask("Balance?", Action("ui_closing_balance", {"ledger": "Cash"})))
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    io = ScriptedIO(answers_by_kind={"ui_closing_balance": "12.00 Dr"})
    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture, io=io)
    steps = store.probe_entry(99)["parts"]["A"]["manual_steps"]
    assert [a.kind for a in io.actions] == ["create_voucher"]
    assert [a.kind for a in io.ask_actions] == ["ui_closing_balance"]
    assert [s["action"] for s in steps] == ["create_voucher", "ui_closing_balance"]
    assert steps[1]["input"] == "12.00 Dr"


async def test_pause_with_an_ask_kind_is_a_harness_error(tmp_path):
    async def part(ctx):
        ctx.pause("Type it", Action("licence"))
        return PartResult(Outcome.CONFIRMED, "unreachable")

    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    assert "is not a pause action" in store.probe_entry(99)["parts"]["A"]["summary"]


async def test_planned_parts_keep_a_one_part_probe_partial(tmp_path):
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    outcome = await run_probe(_probe({"A": _ok}, planned_parts=("A", "B")), labels=None, client=client, store=store,
                              capture=capture, io=ScriptedIO())
    assert outcome is Outcome.PARTIAL
    assert store.probe_entry(99)["parts"]["A"]["outcome"] == "CONFIRMED"
    assert store.probe_entry(99)["remaining"] == ["B"]


def test_built_parts_must_be_planned():
    with pytest.raises(ValueError):
        _probe({"C": _ok}, planned_parts=("A", "B"))


async def test_run_order_skips_an_unbuilt_part_without_switching(tmp_path, monkeypatch):
    async def part(ctx):
        raise AssertionError("should not run")

    probe = _probe({"A": part}, planned_parts=("A", "B"))
    monkeypatch.setattr(runner_module, "load_probe", lambda pid: probe)
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    io = ScriptedIO()
    await run_order([(99, "B")], client=client, store=store, capture=capture, io=io)
    assert io.waits == []
    assert "--- probe 99 part B: not built yet, skipped" in io.said


async def test_ordered_switch_carries_an_open_company_action(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "load_probe", lambda pid: _probe({"A": _ok}))
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    io = ScriptedIO()
    await run_order([(99, "A")], client=client, store=store, capture=capture, io=io)
    assert io.actions == [Action("open_company", {"label": "A"})]


async def test_failed_switch_stops_the_order(tmp_path, monkeypatch):
    def refuse(action):
        raise ProbeBlocked("Tally won't start")

    monkeypatch.setattr(runner_module, "load_probe", lambda pid: _probe({"A": _ok}))
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    io = ScriptedIO(on_action=refuse)
    await run_order([(99, "A")], client=client, store=store, capture=capture, io=io)
    assert store.probe_entry(99) is None
    assert any(s.startswith("Stopped: couldn't switch Tally to company A") for s in io.said)


async def test_auto_run_mode_is_tagged_on_each_part(tmp_path):
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    await run_probe(_probe({"A": _ok}), labels=None, client=client, store=store, capture=capture,
                    io=ScriptedIO(run_mode="auto"))
    assert store.probe_entry(99)["parts"]["A"]["observations"]["run_mode"] == "auto"
    await run_probe(_probe({"A": _ok}, id=98), labels=None, client=client, store=store, capture=capture,
                    io=ScriptedIO())
    assert "run_mode" not in store.probe_entry(98)["parts"]["A"]["observations"]


def _anchor_fake(receivable=RECEIVABLE):
    fake = FakeTally([A])
    fake.route("<ID>Bills Receivable</ID>", lambda body: receivable)
    fake.route("<ID>Bills Payable</ID>", lambda body: PAYABLE)
    fake.route("<ID>Trial Balance</ID>", lambda body: TB)
    return fake


async def test_anchor_step_passes_records_and_continues(tmp_path, monkeypatch):
    calls = []

    async def part(ctx):
        calls.append(1)
        return PartResult(Outcome.CONFIRMED, "ok")

    monkeypatch.setattr(runner_module, "load_probe", lambda pid: _probe({"A": part}))
    client, store, capture = make_harness(tmp_path, _anchor_fake())
    store.update_environment(company_a_tb_baseline=BASELINE)
    io = ScriptedIO()
    await run_order([(ANCHORS_BEFORE_PARITY, "A"), (99, "A")], client=client, store=store, capture=capture, io=io)
    assert calls == [1]
    check = store.anchor_checks[-1]
    assert check["ok"] is True and check["when"] == "before_parity"
    assert check["receivable"] == Decimal("970537.00")
    assert "--- anchors check (before_parity) company A: OK" in io.said
    assert io.actions == [Action("open_company", {"label": "A"})]          # one switch, before the anchors step
    fixtures = tmp_path / "fixtures"
    assert not fixtures.exists() or not any("anchors" in p.name for p in fixtures.iterdir())


async def test_anchor_failure_stops_the_order(tmp_path, monkeypatch):
    calls = []

    async def part(ctx):
        calls.append(1)
        return PartResult(Outcome.CONFIRMED, "ok")

    monkeypatch.setattr(runner_module, "load_probe", lambda pid: _probe({"A": part}))
    client, store, capture = make_harness(tmp_path, _anchor_fake(bills_xml([("S015", "Apex", "-1.00")])))
    store.update_environment(company_a_tb_baseline=BASELINE)
    io = ScriptedIO()
    await run_order([(ANCHORS_AFTER_A, "A"), (99, "A")], client=client, store=store, capture=capture, io=io)
    assert calls == []
    assert store.anchor_checks[-1]["ok"] is False
    assert "Bills Receivable" in store.anchor_checks[-1]["problems"][0]
    assert any(s.startswith("Stopped: company A failed the anchors check.") for s in io.said)


async def test_anchor_step_without_baseline_stops(tmp_path):
    client, store, capture = make_harness(tmp_path, _anchor_fake())
    io = ScriptedIO()
    await run_order([(ANCHORS_BEFORE_PARITY, "A")], client=client, store=store, capture=capture, io=io)
    assert store.anchor_checks[-1]["problems"] == ["No TB baseline yet: run probe 0 first."]


async def test_anchor_step_with_wrong_company_open_stops(tmp_path):
    fake = _anchor_fake()
    fake.companies = ["Other Co"]
    client, store, capture = make_harness(tmp_path, fake)
    store.update_environment(company_a_tb_baseline=BASELINE)
    await run_order([(ANCHORS_BEFORE_PARITY, "A")], client=client, store=store, capture=capture, io=ScriptedIO())
    assert store.anchor_checks[-1]["problems"][0].startswith("GuardError:")


def test_orders_check_anchors_before_parity_and_after_the_a_batch():
    assert ALL_ORDER.index((ANCHORS_BEFORE_PARITY, "A")) == ALL_ORDER.index((16, "A")) - 1
    assert ALL_ORDER.index((ANCHORS_AFTER_A, "A")) == ALL_ORDER.index((13, "A")) + 1
    assert FIRST_ORDER.index((ANCHORS_BEFORE_PARITY, "A")) == FIRST_ORDER.index((16, "A")) - 1
    assert FIRST_ORDER.index((ANCHORS_AFTER_A, "A")) == FIRST_ORDER.index((18, "A")) + 1


def test_report_shows_run_mode_and_anchor_checks(tmp_path):
    store = ResultsStore(tmp_path / "r.json")
    store.update_environment(run_mode="auto (S0-D9)")
    store.record_anchor_check(when="before_parity", label="A", ok=False, problems=["Bills Receivable 1 ≠ 970537.00"],
                              receivable=Decimal("1"), payable=None, ran_at=datetime(2026, 9, 23, 10, 0))
    text = render_report(store, "2026-09-23")
    assert "| run_mode | auto (S0-D9) |" in text
    assert "| 2026-09-23T10:00:00 | before_parity | FAILED | Bills Receivable 1 ≠ 970537.00 |" in text
    assert "No anchors check has run yet." in render_report(ResultsStore(tmp_path / "e.json"), "2026-09-23")


async def test_p00_pauses_and_asks_carry_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(p00_environment, "wine_version", lambda: "wine-9.0")
    fake = FakeTally([])
    fake.route("S0CompanyGuid", lambda body: objects_xml(
        "COMPANY", [{"Name": fake.companies[0], "GUID": "g"}] if fake.companies else []))
    fake.route("<ID>Bills Receivable</ID>", lambda body: RECEIVABLE)
    fake.route("<ID>Bills Payable</ID>", lambda body: PAYABLE)
    fake.route("<ID>Trial Balance</ID>", lambda body: TB)

    def on_action(action):
        if action.kind == "restore_seed":
            fake.companies = [SEED_COMPANY]
        elif action.kind == "rename_company":
            fake.companies = [action.params["to"]]

    io = ScriptedIO(on_action=on_action, answers_by_kind={
        "data_folder": r"C:\s0probe\100003", "tally_version": "TallyPrime 7.0", "edition": "Edit Log",
        "licence": "educational"})
    client, store, capture = make_harness(tmp_path, fake)
    outcome = await run_probe(p00_environment.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    assert outcome is Outcome.CONFIRMED, store.probe_entry(0)["parts"]["A"]["summary"]
    assert [a.kind for a in io.actions] == ["tally_running", "restore_seed", "rename_company"]
    assert io.actions[2].params == {"from": SEED_COMPANY, "to": A}
    assert [a.kind for a in io.ask_actions] == ["data_folder", "tally_version", "edition", "licence"]


async def test_p02_pauses_carry_actions(tmp_path):
    fake = FakeTally([A])
    fake.route("S0ActiveCompany", lambda body: objects_xml(
        "COMPANY", [{"Name": A, "GUID": "g-1"}] if fake.companies else []))

    def on_action(action):
        fake.companies = [] if action.kind == "close_all_companies" else [A]

    io = ScriptedIO(on_action=on_action)
    client, store, capture = make_harness(tmp_path, fake)
    mark_done(store, 0)
    outcome = await run_probe(p02.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    assert outcome is Outcome.CONFIRMED
    assert io.actions == [Action("close_all_companies"), Action("open_company", {"label": "A"})]
