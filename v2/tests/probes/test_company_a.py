import json
from decimal import Decimal

import pytest

from v2.probes.companies import COMPANIES, SEED_COMPANY
from v2.probes.operator import company_a
from v2.probes.operator.tally_control import CLICK_NEEDED, OperatorError, TallyControl, TallyProcess
from v2.probes.setup.writes import TallyWriter
from v2.tests.probes.fake_books import (FOREIGN_COMMAND, OWN_COMMAND, STATE_FILE, FakeBooks, FakeRunner, seed_state,
                                        sync_client, tmp_config, write_company_folder)

A = COMPANIES["A"]


def _setup(tmp_path, procs=(), dirty=True):
    config = tmp_config(tmp_path)
    write_company_folder(config.seed_folder("A"), SEED_COMPANY)
    write_company_folder(config.company_folder("A"), A)
    if dirty:                                      # what the throwaway run left: EMAIL on Electricity
        path = config.company_folder("A") / STATE_FILE
        state = json.loads(path.read_text(encoding="utf-8"))
        state["ledgers"]["Electricity"]["email"] = "s0probe@example.com"
        path.write_text(json.dumps(state), encoding="utf-8")
    books = FakeBooks(config.company_folder("A"), running=bool(procs), loaded=bool(procs), click_polls_on_load=1)
    runner = FakeRunner(books, list(procs))
    said: list[str] = []
    http = sync_client(books.transport())
    return config, books, runner, TallyControl(config, runner, http, said.append), TallyWriter(http, said.append), said


def test_reset_company_a_copies_the_seed_and_renames_it(tmp_path):
    config, books, runner, control, writer, said = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)])
    company_a.reset_company_a(control, writer, config)
    assert books.state == seed_state(A)                  # fresh seed data (no leftover EMAIL), renamed to A
    assert books.companies() == [A]
    assert runner.spawned[-1][-1] == "/LOAD:100003"
    assert any(line.startswith(CLICK_NEEDED) for line in said)


def test_reset_refuses_while_a_foreign_tally_runs_and_leaves_the_folder(tmp_path):
    config, books, _, control, writer, _ = _setup(tmp_path, [TallyProcess(7, FOREIGN_COMMAND)])
    with pytest.raises(OperatorError):
        company_a.reset_company_a(control, writer, config)
    assert books.state["ledgers"]["Electricity"]["email"] == "s0probe@example.com"


def test_replace_folder_refuses_a_target_outside_s0probe(tmp_path):
    config, *_ = _setup(tmp_path)
    with pytest.raises(OperatorError, match="Refusing"):
        company_a.replace_company_folder(config.seed_folder("A"), tmp_path / "elsewhere" / "100003", config)


def test_backup_then_restore_rolls_back_a_voucher(tmp_path):
    config, books, _, control, writer, _ = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)], dirty=False)
    company_a.backup_company(control, config, "A", "t1")
    assert (company_a.backup_folder(config, "A", "t1") / STATE_FILE).exists()
    writer.create_payment(A, ledger="Electricity", amount=Decimal("1"), narration="S0-throwaway 4")
    assert books.state["vouchers"]
    company_a.restore_company(control, config, "A", "t1")
    assert books.state["vouchers"] == {} and books.state["alt_vch"] == 50
    assert books.companies() == [A]


def test_seed_rename_needs_the_operators_own_tally(tmp_path):
    config, books, _, control, writer, _ = _setup(tmp_path)
    books.start(load=True)                              # a Tally we know nothing about (no process listed)
    with pytest.raises(OperatorError, match="own s0probe"):
        company_a.rename_seed_to_a(control, writer)


@pytest.mark.parametrize("tag", ["../x", "a/b", "a\\b", ""])
def test_backup_folder_rejects_a_dangerous_tag(tmp_path, tag):
    """Fix round 1 #2: a tag must not be able to escape config.backups_dir."""
    config, *_ = _setup(tmp_path)
    with pytest.raises(OperatorError):
        company_a.backup_folder(config, "A", tag)


@pytest.mark.parametrize("tag", ["../x", "a/b", "a\\b", ""])
def test_backup_company_refuses_a_dangerous_tag_before_touching_anything(tmp_path, tag):
    config, books, runner, control, writer, _ = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)])
    with pytest.raises(OperatorError):
        company_a.backup_company(control, config, "A", tag)
    assert runner.terminated == []                                  # stop() was never reached
    assert (config.company_folder("A") / STATE_FILE).exists()       # nothing was deleted


def test_backup_folder_accepts_a_plain_tag(tmp_path):
    config, *_ = _setup(tmp_path)
    assert company_a.backup_folder(config, "A", "t1") == config.backups_dir / "100003-t1"


def test_backup_company_translates_a_copy_failure_into_an_operator_error(tmp_path, monkeypatch):
    """Fix round 1 #3: a filesystem error during the file-level backup becomes an OperatorError, not a traceback."""
    config, books, runner, control, writer, _ = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)], dirty=False)

    def _broken_copytree(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(company_a.shutil, "copytree", _broken_copytree)
    with pytest.raises(OperatorError, match="Couldn't back up"):
        company_a.backup_company(control, config, "A", "t1")


# --- M3 (retro): copy to a sibling temp folder, then swap; Tally restarted in finally ---------------------------


def _snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _half_copytree(monkeypatch):
    real = company_a.shutil.copytree

    def _half(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        raise OSError("disk full halfway")

    monkeypatch.setattr(company_a.shutil, "copytree", _half)


def test_failed_copy_leaves_target_intact(tmp_path, monkeypatch):
    config, *_ = _setup(tmp_path)
    before = _snapshot(config.company_folder("A"))
    _half_copytree(monkeypatch)
    with pytest.raises(OperatorError, match="Couldn't copy"):
        company_a.replace_company_folder(config.seed_folder("A"), config.company_folder("A"), config)
    assert _snapshot(config.company_folder("A")) == before
    assert [p.name for p in config.data_dir.iterdir() if ".tmp-" in p.name or ".old-" in p.name] == []


def test_backup_reusing_tag_keeps_old_backup_until_new_copy_succeeds(tmp_path, monkeypatch):
    config, books, runner, control, writer, _ = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)], dirty=False)
    company_a.backup_company(control, config, "A", "t1")
    folder = company_a.backup_folder(config, "A", "t1")
    old = _snapshot(folder)
    assert old
    (config.company_folder("A") / "marker.txt").write_text("new", encoding="utf-8")
    _half_copytree(monkeypatch)
    with pytest.raises(OperatorError):
        company_a.backup_company(control, config, "A", "t1")
    assert _snapshot(folder) == old


def test_backup_restarts_tally_even_when_copy_fails(tmp_path, monkeypatch):
    config, books, runner, control, writer, _ = _setup(tmp_path, [TallyProcess(8, OWN_COMMAND)], dirty=False)
    started = []
    real_start = control.start
    monkeypatch.setattr(control, "start", lambda label: (started.append(label), real_start(label))[1])
    _half_copytree(monkeypatch)
    with pytest.raises(OperatorError):
        company_a.backup_company(control, config, "A", "t1")
    assert started == ["A"]
