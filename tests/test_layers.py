"""Layer rules of the merged project (v2 merge spec M12). AST-based: nothing is imported, only parsed.

Top-level packages and what each must NOT import:

    tally_bridge   backend, agent, contract, probes, tests, scripts   (the bridge stands alone)
    contract       backend, agent, probes, tests, scripts             (it may import tally_bridge)
    agent          backend, probes, tests, scripts
    backend        agent, probes, tests, scripts
    probes         backend, tests, scripts

One recorded exception, which is not an ``import`` statement: ``tally_bridge/mock_handler.py`` loads
``tests/fixtures/generate_fixtures.py`` from its file path at run time (``RUNTIME_FILE_LOADS``). It predates the
merge; the test below keeps the list exact, so a second one cannot appear unnoticed.

The probe-internal rules from the former ``tests/test_isolation.py`` are kept at the bottom.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN: dict[str, frozenset[str]] = {
    "tally_bridge": frozenset({"backend", "agent", "contract", "probes", "tests", "scripts"}),
    "contract": frozenset({"backend", "agent", "probes", "tests", "scripts"}),
    "agent": frozenset({"backend", "probes", "tests", "scripts"}),
    "backend": frozenset({"agent", "probes", "tests", "scripts"}),
    "probes": frozenset({"backend", "tests", "scripts"}),
}
# Files in a layered package that load Python source from a path at run time, and the file they load.
RUNTIME_FILE_LOADS: dict[str, str] = {"tally_bridge/mock_handler.py": "tests/fixtures/generate_fixtures.py"}
_SKIPPED_DIRS = {".venv", "__pycache__", "node_modules"}
_DYNAMIC_IMPORTERS = {"import_module", "__import__"}
_FILE_LOADERS = {"spec_from_file_location", "SourceFileLoader", "run_path"}


def _package_for(rel: Path) -> str:
    """The dotted package name relative imports in `rel` (a path under the repo root) resolve against."""
    return ".".join(rel.parts[:-1])


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    """The absolute dotted name a `from .module import x` (or `..module`, ...) resolves to from `package`."""
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None  # climbs above the root; not resolvable (and not our concern here)
    base = ".".join(parts[:len(parts) - (level - 1)])
    if base and module:
        return f"{base}.{module}"
    return base or module


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def imported_modules(source: str, *, package: str = "") -> list[str]:
    """Absolute imports in `source`; `from a import b` yields both 'a' and 'a.b'.

    A relative import (`from .x import y` / `from ..x import y`) is resolved against `package` (the dotted
    name of the package containing this module) the way Python itself resolves one. A string-based import
    with a literal name (`importlib.import_module("a.b")`, `__import__("a")`) counts as an import of it.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _resolve_relative(package, node.level, node.module)
            if not base:
                continue
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and _call_name(node) in _DYNAMIC_IMPORTERS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and not first.value.startswith("."):
                names.append(first.value)
    return names


def _layered_files(root: Path):
    for area in sorted(FORBIDDEN):
        for path in sorted((root / area).rglob("*.py")):
            rel = path.relative_to(root)
            if not _SKIPPED_DIRS & set(rel.parts):
                yield area, rel, path.read_text(encoding="utf-8")


def violations(root: Path) -> list[str]:
    found: list[str] = []
    for area, rel, source in _layered_files(root):
        modules = imported_modules(source, package=_package_for(rel))
        for top in sorted({m.split(".")[0] for m in modules} & FORBIDDEN[area]):
            found.append(f"{rel.as_posix()}: {area} imports {top}")
    return found


def runtime_file_loads(root: Path) -> list[str]:
    """Layered files that call a load-source-from-a-path function (importlib's spec_from_file_location, ...)."""
    found: list[str] = []
    for _area, rel, source in _layered_files(root):
        if any(isinstance(n, ast.Call) and _call_name(n) in _FILE_LOADERS for n in ast.walk(ast.parse(source))):
            found.append(rel.as_posix())
    return found


# --- the rules, on the real tree --------------------------------------------------------------------------------------

def test_every_layered_package_exists():
    assert [area for area in sorted(FORBIDDEN) if not (REPO_ROOT / area / "__init__.py").is_file()] == []


def test_layer_rules_hold():
    assert violations(REPO_ROOT) == []


def test_the_only_runtime_file_load_is_the_recorded_one():
    assert runtime_file_loads(REPO_ROOT) == sorted(RUNTIME_FILE_LOADS)


def test_the_recorded_runtime_file_load_is_still_real():
    """The exception is removed from the list when the mock handler stops reading tests/fixtures."""
    for rel, target in RUNTIME_FILE_LOADS.items():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert Path(target).name in source, f"{rel} no longer loads {target}: drop it from RUNTIME_FILE_LOADS"
        assert (REPO_ROOT / target).is_file()


# --- the scanner's own tests: a tiny tree where each rule fires ---------------------------------------------------------

