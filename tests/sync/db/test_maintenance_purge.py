"""Task 11: lazy maintenance slices (D20, Q21, Q22, Q23) + the ``purge`` CLI (Q5) — S1 spec §4.9, §7.10, §14
scenarios 16/18/20, S1-R9. Every persisted scenario re-reads from a fresh session (§14).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.sync.clock import FixedClock
from backend.sync.config import V2Settings
from backend.db.sync_models import (
    ParityLine, ParityRun, SyncBatch, SyncQuarantine, SyncRun, SyncWorkspace, TallyVoucher, V2_TABLES,
)
from backend.sync import coverage, maintenance, purge
from tests.sync.conftest import requires_db
from tests.sync.db.ingest_helpers import bind, fresh

pytestmark = requires_db

UTC = timezone.utc
B_GUID = "138b7373-753c-4dbe-aa63-b802035f0ba9"


# --- shared setup ---------------------------------------------------------------------------------------------


async def _bound_sw(app_client, session, *, books_from="20220401") -> tuple[uuid.UUID, SyncWorkspace]:
    uid, ws, headers = await bind(app_client, session, "B")
    sw = await session.get(SyncWorkspace, ws)
    return ws, sw


def _voucher(ws: uuid.UUID, guid: str, d: date, *, raw: dict | None = None) -> TallyVoucher:
    return TallyVoucher(
        workspace_id=ws, guid=guid, master_id=1, alter_id=1, date=d, voucher_type_name="Sales",
        voucher_number="1", party_ledger_name="Some Party", is_cancelled=False, is_optional=False,
        is_post_dated=False, is_invoice=True, has_forex=False, is_deleted=False,
        raw=raw if raw is not None else {"date": d.strftime("%Y%m%d")},
    )


def _parity_run(ws: uuid.UUID, *, created_at: datetime, as_on: date, run_id: uuid.UUID | None = None) -> ParityRun:
    return ParityRun(
        id=run_id or uuid.uuid4(), workspace_id=ws, as_on_date=as_on, rung=2, scope="ledger",
        verified_from=date(2022, 4, 1), status="ok", created_at=created_at,
    )


def _parity_line(ws: uuid.UUID, run_id: uuid.UUID, *, created_at: datetime, verdict: str, name: str) -> ParityLine:
    return ParityLine(
        id=uuid.uuid4(), workspace_id=ws, run_id=run_id, scope="ledger", guid=f"{B_GUID}-{name}", name=name,
        verdict=verdict, as_on_date=date(2023, 3, 31), created_at=created_at,
    )


def _sync_run(ws: uuid.UUID, device_id: uuid.UUID) -> SyncRun:
    return SyncRun(
        id=uuid.uuid4(), workspace_id=ws, device_id=device_id, kind="incremental", status="completed",
    )


def _batch(ws: uuid.UUID, run_id: uuid.UUID, *, created_at: datetime) -> SyncBatch:
    return SyncBatch(
        id=uuid.uuid4(), workspace_id=ws, run_id=run_id, batch_id=uuid.uuid4().hex,
        request_sha256="x" * 64, object_count=1, status="accepted", created_at=created_at,
    )


def _settings(**overrides) -> V2Settings:
    import os

    return V2Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], web_jwt_secret="w" * 32,
                      device_token_secret="d" * 32, **overrides)


async def _table_count(s, table: str, ws: uuid.UUID) -> int:
    return (await s.execute(text(f"SELECT count(*) FROM {table} WHERE workspace_id = :w"), {"w": ws})).scalar_one()


# --- raw purge (Q22) -------------------------------------------------------------------------------------------


async def test_raw_window_follows_ist_fy(app_client, session, engine):
    """Review Focus 4: coverage to FY 2026-27; clock 2027-03-31T20:00Z (= 1 April 2027 IST) then
    ``add_fy 2027-04-01`` -> FY 2025-26 vouchers' `raw` is nulled by the maintenance slice; FY 2026-27 is kept.
    Uses the SAME window definition `store.raw_window_fys` uses (controller ruling 3)."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    await coverage.add_fy(session, sw, date(2025, 4, 1), clock)
    await coverage.add_fy(session, sw, date(2026, 4, 1), clock)

    v_old = _voucher(ws, f"{B_GUID}-old", date(2025, 6, 15))
    v_new = _voucher(ws, f"{B_GUID}-new", date(2026, 6, 15))
    session.add_all([v_old, v_new])
    await session.commit()

    # Rollover: clock crosses into IST FY 2027-28.
    clock2 = FixedClock(datetime(2027, 3, 31, 20, 0, tzinfo=UTC))
    await coverage.add_fy(session, sw, date(2027, 4, 1), clock2)

    settings = _settings()
    report = await maintenance.run_slice(session, sw, settings, clock2)
    await session.commit()
    assert report.raw_nulled == 1

    async with fresh(engine) as s:
        old_row = (await s.execute(select(TallyVoucher).where(TallyVoucher.guid == f"{B_GUID}-old"))).scalar_one()
        new_row = (await s.execute(select(TallyVoucher).where(TallyVoucher.guid == f"{B_GUID}-new"))).scalar_one()
        assert old_row.raw is None
        assert new_row.raw is not None


