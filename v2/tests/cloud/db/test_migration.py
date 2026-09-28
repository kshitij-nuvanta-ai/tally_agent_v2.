import asyncio
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from v2.cloud.cli import downgrade, migrate
from v2.cloud.models import V2_TABLES, Base
from v2.tests.cloud.conftest import STANDIN_DDL, TEST_DB, TEST_EMAIL_DOMAIN, _exec, requires_db, teardown_v2

pytestmark = requires_db


def _include_object(obj, name, type_, reflected, compare_to):
    """Same predicate as ``v2/cloud/alembic/env.py``'s ``include_object`` (duplicated here, not imported: that
    module runs top-level code that only works inside a live Alembic migration context — see its
    ``context.config`` access — so it cannot be imported standalone from a test)."""
    if type_ == "table":
        return name in V2_TABLES and not (getattr(obj, "info", {}) or {}).get("v2_readonly")
    return True

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
# The implementer extends EXPECTED_COLUMNS to every table in V2_TABLES from spec §4.2–§4.7 before implementing
# (21 tables); the test below asserts the dict covers V2_TABLES exactly.


async def _inspect(fn):
    eng = create_async_engine(TEST_DB)
    async with eng.connect() as c:
        out = await c.run_sync(lambda sync: fn(inspect(sync)))
    await eng.dispose()
    return out


def test_expected_columns_cover_every_v2_table(v2_schema):
    assert set(EXPECTED_COLUMNS) == set(V2_TABLES)


def test_every_table_has_exactly_the_spec_columns(v2_schema):
    cols = asyncio.run(_inspect(lambda i: {t: {c["name"] for c in i.get_columns(t)} for t in V2_TABLES}))
    assert cols == EXPECTED_COLUMNS


def test_models_match_migration_v2_001(v2_schema):
    """The v2 ORM metadata (``v2.cloud.models.Base.metadata``) must describe exactly the schema migration
    ``v2_001`` actually builds — not just the same column names (covered above), but the same indexes and
    unique constraints, with the same names. The migration is the authority (it already matches spec §4); this
    catches drift such as a model declaring `index=True` for a column the migration never indexes, or an index
    the migration creates that no model declares. Restricted to v2 tables via the same `include_object`
    predicate `alembic/env.py` uses, so the current app's `users`/`workspaces`/`alembic_version` are ignored.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    def _compare(sync_conn):
        ctx = MigrationContext.configure(
            sync_conn,
            opts={"version_table": "alembic_version_v2", "include_object": _include_object},
        )
        return compare_metadata(ctx, Base.metadata)

    async def _run():
        eng = create_async_engine(TEST_DB)
        async with eng.connect() as c:
            diff = await c.run_sync(_compare)
        await eng.dispose()
        return diff

    diff = asyncio.run(_run())
    assert diff == [], f"v2 models vs migration v2_001 mismatch: {diff}"


def test_money_columns_are_numeric_18_2_never_float(v2_schema):
    def check(i):
        bad = []
        for t in V2_TABLES:
            for c in i.get_columns(t):
                if "FLOAT" in str(c["type"]).upper() or "REAL" in str(c["type"]).upper():
                    bad.append((t, c["name"]))
        return bad, {c["name"]: str(c["type"]) for c in i.get_columns("tally_voucher_ledger_lines")}
    bad, line_types = asyncio.run(_inspect(check))
    assert bad == [] and line_types["amount"] == "NUMERIC(18, 2)" and line_types["fx_rate"] == "NUMERIC(18, 6)"


def test_one_active_device_partial_unique_index(v2_schema):
    idx = asyncio.run(_inspect(lambda i: i.get_indexes("agent_devices")))
    uniq = [x for x in idx if x["unique"] and x["column_names"] == ["workspace_id"]]
    assert uniq and "is_active" in str(uniq[0].get("dialect_options", {}).get("postgresql_where", ""))


def test_covering_line_index_exists(v2_schema):
    idx = asyncio.run(_inspect(lambda i: i.get_indexes("tally_voucher_ledger_lines")))
    assert any(x["column_names"] == ["workspace_id", "ledger_guid", "voucher_date"] for x in idx)


def test_version_table_is_v2_and_current_alembic_version_untouched(v2_schema):
    async def read():
        eng = create_async_engine(TEST_DB)
        async with eng.connect() as c:
            v2 = (await c.execute(text("SELECT version_num FROM alembic_version_v2"))).scalar_one()
            has_current = (await c.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar_one()
            current = None
            if has_current:
                current = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        await eng.dispose()
        return v2, has_current, current
    v2, has_current, current = asyncio.run(read())
    assert v2 == "v2_001"
    if has_current:                       # the current app's chain is never moved by v2
        assert current is None or not current.startswith("v2_")


def test_down_then_up_round_trip_keeps_users(v2_schema):
    downgrade(TEST_DB, "base")
    left = asyncio.run(_inspect(lambda i: set(i.get_table_names())))
    assert not (set(V2_TABLES) & left) and {"users", "workspaces"} <= left
    migrate(TEST_DB)
    assert set(V2_TABLES) <= asyncio.run(_inspect(lambda i: set(i.get_table_names())))


async def _regclass(url: str, name: str):
    eng = create_async_engine(url)
    async with eng.connect() as c:
        r = (await c.execute(text(f"SELECT to_regclass('public.{name}')"))).scalar_one()
    await eng.dispose()
    return r


async def _seed_test_user_and_workspace(url: str) -> None:
    """Insert one ``@v2test.invalid`` user + workspace so teardown_v2's DELETE path has a real row to remove."""
    eng = create_async_engine(url)
    uid, wid = uuid.uuid4(), uuid.uuid4()
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO users (id, email, password_hash, name, is_active, created_at, updated_at) "
                "VALUES (:id, :e, 'x', 'Teardown Test', true, now(), now())"
            ),
            {"id": uid, "e": f"{uid.hex[:8]}{TEST_EMAIL_DOMAIN}"},
        )
        await c.execute(
            text(
                "INSERT INTO workspaces (id, user_id, name, agent_type, config, memory, is_deleted, created_at, "
                "updated_at) VALUES (:id, :u, 'Teardown W', 'tally', '{}', '{}', false, now(), now())"
            ),
            {"id": wid, "u": uid},
        )
    await eng.dispose()


