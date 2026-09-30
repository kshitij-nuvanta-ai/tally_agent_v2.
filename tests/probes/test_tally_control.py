from dataclasses import replace

import pytest

from probes.companies import COMPANIES
from probes.operator import tally_control as tally_control_module
from probes.operator.tally_control import (CLICK_NEEDED, WRONG_COMPANY_LIMIT, OperatorError, TallyControl,
                                              TallyProcess)
from tests.probes.fake_books import FOREIGN_COMMAND, OWN_COMMAND, FakeBooks, FakeRunner, sync_client, tmp_config

A = COMPANIES["A"]


def _control(tmp_path, books, procs=(), *, stubborn=False, hide_args=False, stop_any_tally=False):
    runner = FakeRunner(books, list(procs), stubborn=stubborn, hide_args=hide_args)
    said: list[str] = []
    control = TallyControl(tmp_config(tmp_path), runner, sync_client(books.transport()), said.append,
                           stop_any_tally=stop_any_tally)
    return control, runner, said


def test_config_refuses_a_data_folder_that_is_not_s0probe(tmp_path):
    with pytest.raises(ValueError):
        replace(tmp_config(tmp_path), data_dir=tmp_path / "Data")


def test_config_refuses_backups_inside_s0probe(tmp_path):
    config = tmp_config(tmp_path)
    with pytest.raises(ValueError):
        replace(config, backups_dir=config.data_dir / "backups")


def test_stop_refuses_a_tally_it_did_not_start(tmp_path):
    books = FakeBooks(name=A)
    control, runner, _ = _control(tmp_path, books, [TallyProcess(7, FOREIGN_COMMAND)])
    with pytest.raises(OperatorError, match="didn't start"):
        control.stop()
    assert runner.terminated == [] and books.running


def test_stop_with_port_up_but_no_process_raises(tmp_path):
    """M10: no tally.exe process was found, but the port still answers — needs a person, not silence."""
    books = FakeBooks(name=A, running=True, loaded=True)
    control, runner, _ = _control(tmp_path, books)   # no procs: list_tally() is empty
    with pytest.raises(OperatorError, match="no tally.exe process was found"):
        control.stop()
    assert runner.terminated == [] and books.running


def test_stop_with_nothing_running_returns(tmp_path):
    books = FakeBooks(name=A, running=False)
    control, runner, _ = _control(tmp_path, books)
    control.stop()
    assert runner.terminated == []


def test_stop_any_tally_flag_allows_it(tmp_path):
    books = FakeBooks(name=A)
    control, runner, _ = _control(tmp_path, books, [TallyProcess(7, FOREIGN_COMMAND)], stop_any_tally=True)
    control.stop()
    assert runner.terminated == [7] and not books.running


def test_stop_own_tally_recognised_by_its_data_argument(tmp_path):
    books = FakeBooks(name=A)
    control, runner, _ = _control(tmp_path, books, [TallyProcess(8, OWN_COMMAND)])
    control.stop()
    assert runner.terminated == [8] and runner.killed == [] and not books.running


def test_stubborn_tally_is_killed_after_the_wait(tmp_path):
    books = FakeBooks(name=A)
    control, runner, _ = _control(tmp_path, books, [TallyProcess(8, OWN_COMMAND)], stubborn=True)
    control.stop()
    assert runner.terminated == [8] and runner.killed == [8]


def test_start_passes_data_and_load_and_waits_for_the_port(tmp_path):
    books = FakeBooks(name=A, running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books)
    control.start("A")
    assert runner.spawned == [[str(tmp_config(tmp_path).wine_bin), "tally.exe",
                               r"/DATA:C:\users\Public\TallyPrimeEditLog\s0probe", "/LOAD:100003"]]
    assert books.running and books.loaded


def test_start_refuses_when_tally_is_running(tmp_path):
    control, _, _ = _control(tmp_path, FakeBooks(name=A), [TallyProcess(8, OWN_COMMAND)])
    with pytest.raises(OperatorError, match="already running"):
        control.start(None)


def test_spawned_tally_is_ours_even_when_ps_hides_the_arguments(tmp_path):
    books = FakeBooks(name=A, running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books, hide_args=True)
    control.start(None)
    assert runner.procs[0].command == FOREIGN_COMMAND
    control.stop()
    assert runner.terminated == [101]