async def test_raw_purge_is_bounded_per_slice(app_client, session, engine):
    """§14.20: 12 vouchers outside the window, `maintenance_slice_rows=5` -> 5, 5, 2 nulled across three
    heartbeat-driven slices."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    await coverage.add_fy(session, sw, date(2025, 4, 1), clock)
    await coverage.add_fy(session, sw, date(2026, 4, 1), clock)

    for i in range(12):
        session.add(_voucher(ws, f"{B_GUID}-old-{i}", date(2022, 6, 15)))
    await session.commit()

    settings = _settings(maintenance_slice_rows=5)
    counts = []
    for _ in range(3):
        report = await maintenance.run_slice(session, sw, settings, clock)
        await session.commit()
        counts.append(report.raw_nulled)
    assert counts == [5, 5, 2]

    async with fresh(engine) as s:
        remaining = (
            await s.execute(
                select(TallyVoucher).where(TallyVoucher.workspace_id == ws, TallyVoucher.raw.is_not(None))
            )
        ).scalars().all()
        assert remaining == []


# --- parity retention (Q21: 90 / 7 / 90) -----------------------------------------------------------------------


async def test_parity_retention_7_days_matches_90_days_mismatches(app_client, session, engine):
    """§14.16: an 8-day-old `match` line is pruned; an 8-day-old `mismatch` line is kept (its run isn't 90 days
    old yet); a 91-day-old run is pruned WITH its lines (even the mismatch ones)."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)

    fresh_run = _parity_run(ws, created_at=clock.now() - timedelta(days=8), as_on=date(2023, 3, 31))
    old_run = _parity_run(ws, created_at=clock.now() - timedelta(days=91), as_on=date(2023, 3, 31))
    session.add_all([fresh_run, old_run])
    await session.flush()

    match_line = _parity_line(ws, fresh_run.id, created_at=clock.now() - timedelta(days=8), verdict="match",
                              name="Domestic Sales")
    mismatch_line = _parity_line(ws, fresh_run.id, created_at=clock.now() - timedelta(days=8), verdict="mismatch",
                                 name="Output CGST")
    old_run_line = _parity_line(ws, old_run.id, created_at=clock.now() - timedelta(days=91), verdict="mismatch",
                                name="Output SGST")
    session.add_all([match_line, mismatch_line, old_run_line])
    await session.commit()

    settings = _settings()
    report = await maintenance.run_slice(session, sw, settings, clock)
    await session.commit()

    assert report.parity_runs_pruned == 1        # old_run
    assert report.parity_lines_pruned == 2        # match_line (direct) + old_run_line (cascade)

    async with fresh(engine) as s:
        remaining_lines = {r.name for r in (await s.execute(select(ParityLine))).scalars().all()}
        assert remaining_lines == {"Output CGST"}
        remaining_runs = {r.id for r in (await s.execute(select(ParityRun))).scalars().all()}
        assert remaining_runs == {fresh_run.id}


