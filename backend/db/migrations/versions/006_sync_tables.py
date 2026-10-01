"""sync tables: the 21 agent-sync tables join the one chain (v2 merge M5)

Bookkeeping, masters, vouchers, snapshots and parity tables (S1 spec §4). They used to be created by a separate
Alembic chain (revision ``v2_001``, version table ``alembic_version_v2``). This revision does one of three things
(merge spec §5), decided by that old version table:

- no ``alembic_version_v2`` and none of the 21 tables: a fresh database — create the 21 tables;
- ``alembic_version_v2`` at ``v2_001``: the tables already exist, with their rows — check that they are the
  tables ``v2_001`` built (all 21 there; every column's name, type and nullability as defined below), create
  nothing, drop ``alembic_version_v2``;
- anything else — ``alembic_version_v2`` in any other state, the tables not as ``v2_001`` built them, or sync
  tables present with no ``alembic_version_v2`` at all: stop with an error and change nothing.

Hand-written, CREATE TABLE / CREATE INDEX only. The table definitions are the body of ``v2_001`` unchanged (M6), so
a database that takes the first path ends up identical to one that takes the second.

Revision ID: 006
Revises: 005
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_VERSION_TABLE = "alembic_version_v2"
OLD_HEAD = "v2_001"

MONEY = sa.Numeric(18, 2)
FACE = sa.Numeric(18, 4)
RATE = sa.Numeric(18, 6)
QTY = sa.Numeric(18, 4)

# The 21 tables in FK-safe DROP order (children first). A frozen copy for this revision's own adopt check and
# downgrade(): by design it does not track backend.db.sync_models.SYNC_TABLES (a later revision may add tables
# that do not belong in this file's list).
SYNC_TABLES: tuple[str, ...] = (
    "parity_lines",
    "tally_bill_allocations",
    "tally_voucher_inventory_lines",
    "tally_voucher_ledger_lines",
    "parity_runs",
    "tally_vouchers",
    "sync_batches",
    "sync_runs",
    "sync_workspaces",
    "agent_devices",
    "sync_commands",
    "sync_quarantine",
    "sync_fy_coverage",
    "tally_currencies",
    "tally_groups",
    "tally_voucher_types",
    "tally_ledgers",
    "tally_stock_groups",
    "tally_units",
    "tally_stock_items",
    "tally_report_snapshots",
)


def _master_columns():
    """Common master-row columns (§4.1): id, workspace_id, guid, alter_id, is_deleted, raw, first_seen_at,
    updated_at, created_at, name."""
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("alter_id", sa.BigInteger(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("name", sa.Text(), nullable=False),
    ]


def _master_indexes(ops, table: str) -> None:
    ops.create_index(f"uq_{table}_ws_guid", table, ["workspace_id", "guid"], unique=True)
    ops.create_index(
        f"ix_{table}_ws_name_live",
        table,
        ["workspace_id", "name"],
        postgresql_where=sa.text("NOT is_deleted"),
    )


def _ensure_gen_random_uuid() -> None:
    """gen_random_uuid() is built in on PG >= 13 (this Mac: 16); only reach for pgcrypto if it's actually
    missing. Downgrade never drops the extension — other code on the same DB may depend on it."""
    conn = op.get_bind()
    has_fn = conn.execute(sa.text("SELECT 1 FROM pg_proc WHERE proname = 'gen_random_uuid'")).first()
    if not has_fn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))


def _old_chain_revisions(conn) -> list[str] | None:
    """The rows of the old chain's version table, or ``None`` when that table does not exist."""
    if conn.execute(sa.text("SELECT to_regclass(:name)"), {"name": OLD_VERSION_TABLE}).scalar() is None:
        return None
    return [row[0] for row in conn.execute(sa.text(f"SELECT version_num FROM {OLD_VERSION_TABLE}"))]


