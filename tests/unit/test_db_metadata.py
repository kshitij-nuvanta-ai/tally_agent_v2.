"""One declarative ``Base`` for the app tables and the sync tables (v2 merge M4).

``Base.metadata`` must hold all 30 tables however ``backend.db.models`` is first imported — the DB test fixtures
and Alembic's ``env.py`` run ``create_all`` / ``drop_all`` / autogenerate on it, and half a metadata would build
half a schema. Each check runs in a fresh interpreter, because in this process the models are already imported.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT = "; print(len(Base.metadata.tables), sorted(Base.metadata.tables))"

IMPORTS = {
    "from backend.db.models import Base": "from backend.db.models import Base, User, Workspace",
    "import backend.db.models": "import backend.db.models; Base = backend.db.models.Base",
    "importlib": "import importlib; Base = importlib.import_module('backend.db.models').Base",
    "from backend.db import models": "from backend.db import models; Base = models.Base",
    "sync models first": "from backend.db.sync_models import SyncWorkspace; from backend.db.models import Base",
    "one sync module first": "from backend.db.sync_models.parity import ParityRun; from backend.db.models import Base",
    "engine first": "import backend.db.engine; from backend.db.models import Base",
}


@pytest.mark.parametrize("statement", IMPORTS.values(), ids=IMPORTS.keys())
def test_metadata_holds_all_30_tables_however_the_models_are_imported(statement):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "ANTHROPIC_API_KEY": "test-key"}
    out = subprocess.run([sys.executable, "-c", statement + REPORT], cwd=REPO_ROOT, env=env, capture_output=True,
                         text=True, check=True).stdout
    count, _, names = out.partition(" ")
    assert count == "30", out
    for table in ("users", "workspaces", "voucher_entry_revisions", "sync_workspaces", "agent_devices",
                  "tally_vouchers", "parity_lines"):
        assert f"'{table}'" in names


def test_sync_models_are_mapped_on_the_app_base():
    from backend.db import sync_models
    from backend.db.models import Base, User, Workspace
    from backend.db.sync_models import SYNC_TABLES, AgentDevice, SyncWorkspace

    assert len(SYNC_TABLES) == 21 and len(set(SYNC_TABLES)) == 21
    assert issubclass(SyncWorkspace, Base) and issubclass(AgentDevice, Base)
    assert SyncWorkspace.metadata is User.metadata is Base.metadata
    assert set(Base.metadata.tables) - set(SYNC_TABLES) == {
        "users", "workspaces", "conversations", "messages", "usage_logs", "uploaded_files", "voucher_entries",
        "voucher_entry_revisions", "ledger_mappings"}
    assert not hasattr(sync_models, "Base") and not hasattr(sync_models, "V2_TABLES")
    (fk,) = SyncWorkspace.__table__.c.workspace_id.foreign_keys
    assert fk.column.table is Workspace.__table__


def test_the_old_sync_chain_and_config_are_gone():
    for gone in ("backend/sync/alembic", "backend/sync/alembic.ini", "backend/sync/config.py",
                 "backend/db/sync_models/base.py", "backend/db/sync_models/current.py"):
        assert not (REPO_ROOT / gone).exists(), gone
    versions = sorted(p.name for p in (REPO_ROOT / "backend/db/migrations/versions").glob("0*.py"))
    assert versions[-1] == "006_sync_tables.py" and len(versions) == 6