def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_scanner_flags_every_forbidden_pair(tmp_path):
    """One file per (package, forbidden package) pair, plus one allowed file per package."""
    expected = []
    for area, banned in FORBIDDEN.items():
        for top in banned:
            _write(tmp_path, f"{area}/bad_{top}.py", f"import {top}.something\n")
            expected.append(f"{area}/bad_{top}.py: {area} imports {top}")
    _write(tmp_path, "tally_bridge/ok.py", "import httpx\nfrom tally_bridge import client\nfrom . import sibling\n")
    _write(tmp_path, "contract/ok.py", "from tally_bridge.xml_utils import sanitize_xml\nfrom contract import models\n")
    _write(tmp_path, "agent/ok.py", "from tally_bridge import client\nfrom contract import parse\n")
    _write(tmp_path, "backend/ok.py", "from tally_bridge import client\nfrom contract import parse\nimport backend.x\n")
    _write(tmp_path, "probes/ok.py", "from tally_bridge import client\nfrom contract import parse\nimport agent\n")
    _write(tmp_path, "scripts/unscanned.py", "import backend.main\nimport tests\nimport probes\n")
    _write(tmp_path, "unscanned.py", "import backend.main\nimport scripts\n")          # outside every layered package

    assert violations(tmp_path) == sorted(expected)
    assert len(expected) == sum(len(banned) for banned in FORBIDDEN.values()) == 22


def test_scanner_flags_each_import_shape(tmp_path):
    _write(tmp_path, "probes/a.py", "import backend.main\n")
    _write(tmp_path, "probes/b.py", "from scripts import seed_tally_data\n")
    _write(tmp_path, "contract/c.py", "from tests.fixtures import x\n")
    _write(tmp_path, "agent/d.py", "from probes.setup import import_xml\n")
    _write(tmp_path, "agent/e.py", "from ..probes import x\n")                         # relative, climbing to the root
    _write(tmp_path, "contract/sub/f.py", "from ...backend.sync import x\n")
    _write(tmp_path, "backend/sync/g.py", "from tests.sync import fakeb\nfrom backend.config import x\n")
    _write(tmp_path, "backend/api/h.py", "def f():\n    import agent\n")               # inside a function
    _write(tmp_path, "tally_bridge/i.py", "import importlib\nm = importlib.import_module('backend.config')\n")
    _write(tmp_path, "tally_bridge/j.py", "m = __import__('contract')\n")
    _write(tmp_path, "backend/agents/k.py", "from backend.agents import tools\n")      # `backend.agents` is not `agent`
    _write(tmp_path, "backend/.venv/l.py", "import probes\n")                          # a virtualenv is not scanned

    assert violations(tmp_path) == [
        "agent/d.py: agent imports probes",
        "agent/e.py: agent imports probes",
        "backend/api/h.py: backend imports agent",
        "backend/sync/g.py: backend imports tests",
        "contract/c.py: contract imports tests",
        "contract/sub/f.py: contract imports backend",
        "probes/a.py: probes imports backend",
        "probes/b.py: probes imports scripts",
        "tally_bridge/i.py: tally_bridge imports backend",
        "tally_bridge/j.py: tally_bridge imports contract",
    ]


def test_scanner_flags_a_runtime_file_load(tmp_path):
    _write(tmp_path, "tally_bridge/a.py",
           "import importlib.util\nspec = importlib.util.spec_from_file_location('x', 'tests/fixtures/x.py')\n")
    _write(tmp_path, "backend/b.py", "import runpy\nrunpy.run_path('scripts/seed.py')\n")
    _write(tmp_path, "probes/ok.py", "import importlib\nm = importlib.import_module(name)\n")   # not a file load
    _write(tmp_path, "scripts/unscanned.py", "from importlib.util import spec_from_file_location\n"
                                             "spec_from_file_location('x', 'y.py')\n")
    assert runtime_file_loads(tmp_path) == ["backend/b.py", "tally_bridge/a.py"]
    assert violations(tmp_path) == []            # a file load is its own list, never a silent pass of the import rule


def test_resolve_relative_handles_a_non_package_module_and_a_package():
    # agent/client.py (a regular module) is in package "agent"; "from ..probes import x" climbs to the repo root.
    assert imported_modules("from ..probes import x\n", package="agent") == ["probes", "probes.x"]
    # tally_bridge/queries/__init__.py is in package "tally_bridge.queries"; "from .masters import y" stays inside.
    assert imported_modules("from .masters import y\n", package="tally_bridge.queries") == [
        "tally_bridge.queries.masters", "tally_bridge.queries.masters.y"]
    assert _package_for(Path("tally_bridge/queries/__init__.py")) == "tally_bridge.queries"
    assert _package_for(Path("agent/client.py")) == "agent"


# --- probe-internal rules (unchanged from tests/test_isolation.py) ------------------------------------------------------

def test_probe_modules_never_import_write_code():
    offenders = []
    for path in sorted((REPO_ROOT / "probes").glob("p[0-9][0-9]_*.py")):
        modules = imported_modules(path.read_text(encoding="utf-8"), package="probes")
        if any(m.startswith(("probes.setup", "probes.operator")) for m in modules):
            offenders.append(path.name)
    assert offenders == []


def test_company_b_view_is_the_only_bridge_from_probes_to_the_dataset():
    modules = imported_modules((REPO_ROOT / "probes" / "company_b_view.py").read_text(encoding="utf-8"),
                               package="probes")
    reached = [m for m in modules if m.startswith(("probes.setup", "probes.operator"))]
    assert reached, "company_b_view should read the dataset"
    assert all(m.startswith("probes.setup.company_b_data") for m in reached), reached


def test_the_dataset_module_is_pure():
    modules = imported_modules((REPO_ROOT / "probes" / "setup" / "company_b_data.py").read_text(encoding="utf-8"),
                               package="probes.setup")
    # No project package and no HTTP: the dataset is plain data.
    banned = {"agent", "contract", "probes", "backend", "tally_bridge", "httpx"}
    assert [m for m in modules if m.split(".")[0] in banned] == []