async def test_parity_retention_never_prunes_last_parity_run(app_client, session, engine):
    """Controller ruling 5: the run `last_parity` points at is never pruned, even 91 days old."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    old_run = _parity_run(ws, created_at=clock.now() - timedelta(days=91), as_on=date(2023, 3, 31))
    session.add(old_run)
    await session.flush()
    sw.last_parity = {"state": "ok", "run_id": str(old_run.id), "checked_at": clock.now().isoformat(),
                      "as_on": "2023-03-31", "mismatch_count": 0, "verified_from": "2022-04-01"}
    await session.commit()

    settings = _settings()
    report = await maintenance.run_slice(session, sw, settings, clock)
    await session.commit()
    assert report.parity_runs_pruned == 0

    async with fresh(engine) as s:
        assert await s.get(ParityRun, old_run.id) is not None


async def test_parity_retention_never_prunes_ladder_last_run_id(app_client, session, engine):
    """Controller ruling 5: the ladder's `last_run_id` is never pruned either, independently of `last_parity`."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    old_run = _parity_run(ws, created_at=clock.now() - timedelta(days=91), as_on=date(2023, 3, 31))
    session.add(old_run)
    await session.flush()
    sw.ladder = {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "last_run_id": str(old_run.id)}
    await session.commit()

    settings = _settings()
    report = await maintenance.run_slice(session, sw, settings, clock)
    await session.commit()
    assert report.parity_runs_pruned == 0

    async with fresh(engine) as s:
        assert await s.get(ParityRun, old_run.id) is not None


# --- batch-log retention (90 days) -----------------------------------------------------------------------------


async def test_batch_log_retention_90_days(app_client, session, engine):
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    run = _sync_run(ws, sw.active_device_id)
    session.add(run)
    await session.flush()
    fresh_batch = _batch(ws, run.id, created_at=clock.now() - timedelta(days=89))
    old_batch = _batch(ws, run.id, created_at=clock.now() - timedelta(days=91))
    session.add_all([fresh_batch, old_batch])
    await session.commit()

    settings = _settings()
    report = await maintenance.run_slice(session, sw, settings, clock)
    await session.commit()
    assert report.batches_pruned == 1

    async with fresh(engine) as s:
        remaining = {r.id for r in (await s.execute(select(SyncBatch))).scalars().all()}
        assert remaining == {fresh_batch.id}


# --- storage estimate + alert (Q23) ----------------------------------------------------------------------------


async def test_storage_alert_sets_flag_over_threshold(app_client, session, engine, caplog):
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    session.add(_voucher(ws, f"{B_GUID}-est", date(2026, 6, 15)))
    await session.commit()

    settings = _settings(storage_alert_bytes=1)
    with caplog.at_level(logging.INFO, logger="v2.ops.storage"):
        report = await maintenance.run_slice(session, sw, settings, clock)
        await session.commit()

    assert report.storage_alert is True
    assert report.storage_estimate_bytes > 1

    events = [json.loads(r.message) for r in caplog.records if r.name == "v2.ops.storage"]
    assert len(events) == 1
    assert set(events[0]) == {"workspace_id", "event", "estimate_bytes", "threshold_bytes"}
    assert events[0]["workspace_id"] == str(ws)   # opaque id only -- no ledger/party/company names

    async with fresh(engine) as s:
        row = await s.get(SyncWorkspace, ws)
        assert row.storage_alert is True
        assert row.storage_estimate_bytes == report.storage_estimate_bytes


# --- purge CLI (Q5) --------------------------------------------------------------------------------------------


async def _sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_data(session, sw: SyncWorkspace) -> None:
    ws = sw.workspace_id
    session.add(_voucher(ws, f"{ws}-v1", date(2026, 6, 15)))
    run = _sync_run(ws, sw.active_device_id)
    session.add(run)
    session.add(_parity_run(ws, created_at=datetime.now(UTC), as_on=date(2026, 3, 31)))
    await session.flush()
    session.add(_batch(ws, run.id, created_at=datetime.now(UTC)))
    await session.commit()


