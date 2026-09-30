"""Stop and start TallyPrime under Wine on the s0probe data folder, and wait for it (S0 spec §5.8).

Process control goes through a ProcessRunner so tests never start Wine. The operator stops only a TallyPrime it can
recognise as its own — started with /DATA:…s0probe, or spawned by this operator — unless told `stop_any_tally`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import httpx

from tally_bridge.envelopes import build_company_list
from tally_bridge.xml_utils import parse_company_list, sanitize_xml
from probes.core import ProbeBlocked
from probes.operator.config import DATA_MARKER, OperatorConfig

CLICK_NEEDED = "CLICK NEEDED: in TallyPrime click 'T: Continue In Educational Mode' on the licence box"
BUSY_REPEAT_S = 30.0
CLICK_REPEAT_S = 60.0
WRONG_COMPANY_LIMIT = 5

# C44: this machine's tally.ini has `Default Companies=Yes` + a `Load=<number>` line, so Tally opens BOTH the ini's
# Load= company AND whatever /LOAD:<n> asks for. Rewriting Load= to the wanted company before every labelled start
# is the fix. Line-anchored, case-insensitive (Tally's own installer casing is "Load=" but don't assume callers'
# files always match it); captures only the key prefix so the existing line ending (CRLF on this machine) is left
# untouched — nothing else in the file is even inside the match.
_LOAD_LINE = re.compile(rb'^([ \t]*Load[ \t]*=)[^\r\n]*', re.IGNORECASE | re.MULTILINE)
_DEFAULT_COMPANIES_LINE = re.compile(rb'^([ \t]*Default Companies[ \t]*=[^\r\n]*)(\r\n|\r|\n)?', re.IGNORECASE | re.MULTILINE)


class OperatorError(ProbeBlocked):
    """The operator couldn't perform a step; the part is recorded BLOCKED with this message."""


@dataclass(frozen=True)
class TallyProcess:
    pid: int
    command: str


class ProcessRunner(Protocol):
    def list_tally(self) -> list[TallyProcess]: ...

    def spawn(self, argv: list[str], cwd: Path, log_path: Path) -> None: ...

    def terminate(self, pid: int) -> None: ...

    def kill(self, pid: int) -> None: ...

    def run(self, argv: list[str], timeout: float) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    def now(self) -> float: ...


class SystemRunner:
    """The real ps / kill / Popen. Never used by tests."""

    def list_tally(self) -> list[TallyProcess]:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False).stdout
        found: list[TallyProcess] = []
        for line in out.splitlines():
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit() and "tally.exe" in command.lower():
                found.append(TallyProcess(int(pid), command.strip()))
        return found

    def spawn(self, argv: list[str], cwd: Path, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=log, start_new_session=True)

    def terminate(self, pid: int) -> None:
        subprocess.run(["kill", "-TERM", str(pid)], check=False)

    def kill(self, pid: int) -> None:
        subprocess.run(["kill", "-KILL", str(pid)], check=False)

    def run(self, argv: list[str], timeout: float) -> str:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


