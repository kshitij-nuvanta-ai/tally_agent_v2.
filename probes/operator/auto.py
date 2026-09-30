"""The automated operator (S0-D9, spec §5.8): implements ProbeIO by performing each pause and answering each ask.

It dispatches on Action.kind only (never on the instruction text). Writes go through TallyWriter (verified shapes,
read back, "Probe" companies only); open / close / backup / restore go through TallyControl restarts under Wine.
Every step is written to the operator log.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx

from probes.actions import ASK_KINDS, PAUSE_KINDS, Action
from probes.companies import COMPANIES, SEED_COMPANY
from probes.operator import company_a
from probes.operator.config import OperatorConfig, default_config
from probes.operator.tally_control import OperatorError, ProcessRunner, SystemRunner, TallyControl
from probes.safety import GuardError, check_company
from probes.setup.company_b import CompanyBLoadError, LoadReport, load_company_b
from probes.setup.writes import TallyWriter, WriteFailed, WriteRefused

AUTO_RUN_MODE = (
    "auto (S0-D9): the operator performs every pause — edits via XML import (verified shapes, read back), open/close "
    "via TallyPrime restarts under Wine with /DATA and /LOAD, company-A restore/backup by copying the company folder. "
    "Not exercised: UI-edit parity for probes 1, 7, 8, probe 16's UI balance read and probe 19's UI report view (later "
    "manual checks); probe 10's popup is a deliberate duplicate-master create; probe 13 restores at file level "
    "(Tally's Backup/Restore screens unused)."
)


class OperatorLog:
    """Every operator line goes to `echo` (stdout by default) and, when a path is given, to a log file."""

    def __init__(self, path: Path | None = None, echo: Callable[[str], None] | None = None):
        self.path = path
        self.echo = echo or (lambda line: print(line, flush=True))
        self.lines: list[str] = []

    def write(self, message: str) -> None:
        line = f"[operator {datetime.now().strftime('%H:%M:%S')}] {message}"
        self.lines.append(line)
        self.echo(line)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class AutoOperator:
    interactive = True
    run_mode = "auto"

    def __init__(self, *, control: TallyControl, writer: TallyWriter, config: OperatorConfig, log: OperatorLog,
                console_input: Callable[[str], str] = input):
        self.control = control
        self.writer = writer
        self.config = config
        self.log = log
        self.console_input = console_input           # C18: how an action-less wait prompts the person at the machine
        self.vouchers: dict[str, str] = {}          # a probe's voucher ref → Tally Master ID
        self.popup_raised: bool | None = None       # did the last raise_popup step actually time out? (M9)
        self._pause: dict[str, Callable[[dict[str, Any]], None]] = {
            "tally_running": self._tally_running, "restore_seed": self._restore_seed,
            "rename_company": self._rename_company, "close_all_companies": self._close_all,
            "open_company": self._open_company, "create_voucher": self._create_voucher,
            "alter_voucher": self._alter_voucher, "delete_voucher": self._delete_voucher,
            "create_ledger": self._create_ledger, "alter_ledger": self._alter_ledger,
            "delete_ledger": self._delete_ledger, "rename_ledger": self._rename_ledger,
            "view_report": self._view_report, "raise_popup": self._raise_popup, "dismiss_popup": self._dismiss_popup,
            "quit_tally": self._quit_tally, "backup_company": self._backup_company,
            "restore_company": self._restore_company,
        }
        self._ask: dict[str, Callable[[dict[str, Any]], str]] = {
            "wine_version": self._wine_version, "data_folder": self._data_folder, "tally_version": self._tally_version,
            "edition": self._edition, "licence": self._licence, "expense_ledger": self._expense_ledger,
            "ui_closing_balance": self._ui_closing_balance,
        }
        if set(self._pause) != PAUSE_KINDS or set(self._ask) != ASK_KINDS:
            raise RuntimeError("AutoOperator handlers are out of step with actions.PAUSE_KINDS / ASK_KINDS")

    @property
    def handled_pauses(self) -> frozenset[str]:
        return frozenset(self._pause)

    @property
    def handled_asks(self) -> frozenset[str]:
        return frozenset(self._ask)

    # --- ProbeIO ----------------------------------------------------------------------------------------------------
    def say(self, message: str) -> None:
        self.log.write(message)

    def wait(self, instruction: str, action: Action | None = None) -> None:
        # Ruling C18: an action-less wait is not a programming error — some steps (e.g. setting F2, toggling a
        # cancelled/optional flag) are UI-only and no XML can do them, so a person at the machine performs them.
        # An action WITH a kind AutoOperator doesn't handle for `wait` (e.g. an ask-only kind) is a real bug and
        # still raises.
        if action is None:
            self.log.write(f"STEP (manual — no automated action) — {instruction}")
            self.console_input(f"\n>>> {instruction}\n    Press Enter when done... ")
            return
        if action.kind not in self._pause:
            raise OperatorError(f"No automated action for this step: {instruction}")
        self.log.write(f"STEP {action.kind} {action.params or ''} — {instruction}")
        try:
            self._pause[action.kind](action.params)
        except (WriteFailed, WriteRefused, GuardError) as exc:
            raise OperatorError(f"{action.kind}: {exc}") from exc

    def ask(self, prompt: str, action: Action | None = None) -> str:
        if action is None or action.kind not in self._ask:
            raise OperatorError(f"No automated answer for: {prompt}")
        try:
            answer = self._ask[action.kind](action.params)
        except (WriteFailed, GuardError) as exc:
            raise OperatorError(f"{action.kind}: {exc}") from exc
        self.log.write(f"ASK  {action.kind}: {prompt!r} → {answer!r}")
        return answer

    # --- commands ---------------------------------------------------------------------------------------------------
    def reset_company_a(self) -> None:
        self.log.write("reset-a: fresh copy of the seed company into s0probe, then rename it to company A")
        try:
            company_a.reset_company_a(self.control, self.writer, self.config)
        except (WriteFailed, WriteRefused, GuardError) as exc:
            raise OperatorError(f"reset-a: {exc}") from exc
        self.vouchers.clear()

    def setup_company_b(self, licence: str = "licensed") -> LoadReport:
        self.log.write("setup-b: loading company B from the deterministic dataset")
        try:
            # C1 (final review): ~1,000 objects are about to be written, and `check_writable` inside
            # load_company_b only inspects the string literal COMPANIES["B"] — it cannot see WHICH company Tally
            # actually has open. Company A is also a "Probe" company, so the realistic operator sequence ("run
            # the A probes, then load B") would sail straight past it, and whether Tally honours
            # SVCURRENTCOMPANY for a company that is not loaded is unverified. Every probe run guards this via
            # runner.py; this path now guards it the same way, before the first write.
            self.control.ensure_running()
            self._open_company({"label": "B"})
            check_company(self.control.company_names() or [], COMPANIES["B"], mutating=True)
            # WriteFailed and CompanyBLoadError are covered (test_auto_operator.py). WriteRefused is not:
            # its only raise site is check_writable(company), and both load_company_b's `company` default and
            # this method's own fixed signature (no company override, Ruling C3) always pass COMPANIES["B"]
            # (contains "Probe"). GuardError IS now reachable from this path — check_company above is the guard
            # that raises it — but in the fake the wrong-company case is caught one layer earlier, by
            # TallyControl's own fail-fast ("Tally has [...] open, not [...]"), which is what
            # test_setup_company_b_refuses_when_tally_has_another_company_open asserts; check_company is the
            # second line of defence for the case where that wait returns something unexpected (its own
            # behaviour is covered directly in test_safety.py).
            return load_company_b(self.writer, self, licence=licence)
        except (WriteFailed, WriteRefused, GuardError, CompanyBLoadError) as exc:
            raise OperatorError(f"setup-b: {exc}") from exc

    def close(self) -> None:
        self.writer.http.close()

    # --- pause handlers ---------------------------------------------------------------------------------------------
    def _tally_running(self, params: dict[str, Any]) -> None:
        self.control.ensure_running()

    def _restore_seed(self, params: dict[str, Any]) -> None:
        company_a.restore_seed_copy(self.control, self.config)
        self.vouchers.clear()          # the restored company has none of this run's vouchers (retro M8)

    def _rename_company(self, params: dict[str, Any]) -> None:
        if params["from"] == SEED_COMPANY and params["to"] == COMPANIES["A"]:
            company_a.rename_seed_to_a(self.control, self.writer)
        else:
            self.writer.rename_company(params["from"], params["to"])

    def _close_all(self, params: dict[str, Any]) -> None:
        self.control.restart(None, [])

    def _open_company(self, params: dict[str, Any]) -> None:
        label = params["label"]
        if label not in self.config.company_numbers:
            raise OperatorError(f"Auto mode can't open company {label} yet: no company number configured (plan part 3)")
        wanted = [COMPANIES[label]]
        if self.control.company_names() == wanted:
            self.log.write(f"company {label} is already the only open company")
            return
        self.control.restart(label, wanted)

    def _master_id(self, ref: str) -> str:
        if ref not in self.vouchers:
            raise OperatorError(f"No voucher {ref!r} was created in this run")
        return self.vouchers[ref]

    def _create_voucher(self, params: dict[str, Any]) -> None:
        ref = params["ref"]
        if ref in self.vouchers:
            raise OperatorError(f"Voucher ref {ref!r} is already in use in this run")
        self.vouchers[ref] = self.writer.create_payment(
            params["company"], ledger=params["ledger"], amount=Decimal(params["amount"]), narration=params["narration"],
            post_dated=bool(params.get("post_dated", False)))

    def _alter_voucher(self, params: dict[str, Any]) -> None:
        self.writer.alter_voucher_narration(params["company"], self._master_id(params["ref"]), params["narration"])

    def _delete_voucher(self, params: dict[str, Any]) -> None:
        self.writer.delete_voucher(params["company"], self._master_id(params["ref"]))
        self.vouchers.pop(params["ref"])

    def _create_ledger(self, params: dict[str, Any]) -> None:
        self.writer.create_ledger(params["company"], params["name"], params["parent"])

    def _alter_ledger(self, params: dict[str, Any]) -> None:
        self.writer.alter_ledger_email(params["company"], params["name"], params["email"])

    def _delete_ledger(self, params: dict[str, Any]) -> None:
        self.writer.delete_ledger(params["company"], params["name"])

    def _rename_ledger(self, params: dict[str, Any]) -> None:
        self.writer.rename_ledger(params["company"], params["from"], params["to"])

    def _view_report(self, params: dict[str, Any]) -> None:
        size = self.writer.export_report(params["company"], params["report"], params["from_date"], params["to_date"])
        self.log.write(f"exported {params['report']!r} via XML instead of opening it in the UI ({size} bytes)")

    def _raise_popup(self, params: dict[str, Any]) -> None:
        result = self.writer.raise_duplicate_master_popup(params["company"])
        self.popup_raised = result == "timeout"
        self.log.write(f"duplicate stock-group create → {result}")

    def _dismiss_popup(self, params: dict[str, Any]) -> None:
        label = params["label"]
        self.control.restart(label, [COMPANIES[label]])

    def _quit_tally(self, params: dict[str, Any]) -> None:
        self.control.stop()

    def _backup_company(self, params: dict[str, Any]) -> None:
        folder = company_a.backup_company(self.control, self.config, params["label"], params["tag"])
        self.log.write(f"file-level backup of company {params['label']} → {folder}")

    def _restore_company(self, params: dict[str, Any]) -> None:
        company_a.restore_company(self.control, self.config, params["label"], params["tag"])
        self.vouchers.clear()          # a restore drops every voucher created since the backup (retro M8)

    # --- ask handlers -----------------------------------------------------------------------------------------------
    def _wine_version(self, params: dict[str, Any]) -> str:
        version = self.control.wine_version()
        if not version:
            raise OperatorError("`wine --version` printed nothing")
        return version

    def _data_folder(self, params: dict[str, Any]) -> str:
        number = self.config.company_numbers["A"]
        return f"{self.config.data_dir_windows}\\{number} (fresh copy of seed_data/{number} made by the operator)"

    def _tally_version(self, params: dict[str, Any]) -> str:
        info = self.writer.licence_info()
        if not info.release:
            return "TallyPrime (release not in the XML response header)"
        return f"TallyPrime {info.release} (release from the XML response header)"

    def _edition(self, params: dict[str, Any]) -> str:
        return self.config.edition

    def _licence(self, params: dict[str, Any]) -> str:
        info = self.writer.licence_info()
        if info.educational is None:
            raise OperatorError("$$LicenseInfo:IsEducationalMode gave no Yes/No answer")
        return "educational" if info.educational else "licensed"

    def _expense_ledger(self, params: dict[str, Any]) -> str:
        raise OperatorError("No Indirect Expenses ledger found and no scripted answer — pick one by hand")

    def _ui_closing_balance(self, params: dict[str, Any]) -> str:
        return ""     # auto mode reads no UI; probe 16 records the UI comparison as not done


def build_auto_operator(*, host: str = "localhost", port: int = 9000, log_path: Path | None = None,
                        config: OperatorConfig | None = None, transport: httpx.BaseTransport | None = None,
                        runner: ProcessRunner | None = None, stop_any_tally: bool = False,
                        echo: Callable[[str], None] | None = None,
                        console_input: Callable[[str], str] = input) -> AutoOperator:
    config = config or default_config()
    log = OperatorLog(log_path, echo)
    http = httpx.Client(base_url=f"http://{host}:{port}", transport=transport, trust_env=False)
    control = TallyControl(config, runner or SystemRunner(), http, log.write, stop_any_tally=stop_any_tally)
    return AutoOperator(control=control, writer=TallyWriter(http, log.write), config=config, log=log,
                        console_input=console_input)
