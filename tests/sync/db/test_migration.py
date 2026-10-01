"""The one Alembic chain and the sync tables (v2 merge M4, M5, M6; merge spec §5).

Every test that runs a migration does so in a throwaway database (``<test db>_mig_<random>``), created and
dropped here, so no other test's tables are ever touched. The server is the one ``TEST_DATABASE_URL`` points at.

Covered: the three rows of spec §5 (fresh database, adopting the old ``v2_001`` chain — also one built by the
original ``v2_001`` file from git history — refusing any other state of the old chain, a drifted schema, or sync
tables with no old version table), ``downgrade``, the columns / money types / partial and covering indexes of the sync tables,
that the schema ``alembic upgrade head`` builds equals the schema ``Base.metadata`` describes, and the sync
harness's own session teardown.
"""
import asyncio
import importlib.util
import subprocess
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db.models import Base
from backend.db.sync_models import SYNC_TABLES
from tests.sync.conftest import TEST_DB, TEST_EMAIL_DOMAIN, requires_db, restore_as_found

pytestmark = requires_db

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_TABLES = ("users", "workspaces", "conversations", "messages", "usage_logs", "uploaded_files", "voucher_entries",
              "voucher_entry_revisions", "ledger_mappings")
OLD_VERSION_TABLE_DDL = ("CREATE TABLE alembic_version_v2 (version_num varchar(32) NOT NULL, "
                         "CONSTRAINT alembic_version_v2_pkc PRIMARY KEY (version_num))")


# --- throwaway databases + Alembic ------------------------------------------------------------------------------

async def _admin(sql: str) -> None:
    eng = create_async_engine(TEST_DB, isolation_level="AUTOCOMMIT")
    async with eng.connect() as c:
        await c.execute(text(sql))
    await eng.dispose()


@pytest.fixture
def scratch_db():
    """Factory for empty throwaway databases next to the test database; every one is dropped afterwards."""
    base = make_url(TEST_DB)
    names: list[str] = []

    def _make() -> str:
        name = f"{base.database}_mig_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
        names.append(name)
        return base.set(database=name).render_as_string(hide_password=False)

    yield _make
    for name in names:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "db" / "migrations"))
    cfg.attributes["url"] = url   # env.py reads this before DATABASE_URL / .env, so nothing else is hit
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(_alembic_config(url), revision)


async def _run(url: str, fn):
    eng = create_async_engine(url)
    async with eng.begin() as c:
        out = await fn(c)
    await eng.dispose()
    return out


def _exec(url: str, *statements: str) -> None:
    async def go(c):
        for sql in statements:
            await c.execute(text(sql))
    asyncio.run(_run(url, go))


def _scalar(url: str, sql: str):
    async def go(c):
        return (await c.execute(text(sql))).scalar()
    return asyncio.run(_run(url, go))


def _inspect(url: str, fn):
    async def go(c):
        return await c.run_sync(lambda sync: fn(inspect(sync)))
    return asyncio.run(_run(url, go))


def _tables(url: str) -> set[str]:
    return _inspect(url, lambda i: set(i.get_table_names()))


_CATALOG = {
    # name, type, nullability and default of every column
    "column": """
        SELECT c.relname, a.attname,
               format_type(a.atttypid, a.atttypmod) || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
                   || COALESCE(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped""",
    # primary key, unique, foreign key (with ON DELETE) and check constraints, by name
    "constraint": """
        SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'""",
    # every index by name: columns and order, uniqueness, INCLUDE columns, partial WHERE
    "index": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
}


def schema_snapshot(url: str) -> dict[tuple[str, str, str], str]:
    """``{(table, "column" | "constraint" | "index", name): definition}`` for every table in ``public`` except
    the Alembic version tables, read from the Postgres catalog. Two databases with equal snapshots have the same
    tables, columns (type, nullability, default), constraints and indexes. Column order is not part of it."""
    async def go(c):
        out = {}
        for kind, sql in _CATALOG.items():
            for table, name, definition in await c.execute(text(sql)):
                if not table.startswith("alembic_version"):
                    out[(table, kind, name)] = definition
        return out
    return asyncio.run(_run(url, go))