def test_wait_for_companies_says_click_needed_and_rides_out_timeouts(tmp_path):
    books = FakeBooks(name=A, running=False, loaded=False, click_polls_on_load=2, busy_polls_on_load=2)
    control, _, said = _control(tmp_path, books)
    control.start("A")
    assert control.wait_for_companies([A], click=True) == [A]
    assert sum(line.startswith(CLICK_NEEDED) for line in said) == 1
    assert any("Tally busy" in line for line in said)


def test_wait_for_companies_times_out_after_the_click_window(tmp_path):
    books = FakeBooks(name=A, running=False, loaded=False, click_polls_on_load=100_000)
    control, _, said = _control(tmp_path, books)
    control.start("A")
    with pytest.raises(OperatorError, match="Timed out"):
        control.wait_for_companies([A], click=True)
    assert sum(line.startswith(CLICK_NEEDED) for line in said) > 1


def test_wait_for_companies_fails_fast_on_a_persistently_wrong_company(tmp_path):
    """M11: a non-empty, wrong company list for 5 consecutive polls names what's open instead of waiting it out."""
    books = FakeBooks(name="Some Other Co", running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books)
    control.start("A")
    started_at = runner.clock
    # Wording unique to the fail-fast path: the deadline message names the open company too, so matching on
    # "Some Other Co" alone passes even with the fail-fast raise deleted.
    with pytest.raises(OperatorError, match="close it and open the right company by hand"):
        control.wait_for_companies([A], click=True)
    waited = runner.clock - started_at
    assert waited == (WRONG_COMPANY_LIMIT - 1) * control.config.poll_s   # polls, not the ~15-minute click window
    assert waited < control.config.click_wait_s


def test_wait_for_companies_empty_list_keeps_waiting_for_the_licence_box(tmp_path):
    """An empty list (the licence box) never counts toward the wrong-company streak."""
    books = FakeBooks(name=A, running=False, loaded=False, click_polls_on_load=6)
    control, _, _ = _control(tmp_path, books)
    control.start("A")
    assert control.wait_for_companies([A], click=True) == [A]


def test_restart_without_company_needs_no_click(tmp_path):
    books = FakeBooks(name=A)
    control, runner, said = _control(tmp_path, books, [TallyProcess(8, OWN_COMMAND)])
    assert control.restart(None, []) == []
    assert not any(line.startswith(CLICK_NEEDED) for line in said)
    assert not any(arg.startswith("/LOAD:") for arg in runner.spawned[0])


