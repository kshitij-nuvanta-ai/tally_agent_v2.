"""Part 1 §5 "Code isolation (v2)", adapted to the merged layout (v2 merge T1; replaced by tests/test_layers.py in T8).

The rule "v2 must not import backend" is dropped for the sync cloud code: that code now lives INSIDE ``backend``
(``backend/sync``, ``backend/db/sync_models``, five ``backend/api`` modules, two ``backend/utils`` modules), so the
rule is impossible by design. It is also dropped for the former v2 tests, which now live in ``tests/`` and import
``tests.*`` / ``backend.*``. Everything else is still enforced: ``agent``, ``contract`` and ``probes`` import none of
``backend`` / ``scripts`` / ``tests``; the agent never imports probes; probe modules never import write code; and the
layer rules between the sync cloud code, agent and contract hold.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The sync cloud code (was v2/cloud/): directories and single files, relative to the repo root.
CLOUD_PATHS = ("backend/sync", "backend/db/sync_models", "backend/api/agent_auth.py", "backend/api/devices.py",
               "backend/api/sync.py", "backend/api/workspace_sync.py", "backend/api/sync_dependencies.py",
               "backend/utils/device_tokens.py", "backend/utils/rate_limit.py")
CLOUD_MODULES = tuple(p.removesuffix(".py").replace("/", ".") for p in CLOUD_PATHS)
# Top-level packages each scanned area must not import. "backend" is no longer forbidden for the cloud code or the
# tests (see the module docstring); it still is for agent/contract/probes.
FORBIDDEN_TOP_LEVEL = {"agent": {"backend", "scripts", "tests"}, "contract": {"backend", "scripts", "tests"},
                       "probes": {"backend", "scripts", "tests"}, "cloud": {"scripts", "tests"}}


def _area(rel: Path) -> str | None:
    """Which scanned area `rel` (a path under the repo root) belongs to: agent, contract, probes, cloud or None."""
    posix = rel.as_posix()
    if any(posix == c or posix.startswith(c + "/") for c in CLOUD_PATHS):
        return "cloud"
    return rel.parts[0] if rel.parts[0] in ("agent", "contract", "probes") else None


def _is(module: str, banned: str) -> bool:
    return module == banned or module.startswith(banned + ".")


def _package_for(rel: Path) -> str:
    """The dotted package name relative imports in `rel` (a path under the repo root) resolve against."""
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    """The absolute dotted name a `from .module import x` (or `..module`, ...) resolves to from `package`."""
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None  # climbs above the root; not resolvable (and not our concern here)
    base = ".".join(parts[:len(parts) - (level - 1)])
    if base and module:
        return f"{base}.{module}"
    return base or module


def imported_modules(source: str, *, package: str = "") -> list[str]:
    """Absolute imports in `source`; `from a import b` yields both 'a' and 'a.b'.

    A relative import (`from .x import y` / `from ..x import y`) is resolved against `package` (the dotted
    name of the package containing this module) the way Python itself resolves one.
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
    return names


def violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        area = _area(rel)
        if ".venv" in rel.parts or area is None:
            continue
        modules = imported_modules(path.read_text(encoding="utf-8"), package=_package_for(rel))
        for top in sorted({m.split(".")[0] for m in modules} & FORBIDDEN_TOP_LEVEL[area]):
            found.append(f"{rel}: imports {top}")
        if area == "agent" and any(_is(m, "probes") for m in modules):
            found.append(f"{rel}: agent imports probes")
    return found


def test_v2_tree_has_no_forbidden_imports():
    assert violations(REPO_ROOT) == []