async def test_purge_now_deletes_only_that_workspaces_rows(app_client, session, engine):
    """§14.18: two bound workspaces with data; W1 soft-deleted; `purge --now --workspace W1` -> every v2 table
    has 0 rows for W1 and W2's counts are unchanged; `users` / `workspaces` rows untouched."""
    ws1, sw1 = await _bound_sw(app_client, session)
    await _seed_workspace_data(session, sw1)
    uid2, ws2, headers2 = await bind(app_client, session, "B")
    sw2 = await session.get(SyncWorkspace, ws2)
    await _seed_workspace_data(session, sw2)

    await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id = :w"), {"w": ws1})
    await session.commit()

    async with fresh(engine) as s:
        before2 = {t: await _table_count(s, t, ws2) for t in V2_TABLES}
    sm = await _sessionmaker(engine)
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    counts = await purge.purge(sm, workspace_id=ws1, now=True, clock=clock, settings=_settings())
    assert counts["tally_vouchers"] == 1
    assert counts["parity_runs"] == 1
    assert counts["sync_batches"] == 1
    assert counts["sync_workspaces"] == 1

    async with fresh(engine) as s:
        after = {t: await _table_count(s, t, ws2) for t in V2_TABLES}
        gone = {t: await _table_count(s, t, ws1) for t in V2_TABLES}
    assert gone == {t: 0 for t in V2_TABLES}                       # purged workspace: 0 rows in EVERY v2 table
    assert after == before2                                        # neighbour: every table's count unchanged
    assert before2["tally_vouchers"] == 1 and before2["agent_devices"] >= 1 and before2["sync_runs"] == 1
    async with fresh(engine) as s:
        row = (await s.execute(text("SELECT is_deleted FROM workspaces WHERE id = :w"), {"w": ws1})).mappings().first()
        assert row is not None                               # workspaces row itself is untouched by purge
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() >= 2


async def test_purge_respects_30_day_grace(app_client, session, engine):
    """`workspaces.updated_at` of the deleted row is the grace clock (A16: the only timestamp the current app's
    soft-delete sets -- `backend/api/workspaces.py` flips `is_deleted`; `Workspace.updated_at` has
    `onupdate=_utcnow` so it moves on that same flush). Deleted 29 days ago (by `updated_at`) -> nothing purged;
    31 days ago -> purged."""
    ws, sw = await _bound_sw(app_client, session)
    await _seed_workspace_data(session, sw)
    now = datetime(2026, 9, 25, 6, 30, tzinfo=UTC)
    await session.execute(
        text("UPDATE workspaces SET is_deleted = true, updated_at = :u WHERE id = :w"),
        {"u": now - timedelta(days=29), "w": ws},
    )
    await session.commit()

    sm = await _sessionmaker(engine)
    counts = await purge.purge(sm, workspace_id=None, now=False, clock=FixedClock(now), settings=_settings())
    assert counts["sync_workspaces"] == 0
    async with fresh(engine) as s:
        assert await s.get(SyncWorkspace, ws) is not None

    await session.execute(text("UPDATE workspaces SET updated_at = :u WHERE id = :w"),
                          {"u": now - timedelta(days=31), "w": ws})
    await session.commit()
    counts2 = await purge.purge(sm, workspace_id=None, now=False, clock=FixedClock(now), settings=_settings())
    assert counts2["sync_workspaces"] == 1
    async with fresh(engine) as s:
        assert await s.get(SyncWorkspace, ws) is None


