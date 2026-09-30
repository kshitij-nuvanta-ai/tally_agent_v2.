import os
import re
import signal

import pytest

from probes import p00_environment
from probes.__main__ import build_parser, main
from probes.actions import Action
from probes.companies import COMPANIES, SEED_COMPANY
from probes.core import Outcome, PartResult
from probes.core import Probe
from probes.operator.auto import AUTO_RUN_MODE, build_auto_operator
from probes.operator.tally_control import TallyProcess
from probes.registry import PROBES, load_probe
from probes.report import render_report
from probes.results import ResultsStore
from tests.probes.fake_books import (OWN_COMMAND, USD_CURRENCY_ROW, FakeBooks, FakeRunner, tmp_config,
                                        write_company_folder)
from tests.probes.fakes import FakeTally, ScriptedIO


def test_list_shows_every_probe_and_deferred(tmp_path, capsys):
    assert main(["--results", str(tmp_path / "r.json"), "list"]) == 0
    out = capsys.readouterr().out
    for item in PROBES:
        assert f"{item.id:>2}  {item.name}" in out
    assert out.count("⏭ deferred") == 2  # probes 9 and 20


def test_run_unbuilt_probe_returns_2(tmp_path, capsys):
    # Plan part 7 (pre-flight F1): probe 22 is built now; the deferred probe 9 (Q29) is the one left without a module.
    assert main(["--results", str(tmp_path / "r.json"), "run", "9"]) == 2
    assert "not built yet" in capsys.readouterr().out


def test_run_unknown_probe_id_returns_2(tmp_path, capsys):
    assert main(["--results", str(tmp_path / "r.json"), "run", "99"]) == 2
    assert "No probe 99 (probes are 0–25)." in capsys.readouterr().out


def test_run_unknown_part_returns_2(tmp_path, capsys):
    assert main(["--results", str(tmp_path / "r.json"), "run", "0", "--company", "B"]) == 2
    assert "Probe 0 has no part B (has A)." in capsys.readouterr().out


def test_company_with_first_is_a_parser_error(tmp_path):
    with pytest.raises(SystemExit):
        main(["--results", str(tmp_path / "r.json"), "run", "--first", "--company", "A"])


def test_first_order_stops_after_probe_0_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(p00_environment, "wine_version", lambda: "wine-9.0")
    fake = FakeTally([])
    results_path = tmp_path / "r.json"

    rc = main(["--results", str(results_path), "--fixtures", str(tmp_path / "fixtures"), "run", "--first"],
             transport=fake.transport(), io=ScriptedIO())

    assert rc == 0
    store = ResultsStore(results_path)
    assert store.probe_entry(0)["outcome"] == "BLOCKED"
    assert store.probe_entry(2) is None
    assert store.probe_entry(1) is None


def test_registry_matches_built_modules():
    ids = [item.id for item in PROBES]
    assert ids == list(range(26))
    for item in PROBES:
        probe = load_probe(item.id)
        if item.module is None:
            assert probe is None
            continue
        assert probe.id == item.id
        assert probe.name == item.name
        assert sorted(probe.part_labels) == sorted(item.companies.split("+"))
        assert set(probe.parts) <= set(probe.part_labels)


def test_report_rows_and_deferred(tmp_path):
    store = ResultsStore(tmp_path / "r.json")
    store.update_environment(wine="wine-9.0", tally_version="7.0", edition="Edit Log", licence="licensed")
    store.record_part(0, "A", PartResult(Outcome.CONFIRMED, "Tally answers | ok"), all_parts=["A"],
                      fixtures=[], manual_steps=[], ran_at=__import__("datetime").datetime(2026, 9, 23))
    store.record_part(1, "A", PartResult(Outcome.DIFFERENT, "AltVchId did not move", spec_impact="add fallback"),
                      all_parts=["A"], fixtures=[], manual_steps=[], ran_at=__import__("datetime").datetime(2026, 9, 23))
    text = render_report(store, "2026-09-23")
    assert "| wine | wine-9.0 |" in text
    assert "| 0 | environment | A | CONFIRMED | A: Tally answers \\| ok | — |" in text
    assert "| 1 | company_counters | A | DIFFERENT | A: AltVchId did not move | A: add fallback |" in text
    assert "| 3 | voucher_ids_flags | A+B | not run | — | — |" in text
    assert "- Probe 9 — chunk_latency" in text and "- Probe 20 — parity_cost" in text
    assert render_report(store, "2026-09-23") == text  # deterministic


