"""Company A's lifecycle in automated mode (S0 spec §5.8): fresh seed copy, the rename, file-level backup / restore."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from probes.companies import COMPANIES, SEED_COMPANY
from probes.operator.config import OperatorConfig
from probes.operator.tally_control import OperatorError, TallyControl
from probes.setup.writes import TallyWriter, WriteFailed

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _copy_then_swap(source: Path, target: Path) -> None:
    """Copy into a sibling temp folder first; only a complete copy replaces `target` (old kept until then)."""
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    old = target.with_name(f"{target.name}.old-{os.getpid()}")
    for leftover in (tmp, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        shutil.copytree(source, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    had_target = target.exists()
    if had_target:
        os.replace(target, old)
    try:
        os.replace(tmp, target)
    except BaseException:
        if had_target:
            os.replace(old, target)
        raise
    if had_target:
        shutil.rmtree(old, ignore_errors=True)


def replace_company_folder(source: Path, target: Path, config: OperatorConfig) -> None:
    """Copy `source` over `target`; `target` must be a company folder directly inside the s0probe data folder."""
    if target.parent.resolve() != config.data_dir.resolve():
        raise OperatorError(f"Refusing to replace {target}: it isn't a company folder inside {config.data_dir}")
    if not source.is_dir():
        raise OperatorError(f"No company folder at {source}")
    try:
        _copy_then_swap(source, target)
    except OSError as exc:
        raise OperatorError(f"Couldn't copy {source} to {target}: {exc}") from exc


def restore_seed_copy(control: TallyControl, config: OperatorConfig) -> None:
    """Stop Tally, copy the pristine seed company into s0probe, start Tally with it loaded (licence click)."""
    control.stop()
    replace_company_folder(config.seed_folder("A"), config.company_folder("A"), config)
    control.start("A")
    control.wait_for_companies([SEED_COMPANY], click=True)


def rename_seed_to_a(control: TallyControl, writer: TallyWriter) -> None:
    """The one write to a company without 'Probe' in its name — only on the operator's own s0probe TallyPrime."""
    if not control.own_tally_running():
        raise OperatorError("The seed rename runs only on the operator's own s0probe TallyPrime")
    try:
        writer.rename_company(SEED_COMPANY, COMPANIES["A"])
    except WriteFailed as exc:
        raise OperatorError(f"Company rename failed: {exc}") from exc


def reset_company_a(control: TallyControl, writer: TallyWriter, config: OperatorConfig) -> None:
    """`reset-a`: a fresh seed copy renamed to company A. Also undoes anything a probe could not revert."""
    restore_seed_copy(control, config)
    rename_seed_to_a(control, writer)


def backup_folder(config: OperatorConfig, label: str, tag: str) -> Path:
    if not _TAG_RE.match(tag):
        raise OperatorError(f"Invalid backup tag {tag!r}: must match {_TAG_RE.pattern}")
    target = config.backups_dir / f"{config.company_numbers[label]}-{tag}"
    if target.parent.resolve() != config.backups_dir.resolve():
        raise OperatorError(f"Refusing to use backup folder {target}: it isn't directly inside {config.backups_dir}")
    return target


def backup_company(control: TallyControl, config: OperatorConfig, label: str, tag: str) -> Path:
    """File-level backup with Tally stopped (a consistent copy), then Tally back up with the company."""
    target = backup_folder(config, label, tag)          # validated before anything is stopped or touched
    control.stop()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_then_swap(config.company_folder(label), target)
    except OSError as exc:
        raise OperatorError(f"Couldn't back up company {label} to {target}: {exc}") from exc
    finally:
        control.start(label)          # Tally comes back up even when the copy failed
    control.wait_for_companies([COMPANIES[label]], click=True)
    return target


def restore_company(control: TallyControl, config: OperatorConfig, label: str, tag: str) -> None:
    """File-level restore of `backup_company`'s copy over the company folder (Tally's Restore screen isn't used)."""
    source = backup_folder(config, label, tag)
    control.stop()
    replace_company_folder(source, config.company_folder(label), config)
    control.start(label)
    control.wait_for_companies([COMPANIES[label]], click=True)