def test_tally_ini_is_backed_up_once(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    (config.tally_dir / "tally.ini").write_text("Load=1\n", encoding="utf-8")
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start(None)
    control.stop()
    (config.tally_dir / "tally.ini").write_text("Load=2\n", encoding="utf-8")
    control.start(None)
    assert (config.tally_dir / "tally.ini.before-s0").read_text(encoding="utf-8") == "Load=1\n"


def test_ensure_running_starts_tally_without_a_company(tmp_path):
    books = FakeBooks(name=A, running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books)
    control.ensure_running()
    assert books.running and not books.loaded
    assert len(runner.spawned) == 1
    control.ensure_running()
    assert len(runner.spawned) == 1


def test_stop_never_kills_a_foreign_tally_that_appears_during_the_wait(tmp_path):
    """Fix round 1 #1: the kill fallback must only target the pids stop() was already authorised to stop."""
    books = FakeBooks(name=A)
    runner = FakeRunner(books, [TallyProcess(8, OWN_COMMAND)], stubborn=True,
                        appears_during_wait=TallyProcess(99, FOREIGN_COMMAND))
    said: list[str] = []
    control = TallyControl(tmp_config(tmp_path), runner, sync_client(books.transport()), said.append)
    control.stop()
    assert runner.terminated == [8]
    assert runner.killed == [8]
    assert 99 not in runner.killed and 99 not in runner.terminated


def test_start_translates_a_spawn_failure_into_an_operator_error(tmp_path, monkeypatch):
    """Fix round 1 #3: a missing Wine binary (or any OSError from spawn) becomes an OperatorError, not a traceback."""
    books = FakeBooks(name=A, running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books)

    def _broken_spawn(argv, cwd, log_path):
        raise FileNotFoundError("no such file or directory: wine")

    monkeypatch.setattr(runner, "spawn", _broken_spawn)
    with pytest.raises(OperatorError, match="Can't start TallyPrime"):
        control.start(None)


def test_start_with_a_label_rewrites_load_to_that_companys_number(tmp_path):
    """C44: without this, /LOAD:<n> opens the ini's own Load= company IN ADDITION to the one requested."""
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    original = (b"Version=1\r\nDefault Companies=Yes\r\nLoad=100003\r\nLicence=Foo\r\n")
    ini.write_bytes(original)
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start("B")
    assert ini.read_bytes() == (b"Version=1\r\nDefault Companies=Yes\r\nLoad=100000\r\nLicence=Foo\r\n")


def test_start_with_a_label_preserves_every_other_byte_and_crlf(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    original = b"Setting1=X\r\nDefault Companies=Yes\r\nLoad=100003\r\nSetting2=Y\r\n\xff\xfeodd-bytes\r\n"
    ini.write_bytes(original)
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start("B")
    result = ini.read_bytes()
    assert result != original
    # Every line except the Load= line is byte-identical, including the odd non-UTF8 bytes and every CRLF.
    orig_lines = original.split(b"\r\n")
    new_lines = result.split(b"\r\n")
    assert len(orig_lines) == len(new_lines)
    for o, n in zip(orig_lines, new_lines):
        if o.startswith(b"Load="):
            assert n == b"Load=100000"
        else:
            assert n == o


def test_start_with_a_label_inserts_load_after_default_companies_when_missing(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    ini.write_bytes(b"Version=1\r\nDefault Companies=Yes\r\nLicence=Foo\r\n")
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start("A")
    assert ini.read_bytes() == b"Version=1\r\nDefault Companies=Yes\r\nLoad=100003\r\nLicence=Foo\r\n"


def test_start_with_a_label_raises_when_ini_has_neither_line(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    ini.write_bytes(b"Version=1\r\nLicence=Foo\r\n")
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    with pytest.raises(OperatorError, match="neither a 'Load='"):
        control.start("A")
    assert ini.read_bytes() == b"Version=1\r\nLicence=Foo\r\n"   # untouched, not half-written


def test_start_with_label_none_leaves_ini_untouched(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    original = b"Version=1\r\nDefault Companies=Yes\r\nLoad=100003\r\nLicence=Foo\r\n"
    ini.write_bytes(original)
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start(None)
    assert ini.read_bytes() == original


def test_start_with_a_label_leaves_ini_alone_when_load_already_correct(tmp_path):
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    original = b"Default Companies=Yes\r\nLoad=100000\r\n"
    ini.write_bytes(original)
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)
    control.start("B")
    assert ini.read_bytes() == original


def test_start_backup_is_taken_once_and_never_overwritten_across_label_switches(tmp_path):
    """The A+B live scenario: start A (Load=100003 already matches, no rewrite needed), stop, then start B — the
    backup must still hold the file exactly as it was before any S0 run touched it, and Load= must now say B."""
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    ini = config.tally_dir / "tally.ini"
    pristine = b"Default Companies=Yes\r\nLoad=100003\r\n"
    ini.write_bytes(pristine)
    books = FakeBooks(name=A, running=False, loaded=False)
    control, _, _ = _control(tmp_path, books)

    control.start("A")
    control.stop()
    control.start("B")

    backup = config.tally_dir / "tally.ini.before-s0"
    assert backup.read_bytes() == pristine
    # This is the mutation check: if the Load= rewrite is skipped, this line still reads 100003 and fails.
    assert ini.read_bytes() == b"Default Companies=Yes\r\nLoad=100000\r\n"


def test_start_translates_a_tally_ini_backup_failure_into_an_operator_error(tmp_path, monkeypatch):
    """Fix round 2: _backup_ini_once()'s shutil.copy2 must also become an OperatorError, and nothing gets spawned."""
    config = tmp_config(tmp_path)
    config.tally_dir.mkdir(parents=True)
    (config.tally_dir / "tally.ini").write_text("Load=1\n", encoding="utf-8")
    books = FakeBooks(name=A, running=False, loaded=False)
    control, runner, _ = _control(tmp_path, books)

    def _broken_copy2(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tally_control_module.shutil, "copy2", _broken_copy2)
    with pytest.raises(OperatorError, match="Can't back up tally.ini"):
        control.start(None)
    assert runner.spawned == []


def test_restart_refuses_a_company_with_no_number_before_stopping_tally(tmp_path):
    books = FakeBooks(name=A)
    control, runner, _ = _control(tmp_path, books, [TallyProcess(7, OWN_COMMAND)])
    ini = tmp_config(tmp_path).tally_dir / "tally.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_bytes(b"Default Companies=Yes\r\nLoad=100000\r\n")
    with pytest.raises(OperatorError, match="opened by hand only"):
        control.restart("C", [COMPANIES["C"]])
    assert runner.terminated == [] and runner.spawned == [] and books.running
    assert ini.read_bytes() == b"Default Companies=Yes\r\nLoad=100000\r\n"
