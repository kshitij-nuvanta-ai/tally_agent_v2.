import json
from datetime import datetime

import httpx
import pytest

from probes import runner as runner_module
from probes.companies import COMPANIES
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.registry import ANCHORS_BEFORE_PARITY
from probes.runner import run_order, run_probe
from tests.probes.fakes import FakeTally, ScriptedIO, make_harness, mark_done, objects_xml

A, B = COMPANIES["A"], COMPANIES["B"]


def _probe(parts, **kw):
    kw.setdefault("id", 99)
    return Probe(name="fake", question="?", feeds=(), parts=parts, **kw)


async def _run(tmp_path, fake, probe, io=None, labels=None):
    client, store, capture = make_harness(tmp_path, fake)
    io = io or ScriptedIO()
    outcome = await run_probe(probe, labels=labels, client=client, store=store, capture=capture, io=io)
    return outcome, store, io


async def test_confirmed_part_records_fixture_and_observations(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Thing", lambda body: "<ENVELOPE><X>1</X></ENVELOPE>")

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        ctx.observe("seen", 1)
        return PartResult(Outcome.CONFIRMED, "ok")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    assert outcome is Outcome.CONFIRMED
    entry = store.probe_entry(99)["parts"]["A"]
    assert entry["fixtures"] == ["p99_A_thing.xml"]
    assert entry["observations"] == {"seen": 1}
    assert (tmp_path / "fixtures" / "p99_A_thing.xml").read_text() == "<ENVELOPE><X>1</X></ENVELOPE>"


async def test_wrong_company_blocks_before_any_probe_request(tmp_path):
    fake = FakeTally(["Some Other Co"])

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE/>")
        return PartResult(Outcome.CONFIRMED, "ok")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    assert outcome is Outcome.BLOCKED
    assert "expects" in store.probe_entry(99)["parts"]["A"]["summary"]
    assert fake.probe_requests() == []


async def test_two_companies_loaded_blocks(tmp_path):
    outcome, store, _ = await _run(tmp_path, FakeTally([A, B]), _probe({"A": lambda ctx: None}))
    assert outcome is Outcome.BLOCKED
    assert "2 companies are loaded" in store.probe_entry(99)["parts"]["A"]["summary"]


async def test_timeout_blocks_with_popup_hint_and_error_sidecar(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Slow", lambda body: httpx.ReadTimeout)

    async def part(ctx):
        await ctx.send("slow", "<ENVELOPE><ID>S0Slow</ID></ENVELOPE>")
        return PartResult(Outcome.CONFIRMED, "unreachable")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    entry = store.probe_entry(99)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert "popup" in entry["summary"]
    assert entry["fixtures"] == ["p99_A_slow.xml.json"]
    assert len([r for r in fake.probe_requests() if "S0Slow" in r]) == 1  # no retry


async def test_tally_down_blocks(tmp_path):
    fake = FakeTally([A])
    fake.down = True
    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": lambda ctx: None}))
    assert outcome is Outcome.BLOCKED
    assert "not reachable" in store.probe_entry(99)["parts"]["A"]["summary"]


async def test_try_send_returns_error_instead_of_blocking(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Slow", lambda body: httpx.ReadTimeout)

    async def part(ctx):
        text, error = await ctx.try_send("slow", "<ENVELOPE><ID>S0Slow</ID></ENVELOPE>")
        assert text is None and error["kind"] == "timeout"
        return PartResult(Outcome.CONFIRMED, "handled")

    outcome, _, _ = await _run(tmp_path, fake, _probe({"A": part}))
    assert outcome is Outcome.CONFIRMED


async def test_forbidden_request_refused_and_not_sent(tmp_path):
    fake = FakeTally([A])

    async def part(ctx):
        await ctx.send("bad", "<ENVELOPE><NATIVEMETHOD>*</NATIVEMETHOD></ENVELOPE>")
        return PartResult(Outcome.CONFIRMED, "unreachable")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    assert outcome is Outcome.BLOCKED
    assert "Request refused" in store.probe_entry(99)["parts"]["A"]["summary"]
    assert fake.probe_requests() == []


async def test_pause_non_interactive_blocks(tmp_path):
    async def part(ctx):
        ctx.pause("Delete the voucher")
        return PartResult(Outcome.CONFIRMED, "unreachable")

    outcome, store, _ = await _run(tmp_path, FakeTally([A]), _probe({"A": part}), io=ScriptedIO(interactive=False))
    assert outcome is Outcome.BLOCKED
    assert "manual step" in store.probe_entry(99)["parts"]["A"]["summary"]


async def test_pause_and_ask_are_logged(tmp_path):
    async def part(ctx):
        ctx.pause("Delete the voucher")
        ctx.observe("answer", ctx.ask("Type the balance"))
        return PartResult(Outcome.CONFIRMED, "ok")

    io = ScriptedIO(answers=["1234.00"])
    outcome, store, _ = await _run(tmp_path, FakeTally([A]), _probe({"A": part}), io=io)
    steps = store.probe_entry(99)["parts"]["A"]["manual_steps"]
    assert outcome is Outcome.CONFIRMED
    assert [s["instruction"] for s in steps] == ["Delete the voucher", "Type the balance"]
    assert steps[1]["input"] == "1234.00"


async def test_missing_prerequisite_blocks_without_requests(tmp_path):
    fake = FakeTally([A])
    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": lambda ctx: None}, requires=(1,)))
    assert outcome is Outcome.BLOCKED
    assert "Run probe(s) 1 first" in store.probe_entry(99)["parts"]["A"]["summary"]
    assert fake.requests == []


async def test_counters_before_probe_1_confirmed_blocks(tmp_path):
    async def part(ctx):
        await ctx.counters("counters")
        return PartResult(Outcome.CONFIRMED, "unreachable")

    outcome, store, _ = await _run(tmp_path, FakeTally([A]), _probe({"A": part}))
    assert outcome is Outcome.BLOCKED
    assert "probe 1" in store.probe_entry(99)["parts"]["A"]["summary"]


async def test_counters_use_confirmed_template_and_fill_guid(tmp_path):
    fake = FakeTally([A])
    fake.route("S0CompanyCounters", lambda body: objects_xml("COMPANY", [{"Name": A, "GUID": "g-1", "AltVchId": "7"}]))
    client, store, capture = make_harness(tmp_path, fake)
    store.confirm_request("company_counters", 1, "<ENVELOPE><ID>S0CompanyCounters</ID></ENVELOPE>",
                          fields=["GUID", "AltVchId"])
    seen = {}

    async def part(ctx):
        seen["guid"] = ctx.company_guid
        seen["counters"] = await ctx.counters("counters_now")
        return PartResult(Outcome.CONFIRMED, "ok")

    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    assert seen == {"guid": "g-1", "counters": {"GUID": "g-1", "AltVchId": "7"}}


async def test_two_parts_switch_company_and_combine(tmp_path):
    fake = FakeTally([A])

    def on_wait(instruction):
        if "company B" in instruction:
            fake.companies = [B]

    async def ok(ctx):
        return PartResult(Outcome.CONFIRMED, "ok")

    async def diff(ctx):
        return PartResult(Outcome.DIFFERENT, "diff", spec_impact="change")

    client, store, capture = make_harness(tmp_path, fake)
    probe = _probe({"A": ok, "B": diff})
    first = await run_probe(probe, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    assert first is Outcome.PARTIAL
    both = await run_probe(probe, labels=None, client=client, store=store, capture=capture,
                           io=ScriptedIO(on_wait=on_wait))
    assert both is Outcome.DIFFERENT
    assert [h["part"] for h in store.probe_entry(99)["history"]] == ["A"]


# --- F1: record instead of crashing; cleanup notes -------------------------------------------------


async def test_generic_exception_is_recorded_as_harness_error(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Thing", lambda body: "<ENVELOPE><X>1</X></ENVELOPE>")

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        raise ValueError("boom")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    part_entry = store.probe_entry(99)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert part_entry["summary"] == "Harness error: ValueError: boom"
    assert part_entry["fixtures"] == ["p99_A_thing.xml"]


async def test_keyboard_interrupt_records_blocked_and_propagates(tmp_path):
    async def part(ctx):
        ctx.observe("seen", 1)
        raise KeyboardInterrupt

    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    with pytest.raises(KeyboardInterrupt):
        await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture,
                        io=ScriptedIO())
    part_entry = store.probe_entry(99)["parts"]["A"]
    assert part_entry["outcome"] == "BLOCKED"
    assert part_entry["summary"] == "Interrupted by operator (Ctrl-C)"
    assert part_entry["observations"]["seen"] == 1


async def test_abort_notes_print_cleanup_and_resolved_notes_are_hidden(tmp_path):
    async def part(ctx):
        ctx.on_abort("X")
        ctx.on_abort("Y")
        ctx.resolve_abort("Y")
        raise ProbeBlocked("boom")

    outcome, store, io = await _run(tmp_path, FakeTally([A]), _probe({"A": part}))
    part_entry = store.probe_entry(99)["parts"]["A"]
    assert outcome is Outcome.BLOCKED
    assert "CLEANUP NEEDED in Tally:" in io.said
    assert "  - X" in io.said
    assert "  - Y" not in io.said
    assert part_entry["observations"]["cleanup_needed"] == ["X"]


async def test_confirmed_part_has_no_cleanup_notes_even_with_abort_notes_pending(tmp_path):
    async def part(ctx):
        ctx.on_abort("X")  # forgotten to resolve, but the part still confirmed
        return PartResult(Outcome.CONFIRMED, "ok")

    outcome, store, io = await _run(tmp_path, FakeTally([A]), _probe({"A": part}))
    assert outcome is Outcome.CONFIRMED
    assert "CLEANUP NEEDED in Tally:" not in io.said
    assert "cleanup_needed" not in store.probe_entry(99)["parts"]["A"]["observations"]


# --- F2: ordered runs stop, skip done parts, respect --rerun ---------------------------------------


async def test_run_order_stops_after_a_blocked_part(tmp_path, monkeypatch):
    async def blocked_part(ctx):
        return PartResult(Outcome.BLOCKED, "nope")

    async def never_called(ctx):
        raise AssertionError("should not run")

    probes = {10: _probe({"A": blocked_part}, id=10), 11: _probe({"A": never_called}, id=11)}
    monkeypatch.setattr(runner_module, "load_probe", lambda pid: probes.get(pid))
    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    io = ScriptedIO()

    await run_order([(10, "A"), (11, "A")], client=client, store=store, capture=capture, io=io)

    assert store.probe_entry(10)["outcome"] == "BLOCKED"
    assert store.probe_entry(11) is None
    assert any("Stopped: probe 10 part A is BLOCKED. Fix that, then re-run." in s for s in io.said)


async def test_run_order_skips_a_done_part_unless_rerun(tmp_path, monkeypatch):
    fake = FakeTally([A])
    calls = {"n": 0}

    async def part(ctx):
        calls["n"] += 1
        return PartResult(Outcome.CONFIRMED, "ok")

    probe = _probe({"A": part}, id=20)
    monkeypatch.setattr(runner_module, "load_probe", lambda pid: probe if pid == 20 else None)
    client, store, capture = make_harness(tmp_path, fake)
    mark_done(store, 20, part="A")
    io = ScriptedIO()

    await run_order([(20, "A")], client=client, store=store, capture=capture, io=io)
    assert calls["n"] == 0
    assert any("already CONFIRMED, skipped (use --rerun)" in s for s in io.said)

    await run_order([(20, "A")], client=client, store=store, capture=capture, io=io, rerun=True)
    assert calls["n"] == 1


async def test_run_order_skips_a_stored_failed_part_and_continues_to_the_next_step(tmp_path, monkeypatch):
    """I1: a FAILED part is skipped too (not just CONFIRMED/DIFFERENT), and the order still moves on — even into an
    anchors step — rather than stopping."""
    fake = FakeTally([A])
    calls = {"n": 0}

    async def part(ctx):
        calls["n"] += 1
        return PartResult(Outcome.CONFIRMED, "ok")

    probe = _probe({"A": part}, id=21)
    monkeypatch.setattr(runner_module, "load_probe", lambda pid: probe if pid == 21 else None)
    client, store, capture = make_harness(tmp_path, fake)
    store.record_part(21, "A", PartResult(Outcome.FAILED, "boom", spec_impact="x"), all_parts=["A"], fixtures=[],
                      manual_steps=[], ran_at=datetime.now().astimezone())
    io = ScriptedIO()

    await run_order([(21, "A"), (ANCHORS_BEFORE_PARITY, "A")], client=client, store=store, capture=capture, io=io)
    assert calls["n"] == 0
    assert any("already FAILED, skipped (use --rerun)" in s for s in io.said)
    assert any(s.startswith("--- anchors check") for s in io.said)   # the run continued past the skipped part

    await run_order([(21, "A")], client=client, store=store, capture=capture, io=io, rerun=True)
    assert calls["n"] == 1


# --- F3: the raw response is exposed on the context -------------------------------------------------


async def test_last_response_set_on_success_and_none_on_timeout(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Thing", lambda body: "<ENVELOPE><X>1</X></ENVELOPE>")
    fake.route("S0Slow", lambda body: httpx.ReadTimeout)
    seen = {}

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        seen["after_send"] = ctx.last_response
        await ctx.try_send("slow", "<ENVELOPE><ID>S0Slow</ID></ENVELOPE>")
        seen["after_timeout"] = ctx.last_response
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, fake)
    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    assert seen["after_send"].raw == b"<ENVELOPE><X>1</X></ENVELOPE>"
    assert seen["after_timeout"] is None


# --- M4: sidecars carry only the spec §5.6 environment keys -----------------------------------------


async def test_sidecar_environment_only_has_the_spec_keys(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Thing", lambda body: "<ENVELOPE><X>1</X></ENVELOPE>")

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, fake)
    store.update_environment(wine="wine-9.0", tally_version="7.0", edition="Edit Log", licence="licensed",
                             company_a_tb_baseline={"x": "1"})
    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    sidecar = json.loads((tmp_path / "fixtures" / "p99_A_thing.xml.json").read_text(encoding="utf-8"))
    assert sidecar["environment"] == {"wine": "wine-9.0", "tally_version": "7.0", "edition": "Edit Log",
                                      "licence": "licensed"}


# --- M6: duplicate step names within one part are refused before sending ---------------------------


async def test_duplicate_step_name_is_a_harness_error(tmp_path):
    fake = FakeTally([A])
    fake.route("S0Thing", lambda body: "<ENVELOPE><X>1</X></ENVELOPE>")

    async def part(ctx):
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        await ctx.send("thing", "<ENVELOPE><ID>S0Thing</ID></ENVELOPE>")
        return PartResult(Outcome.CONFIRMED, "ok")

    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}))
    assert outcome is Outcome.BLOCKED
    summary = store.probe_entry(99)["parts"]["A"]["summary"]
    assert summary == "Harness error: ValueError: Duplicate step name in this part: 'thing'"


# --- F6: Educational tagging when the recorded licence is "educational" ----------------------------


async def test_educational_sensitive_probe_tagged_when_licence_educational(tmp_path):
    async def part(ctx):
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    store.update_environment(licence="educational")
    probe = _probe({"A": part}, educational_sensitive=True)
    await run_probe(probe, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    part_entry = store.probe_entry(99)["parts"]["A"]
    assert part_entry["observations"]["tags"] == ["Educational"]
    assert part_entry["summary"] == "ok [Educational mode — confirm on a licensed Tally]"


async def test_educational_sensitive_probe_not_tagged_when_licence_licensed(tmp_path):
    async def part(ctx):
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, FakeTally([A]))
    store.update_environment(licence="licensed")
    probe = _probe({"A": part}, educational_sensitive=True)
    await run_probe(probe, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    part_entry = store.probe_entry(99)["parts"]["A"]
    assert "tags" not in part_entry["observations"]
    assert part_entry["summary"] == "ok"


# --- M7: the mutation guard runs before any request, including the guard's own company list --------


async def test_mutation_guard_blocks_before_any_request(tmp_path, monkeypatch):
    from probes import companies as companies_module
    monkeypatch.setitem(companies_module.COMPANIES, "A", "Plain Co")

    async def part(ctx):
        raise AssertionError("should not run")

    fake = FakeTally(["Plain Co"])
    outcome, store, _ = await _run(tmp_path, fake, _probe({"A": part}, mutating=True))
    assert outcome is Outcome.BLOCKED
    assert "only companies with 'Probe'" in store.probe_entry(99)["parts"]["A"]["summary"]
    assert fake.requests == []


# --- --allow-risky: off unless the operator asked for it (probe 16's SVFROMDATE ledger read) -------------------------


@pytest.mark.parametrize("allow_risky, expected", [(None, False), (False, False), (True, True)])
async def test_allow_risky_reaches_the_probe_context_and_defaults_off(tmp_path, allow_risky, expected):
    fake = FakeTally([A])
    seen = {}

    async def part(ctx):
        seen["allow_risky"] = ctx.allow_risky
        return PartResult(Outcome.CONFIRMED, "ok")

    client, store, capture = make_harness(tmp_path, fake)
    kwargs = {} if allow_risky is None else {"allow_risky": allow_risky}
    await run_probe(_probe({"A": part}), labels=None, client=client, store=store, capture=capture,
                    io=ScriptedIO(), **kwargs)
    assert seen["allow_risky"] is expected
