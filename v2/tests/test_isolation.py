"""Part 1 §5 "Code isolation (v2)": v2 never imports current code, and the agent never imports probes."""
from __future__ import annotations

import ast
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOP_LEVEL = {"backend", "scripts", "tests"}


def _package_for(rel: Path) -> str:
    """The dotted package name relative imports in `rel` (a path under the `v2` root) resolve against."""
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
        parts = parts[:-1]
    return ".".join(["v2", *parts]) if parts else "v2"


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    """The absolute dotted name a `from .module import x` (or `..module`, ...) resolves to from `package`."""
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None  # climbs above the root; not resolvable (and not our concern here)
    base = bits[0]
    return f"{base}.{module}" if module else base


def imported_modules(source: str, *, package: str = "v2") -> list[str]:
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
        if ".venv" in path.parts:
            continue
        rel = path.relative_to(root)
        modules = imported_modules(path.read_text(encoding="utf-8"), package=_package_for(rel))
        for top in sorted({m.split(".")[0] for m in modules} & FORBIDDEN_TOP_LEVEL):
            found.append(f"{rel}: imports {top}")
        if rel.parts[:1] == ("agent",) and any(m == "v2.probes" or m.startswith("v2.probes.") for m in modules):
            found.append(f"{rel}: agent imports v2.probes")
    return found


def test_v2_tree_has_no_forbidden_imports():
    assert violations(V2_ROOT) == []


def test_scanner_flags_each_forbidden_shape(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "a.py").write_text("import backend.tally_bridge.client\n")
    (tmp_path / "b.py").write_text("from scripts import seed_tally_data\n")
    (tmp_path / "c.py").write_text("from tests.fixtures import x\n")
    (tmp_path / "agent" / "d.py").write_text("from v2.probes.setup import import_xml\n")
    (tmp_path / "agent" / "e.py").write_text("from v2 import probes\n")
    (tmp_path / "agent" / "f.py").write_text("from ..probes import x\n")
    (tmp_path / "ok.py").write_text("import httpx\nfrom v2.agent.tally import client\nfrom . import sibling\n")

    found = violations(tmp_path)

    assert found == [
        "a.py: imports backend",
        "agent/d.py: agent imports v2.probes",
        "agent/e.py: agent imports v2.probes",
        "agent/f.py: agent imports v2.probes",
        "b.py: imports scripts",
        "c.py: imports tests",
    ]


def test_resolve_relative_handles_a_non_package_module_and_a_package():
    # agent/client.py (a regular module) is in package "v2.agent"; "from ..probes import x" climbs to "v2".
    assert imported_modules("from ..probes import x\n", package="v2.agent") == ["v2.probes", "v2.probes.x"]
    # agent/tally/__init__.py (a package) is itself "v2.agent.tally"; "from .client import y" stays inside it.
    assert imported_modules("from .client import y\n", package="v2.agent.tally") == [
        "v2.agent.tally.client", "v2.agent.tally.client.y"]


def test_probe_modules_never_import_write_code():
    offenders = []
    for path in sorted((V2_ROOT / "probes").glob("p[0-9][0-9]_*.py")):
        modules = imported_modules(path.read_text(encoding="utf-8"))
        if any(m.startswith(("v2.probes.setup", "v2.probes.operator")) for m in modules):
            offenders.append(path.name)
    assert offenders == []


def test_company_b_view_is_the_only_bridge_from_probes_to_the_dataset():
    modules = imported_modules((V2_ROOT / "probes" / "company_b_view.py").read_text(encoding="utf-8"))
    reached = [m for m in modules if m.startswith(("v2.probes.setup", "v2.probes.operator"))]
    assert reached, "company_b_view should read the dataset"
    assert all(m.startswith("v2.probes.setup.company_b_data") for m in reached), reached


def test_the_dataset_module_is_pure():
    modules = imported_modules((V2_ROOT / "probes" / "setup" / "company_b_data.py").read_text(encoding="utf-8"))
    assert [m for m in modules if m.split(".")[0] in {"v2", "httpx"}] == []


def _layer_violations(root: Path) -> list[str]:
    """S1 Global Constraints: cloud never imports agent/probes, agent never imports cloud, contract imports neither."""
    rules = {"cloud": ("v2.agent", "v2.probes"), "agent": ("v2.cloud",), "contract": ("v2.cloud", "v2.agent", "v2.probes")}
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if ".venv" in rel.parts or rel.parts[0] not in rules:
            continue
        mods = imported_modules(path.read_text(encoding="utf-8"), package=_package_for(rel))
        for banned in rules[rel.parts[0]]:
            if any(m == banned or m.startswith(banned + ".") for m in mods):
                found.append(f"{rel}: {rel.parts[0]} imports {banned}")
    return found


def test_layer_rules_hold():
    assert _layer_violations(V2_ROOT) == []


def test_layer_scanner_flags_each_rule(tmp_path):
    for d in ("cloud", "agent", "contract"):
        (tmp_path / d).mkdir()
    (tmp_path / "cloud" / "a.py").write_text("from v2.agent.tally import client\n")
    (tmp_path / "cloud" / "b.py").write_text("import v2.probes.reads\n")
    (tmp_path / "agent" / "c.py").write_text("from v2.cloud import main\n")
    (tmp_path / "contract" / "d.py").write_text("from ..cloud import x\n")
    (tmp_path / "cloud" / "ok.py").write_text("from v2.contract import parse\n")
    assert _layer_violations(tmp_path) == [
        "agent/c.py: agent imports v2.cloud", "cloud/a.py: cloud imports v2.agent",
        "cloud/b.py: cloud imports v2.probes", "contract/d.py: contract imports v2.cloud"]