async def test_deleted_workspace_next_call_410_and_device_revoked(app_client, session):
    """§14.18 first half: soft-delete flips `is_deleted`; the device's NEXT sync call gets 410
    `workspace_deleted` and the active device is revoked (existing `dependencies.active_device` behaviour,
    lazily on the next call — before any physical purge ever runs)."""
    uid, ws, headers = await bind(app_client, session, "B")
    sw = await session.get(SyncWorkspace, ws)
    device_id = sw.active_device_id

    r = await session.execute(text("SELECT revoked_at FROM agent_devices WHERE id = :d"), {"d": device_id})
    assert r.mappings().first()["revoked_at"] is None      # not yet -- only the NEXT call revokes it

    await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id = :w"), {"w": ws})
    await session.commit()

    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    r2 = await app_client.post(
        f"/api/sync/{ws}/heartbeat", json={"tally_status": "closed", "pc_clock": clock.now().isoformat()},
        headers=headers,
    )
    assert r2.status_code == 410
    assert r2.json()["error"] == "workspace_deleted"

    r3 = await session.execute(text("SELECT revoked_at, revoke_reason FROM agent_devices WHERE id = :d"), {"d": device_id})
    row = r3.mappings().first()
    assert row["revoked_at"] is not None
    assert row["revoke_reason"] == "workspace_deleted"


# --- purge CLI logging (capsys) --------------------------------------------------------------------------------


def test_purge_cli_logs_counts_only(capsys):
    """The `python -m backend.sync purge` entry point prints row counts only -- no GUIDs, no names, no workspace id
    (even when `--workspace` was passed)."""
    import asyncio

    from backend.sync.__main__ import main

    fake_counts = {"tally_vouchers": 3, "sync_workspaces": 1}

    async def _fake_purge_cli(url, *, workspace_id=None, now=False):
        return fake_counts

    import backend.sync.__main__ as main_mod

    orig = main_mod.purge_cli
    main_mod.purge_cli = _fake_purge_cli
    try:
        import sys

        argv = sys.argv
        sys.argv = ["backend.sync", "purge", "--url", "postgresql+asyncpg://x/y",
                    "--workspace", str(uuid.uuid4()), "--now"]
        try:
            main()
        finally:
            sys.argv = argv
    finally:
        main_mod.purge_cli = orig

    out = capsys.readouterr().out
    assert "tally_vouchers" in out and "3" in out
    parsed = json.loads(out.strip().splitlines()[-1])
    assert parsed["counts"] == fake_counts
    assert set(parsed) == {"command", "now", "counts"}     # no workspace id, no names, no GUIDs


# --- Task 11 fix round 1 ---------------------------------------------------------------------------------------


async def test_purge_survives_device_moved_between_workspaces(app_client, session, engine):
    """I3: device D1 synced W2, then moved to W1 (`agent_devices.workspace_id` re-pointed). W1 is deleted and
    purged: W2's `sync_runs.device_id` still references D1, so D1 is DETACHED (not deleted) instead of the whole
    purge aborting on the FK; W2's data is untouched; a second purge run is a clean no-op."""
    ws1, sw1 = await _bound_sw(app_client, session)
    dev1 = sw1.active_device_id
    await _seed_workspace_data(session, sw1)
    uid2, ws2, headers2 = await bind(app_client, session, "B")
    sw2 = await session.get(SyncWorkspace, ws2)
    await _seed_workspace_data(session, sw2)
    moved_run = _sync_run(ws2, dev1)                     # W2's history references the device now living in W1
    session.add(moved_run)
    await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id = :w"), {"w": ws1})
    await session.commit()

    sm = await _sessionmaker(engine)
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    async with fresh(engine) as s:
        before2 = {t: await _table_count(s, t, ws2) for t in V2_TABLES}
    await purge.purge(sm, workspace_id=ws1, now=True, clock=clock, settings=_settings())
    async with fresh(engine) as s:
        assert await s.get(SyncWorkspace, ws1) is None
        assert {t: await _table_count(s, t, ws1) for t in V2_TABLES} == {t: 0 for t in V2_TABLES}
        assert {t: await _table_count(s, t, ws2) for t in V2_TABLES} == before2
        assert await s.get(SyncRun, moved_run.id) is not None
        d = (await s.execute(text("SELECT workspace_id, is_active, revoked_at FROM agent_devices WHERE id = :d"),
                             {"d": dev1})).mappings().one()
        assert d["workspace_id"] is None and d["is_active"] is False and d["revoked_at"] is not None
    again = await purge.purge(sm, workspace_id=ws1, now=True, clock=clock, settings=_settings())
    assert sum(again.values()) == 0