def test_scanner_flags_each_forbidden_shape(tmp_path):
    for d in ("agent", "probes", "contract", "backend/sync"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "probes" / "a.py").write_text("import backend.tally_bridge.client\n")
    (tmp_path / "probes" / "b.py").write_text("from scripts import seed_tally_data\n")
    (tmp_path / "contract" / "c.py").write_text("from tests.fixtures import x\n")
    (tmp_path / "agent" / "d.py").write_text("from probes.setup import import_xml\n")
    (tmp_path / "agent" / "e.py").write_text("import probes\n")
    (tmp_path / "agent" / "f.py").write_text("from ..probes import x\n")
    (tmp_path / "backend" / "sync" / "g.py").write_text("from tests.sync import fakeb\nfrom backend.config import x\n")
    (tmp_path / "probes" / "ok.py").write_text("import httpx\nfrom agent.tally import client\nfrom . import sibling\n")
    (tmp_path / "unscanned.py").write_text("import backend.main\nimport scripts\n")   # outside every scanned area

    found = violations(tmp_path)

    assert found == [
        "agent/d.py: agent imports probes",
        "agent/e.py: agent imports probes",
        "agent/f.py: agent imports probes",
        "backend/sync/g.py: imports tests",
        "contract/c.py: imports tests",
        "probes/a.py: imports backend",
        "probes/b.py: imports scripts",
    ]


def test_resolve_relative_handles_a_non_package_module_and_a_package():
    # agent/client.py (a regular module) is in package "agent"; "from ..probes import x" climbs to the repo root.
    assert imported_modules("from ..probes import x\n", package="agent") == ["probes", "probes.x"]
    # agent/tally/__init__.py (a package) is itself "agent.tally"; "from .client import y" stays inside it.
    assert imported_modules("from .client import y\n", package="agent.tally") == [
        "agent.tally.client", "agent.tally.client.y"]


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
    # Was: nothing from "v2" or httpx. "v2" is now these four top-level packages.
    assert [m for m in modules if m.split(".")[0] in {"agent", "contract", "probes", "backend", "httpx"}] == []


def _layer_violations(root: Path) -> list[str]:
    """S1 Global Constraints: cloud never imports agent/probes, agent never imports cloud, contract imports neither.
    Task 12: cloud never imports the test-support modules either (``tests``: ``realdata``, ``fakeb``, ...).
    "cloud" is the sync cloud code listed in ``CLOUD_PATHS`` / ``CLOUD_MODULES`` (was ``v2.cloud``)."""
    rules = {"cloud": ("agent", "probes", "tests"), "agent": CLOUD_MODULES,
             "contract": (*CLOUD_MODULES, "agent", "probes")}
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        area = _area(rel)
        if ".venv" in rel.parts or area not in rules:
            continue
        mods = imported_modules(path.read_text(encoding="utf-8"), package=_package_for(rel))
        for banned in rules[area]:
            if any(_is(m, banned) for m in mods):
                found.append(f"{rel}: {area} imports {banned}")
    return found


def test_layer_rules_hold():
    assert _layer_violations(REPO_ROOT) == []


def test_layer_scanner_flags_each_rule(tmp_path):
    for d in ("backend/sync", "backend/api", "backend/db/sync_models", "agent", "contract"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "backend" / "sync" / "a.py").write_text("from agent.tally import client\n")
    (tmp_path / "backend" / "api" / "sync.py").write_text("import probes.reads\n")
    (tmp_path / "agent" / "c.py").write_text("from backend.sync import app\n")
    (tmp_path / "contract" / "d.py").write_text("from ..backend.sync import x\n")
    (tmp_path / "backend" / "db" / "sync_models" / "e.py").write_text("from tests.sync.fakeb import FakeB\n")
    (tmp_path / "backend" / "sync" / "ok.py").write_text("from contract import parse\nfrom backend.config import x\n")
    (tmp_path / "backend" / "api" / "chat.py").write_text("import agent\n")   # not sync cloud code: not scanned
    assert _layer_violations(tmp_path) == [
        "agent/c.py: agent imports backend.sync", "backend/api/sync.py: cloud imports probes",
        "backend/db/sync_models/e.py: cloud imports tests", "backend/sync/a.py: cloud imports agent",
        "contract/d.py: contract imports backend.sync"]