async def _seed_non_test_user(url: str, email: str) -> None:
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO users (id, email, password_hash, name, is_active, created_at, updated_at) "
                "VALUES (:id, :e, 'x', 'Real Tenant', true, now(), now())"
            ),
            {"id": uuid.uuid4(), "e": email},
        )
    await eng.dispose()


async def _count_email(url: str, email: str) -> int:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        n = (await c.execute(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email})).scalar_one()
    await eng.dispose()
    return n


async def _count_test_users(url: str) -> int:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        n = (
            await c.execute(text(f"SELECT count(*) FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'"))
        ).scalar_one()
    await eng.dispose()
    return n


def test_session_teardown_leaves_no_v2_objects(v2_schema):
    """Review Focus 2 / controller ruling 1 (fix round 1, items 1(a)/1(b)): exercises the REAL harness teardown
    (``teardown_v2``), not just a bare migrate/downgrade pair, on BOTH branches it can take — deterministically,
    regardless of what ``v2_schema["created_standins"]`` happens to be in this environment:

    - Branch A — the normal shared-DB case (A2): ``users``/``workspaces`` already existed before the harness
      ran (``created == []``). Teardown must leave both tables AND any non-test rows in place, deleting only
      the seeded ``@v2test.invalid`` rows.
    - Branch B — the harness created the stand-ins itself (``created == ["users", "workspaces"]``). Teardown
      must drop both tables entirely.

    Restores schema + stand-ins afterwards so the rest of the session-scoped ``v2_schema`` fixture's dependents
    keep working.
    """
    try:
        # --- Branch A: users/workspaces PRE-EXIST (created=[]) ---
        non_test_email = f"real-{uuid.uuid4().hex[:8]}@example.com"
        asyncio.run(_seed_non_test_user(TEST_DB, non_test_email))
        asyncio.run(_seed_test_user_and_workspace(TEST_DB))

        teardown_v2(TEST_DB, [])

        assert asyncio.run(_regclass(TEST_DB, "users")) is not None, "pre-existing users table must survive"
        assert (
            asyncio.run(_regclass(TEST_DB, "workspaces")) is not None
        ), "pre-existing workspaces table must survive"
        assert asyncio.run(_count_email(TEST_DB, non_test_email)) == 1, "non-test rows must not be touched"
        assert asyncio.run(_count_test_users(TEST_DB)) == 0, "test users must be deleted"
        left = asyncio.run(_inspect(lambda i: set(i.get_table_names())))
        assert not (set(V2_TABLES) & left), "v2 tables must be gone after teardown"
        assert asyncio.run(_regclass(TEST_DB, "alembic_version_v2")) is None

        # --- Branch B: the harness created users/workspaces itself (created=["users","workspaces"]) ---
        # Drop them (safe: branch A's teardown already downgraded the v2 schema, so no v2 FK points at them),
        # recreate as the harness would on a from-scratch DB, migrate, seed a test user, then tear down
        # declaring them as harness-created stand-ins.
        asyncio.run(_exec(TEST_DB, "DROP TABLE IF EXISTS workspaces", "DROP TABLE IF EXISTS users"))
        asyncio.run(_exec(TEST_DB, *STANDIN_DDL))
        migrate(TEST_DB)
        asyncio.run(_seed_test_user_and_workspace(TEST_DB))

        teardown_v2(TEST_DB, ["users", "workspaces"])

        assert asyncio.run(_regclass(TEST_DB, "users")) is None, "harness-created users stand-in must be dropped"
        assert (
            asyncio.run(_regclass(TEST_DB, "workspaces")) is None
        ), "harness-created workspaces stand-in must be dropped"
        left = asyncio.run(_inspect(lambda i: set(i.get_table_names())))
        assert not (set(V2_TABLES) & left)
        assert asyncio.run(_regclass(TEST_DB, "alembic_version_v2")) is None
    finally:
        # Restore the schema this test intentionally tore down twice, so the rest of the session's tests
        # (which all depend on the session-scoped v2_schema fixture) still see a migrated v2 schema + stand-ins.
        asyncio.run(_exec(TEST_DB, *STANDIN_DDL))
        migrate(TEST_DB)