def _row_counts(url: str, tables) -> dict[str, int]:
    async def go(c):
        return {t: (await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() for t in tables}
    return asyncio.run(_run(url, go))


def _seed(url: str) -> None:
    """One user, one workspace and a few sync rows, so a test can prove rows survive."""
    uid, wid = uuid.uuid4(), uuid.uuid4()

    async def go(c):
        await c.execute(text("INSERT INTO users (id, email, password_hash, name) VALUES (:i, :e, 'x', 'Owner')"),
                        {"i": uid, "e": f"{uid.hex[:8]}@example.com"})
        await c.execute(text("INSERT INTO workspaces (id, user_id, name) VALUES (:i, :u, 'W')"),
                        {"i": wid, "u": uid})
        await c.execute(text("INSERT INTO sync_workspaces (workspace_id, tally_company_guid, tally_company_name, "
                             "books_from, sync_state) VALUES (:w, 'guid-1', 'Acme', DATE '2024-04-01', 'bound')"),
                        {"w": wid})
        for n in range(3):
            await c.execute(text("INSERT INTO tally_groups (workspace_id, guid, alter_id, name, parent_name) "
                                 "VALUES (:w, :g, :a, :n, 'Primary')"),
                            {"w": wid, "g": f"g-{n}", "a": n, "n": f"Group {n}"})
    asyncio.run(_run(url, go))


def _as_if_migrated_by_the_old_chain(url: str, old_revisions: tuple[str, ...] = ("v2_001",)) -> None:
    """Put an empty database in the state the dev databases are in today: the app chain at ``005``, the 21 sync
    tables present, and the old chain's own version table. The tables are built by ``006`` itself and the version
    rows rewound — the old ``v2_001`` revision is deleted, and its body is ``006``'s body unchanged (a
    ``pg_dump`` of both was compared when ``006`` was written)."""
    upgrade(url, "head")
    _exec(url, "UPDATE alembic_version SET version_num = '005'", OLD_VERSION_TABLE_DDL,
          *[f"INSERT INTO alembic_version_v2 (version_num) VALUES ('{r}')" for r in old_revisions])


OLD_V2_001_IN_GIT = "99ced44:v2/cloud/alembic/versions/v2_001_sync_tables.py"


def _migrated_by_the_real_old_chain(url: str, tmp_path: Path) -> None:
    """Like ``_as_if_migrated_by_the_old_chain``, but the 21 tables are built by the ORIGINAL ``v2_001`` revision
    file, read out of git history and run with Alembic's operations context — not by ``006``. Skips when that
    commit is not in this checkout (a shallow clone, or a source archive with no ``.git``)."""
    try:
        shown = subprocess.run(["git", "-C", str(REPO_ROOT), "show", OLD_V2_001_IN_GIT], capture_output=True,
                               text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"cannot run git to read the old v2_001 revision: {type(exc).__name__}")
    if shown.returncode != 0 or "def upgrade()" not in shown.stdout:
        pytest.skip(f"`git show {OLD_V2_001_IN_GIT}` failed (commit not in this checkout): "
                    f"{shown.stderr.strip()[:200]}")
    path = tmp_path / "v2_001_sync_tables.py"
    path.write_text(shown.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("old_v2_001_sync_tables", path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    assert old.revision == "v2_001" and len(old.V2_TABLES) == 21

    upgrade(url, "005")

    def run_old_upgrade(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            old.upgrade()

    asyncio.run(_run(url, lambda c: c.run_sync(run_old_upgrade)))
    _exec(url, OLD_VERSION_TABLE_DDL, "INSERT INTO alembic_version_v2 (version_num) VALUES ('v2_001')")


@pytest.fixture(scope="module")
def migrated_db():
    """One empty throwaway database upgraded to head, shared by this module's read-only tests (spec §5 row 1)."""
    base = make_url(TEST_DB)
    name = f"{base.database}_mig_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    try:
        upgrade(url, "head")
        yield url
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


# --- the sync tables' contract (spec §4) ------------------------------------------------------------------------

# Common columns every master row carries (spec §4.1): id, workspace_id, guid, alter_id, is_deleted, raw,
# first_seen_at, updated_at, created_at, name.
_MASTER_COMMON = {
    "id", "workspace_id", "guid", "alter_id", "is_deleted", "raw", "first_seen_at", "updated_at", "created_at",
    "name",
}

EXPECTED_COLUMNS = {   # spec §4 — the contract; an extra or missing column fails
    # --- Bookkeeping (§4.2) ---
    "sync_workspaces": {"workspace_id", "tally_company_guid", "tally_company_name", "previous_company_guids",
        "books_from", "base_currency_name", "sync_state", "restore_reason", "active_device_id", "cursor_alt_vch_id",
        "cursor_alt_mst_id", "cursor_set_at", "last_synced_at", "caught_up_at", "last_seen_at", "last_heartbeat",
        "relink_prompt", "oldest_available_fy", "oldest_complete_fy", "backfill_state", "backfill_percent",
        "last_parity", "ladder", "tb_imbalance_baseline", "quarantine_count", "storage_estimate_bytes",
        "storage_alert", "bound_at", "updated_at"},
    "agent_devices": {"id", "user_id", "workspace_id", "device_name", "agent_version", "refresh_hash",
        "refresh_prev_hash", "refresh_expires_at", "last_login_at", "last_seen_at", "is_active", "revoked_at",
        "revoke_reason", "created_at"},
    "sync_runs": {"id", "workspace_id", "device_id", "kind", "scope", "command_id", "status", "progress_done",
        "progress_total", "batches_declared", "counters_at_start", "cursor_after", "error_code", "started_at",
        "finished_at", "created_at"},
    "sync_batches": {"id", "workspace_id", "run_id", "batch_id", "request_sha256", "object_count", "status",
        "response", "received_at", "created_at"},
    "sync_quarantine": {"id", "workspace_id", "kind", "guid", "code", "detail", "voucher_date", "first_seen_at",
        "last_seen_at", "times_seen", "resolved_at", "created_at"},
    "sync_commands": {"id", "workspace_id", "type", "params", "requested_by", "status", "created_at",
        "delivered_at", "done_at"},
    "sync_fy_coverage": {"id", "workspace_id", "fy_start", "fy_end", "state", "months_done", "months_complete",
        "months_total", "completed_at", "created_at"},

    # --- Masters (§4.3) ---
    "tally_currencies": _MASTER_COMMON | {"mailing_name", "expanded_symbol", "decimal_places", "is_base"},
    "tally_groups": _MASTER_COMMON | {"parent_name", "parent_guid", "primary_group", "nature", "is_revenue",
        "affects_gross_profit", "is_deemed_positive", "reserved_name", "derivation_warning"},
    "tally_voucher_types": _MASTER_COMMON | {"parent_name", "parent_guid", "reserved_name", "base_type"},
    "tally_ledgers": {"id", "workspace_id", "guid", "alter_id", "is_deleted", "raw", "first_seen_at", "updated_at",
        "created_at", "name", "parent_name", "group_guid", "currency_name", "is_forex", "is_bill_wise", "tax_type",
        "gst_duty_head", "opening_balance", "closing_balance", "opening_fx_amount", "opening_fx_rate",
        "closing_fx_amount", "closing_fx_rate", "fx_currency", "balance_source", "balance_captured_at",
        "balance_text"},
    "tally_stock_groups": _MASTER_COMMON | {"parent_name", "parent_guid"},
    "tally_units": _MASTER_COMMON | {"is_simple", "base_units", "additional_units", "conversion"},
    "tally_stock_items": _MASTER_COMMON | {"parent_name", "parent_guid", "base_unit_name", "base_unit_guid",
        "closing_qty", "closing_qty_text", "closing_value", "balance_captured_at"},

    # --- Vouchers (§4.5) ---
    "tally_vouchers": {"id", "workspace_id", "guid", "master_id", "alter_id", "date", "effective_date",
        "voucher_type_name", "voucher_type_guid", "base_type", "voucher_number", "reference", "party_ledger_name",
        "party_ledger_guid", "narration", "is_cancelled", "is_optional", "is_post_dated", "is_invoice",
        "has_forex", "is_deleted", "deleted_at", "raw", "run_id", "first_seen_at", "updated_at", "created_at"},
    "tally_voucher_ledger_lines": {"id", "voucher_id", "workspace_id", "line_no", "ledger_name", "ledger_guid",
        "amount", "is_deemed_positive", "fx_currency", "fx_amount", "fx_rate", "voucher_date", "countable",
        "created_at"},
    "tally_voucher_inventory_lines": {"id", "voucher_id", "workspace_id", "line_no", "stock_item_name",
        "stock_item_guid", "actual_qty", "billed_qty", "qty_text", "rate", "rate_text", "amount", "fx_currency",
        "fx_amount", "fx_rate", "is_deemed_positive", "voucher_date", "created_at"},
    "tally_bill_allocations": {"id", "voucher_id", "workspace_id", "ledger_line_no", "ledger_guid", "bill_name",
        "bill_type", "amount", "fx_currency", "fx_amount", "fx_rate", "credit_period_text", "credit_period_days",
        "bill_date", "voucher_date", "created_at"},

    # --- Snapshots (§4.6) ---
    "tally_report_snapshots": {"id", "workspace_id", "report_type", "from_date", "as_on_date", "purpose",
        "request_flags", "captured_at", "counters", "cells", "rows", "row_count", "synthetic_rows", "imbalance",
        "created_at"},

    # --- Parity (§4.7) ---
    "parity_runs": {"id", "workspace_id", "sync_run_id", "as_on_date", "rung", "scope", "verified_from",
        "anchor_as_on", "status", "abort_reason", "lines_compared", "mismatch_count", "max_abs_diff", "net_diff",
        "forex_unrealised_total", "tb_imbalance", "counters_before", "counters_after", "remediation", "started_at",
        "finished_at", "created_at"},
    "parity_lines": {"id", "workspace_id", "run_id", "scope", "guid", "name", "our_amount", "tally_amount", "diff",
        "verdict", "cause", "remediation_status", "unrealised_diff", "our_fx_amount", "tally_fx_amount",
        "as_on_date", "created_at"},
}


def test_expected_columns_cover_every_sync_table():
    assert set(EXPECTED_COLUMNS) == set(SYNC_TABLES) and len(SYNC_TABLES) == 21


def test_every_table_has_exactly_the_spec_columns(migrated_db):
    cols = _inspect(migrated_db, lambda i: {t: {c["name"] for c in i.get_columns(t)} for t in SYNC_TABLES})
    assert cols == EXPECTED_COLUMNS


def test_money_columns_are_numeric_18_2_never_float(migrated_db):
    def check(i):
        bad = []
        for t in SYNC_TABLES:
            for c in i.get_columns(t):
                if "FLOAT" in str(c["type"]).upper() or "REAL" in str(c["type"]).upper():
                    bad.append((t, c["name"]))
        return bad, {c["name"]: str(c["type"]) for c in i.get_columns("tally_voucher_ledger_lines")}
    bad, line_types = _inspect(migrated_db, check)
    assert bad == [] and line_types["amount"] == "NUMERIC(18, 2)" and line_types["fx_rate"] == "NUMERIC(18, 6)"


def test_one_active_device_partial_unique_index(migrated_db):
    idx = _inspect(migrated_db, lambda i: i.get_indexes("agent_devices"))
    uniq = [x for x in idx if x["unique"] and x["column_names"] == ["workspace_id"]]
    assert uniq and "is_active" in str(uniq[0].get("dialect_options", {}).get("postgresql_where", ""))


def test_covering_line_index_exists(migrated_db):
    idx = _inspect(migrated_db, lambda i: i.get_indexes("tally_voucher_ledger_lines"))
    assert any(x["column_names"] == ["workspace_id", "ledger_guid", "voucher_date"] for x in idx)
    definition = schema_snapshot(migrated_db)[("tally_voucher_ledger_lines", "index", "ix_lines_cover")]
    assert "INCLUDE (amount, fx_amount)" in definition and definition.endswith("WHERE countable")


def test_sync_foreign_keys_point_at_the_real_users_and_workspaces(migrated_db):
    """M4: one ``Base`` — the sync models' foreign keys resolve to the app's own ``User`` / ``Workspace`` tables
    (no read-only stand-in tables), in the metadata and in the migrated database alike."""
    from backend.db.models import User, Workspace

    tables = Base.metadata.tables
    for name in SYNC_TABLES:
        (fk,) = tables[name].c.workspace_id.foreign_keys
        assert fk.column.table is Workspace.__table__, name
    (fk,) = tables["agent_devices"].c.user_id.foreign_keys
    assert fk.column.table is User.__table__
    assert not any(t.info.get("v2_readonly") for t in tables.values())

    snap = schema_snapshot(migrated_db)
    fks = {k[0]: v for k, v in snap.items() if k[1] == "constraint" and k[2].endswith("_workspace_id_fkey")}
    assert set(SYNC_TABLES) <= set(fks)
    assert all(fks[t].endswith("REFERENCES workspaces(id)") for t in SYNC_TABLES)
    assert snap[("agent_devices", "constraint", "agent_devices_user_id_fkey")].endswith("REFERENCES users(id)")


# --- models equal the migrated schema ---------------------------------------------------------------------------

# Differences between what migrations 001–005 build and what the app's own nine models declare. They predate the
# merge (the app models carry Python-side defaults where the migrations put server defaults, and two names differ)
# and are not the merge's to change. They are pinned here exactly: a new difference, or one of these going away,
# fails the test. Each value is (migrated, metadata); ``None`` = absent. The 21 sync tables have no entry: they
# must match exactly (M6).
_NOW = "timestamp with time zone DEFAULT now()"
_NOT_NULL_TS = "timestamp with time zone NOT NULL"
KNOWN_APP_TABLE_DRIFT = {
    **{(t, "column", "created_at"): (_NOW, _NOT_NULL_TS) for t in APP_TABLES},
    **{(t, "column", "updated_at"): (_NOW, _NOT_NULL_TS)
       for t in ("users", "workspaces", "conversations", "voucher_entries")},
    ("users", "column", "is_active"): ("boolean DEFAULT true", "boolean NOT NULL"),
    ("workspaces", "column", "is_deleted"): ("boolean DEFAULT false", "boolean NOT NULL"),
    ("conversations", "column", "is_deleted"): ("boolean DEFAULT false", "boolean NOT NULL"),
    ("workspaces", "column", "agent_type"): ("character varying(50) NOT NULL DEFAULT 'tally'::character varying",
                                             "character varying(50) NOT NULL"),
    ("workspaces", "column", "config"): ("jsonb NOT NULL DEFAULT '{}'::jsonb", "jsonb NOT NULL"),
    ("workspaces", "column", "memory"): ("jsonb NOT NULL DEFAULT '{}'::jsonb", "jsonb NOT NULL"),
    ("uploaded_files", "column", "status"): ("character varying(30) NOT NULL DEFAULT 'uploaded'::character varying",
                                             "character varying(30) NOT NULL"),
    ("voucher_entries", "column", "status"): ("character varying(20) NOT NULL DEFAULT 'draft'::character varying",
                                              "character varying(20) NOT NULL"),
    ("ledger_mappings", "column", "confidence"): ("double precision NOT NULL DEFAULT 0.8",
                                                  "double precision NOT NULL"),
    ("ledger_mappings", "column", "use_count"): ("integer NOT NULL DEFAULT 0", "integer NOT NULL"),
    # Migration 005 names the file_id foreign key and index itself; the model leaves both to the default names.
    ("voucher_entry_revisions", "constraint", "fk_voucher_revisions_file_id"): (
        "FOREIGN KEY (file_id) REFERENCES uploaded_files(id)", None),
    ("voucher_entry_revisions", "constraint", "voucher_entry_revisions_file_id_fkey"): (
        None, "FOREIGN KEY (file_id) REFERENCES uploaded_files(id)"),
    ("voucher_entry_revisions", "index", "ix_voucher_revisions_file_id"): (
        "CREATE INDEX ix_voucher_revisions_file_id ON public.voucher_entry_revisions USING btree (file_id)", None),
    ("voucher_entry_revisions", "index", "ix_voucher_entry_revisions_file_id"): (
        None,
        "CREATE INDEX ix_voucher_entry_revisions_file_id ON public.voucher_entry_revisions USING btree (file_id)"),
}


def test_migrated_schema_equals_model_metadata(migrated_db, scratch_db):
    """``alembic upgrade head`` on an empty database and ``Base.metadata.create_all`` on another build the same
    schema: same tables, and for each the same columns (type, nullability, default), the same primary key,
    unique and foreign key constraints, and the same indexes (unique, partial and covering ones included), all
    by name. Replaces the old ``compare_metadata`` check, which covered only the sync tables and did not compare
    defaults or foreign keys."""
    from_models = scratch_db()
    asyncio.run(_run(from_models, lambda c: c.run_sync(Base.metadata.create_all)))

    assert _tables(migrated_db) - {"alembic_version"} == _tables(from_models) == set(Base.metadata.tables)
    assert len(Base.metadata.tables) == 30 and set(SYNC_TABLES) | set(APP_TABLES) == set(Base.metadata.tables)

    migrated, modelled = schema_snapshot(migrated_db), schema_snapshot(from_models)
    diff = {k: (migrated.get(k), modelled.get(k))
            for k in migrated.keys() | modelled.keys() if migrated.get(k) != modelled.get(k)}

    sync_diff = {k: v for k, v in diff.items() if k[0] in SYNC_TABLES}
    assert sync_diff == {}, f"sync models vs migration 006 mismatch: {sync_diff}"
    assert {k for k in migrated if k[0] in SYNC_TABLES} and {k[0] for k in migrated} == set(Base.metadata.tables)
    assert {k: v for k, v in diff.items() if k[0] not in SYNC_TABLES} == KNOWN_APP_TABLE_DRIFT


# --- spec §5, the three rows + downgrade ------------------------------------------------------------------------

def test_fresh_database_gets_all_30_tables_on_the_one_chain(migrated_db):
    """§5 row 1 (replaces the old "own version table ``alembic_version_v2``" check): an empty database ends with
    the 9 app tables and the 21 sync tables, one version table at ``006``, and no ``alembic_version_v2``."""
    assert _tables(migrated_db) == set(APP_TABLES) | set(SYNC_TABLES) | {"alembic_version"}
    assert _scalar(migrated_db, "SELECT version_num FROM alembic_version") == "006"
    assert _scalar(migrated_db, "SELECT count(*) FROM alembic_version") == 1
    assert _scalar(migrated_db, "SELECT to_regclass('public.alembic_version_v2')") is None


def test_fresh_database_at_005_gets_the_sync_tables_and_keeps_its_rows(scratch_db):
    """§5 row 1, the production path for a database that never ran the old chain: it sits at ``005`` with data."""
    url = scratch_db()
    upgrade(url, "005")
    assert _tables(url) == set(APP_TABLES) | {"alembic_version"}
    _exec(url, "INSERT INTO users (id, email, password_hash, name) "
               "VALUES (gen_random_uuid(), 'keep@example.com', 'x', 'Keep')")
    upgrade(url, "head")
    assert set(SYNC_TABLES) <= _tables(url)
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "006"
    assert _scalar(url, "SELECT count(*) FROM users WHERE email = 'keep@example.com'") == 1


def test_database_with_the_old_chain_at_v2_001_is_adopted(scratch_db, migrated_db):
    """§5 row 2: nothing is created, no row is lost, ``alembic_version_v2`` is dropped, the chain moves to
    ``006`` — and the result is the same schema a fresh database gets."""
    url = scratch_db()
    _as_if_migrated_by_the_old_chain(url)
    _seed(url)
    tables = [*APP_TABLES, *SYNC_TABLES]
    schema_before, rows_before = schema_snapshot(url), _row_counts(url, tables)
    assert rows_before["tally_groups"] == 3 and rows_before["sync_workspaces"] == 1

    upgrade(url, "head")

    assert schema_snapshot(url) == schema_before == schema_snapshot(migrated_db)
    assert _row_counts(url, tables) == rows_before
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "006"
    assert _scalar(url, "SELECT to_regclass('public.alembic_version_v2')") is None
    assert _tables(url) == set(tables) | {"alembic_version"}


@pytest.mark.parametrize("old_revisions,found", [
    (("v2_002",), "'v2_002'"),
    (("v2_000",), "'v2_000'"),
    ((), r"no revision \(empty table\)"),
    (("v2_001", "v2_002"), "'v2_001', 'v2_002'"),
])
def test_old_chain_at_any_other_revision_is_refused_and_nothing_changes(scratch_db, old_revisions, found):
    """§5 row 3: fail loudly, never guess — and leave the database exactly as it was."""
    url = scratch_db()
    _as_if_migrated_by_the_old_chain(url, old_revisions)
    _seed(url)
    tables = [*APP_TABLES, *SYNC_TABLES]
    schema_before, rows_before = schema_snapshot(url), _row_counts(url, tables)

    with pytest.raises(RuntimeError, match=f"alembic_version_v2 exists and holds {found}; expected exactly 'v2_001'"):
        upgrade(url, "head")

    assert schema_snapshot(url) == schema_before and _row_counts(url, tables) == rows_before
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"
    assert _scalar(url, "SELECT count(*) FROM alembic_version_v2") == len(old_revisions)


def test_old_chain_refusal_rolls_back_the_whole_upgrade_run(scratch_db):
    """Row 3 on a database the app chain never touched: 001–005 run in the same transaction as 006, so the
    refusal leaves no app table and no version row behind either."""
    url = scratch_db()
    _exec(url, OLD_VERSION_TABLE_DDL, "INSERT INTO alembic_version_v2 (version_num) VALUES ('v2_009')")
    with pytest.raises(RuntimeError, match="expected exactly 'v2_001'"):
        upgrade(url, "head")
    assert _tables(url) <= {"alembic_version_v2", "alembic_version"}
    assert _scalar(url, "SELECT version_num FROM alembic_version_v2") == "v2_009"


def test_adopt_refuses_when_a_sync_table_is_missing(scratch_db):
    """``alembic_version_v2`` says ``v2_001`` but a table it should have created is gone: not a state to adopt."""
    url = scratch_db()
    _as_if_migrated_by_the_old_chain(url)
    _exec(url, "DROP TABLE parity_lines")
    schema_before = schema_snapshot(url)
    with pytest.raises(RuntimeError, match="these are missing: parity_lines"):
        upgrade(url, "head")
    assert schema_snapshot(url) == schema_before
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"
    assert _scalar(url, "SELECT version_num FROM alembic_version_v2") == "v2_001"


def test_database_built_by_the_original_v2_001_revision_is_adopted(scratch_db, migrated_db, tmp_path):
    """§5 row 2 against the REAL old chain (review I1): the sync tables are created by the original ``v2_001``
    file from git history, not by ``006``. Adopt accepts them, keeps the rows, and the schema equals a fresh
    database's."""
    url = scratch_db()
    _migrated_by_the_real_old_chain(url, tmp_path)
    _seed(url)
    tables = [*APP_TABLES, *SYNC_TABLES]
    schema_before, rows_before = schema_snapshot(url), _row_counts(url, tables)
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"
    assert rows_before["tally_groups"] == 3

    upgrade(url, "head")

    assert schema_snapshot(url) == schema_before == schema_snapshot(migrated_db)
    assert _row_counts(url, tables) == rows_before
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "006"
    assert _scalar(url, "SELECT to_regclass('public.alembic_version_v2')") is None


@pytest.mark.parametrize("drift,message", [
    # a column dropped
    (("ALTER TABLE tally_groups DROP COLUMN nature",), r"tally_groups\.nature: column is missing"),
    # a column added
    (("ALTER TABLE parity_lines ADD COLUMN extra_note text",), r"parity_lines\.extra_note: unexpected column"),
    # a type changed — to another type, and to another precision of the same type
    (("ALTER TABLE tally_vouchers ALTER COLUMN narration TYPE varchar(50)",),
     r"tally_vouchers\.narration: type is character varying\(50\), expected text"),
    (("ALTER TABLE tally_ledgers ALTER COLUMN opening_balance TYPE numeric(18,4)",),
     r"tally_ledgers\.opening_balance: type is numeric\(18,4\), expected numeric\(18,2\)"),
    (("ALTER TABLE tally_voucher_ledger_lines ALTER COLUMN amount TYPE double precision",),
     r"tally_voucher_ledger_lines\.amount: type is double precision, expected numeric\(18,2\)"),
    # nullability changed, both directions
    (("ALTER TABLE sync_commands ALTER COLUMN requested_by DROP NOT NULL",),
     r"sync_commands\.requested_by: is nullable, expected NOT NULL"),
    (("ALTER TABLE tally_vouchers ALTER COLUMN narration SET NOT NULL",),
     r"tally_vouchers\.narration: is NOT NULL, expected nullable"),
    # several at once, in different tables: every one is named
    (("ALTER TABLE tally_groups DROP COLUMN nature", "ALTER TABLE parity_lines ADD COLUMN extra_note text"),
     r"tally_groups\.nature: column is missing.*parity_lines\.extra_note: unexpected column"
     r"|parity_lines\.extra_note: unexpected column.*tally_groups\.nature: column is missing"),
])
def test_adopt_refuses_a_drifted_schema_and_nothing_changes(scratch_db, drift, message):
    """Review I1: ``alembic_version_v2`` says ``v2_001`` and all 21 tables exist, but a column was altered by
    hand. That is not the schema ``v2_001`` built, so it is not stamped ``006``: the upgrade stops, names the
    table and column, and leaves the database exactly as it was."""
    url = scratch_db()
    _as_if_migrated_by_the_old_chain(url)
    _exec(url, *drift)
    _seed(url)
    tables = [*APP_TABLES, *SYNC_TABLES]
    schema_before, rows_before = schema_snapshot(url), _row_counts(url, tables)

    with pytest.raises(RuntimeError, match=message) as ei:
        upgrade(url, "head")
    assert "Migration 006" in str(ei.value) and "Nothing was changed" in str(ei.value)

    assert schema_snapshot(url) == schema_before and _row_counts(url, tables) == rows_before
    assert rows_before["tally_groups"] == 3 and rows_before["sync_workspaces"] == 1
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"
    assert _scalar(url, "SELECT version_num FROM alembic_version_v2") == "v2_001"


@pytest.mark.parametrize("kept", [
    tuple(SYNC_TABLES),                                  # all 21 present
    ("tally_groups", "sync_commands"),                   # a partial restore: only some present
    ("tally_report_snapshots",),
])
def test_sync_tables_without_the_old_version_table_are_refused(scratch_db, kept):
    """Review I2: no ``alembic_version_v2``, yet sync tables are already there (the version table was dropped, or
    a partial restore). Not a fresh database and not an adoptable one: refuse before creating anything, list the
    tables found, and leave everything as it was — instead of dying on a raw ``DuplicateTableError``."""
    url = scratch_db()
    upgrade(url, "head")
    _seed(url)
    drop = [t for t in SYNC_TABLES if t not in kept]
    _exec(url, "UPDATE alembic_version SET version_num = '005'",
          *([f"DROP TABLE {', '.join(drop)} CASCADE"] if drop else []))
    assert _scalar(url, "SELECT to_regclass('public.alembic_version_v2')") is None
    tables = [*APP_TABLES, *kept]
    schema_before, rows_before = schema_snapshot(url), _row_counts(url, tables)

    with pytest.raises(RuntimeError, match="alembic_version_v2 does not exist, but") as ei:
        upgrade(url, "head")
    message = str(ei.value)
    assert "Migration 006" in message and "Nothing was changed" in message
    assert f"{len(kept)} of the 21 sync tables already exist" in message
    assert all(t in message for t in kept) and not any(t in message for t in drop)

    assert _tables(url) == set(tables) | {"alembic_version"}
    assert schema_snapshot(url) == schema_before and _row_counts(url, tables) == rows_before
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"


def test_downgrade_drops_the_21_sync_tables_and_keeps_the_app_tables(scratch_db):
    """Replaces the old chain's down/up round trip: ``006`` down removes exactly the sync tables, the app's
    tables and rows stay, and up again rebuilds the same schema."""
    url = scratch_db()
    upgrade(url, "head")
    _seed(url)
    schema_at_head = schema_snapshot(url)

    downgrade(url, "005")
    assert _tables(url) == set(APP_TABLES) | {"alembic_version"}
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "005"
    assert _row_counts(url, ["users", "workspaces"]) == {"users": 1, "workspaces": 1}
    assert {k: v for k, v in schema_at_head.items() if k[0] in APP_TABLES} == schema_snapshot(url)

    upgrade(url, "head")
    assert schema_snapshot(url) == schema_at_head
    assert _row_counts(url, ["users", "tally_groups"]) == {"users": 1, "tally_groups": 0}


def test_downgrade_after_adopting_also_drops_the_sync_tables(scratch_db):
    url = scratch_db()
    _as_if_migrated_by_the_old_chain(url)
    upgrade(url, "head")
    downgrade(url, "005")
    assert _tables(url) == set(APP_TABLES) | {"alembic_version"}


def test_chain_runs_down_to_base_and_up_again(scratch_db):
    url = scratch_db()
    upgrade(url, "head")
    downgrade(url, "base")
    assert _tables(url) == {"alembic_version"} and _scalar(url, "SELECT count(*) FROM alembic_version") == 0
    upgrade(url, "head")
    assert _tables(url) == set(APP_TABLES) | set(SYNC_TABLES) | {"alembic_version"}


# --- the sync harness's session teardown ------------------------------------------------------------------------

def _seed_users(url: str, *emails: str) -> None:
    async def go(c):
        for email in emails:
            uid = uuid.uuid4()
            await c.execute(text("INSERT INTO users (id, email, password_hash, name, is_active, created_at, "
                                 "updated_at) VALUES (:i, :e, 'x', 'U', true, now(), now())"), {"i": uid, "e": email})
            await c.execute(text("INSERT INTO workspaces (id, user_id, name, agent_type, config, memory, is_deleted, "
                                 "created_at, updated_at) VALUES (gen_random_uuid(), :u, 'W', 'tally', '{}', '{}', "
                                 "false, now(), now())"), {"u": uid})
    asyncio.run(_run(url, go))


def test_harness_teardown_leaves_the_database_as_found(scratch_db):
    """Exercises the harness's real teardown (``restore_as_found``) on both branches it can take. (The old test
    checked that teardown ran the v2 chain down, dropped ``alembic_version_v2`` and dropped the stand-in
    ``users`` / ``workspaces``; there is no v2 chain and there are no stand-ins now.)

    - Branch A — the database held none of the tables (the shared test database): every table the harness
      created is dropped, and what was there before (here: a stale ``alembic_version`` and an unrelated table)
      is untouched.
    - Branch B — the tables were already there: they stay, with every non-test row; only the
      ``@v2test.invalid`` users / workspaces and the sync rows are removed.
    """
    # --- Branch A ---
    url = scratch_db()
    _exec(url, "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
          "INSERT INTO alembic_version VALUES ('003')", "CREATE TABLE unrelated (id int)")
    before = _tables(url)
    asyncio.run(_run(url, lambda c: c.run_sync(Base.metadata.create_all)))   # what the `engine` fixture does
    _seed_users(url, f"a{TEST_EMAIL_DOMAIN}")
    assert len(_tables(url)) == 32

    restore_as_found(url, before)

    assert _tables(url) == before == {"alembic_version", "unrelated"}
    assert _scalar(url, "SELECT version_num FROM alembic_version") == "003"

    # --- Branch B ---
    url = scratch_db()
    asyncio.run(_run(url, lambda c: c.run_sync(Base.metadata.create_all)))
    _seed_users(url, "real-tenant@example.com")
    before, schema_before = _tables(url), schema_snapshot(url)
    _seed_users(url, f"b{TEST_EMAIL_DOMAIN}", f"c{TEST_EMAIL_DOMAIN}")
    _exec(url, "INSERT INTO sync_commands (workspace_id, type, requested_by, status) "
               "SELECT id, 'resync', 'web', 'pending' FROM workspaces")

    restore_as_found(url, before)

    assert _tables(url) == before and schema_snapshot(url) == schema_before
    assert _scalar(url, "SELECT string_agg(email, ',') FROM users") == "real-tenant@example.com"
    assert _row_counts(url, ["workspaces", "sync_commands"]) == {"workspaces": 1, "sync_commands": 0}


def test_throwaway_databases_are_named_after_the_test_database_and_dropped(scratch_db):
    """The factory's databases are ``<test db>_mig_*`` (so a leftover is recognisable), and dropping one the way
    the fixture's teardown does really removes it."""
    url = scratch_db()
    name = make_url(url).database
    assert name.startswith(make_url(TEST_DB).database + "_mig_")
    exists = f"SELECT count(*) FROM pg_database WHERE datname = '{name}'"
    assert _scalar(TEST_DB, exists) == 1
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    assert _scalar(TEST_DB, exists) == 0