class TallyControl:
    def __init__(self, config: OperatorConfig, runner: ProcessRunner, http: httpx.Client, say: Callable[[str], None],
                 *, stop_any_tally: bool = False):
        self.config = config
        self.runner = runner
        self.http = http
        self.say = say
        self.stop_any_tally = stop_any_tally
        self._own_pids: set[int] = set()

    # --- what is running ------------------------------------------------------------------------------------------
    def port_up(self) -> bool:
        try:
            return "Running" in self.http.get("/", timeout=httpx.Timeout(3.0, connect=3.0)).text
        except httpx.HTTPError:
            return False

    def company_names(self) -> list[str] | None:
        """Loaded companies, or None while Tally doesn't answer (starting, loading a company, stuck)."""
        try:
            response = self.http.post("/", content=build_company_list().encode("utf-8"),
                                      headers={"Content-Type": "text/xml; charset=utf-8"},
                                      timeout=httpx.Timeout(10.0, connect=3.0))
            response.raise_for_status()
            return parse_company_list(sanitize_xml(response.text))
        except (httpx.HTTPError, ET.ParseError):
            return None

    def _classify(self) -> tuple[list[TallyProcess], list[TallyProcess]]:
        own: list[TallyProcess] = []
        foreign: list[TallyProcess] = []
        for proc in self.runner.list_tally():
            if proc.pid in self._own_pids or DATA_MARKER in proc.command.lower():
                own.append(proc)
            else:
                foreign.append(proc)
        return own, foreign

    def own_tally_running(self) -> bool:
        own, foreign = self._classify()
        return bool(own) and not foreign

    # --- stop / start -----------------------------------------------------------------------------------------------
    def stop(self) -> None:
        own, foreign = self._classify()
        if foreign and not self.stop_any_tally:
            pids = ", ".join(str(p.pid) for p in foreign)
            raise OperatorError(f"A TallyPrime the operator didn't start is running (pid {pids}). Close it by hand, "
                                f"or pass --stop-any-tally; the operator only stops its own s0probe TallyPrime.")
        targets = own + foreign
        if not targets:
            if self.port_up():
                raise OperatorError("TallyPrime answers on 9000 but no tally.exe process was found — stop it by hand")
            return
        self.say(f"stopping TallyPrime (pid {', '.join(str(p.pid) for p in targets)})")
        for proc in targets:
            self.runner.terminate(proc.pid)
        if not self._wait(lambda: not self.runner.list_tally() and not self.port_up(), self.config.stop_wait_s):
            # Kill only the pids we were already authorised to stop — never a foreign TallyPrime that showed up
            # while we were waiting (that one is not ours to touch, stop_any_tally or not).
            target_pids = {proc.pid for proc in targets}
            for proc in self.runner.list_tally():
                if proc.pid in target_pids:
                    self.runner.kill(proc.pid)
            self.runner.sleep(2.0)
            still_own, _ = self._classify()      # post-condition: did our own TallyPrime actually stop?
            if still_own:
                raise OperatorError("TallyPrime didn't stop, even after kill -9")
        self._own_pids.clear()

    def _require_number(self, load_label: str | None) -> None:
        if load_label is not None and load_label not in self.config.company_numbers:
            raise OperatorError(f"No company number configured for company {load_label}: it is opened by hand only "
                                "(company C — a security login or TallyVault prompt would block an unattended start; "
                                "S0 plan part 6).")

    def start(self, load_label: str | None) -> None:
        self._require_number(load_label)
        if self.runner.list_tally():
            raise OperatorError("TallyPrime is already running; stop it first")
        self._backup_ini_once()
        if load_label is not None:
            self._set_ini_load(load_label)
        argv = [str(self.config.wine_bin), "tally.exe", f"/DATA:{self.config.data_dir_windows}"]
        if load_label is not None:
            argv.append(f"/LOAD:{self.config.company_numbers[load_label]}")
        self.say("starting TallyPrime: " + " ".join(argv[1:]))
        try:
            self.runner.spawn(argv, self.config.tally_dir, self.config.wine_log)
        except OSError as exc:
            raise OperatorError(f"Can't start TallyPrime: {exc}") from exc

        def up() -> bool:
            self._own_pids.update(p.pid for p in self.runner.list_tally())   # nothing ran before the spawn
            return self.port_up()

        if not self._wait(up, self.config.start_wait_s):
            raise OperatorError(f"TallyPrime didn't answer on its port within {self.config.start_wait_s:.0f}s")

    def wait_for_companies(self, expected: list[str], *, click: bool) -> list[str]:
        """Poll the company list until it equals `expected`, riding out timeouts while a company loads.

        A non-empty, wrong company list for WRONG_COMPANY_LIMIT consecutive polls (once the XML server is actually
        answering) means the wrong company is open, not that Tally is still loading — fail fast instead of waiting out
        the whole click window. An empty list keeps waiting: that's the licence box (S0 spec §5.8).
        """
        limit = self.config.click_wait_s if click else self.config.start_wait_s
        deadline = self.runner.now() + limit
        told_at = busy_at = None
        wrong_streak = 0
        if click:
            self.say(f"{CLICK_NEEDED} (waiting up to {limit / 60:.0f} min for {expected})")
            told_at = self.runner.now()
        while True:
            names = self.company_names()
            if names == expected:
                if click:
                    self.say(f"companies open: {names}")
                return names
            now = self.runner.now()
            if names is None:
                wrong_streak = 0
                if busy_at is None or now - busy_at >= BUSY_REPEAT_S:
                    self.say("Tally busy (loading a company?) — still waiting")
                    busy_at = now
            elif names:
                wrong_streak += 1
                if wrong_streak >= WRONG_COMPANY_LIMIT:
                    raise OperatorError(f"Tally has {names} open, not {expected}; close it and open the right "
                                        "company by hand")
            else:
                wrong_streak = 0
            if click and told_at is not None and now - told_at >= CLICK_REPEAT_S:
                self.say(f"{CLICK_NEEDED} (still waiting for {expected}; Tally shows {names})")
                told_at = now
            if now >= deadline:
                raise OperatorError(f"Timed out waiting for {expected}; Tally shows {names}")
            self.runner.sleep(self.config.poll_s)

    def restart(self, load_label: str | None, expected: list[str]) -> list[str]:
        self._require_number(load_label)
        self.stop()
        self.start(load_label)
        return self.wait_for_companies(expected, click=load_label is not None)

    def ensure_running(self) -> None:
        if self.port_up():
            return
        if self.runner.list_tally():
            raise OperatorError("A TallyPrime process exists but its port doesn't answer — check Tally for a popup")
        self.start(None)
        self.wait_for_companies([], click=False)

    def wine_version(self) -> str:
        return self.runner.run([str(self.config.wine_bin), "--version"], timeout=15.0)

    # --- helpers ------------------------------------------------------------------------------------------------------
    def _wait(self, condition: Callable[[], bool], seconds: float) -> bool:
        deadline = self.runner.now() + seconds
        while True:
            if condition():
                return True
            if self.runner.now() >= deadline:
                return False
            self.runner.sleep(1.0)

    def _backup_ini_once(self) -> None:
        ini = self.config.tally_dir / "tally.ini"
        backup = self.config.tally_dir / "tally.ini.before-s0"
        if ini.exists() and not backup.exists():
            try:
                shutil.copy2(ini, backup)
            except OSError as exc:
                raise OperatorError(f"Can't back up tally.ini: {exc}") from exc
            self.say(f"backed up {ini.name} → {backup.name} (the operator never edits tally.ini)")

    def _set_ini_load(self, load_label: str) -> None:
        """C44: rewrite tally.ini's `Load=` line to exactly `load_label`'s company number, so a labelled start
        opens ONLY that company — not that plus whatever the ini already had loaded — byte-identical otherwise
        (same line endings, same every other line). No-op if there's no ini yet (nothing to clash with; Tally
        will write its own on first run). `_backup_ini_once` in `start()` has already run by the time this is
        called, so the pre-S0 ini is preserved before this ever touches it."""
        ini = self.config.tally_dir / "tally.ini"
        if not ini.exists():
            return
        number = self.config.company_numbers[load_label].encode("ascii")
        original = ini.read_bytes()

        content, replaced = _LOAD_LINE.subn(lambda m: m.group(1) + number, original, count=1)
        if not replaced:
            match = _DEFAULT_COMPANIES_LINE.search(original)
            if match is None:
                raise OperatorError(f"{ini} has neither a 'Load=' nor a 'Default Companies=' line — can't set "
                                    "which company opens; add a 'Load=' line by hand")
            eol = match.group(2) or b"\r\n"   # Default Companies= was the file's last line: fall back to this
                                               # machine's own CRLF rather than guessing from elsewhere in the file
            insert_at = match.end()
            content = original[:insert_at] + b"Load=" + number + eol + original[insert_at:]

        if content == original:
            return   # already set to the wanted company — nothing to write, nothing to verify
        ini.write_bytes(content)

        readback = ini.read_bytes()
        if readback != content:
            raise OperatorError(f"{ini} didn't read back as written after setting Load={number.decode()}")
        check = _LOAD_LINE.search(readback)
        value = check.group(0)[len(check.group(1)):] if check else None
        if value != number:
            raise OperatorError(f"{ini} still doesn't show Load={number.decode()} after the rewrite")
        self.say(f"tally.ini: Load={number.decode()} (company {load_label}, so only it opens)")
