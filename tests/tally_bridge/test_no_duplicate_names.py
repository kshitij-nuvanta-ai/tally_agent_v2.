"""One definition of each top-level function and class in ``tally_bridge``. AST-based: nothing is imported, only parsed.

A name defined at module level in two bridge modules is how the company-list copies crept in (dedupe 1). The allow-list
below is exact both ways: a new duplicate fails, and so does an allowed name that is no longer duplicated.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "tally_bridge"

# float (response_parser, chat path) vs Decimal (amounts / sync_reports, sync path), merge spec M11; removed when chat
# reads the synced tables (plan step 3); see docs/open-items-parked.md.
ALLOWED = {"parse_amount", "parse_trial_balance", "parse_ledger_list", "parse_bills", "parse_stock_summary"}


def _top_level_names() -> dict[str, list[str]]:
    modules: dict[str, list[str]] = defaultdict(list)
    for path in sorted(BRIDGE.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                modules[node.name].append(str(path.relative_to(BRIDGE.parent)))
    return modules


def _duplicated() -> dict[str, list[str]]:
    return {name: paths for name, paths in _top_level_names().items() if len(set(paths)) > 1}


def test_no_name_is_defined_in_two_bridge_modules():
    unexpected = {name: paths for name, paths in _duplicated().items() if name not in ALLOWED}
    assert unexpected == {}


def test_every_allowed_duplicate_is_still_duplicated():
    assert ALLOWED - set(_duplicated()) == set()