async def test_purge_one_workspace_failing_does_not_block_the_others(app_client, session, engine, monkeypatch):
    """I3: one transaction per workspace -- a failure purging one workspace rolls back only that workspace."""
    ws1, sw1 = await _bound_sw(app_client, session)
    await _seed_workspace_data(session, sw1)
    uid2, ws2, _ = await bind(app_client, session, "B")
    sw2 = await session.get(SyncWorkspace, ws2)
    await _seed_workspace_data(session, sw2)
    await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id IN (:a, :b)"), {"a": ws1, "b": ws2})
    await session.commit()

    real = purge._purge_workspace

    async def flaky(sess, ws_id, clock):
        if ws_id == ws1:
            await sess.execute(text("DELETE FROM tally_vouchers WHERE workspace_id = :w"), {"w": ws_id})
            raise RuntimeError("boom")
        return await real(sess, ws_id, clock)

    monkeypatch.setattr(purge, "_purge_workspace", flaky)
    sm = await _sessionmaker(engine)
    await purge.purge(sm, now=True, clock=FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC)), settings=_settings())
    async with fresh(engine) as s:
        assert await s.get(SyncWorkspace, ws2) is None            # ws2 purged
        assert await s.get(SyncWorkspace, ws1) is not None        # ws1 fully rolled back
        assert await _table_count(s, "tally_vouchers", ws1) == 1


