"""One definition of each top-level function and class in ``tally_bridge``. AST-based: nothing is imported, only parsed.

A name defined at module level in two bridge modules is how the company-list copies crept in (dedupe 1). Counted as a
definition: a top-level ``def``, ``async def`` or ``class``, and a top-level plain-name assignment whose value is a
name, an attribute or a lambda (an alias of a function, e.g. ``esc = _esc`` or ``parse_bills = other.parse_bills``).
Not counted: imports (``from tally_bridge.import_builder import esc`` is a use, not a copy), other assignments, and
anything nested inside a function, class or ``if`` block.

The allow-list below is exact both ways: a new duplicate fails, a third copy of an allowed name fails, and so does an
allowed name whose set of modules has changed (e.g. one copy removed, so it is no longer duplicated).
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "tally_bridge"

# float (response_parser, chat path) vs Decimal (amounts / sync_reports, sync path), merge spec M11; removed when chat
# reads the synced tables (plan step 3); see docs/open-items-parked.md.
ALLOWED: dict[str, set[str]] = {
    "parse_amount": {"tally_bridge/amounts.py", "tally_bridge/response_parser.py"},
    "parse_trial_balance": {"tally_bridge/response_parser.py", "tally_bridge/sync_reports.py"},
    "parse_ledger_list": {"tally_bridge/response_parser.py", "tally_bridge/sync_reports.py"},
    "parse_bills": {"tally_bridge/response_parser.py", "tally_bridge/sync_reports.py"},
    "parse_stock_summary": {"tally_bridge/response_parser.py", "tally_bridge/sync_reports.py"},
}


def _defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute, ast.Lambda)):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, (ast.Name, ast.Attribute, ast.Lambda))
    ):
        return [node.target.id]
    return []


def _top_level_names() -> dict[str, set[str]]:
    modules: dict[str, set[str]] = defaultdict(set)
    for path in sorted(BRIDGE.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
            for name in _defined_names(node):
                modules[name].add(path.relative_to(BRIDGE.parent).as_posix())
    return modules


def _duplicated() -> dict[str, set[str]]:
    return {name: paths for name, paths in _top_level_names().items() if len(paths) > 1}


def test_no_name_is_defined_in_two_bridge_modules():
    unexpected = {name: paths for name, paths in _duplicated().items() if name not in ALLOWED}
    assert unexpected == {}


def test_every_allowed_duplicate_is_in_exactly_its_allowed_modules():
    names = _top_level_names()
    actual = {name: names.get(name, set()) for name in ALLOWED}
    assert actual == ALLOWED