class _ColumnRecorder:
    """Stands in for ``op`` in ``_create_sync_tables``: creates nothing, and records for every table each
    column's type (as Postgres' ``format_type`` prints it) and nullability."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, tuple[str, bool]]] = {}

    def create_table(self, name: str, *columns: sa.Column) -> None:
        self.tables[name] = {
            c.name: (str(c.type.compile(dialect=postgresql.dialect())).lower().replace(", ", ","), bool(c.nullable))
            for c in columns
        }

    def create_index(self, *args, **kwargs) -> None:
        """Indexes are not part of the adopt check (see ``_schema_drift``)."""


def _expected_columns() -> dict[str, dict[str, tuple[str, bool]]]:
    """``{table: {column: (type, nullable)}}`` exactly as this revision creates them. Frozen with this file: it
    comes from the definitions in ``_create_sync_tables`` below, never from ``backend.db.sync_models`` (which
    later revisions change)."""
    recorder = _ColumnRecorder()
    _create_sync_tables(recorder)
    return recorder.tables


def _existing_sync_tables(conn) -> list[str]:
    """Those of the 21 tables that exist, in creation order."""
    return [
        table for table in reversed(SYNC_TABLES)
        if conn.execute(sa.text("SELECT to_regclass(:name)"), {"name": table}).scalar() is not None
    ]


def _schema_drift(conn) -> list[str]:
    """Every difference between the sync tables in the database and the ones this revision creates, one line
    per column: a column missing, an unexpected column, another data type (length / precision included), another
    nullability.

    NOT compared: column defaults, indexes, and primary key / foreign key / unique / check constraints. A
    database whose only hand-made change is one of those is still adopted.
    """
    drift: list[str] = []
    for table, expected in _expected_columns().items():
        actual = {
            name: (type_, not not_null)
            for name, type_, not_null in conn.execute(
                sa.text(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                    "FROM pg_attribute a "
                    "WHERE a.attrelid = to_regclass(:name) AND a.attnum > 0 AND NOT a.attisdropped"
                ),
                {"name": table},
            )
        }
        for column in expected.keys() - actual.keys():
            drift.append(f"{table}.{column}: column is missing")
        for column in actual.keys() - expected.keys():
            drift.append(f"{table}.{column}: unexpected column")
        for column in expected.keys() & actual.keys():
            (want_type, want_nullable), (have_type, have_nullable) = expected[column], actual[column]
            if have_type != want_type:
                drift.append(f"{table}.{column}: type is {have_type}, expected {want_type}")
            if have_nullable != want_nullable:
                words = {True: "nullable", False: "NOT NULL"}
                drift.append(f"{table}.{column}: is {words[have_nullable]}, expected {words[want_nullable]}")
    return sorted(drift)


def _adopt(conn) -> None:
    """The old chain already built the tables (spec §5 row 2): check they are all there and are still the tables
    ``v2_001`` built (``_schema_drift`` — columns, types, nullability; not indexes or constraints), keep every
    row, and retire the old version table. Any difference: refuse, change nothing."""
    existing = _existing_sync_tables(conn)
    missing = [table for table in reversed(SYNC_TABLES) if table not in existing]
    if missing:
        raise RuntimeError(
            f"Migration 006: {OLD_VERSION_TABLE} says the sync tables were created by revision {OLD_HEAD}, but "
            f"these are missing: {', '.join(missing)}. Nothing was changed. Repair the database by hand before "
            "upgrading."
        )
    drift = _schema_drift(conn)
    if drift:
        raise RuntimeError(
            f"Migration 006: {OLD_VERSION_TABLE} says the sync tables were created by revision {OLD_HEAD}, but "
            f"they are not the tables that revision built. {len(drift)} difference(s): {'; '.join(drift)}. "
            "Nothing was changed. Bring these columns back to the original definition by hand (or, if the rows "
            f"are not needed, drop the sync tables and {OLD_VERSION_TABLE}), then run the upgrade again."
        )
    op.drop_table(OLD_VERSION_TABLE)


def upgrade() -> None:
    conn = op.get_bind()
    old = _old_chain_revisions(conn)
    if old == [OLD_HEAD]:
        _adopt(conn)
        return
    if old is not None:
        found = ", ".join(repr(r) for r in old) or "no revision (empty table)"
        raise RuntimeError(
            f"Migration 006: {OLD_VERSION_TABLE} exists and holds {found}; expected exactly {OLD_HEAD!r}. "
            "This database was migrated by the old separate sync chain to a state this revision does not know "
            "how to adopt. Nothing was changed. Bring the old chain to v2_001, or drop the sync tables and "
            f"{OLD_VERSION_TABLE} by hand, then run the upgrade again."
        )
    existing = _existing_sync_tables(conn)
    if existing:
        raise RuntimeError(
            f"Migration 006: {OLD_VERSION_TABLE} does not exist, but {len(existing)} of the {len(SYNC_TABLES)} "
            f"sync tables already exist: {', '.join(existing)}. This is neither a fresh database nor one the old "
            "sync chain left in a state this revision can adopt. Nothing was changed. If the old chain built "
            f"all {len(SYNC_TABLES)} tables at {OLD_HEAD} and only its version table was lost, recreate "
            f"{OLD_VERSION_TABLE} with the single row {OLD_HEAD!r} and run the upgrade again (the tables are "
            "then checked and adopted, rows kept). Otherwise drop these tables by hand and run the upgrade "
            "again to create all of them new."
        )
    _ensure_gen_random_uuid()
    _create_sync_tables(op)


def _create_sync_tables(ops) -> None:
    """Every CREATE TABLE / CREATE INDEX of this revision, issued through ``ops``: Alembic's ``op`` to really
    create them, or a ``_ColumnRecorder`` to read the same definitions back for the adopt check."""

    # --- Masters (§4.3) ---
    ops.create_table(
        "tally_currencies",
        *_master_columns(),
        sa.Column("mailing_name", sa.Text(), nullable=True),
        sa.Column("expanded_symbol", sa.Text(), nullable=True),
        sa.Column("decimal_places", sa.Integer(), nullable=True),
        sa.Column("is_base", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _master_indexes(ops, "tally_currencies")

    ops.create_table(
        "tally_groups",
        *_master_columns(),
        sa.Column("parent_name", sa.Text(), nullable=False),
        sa.Column("parent_guid", sa.Text(), nullable=True),
        sa.Column("primary_group", sa.Text(), nullable=True),
        sa.Column("nature", sa.Text(), nullable=True),
        sa.Column("is_revenue", sa.Boolean(), nullable=True),
        sa.Column("affects_gross_profit", sa.Boolean(), nullable=True),
        sa.Column("is_deemed_positive", sa.Boolean(), nullable=True),
        sa.Column("reserved_name", sa.Text(), nullable=True),
        sa.Column("derivation_warning", sa.Text(), nullable=True),
    )
    _master_indexes(ops, "tally_groups")
    ops.create_index("ix_tally_groups_ws_parent", "tally_groups", ["workspace_id", "parent_guid"])

    ops.create_table(
        "tally_voucher_types",
        *_master_columns(),
        sa.Column("parent_name", sa.Text(), nullable=False),
        sa.Column("parent_guid", sa.Text(), nullable=True),
        sa.Column("reserved_name", sa.Text(), nullable=True),
        sa.Column("base_type", sa.Text(), nullable=True),
    )
    _master_indexes(ops, "tally_voucher_types")

    ops.create_table(
        "tally_ledgers",
        *_master_columns(),
        sa.Column("parent_name", sa.Text(), nullable=False),
        sa.Column("group_guid", sa.Text(), nullable=True),
        sa.Column("currency_name", sa.Text(), nullable=True),
        sa.Column("is_forex", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_bill_wise", sa.Boolean(), nullable=True),
        sa.Column("tax_type", sa.Text(), nullable=True),
        sa.Column("gst_duty_head", sa.Text(), nullable=True),
        sa.Column("opening_balance", MONEY, nullable=True),
        sa.Column("closing_balance", MONEY, nullable=True),
        sa.Column("opening_fx_amount", FACE, nullable=True),
        sa.Column("opening_fx_rate", RATE, nullable=True),
        sa.Column("closing_fx_amount", FACE, nullable=True),
        sa.Column("closing_fx_rate", RATE, nullable=True),
        sa.Column("fx_currency", sa.Text(), nullable=True),
        sa.Column("balance_source", sa.Text(), nullable=False, server_default=sa.text("'tally'")),
        sa.Column("balance_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_text", JSONB(), nullable=True),
    )
    _master_indexes(ops, "tally_ledgers")
    ops.create_index("ix_tally_ledgers_ws_group", "tally_ledgers", ["workspace_id", "group_guid"])

    ops.create_table(
        "tally_stock_groups",
        *_master_columns(),
        sa.Column("parent_name", sa.Text(), nullable=False),
        sa.Column("parent_guid", sa.Text(), nullable=True),
    )
    _master_indexes(ops, "tally_stock_groups")

    ops.create_table(
        "tally_units",
        *_master_columns(),
        sa.Column("is_simple", sa.Boolean(), nullable=True),
        sa.Column("base_units", sa.Text(), nullable=True),
        sa.Column("additional_units", sa.Text(), nullable=True),
        sa.Column("conversion", QTY, nullable=True),
    )
    _master_indexes(ops, "tally_units")

    ops.create_table(
        "tally_stock_items",
        *_master_columns(),
        sa.Column("parent_name", sa.Text(), nullable=False),
        sa.Column("parent_guid", sa.Text(), nullable=True),
        sa.Column("base_unit_name", sa.Text(), nullable=True),
        sa.Column("base_unit_guid", sa.Text(), nullable=True),
        sa.Column("closing_qty", QTY, nullable=True),
        sa.Column("closing_qty_text", sa.Text(), nullable=True),
        sa.Column("closing_value", MONEY, nullable=True),
        sa.Column("balance_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    _master_indexes(ops, "tally_stock_items")

    # --- Snapshots (§4.6) ---
    ops.create_table(
        "tally_report_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("as_on_date", sa.Date(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("request_flags", JSONB(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("counters", JSONB(), nullable=True),
        sa.Column("cells", JSONB(), nullable=True),
        sa.Column("rows", JSONB(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("synthetic_rows", JSONB(), nullable=True),
        sa.Column("imbalance", MONEY, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_tally_report_snapshots_ws", "tally_report_snapshots", ["workspace_id"])
    ops.create_index(
        "uq_snapshots_ws_type_ason",
        "tally_report_snapshots",
        ["workspace_id", "report_type", "as_on_date"],
        unique=True,
    )

    # --- Bookkeeping (§4.2) ---
    ops.create_table(
        "sync_fy_coverage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("fy_start", sa.Date(), nullable=False),
        sa.Column("fy_end", sa.Date(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("months_done", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("months_complete", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("months_total", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_sync_fy_coverage_ws", "sync_fy_coverage", ["workspace_id"])
    ops.create_index("uq_sync_fy_coverage_ws_fystart", "sync_fy_coverage", ["workspace_id", "fy_start"], unique=True)

    ops.create_table(
        "sync_quarantine",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("voucher_date", sa.Date(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_sync_quarantine_ws", "sync_quarantine", ["workspace_id"])
    ops.create_index(
        "uq_sync_quarantine_open",
        "sync_quarantine",
        ["workspace_id", "kind", "guid"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    ops.create_table(
        "sync_commands",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    ops.create_index("ix_sync_commands_ws_status", "sync_commands", ["workspace_id", "status"])

    ops.create_table(
        "agent_devices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("device_name", sa.Text(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("refresh_hash", sa.Text(), nullable=False),
        sa.Column("refresh_prev_hash", sa.Text(), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_agent_devices_user", "agent_devices", ["user_id"])
    ops.create_index("ix_agent_devices_ws", "agent_devices", ["workspace_id"])
    ops.create_index("uq_agent_devices_refresh_hash", "agent_devices", ["refresh_hash"], unique=True)
    ops.create_index(
        "uq_agent_devices_one_active",
        "agent_devices",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    ops.create_table(
        "sync_workspaces",
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("tally_company_guid", sa.Text(), nullable=False),
        sa.Column("tally_company_name", sa.Text(), nullable=False),
        sa.Column("previous_company_guids", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("books_from", sa.Date(), nullable=False),
        sa.Column("base_currency_name", sa.Text(), nullable=True),
        sa.Column("sync_state", sa.Text(), nullable=False),
        sa.Column("restore_reason", sa.Text(), nullable=True),
        sa.Column("active_device_id", UUID(as_uuid=True), sa.ForeignKey("agent_devices.id"), nullable=True),
        sa.Column("cursor_alt_vch_id", sa.BigInteger(), nullable=True),
        sa.Column("cursor_alt_mst_id", sa.BigInteger(), nullable=True),
        sa.Column("cursor_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("caught_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat", JSONB(), nullable=True),
        sa.Column("relink_prompt", JSONB(), nullable=True),
        sa.Column("oldest_available_fy", sa.Date(), nullable=True),
        sa.Column("oldest_complete_fy", sa.Date(), nullable=True),
        sa.Column("backfill_state", sa.Text(), nullable=True),
        sa.Column("backfill_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("last_parity", JSONB(), nullable=True),
        sa.Column("ladder", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("tb_imbalance_baseline", JSONB(), nullable=True),
        sa.Column("quarantine_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("storage_estimate_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("storage_alert", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("bound_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_sync_workspaces_company_guid", "sync_workspaces", ["tally_company_guid"])

    ops.create_table(
        "sync_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("agent_devices.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", JSONB(), nullable=True),
        sa.Column("command_id", UUID(as_uuid=True), sa.ForeignKey("sync_commands.id"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("progress_done", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("batches_declared", sa.Integer(), nullable=True),
        sa.Column("counters_at_start", JSONB(), nullable=True),
        sa.Column("cursor_after", JSONB(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_sync_runs_ws", "sync_runs", ["workspace_id"])
    ops.create_index("ix_sync_runs_ws_started", "sync_runs", ["workspace_id", sa.desc("started_at")])
    ops.create_index(
        "uq_sync_runs_one_open_first_sync",
        "sync_runs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND kind = 'first_sync'"),
    )

    ops.create_table(
        "sync_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("sync_runs.id"), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response", JSONB(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_sync_batches_ws", "sync_batches", ["workspace_id"])
    ops.create_index("uq_sync_batches_ws_batch", "sync_batches", ["workspace_id", "batch_id"], unique=True)

    # --- Vouchers (§4.5) ---
    ops.create_table(
        "tally_vouchers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("master_id", sa.BigInteger(), nullable=False),
        sa.Column("alter_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("voucher_type_name", sa.Text(), nullable=False),
        sa.Column("voucher_type_guid", sa.Text(), nullable=True),
        sa.Column("base_type", sa.Text(), nullable=True),
        sa.Column("voucher_number", sa.Text(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("party_ledger_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("party_ledger_guid", sa.Text(), nullable=True),
        sa.Column("narration", sa.Text(), nullable=True),
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_optional", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_post_dated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_invoice", sa.Boolean(), nullable=True),
        sa.Column("has_forex", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("sync_runs.id"), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("uq_tally_vouchers_ws_guid", "tally_vouchers", ["workspace_id", "guid"], unique=True)
    ops.create_index("ix_tally_vouchers_ws_date", "tally_vouchers", ["workspace_id", "date"])
    ops.create_index(
        "ix_tally_vouchers_ws_party_date", "tally_vouchers", ["workspace_id", "party_ledger_guid", "date"]
    )
    ops.create_index("ix_tally_vouchers_ws_alter", "tally_vouchers", ["workspace_id", "alter_id"])

    # --- Parity (§4.7) ---
    ops.create_table(
        "parity_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("sync_run_id", UUID(as_uuid=True), sa.ForeignKey("sync_runs.id"), nullable=True),
        sa.Column("as_on_date", sa.Date(), nullable=False),
        sa.Column("rung", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("verified_from", sa.Date(), nullable=False),
        sa.Column("anchor_as_on", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("abort_reason", sa.Text(), nullable=True),
        sa.Column("lines_compared", sa.Integer(), nullable=True),
        sa.Column("mismatch_count", sa.Integer(), nullable=True),
        sa.Column("max_abs_diff", MONEY, nullable=True),
        sa.Column("net_diff", MONEY, nullable=True),
        sa.Column("forex_unrealised_total", MONEY, nullable=True),
        sa.Column("tb_imbalance", MONEY, nullable=True),
        sa.Column("counters_before", JSONB(), nullable=True),
        sa.Column("counters_after", JSONB(), nullable=True),
        sa.Column("remediation", JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_parity_runs_ws", "parity_runs", ["workspace_id"])
    ops.create_index("ix_parity_runs_ws_started", "parity_runs", ["workspace_id", "started_at"])

    ops.create_table(
        "tally_voucher_ledger_lines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "voucher_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tally_vouchers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("ledger_name", sa.Text(), nullable=False),
        sa.Column("ledger_guid", sa.Text(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("is_deemed_positive", sa.Boolean(), nullable=False),
        sa.Column("fx_currency", sa.Text(), nullable=True),
        sa.Column("fx_amount", FACE, nullable=True),
        sa.Column("fx_rate", RATE, nullable=True),
        sa.Column("voucher_date", sa.Date(), nullable=False),
        sa.Column("countable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_tally_voucher_ledger_lines_voucher", "tally_voucher_ledger_lines", ["voucher_id"])
    ops.create_index("ix_tally_voucher_ledger_lines_ws", "tally_voucher_ledger_lines", ["workspace_id"])
    ops.create_index(
        "ix_lines_cover",
        "tally_voucher_ledger_lines",
        ["workspace_id", "ledger_guid", "voucher_date"],
        postgresql_include=["amount", "fx_amount"],
        postgresql_where=sa.text("countable"),
    )

    ops.create_table(
        "tally_voucher_inventory_lines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "voucher_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tally_vouchers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("stock_item_name", sa.Text(), nullable=False),
        sa.Column("stock_item_guid", sa.Text(), nullable=False),
        sa.Column("actual_qty", QTY, nullable=True),
        sa.Column("billed_qty", QTY, nullable=True),
        sa.Column("qty_text", sa.Text(), nullable=True),
        sa.Column("rate", QTY, nullable=True),
        sa.Column("rate_text", sa.Text(), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("fx_currency", sa.Text(), nullable=True),
        sa.Column("fx_amount", FACE, nullable=True),
        sa.Column("fx_rate", RATE, nullable=True),
        sa.Column("is_deemed_positive", sa.Boolean(), nullable=False),
        sa.Column("voucher_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index(
        "ix_tally_voucher_inventory_lines_voucher", "tally_voucher_inventory_lines", ["voucher_id"]
    )
    ops.create_index("ix_tally_voucher_inventory_lines_ws", "tally_voucher_inventory_lines", ["workspace_id"])
    ops.create_index(
        "ix_inv_lines_cover",
        "tally_voucher_inventory_lines",
        ["workspace_id", "stock_item_guid", "voucher_date"],
    )

    ops.create_table(
        "tally_bill_allocations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "voucher_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tally_vouchers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("ledger_line_no", sa.Integer(), nullable=False),
        sa.Column("ledger_guid", sa.Text(), nullable=False),
        sa.Column("bill_name", sa.Text(), nullable=False),
        sa.Column("bill_type", sa.Text(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("fx_currency", sa.Text(), nullable=True),
        sa.Column("fx_amount", FACE, nullable=True),
        sa.Column("fx_rate", RATE, nullable=True),
        sa.Column("credit_period_text", sa.Text(), nullable=True),
        sa.Column("credit_period_days", sa.Integer(), nullable=True),
        sa.Column("bill_date", sa.Date(), nullable=True),
        sa.Column("voucher_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_tally_bill_allocations_voucher", "tally_bill_allocations", ["voucher_id"])
    ops.create_index("ix_tally_bill_allocations_ws", "tally_bill_allocations", ["workspace_id"])
    ops.create_index(
        "ix_bill_allocations_cover", "tally_bill_allocations", ["workspace_id", "ledger_guid", "bill_name"]
    )

    ops.create_table(
        "parity_lines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("parity_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("guid", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("our_amount", MONEY, nullable=True),
        sa.Column("tally_amount", MONEY, nullable=True),
        sa.Column("diff", MONEY, nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.Column("remediation_status", sa.Text(), nullable=True),
        sa.Column("unrealised_diff", MONEY, nullable=True),
        sa.Column("our_fx_amount", FACE, nullable=True),
        sa.Column("tally_fx_amount", FACE, nullable=True),
        sa.Column("as_on_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    ops.create_index("ix_parity_lines_ws", "parity_lines", ["workspace_id"])
    ops.create_index("ix_parity_lines_ws_run", "parity_lines", ["workspace_id", "run_id"])
    ops.create_index("ix_parity_lines_ws_verdict", "parity_lines", ["workspace_id", "verdict"])


def downgrade() -> None:
    for table in SYNC_TABLES:
        op.drop_table(table)