async def test_maintenance_failure_does_not_fail_or_roll_back_the_heartbeat(app_client, session, engine, monkeypatch):
    """I1: `run_slice` raising must not 500 the heartbeat nor lose `last_seen_at`; an ops signal is logged."""
    uid, ws, headers = await bind(app_client, session, "B")

    async def boom(*a, **kw):
        raise RuntimeError("slice exploded")

    monkeypatch.setattr(maintenance, "run_slice", boom)
    clock_now = datetime.now(UTC)
    r = await app_client.post(f"/api/sync/{ws}/heartbeat",
                              json={"tally_status": "closed", "pc_clock": clock_now.isoformat()}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["sync_state"]
    async with fresh(engine) as s:
        row = await s.get(SyncWorkspace, ws)
        assert row.last_seen_at is not None
        assert row.last_heartbeat is not None


async def test_storage_estimate_is_time_gated(app_client, session, engine, monkeypatch):
    """I2: the O(rows) estimate refreshes at most once per `storage_estimate_interval_seconds`, not per heartbeat."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    calls = []
    real = maintenance._storage_estimate

    async def counting(sess, ws_id):
        calls.append(ws_id)
        return await real(sess, ws_id)

    monkeypatch.setattr(maintenance, "_storage_estimate", counting)
    settings = _settings(storage_estimate_interval_seconds=3600)
    for _ in range(3):
        await maintenance.run_slice(session, sw, settings, clock)
        await session.commit()
    assert len(calls) == 1
    clock.advance(seconds=3601)
    await maintenance.run_slice(session, sw, settings, clock)
    await session.commit()
    assert len(calls) == 2
    async with fresh(engine) as s:
        assert (await s.get(SyncWorkspace, ws)).storage_estimate_bytes > 0


async def test_storage_alert_signal_only_on_transition(app_client, session, caplog):
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)
    session.add(_voucher(ws, f"{B_GUID}-tr", date(2026, 6, 15)))
    await session.commit()
    settings = _settings(storage_alert_bytes=1, storage_estimate_interval_seconds=1)
    with caplog.at_level(logging.INFO, logger="v2.ops.storage"):
        for _ in range(3):
            await maintenance.run_slice(session, sw, settings, clock)
            clock.advance(seconds=5)
    assert len([r for r in caplog.records if r.name == "v2.ops.storage"]) == 1


async def test_quarantine_retention_resolved_plus_90_days(app_client, session, engine):
    """I5 / §4.9: resolved > 90 d pruned; resolved < 90 d kept; open (unresolved) kept however old."""
    clock = FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=UTC))
    ws, sw = await _bound_sw(app_client, session)

    def q(guid, resolved):
        return SyncQuarantine(workspace_id=ws, kind="voucher", guid=guid, code="x", resolved_at=resolved,
                              first_seen_at=clock.now() - timedelta(days=400))

    old_resolved = q("g-old", clock.now() - timedelta(days=91))
    recent_resolved = q("g-recent", clock.now() - timedelta(days=89))
    open_old = q("g-open", None)
    session.add_all([old_resolved, recent_resolved, open_old])
    await session.commit()

    report = await maintenance.run_slice(session, sw, _settings(), clock)
    await session.commit()
    assert report.quarantine_pruned == 1
    async with fresh(engine) as s:
        assert {r.guid for r in (await s.execute(select(SyncQuarantine).where(SyncQuarantine.workspace_id == ws))
                                 ).scalars().all()} == {"g-recent", "g-open"}


async def test_out_of_window_voucher_raw_is_sql_null_not_json_null(app_client, session, engine):
    """Concern 1: FY-2 vouchers ingested outside the raw window store SQL NULL (§4.9 / §14.20), on both the bulk
    insert and the executemany update path -- not the JSON literal `null`."""
    from tests.sync import realdata
    from tests.sync.db.ingest_helpers import bound, post_ok, voucher_by_guid

    ws, headers, run_id, uid = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    v = voucher_by_guid(realdata.vouchers("p21_B_fy2022_month_09.xml"), "-00000067")
    await post_ok(app_client, ws, headers, run_id, [v])
    guid = realdata.COMPANY_B_GUID + "-00000067"

    async def state():
        async with fresh(engine) as s:
            return (await s.execute(text(
                "SELECT raw IS NULL AS sql_null, raw::text AS txt FROM tally_vouchers "
                "WHERE workspace_id = :w AND guid = :g"), {"w": ws, "g": guid})).mappings().one()

    st = await state()
    assert st["sql_null"] is True, st

    v2 = json.loads(json.dumps(v))
    v2["data"]["alterid"] = " 999"
    body = await post_ok(app_client, ws, headers, run_id, [v2])
    assert body["counts"]["updated"] == 1
    st = await state()
    assert st["sql_null"] is True, st


def test_window_fys_single_definition():
    from backend.sync.clock import window_fys

    d = date
    # newest two coverage rows win, regardless of today
    assert window_fys([d(2023, 4, 1), d(2025, 4, 1), d(2026, 4, 1)], d(2027, 5, 1)) == {d(2025, 4, 1), d(2026, 4, 1)}
    # floor (first-sync progress: not before FY(books_from))
    assert window_fys([d(2021, 4, 1), d(2022, 4, 1)], floor=d(2022, 4, 1)) == {d(2022, 4, 1)}
    # no coverage: clock fallback (current + previous), or empty when no clock given
    assert window_fys([], d(2026, 5, 1)) == {d(2026, 4, 1), d(2025, 4, 1)}
    assert window_fys([]) == set()


async def test_storage_estimate_counts_raw_bearing_vouchers_extra(app_client, session):
    """Concern 1 re-measure: an in-window voucher (raw kept) costs AVG_ROW_BYTES + AVG_RAW_BYTES; an
    out-of-window one (raw SQL NULL) only AVG_ROW_BYTES."""
    ws, sw = await _bound_sw(app_client, session)
    base = await maintenance._storage_estimate(session, ws)
    session.add(_voucher(ws, f"{B_GUID}-r1", date(2026, 6, 15)))
    session.add(TallyVoucher(
        workspace_id=ws, guid=f"{B_GUID}-r2", master_id=2, alter_id=1, date=date(2022, 6, 15), raw=None,
        voucher_type_name="Sales", voucher_number="2", party_ledger_name="P", is_cancelled=False,
        is_optional=False, is_post_dated=False, is_invoice=True, has_forex=False, is_deleted=False))
    await session.commit()
    est = await maintenance._storage_estimate(session, ws)
    assert est - base == 2 * maintenance.AVG_ROW_BYTES["tally_vouchers"] + maintenance.AVG_RAW_BYTES