def test_partial_probe_shows_remaining_parts(tmp_path, capsys):
    store = ResultsStore(tmp_path / "r.json")
    store.record_part(3, "A", PartResult(Outcome.CONFIRMED, "ok"), all_parts=["A", "B"], fixtures=[],
                      manual_steps=[], ran_at=__import__("datetime").datetime(2026, 9, 23))
    assert main(["--results", str(tmp_path / "r.json"), "list"]) == 0
    out = capsys.readouterr().out
    assert "PARTIAL (remaining: B)" in out
    text = render_report(store, "2026-09-23")
    assert "| 3 | voucher_ids_flags | A+B | PARTIAL (remaining: B) |" in text


def test_report_command_writes_file(tmp_path):
    out = tmp_path / "report.md"
    assert main(["--results", str(tmp_path / "r.json"), "report", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# S0 probe results")


def test_reset_a_resets_company_a_and_records_the_time(tmp_path, capsys):
    config = tmp_config(tmp_path)
    write_company_folder(config.seed_folder("A"), SEED_COMPANY)
    books = FakeBooks(config.company_folder("A"), running=False, loaded=False)
    op = build_auto_operator(config=config, transport=books.transport(), runner=FakeRunner(books, []),
                             echo=lambda line: None)
    assert main(["--results", str(tmp_path / "r.json"), "reset-a"], operator=op) == 0
    assert books.companies() == [COMPANIES["A"]]
    assert "company_a_reset_at" in ResultsStore(tmp_path / "r.json").environment
    assert "Company A reset" in capsys.readouterr().out


def test_reset_a_failure_returns_1(tmp_path, capsys):
    config = tmp_config(tmp_path)                       # no seed folder: the copy can't happen
    books = FakeBooks(config.company_folder("A"), running=False, loaded=False)
    op = build_auto_operator(config=config, transport=books.transport(), runner=FakeRunner(books, []),
                             echo=lambda line: None)
    assert main(["--results", str(tmp_path / "r.json"), "reset-a"], operator=op) == 1
    assert "reset-a failed" in capsys.readouterr().out


def test_run_auto_records_run_mode_and_tags_parts(tmp_path, monkeypatch):
    from probes import __main__ as cli

    async def part(ctx):
        ctx.pause("Create it", Action("create_voucher", {"company": ctx.company_name, "ref": "c1", "ledger": "Electricity",
                                                         "amount": "1.00", "narration": "S0-throwaway cli"}))
        ctx.pause("Delete it", Action("delete_voucher", {"company": ctx.company_name, "ref": "c1"}))
        return PartResult(Outcome.CONFIRMED, "ok")

    probe = Probe(id=5, name="voucher_month_bounds", question="?", feeds=(), parts={"A": part})
    monkeypatch.setattr(cli, "load_probe", lambda pid: probe)
    books = FakeBooks(name=COMPANIES["A"])
    op = build_auto_operator(config=tmp_config(tmp_path), transport=books.transport(),
                             runner=FakeRunner(books, [TallyProcess(8, OWN_COMMAND)]), echo=lambda line: None)
    results = tmp_path / "r.json"
    rc = main(["--results", str(results), "--fixtures", str(tmp_path / "f"), "run", "5", "--auto"],
              transport=books.transport(), operator=op)
    assert rc == 0
    store = ResultsStore(results)
    assert store.environment["run_mode"] == AUTO_RUN_MODE
    assert store.probe_entry(5)["parts"]["A"]["observations"]["run_mode"] == "auto"
    assert books.state["vouchers"] == {}


def test_auto_with_non_interactive_is_a_parser_error(tmp_path):
    with pytest.raises(SystemExit):
        main(["--results", str(tmp_path / "r.json"), "--non-interactive", "run", "1", "--auto"])


def test_ctrl_c_during_a_blocking_wait_is_recorded_and_propagates(tmp_path, monkeypatch):
    """I2: signal.default_int_handler is installed for the duration of `run`, so an operator's Ctrl-C during a
    blocking pause surfaces as KeyboardInterrupt in run_probe, and the previous handler is restored afterwards."""
    from probes import __main__ as cli

    async def part(ctx):
        ctx.pause("Blocking step", Action("close_all_companies"))
        return PartResult(Outcome.CONFIRMED, "unreachable")

    probe = Probe(id=5, name="voucher_month_bounds", question="?", feeds=(), parts={"A": part})
    monkeypatch.setattr(cli, "load_probe", lambda pid: probe)

    def on_wait(instruction):
        os.kill(os.getpid(), signal.SIGINT)

    io = ScriptedIO(on_wait=on_wait)
    previous_handler = signal.getsignal(signal.SIGINT)
    fake = FakeTally([COMPANIES["A"]])
    try:
        with pytest.raises(KeyboardInterrupt):
            main(["--results", str(tmp_path / "r.json"), "run", "5"], transport=fake.transport(), io=io)
    finally:
        assert signal.getsignal(signal.SIGINT) is previous_handler   # the previous handler wasn't leaked
    store = ResultsStore(tmp_path / "r.json")
    part_entry = store.probe_entry(5)["parts"]["A"]
    assert part_entry["outcome"] == "BLOCKED"
    assert "Interrupted by operator" in part_entry["summary"]


def _console_input_honouring_flag_pauses(books: FakeBooks):
    """Mirrors test_company_b.py's `_operator_who_honours_flag_pauses`, but as an `AutoOperator` `console_input`
    callback rather than a `ScriptedIO` `on_wait` — our pauses carry no `Action` (the loader's F2/flag-pause
    steps are UI-only, module docstring), so `AutoOperator.wait` falls through to `console_input`. Without this,
    the fake never simulates the Tally UI, so the two cancelled-tag vouchers' flag re-read fails forever by
    construction (an artefact of the fake, not the loader) — see company_b.py's module docstring."""
    flag_pause_re = re.compile(r"\[S0-B:(\d+)\].*?(ISCANCELLED|ISOPTIONAL) did not stick")

    def console_input(prompt: str) -> str:
        if "Opening bill 'Op/2022-001'" in prompt:
            # C35: receipts settle Op/2022-001, and the fake refuses an Agst Ref to a bill nobody opened — the
            # operator enters it in the UI at this pause, as live.
            books.edit_state(lambda s: s.setdefault("bills", {}).update(
                {"Op/2022-001": {"party": "Pune Digital Solutions", "amount": "-62500.00"}}))
        match = flag_pause_re.search(prompt)
        if match:
            tag, xml_tag = match.group(1), match.group(2)
            field = "cancelled" if xml_tag == "ISCANCELLED" else "optional"

            def fix(s, tag=tag, field=field):
                for v in s["vouchers"].values():
                    if v["narration"].startswith(f"[S0-B:{tag}]"):
                        v[field] = "Yes"

            books.edit_state(fix)
        return ""

    return console_input


def test_setup_b_loads_company_b_and_reports(tmp_path, capsys):
    config = tmp_config(tmp_path)
    write_company_folder(config.company_folder("B"), COMPANIES["B"])
    books = FakeBooks(config.company_folder("B"), name=COMPANIES["B"], educational=False)   # the load is licensed
    # NOTE (deviation from the task-8 brief's verbatim test): `books.state` is a read-only snapshot (a copy, per
    # fake_books.py's own docstring — "a snapshot that silently discards books.state[...] = ... is a trap"), so
    # `books.state["voucherTypes"] = [...]` as the brief wrote it is a no-op against the live fake. Using
    # `edit_state` instead is what actually mutates it. Also added `console_input` — the loader's F2 and flag
    # pauses (S0-B spec §4.3) are action-less, and `AutoOperator`'s default `console_input=input` reads real
    # stdin, which pytest's capture refuses (OSError) unless `-s` is passed; a scripted console answers each
    # pause instead, and — for the two ISCANCELLED pauses — actually flips the flag in the fake so the loader's
    # own re-read (I1) sees it, the same way `test_company_b.py` simulates the operator honouring the UI step.
    books.edit_state(lambda s: s.__setitem__(
        "voucherTypes", ["Sales", "Purchase", "Receipt", "Payment", "Sales - GST"]))
    # plan part 7: live B has the `$` Currency master, made in the UI (setup-b never creates it over XML).
    books.edit_state(lambda s: s["currencies"].__setitem__("$", dict(USD_CURRENCY_ROW)))
    op = build_auto_operator(config=config, transport=books.transport(), runner=FakeRunner(books, []),
                             echo=lambda line: None, console_input=_console_input_honouring_flag_pauses(books))
    assert main(["--results", str(tmp_path / "r.json"), "setup-b"], operator=op) == 0
    out = capsys.readouterr().out
    assert "Company B loaded" in out and "vouchers: created 960" in out          # plan part 7: 101/102 written with forex (was C36-skipped)
    assert "currencies: created 0, skipped 1" in out                            # `$` made in the UI, never over XML
    # F19: a note (the observed Trial Balance Dr/Cr total) is informational — it appears, separated under its
    # own "Notes:" heading, and does NOT flip the exit code (asserted together with `== 0` above, same run).
    assert "Notes:" in out and "Trial Balance Dr/Cr total observed" in out
    # M9: the created/skipped block sits under its own heading, like "Pauses:" / "Notes:" / "Problems:".
    lines = out.splitlines()
    heading = lines.index("Created / skipped:")
    assert lines[heading + 1].startswith("  ") and ": created " in lines[heading + 1]


def test_setup_b_without_the_usd_currency_names_the_ui_step_and_writes_nothing(tmp_path, capsys):
    """Plan part 7: the `$` Currency master can't be created over XML on this Tally (live 2026-09-25, run1), so
    setup-b refuses before any write and says how to make it in the UI."""
    books = FakeBooks(name=COMPANIES["B"])
    books.edit_state(lambda s: s.__setitem__(
        "voucherTypes", ["Sales", "Purchase", "Receipt", "Payment", "Sales - GST"]))
    op = build_auto_operator(config=tmp_config(tmp_path), transport=books.transport(),
                             runner=FakeRunner(books, []), echo=lambda line: None,
                             console_input=lambda prompt: "")
    assert main(["--results", str(tmp_path / "r.json"), "setup-b"], operator=op) == 1
    out = capsys.readouterr().out
    assert "setup-b failed" in out and "Create → Currency" in out and "Formal name USD" in out
    assert not any("<TALLYREQUEST>Import Data</TALLYREQUEST>" in r for r in books.requests)


def test_setup_b_returns_one_when_a_prerequisite_blocks_the_loader_before_it_can_verify(tmp_path, capsys):
    """The CompanyBLoadError -> OperatorError -> `except` path (stage 1 preflight never reaches `_verify`, so
    `report` isn't even constructed) — worth covering in its own right, but it must not be mistaken for
    coverage of the `report.problems` exit-code branch: see
    `test_setup_b_returns_one_when_verification_finds_a_problem` for that."""
    books = FakeBooks(name=COMPANIES["B"])
    books.edit_state(lambda s: s.__setitem__("voucherTypes", ["Sales"]))   # no "Sales - GST" — see note above
    op = build_auto_operator(config=tmp_config(tmp_path), transport=books.transport(),
                             runner=FakeRunner(books, []), echo=lambda line: None,
                             console_input=lambda prompt: "")
    assert main(["--results", str(tmp_path / "r.json"), "setup-b"], operator=op) == 1
    assert "setup-b failed" in capsys.readouterr().out


def test_setup_b_returns_one_when_verification_finds_a_problem(tmp_path, capsys):
    """F18: drives the run all the way to `_verify` — voucher type present, every pause honoured — so `report`
    is genuinely constructed, and plants exactly one real defect an honest load can produce: an already-existing
    group under the wrong parent (Task 6's I4, `company_b.py`'s `_load_masters`, ~line 136). That appends to
    `report.problems` without raising, so the run completes and only the `if report.problems:` branch in
    `_setup_b` is what makes this exit 1 — unlike the sibling test above, whose 1 comes from `except
    OperatorError` before `report.problems` is ever consulted. Mutation-tested: deleting `return 1` from that
    `if report.problems:` block (leaving the function to fall through to `return 0`) makes this test FAIL —
    confirmed locally per the fix-round instructions, then reverted; the CompanyBLoadError test above does NOT
    fail under that same mutation, which is exactly the blind spot this test closes."""
    config = tmp_config(tmp_path)
    write_company_folder(config.company_folder("B"), COMPANIES["B"])
    books = FakeBooks(config.company_folder("B"), name=COMPANIES["B"], educational=False)   # the load is licensed
    books.edit_state(lambda s: s.__setitem__(
        "voucherTypes", ["Sales", "Purchase", "Receipt", "Payment", "Sales - GST"]))
    # National Creditors already exists in Tally, but under the wrong parent — I4 flags this as a `problems`
    # line and skips (does not raise), so the loader proceeds all the way through to `_verify`.
    books.edit_state(lambda s: s.setdefault("groups", {}).__setitem__(
        "National Creditors", {"parent": "Local Creditors"}))
    books.edit_state(lambda s: s["currencies"].__setitem__("$", dict(USD_CURRENCY_ROW)))   # plan part 7: UI-made
    op = build_auto_operator(config=config, transport=books.transport(), runner=FakeRunner(books, []),
                             echo=lambda line: None, console_input=_console_input_honouring_flag_pauses(books))
    assert main(["--results", str(tmp_path / "r.json"), "setup-b"], operator=op) == 1
    out = capsys.readouterr().out
    assert "Problems:" in out
    assert "group 'National Creditors': parent is 'Local Creditors' in Tally, expected 'Sundry Creditors'" in out


def test_allow_risky_is_opt_in_on_the_run_command():
    """A default `run` / `run --all` must never carry the risky steps (probe 16's SVFROMDATE ledger read, which
    froze Tally live on 2026-09-23)."""
    parser = build_parser()
    assert parser.parse_args(["run", "16"]).allow_risky is False
    assert parser.parse_args(["run", "--all"]).allow_risky is False
    assert parser.parse_args(["run", "16", "--allow-risky"]).allow_risky is True


def test_anchors_command_without_a_baseline_fails_and_is_recorded(tmp_path):
    io = ScriptedIO()
    results = tmp_path / "r.json"
    assert main(["--results", str(results), "anchors"], transport=FakeTally([COMPANIES["A"]]).transport(), io=io) == 1
    assert any("anchors check (before_parity) company A: FAILED" in s and "No TB baseline" in s for s in io.said)
    assert ResultsStore(results).anchor_checks[-1]["when"] == "before_parity"


def test_setup_c_loads_company_c(tmp_path, capsys):
    books = FakeBooks(name=COMPANIES["C"], educational=True)
    assert main(["--results", str(tmp_path / "r.json"), "setup-c"], transport=books.transport()) == 0
    assert "Company C loaded" in capsys.readouterr().out
    assert ResultsStore(tmp_path / "r.json").environment["company_c_loaded_at"]


def test_setup_c_refuses_when_another_company_is_open(tmp_path, capsys):
    books = FakeBooks(name=COMPANIES["B"])
    assert main(["--results", str(tmp_path / "r.json"), "setup-c"], transport=books.transport()) == 1
    assert "setup-c failed" in capsys.readouterr().out
