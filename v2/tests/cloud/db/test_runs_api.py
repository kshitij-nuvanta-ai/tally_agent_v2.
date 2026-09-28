"""Runs (§7.8), server-side cursors (D15), FY coverage (§7.10, §8.3, §15.4) and the two edges + backfill copy
(S1 spec §7.8, §7.10, §8.2 run-driven transitions, §8.3, §15.3, §15.4, D15, D16, §14 scenarios 3, 12, 13, 14
restore half). Every test re-reads from a fresh session (``session`` — a separate ``AsyncSession`` from the one
the API request used) and asserts the whole persisted state (§14).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from v2.cloud.clock import FixedClock
from v2.cloud.models import AgentDevice, SyncWorkspace
from v2.cloud.sync import runs
from v2.tests.cloud.conftest import login_device, make_workspace, requires_db, web_headers

pytestmark = requires_db

BIND_BODY = {
    "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
    "company_name": "Sharma & Sons' Probe Traders",
    "books_from": "20220401",
    "base_currency_name": "INR",
    "takeover": False,
}


async def _login_and_bind(app_client, session, *, email=None, device_name="ACCOUNTS-PC", books_from="20220401"):
    uid, login_body, headers = await login_device(app_client, session, email=email, device_name=device_name)
    ws = await make_workspace(session, uid)
    body = {**BIND_BODY, "workspace_id": str(ws), "books_from": books_from}
    r = await app_client.post("/api/sync/company", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return uid, ws, login_body["device_id"], headers


async def _set_cursors(session, ws, vch, mst) -> None:
    await session.execute(
        text(
            "UPDATE sync_workspaces SET cursor_alt_vch_id = :v, cursor_alt_mst_id = :m, cursor_set_at = now() "
            "WHERE workspace_id = :w"
        ),
        {"v": vch, "m": mst, "w": ws},
    )
    await session.commit()


async def _set_state_row(session, ws, sync_state: str) -> None:
    await session.execute(
        text("UPDATE sync_workspaces SET sync_state = :s WHERE workspace_id = :w"), {"s": sync_state, "w": ws}
    )
    await session.commit()


async def _sw_row(session, ws):
    return (
        await session.execute(text("SELECT * FROM sync_workspaces WHERE workspace_id = :w"), {"w": ws})
    ).mappings().first()


async def _run_row(session, run_id):
    return (
        await session.execute(text("SELECT * FROM sync_runs WHERE id = :i"), {"i": run_id})
    ).mappings().first()


async def _coverage_row(session, ws, fy_start: date):
    return (
        await session.execute(
            text("SELECT * FROM sync_fy_coverage WHERE workspace_id = :w AND fy_start = :f"),
            {"w": ws, "f": fy_start},
        )
    ).mappings().first()


async def _coverage_rows(session, ws):
    rows = (
        await session.execute(
            text("SELECT * FROM sync_fy_coverage WHERE workspace_id = :w ORDER BY fy_start"), {"w": ws}
        )
    ).mappings().all()
    return {r["fy_start"]: r for r in rows}


async def _command_row(session, cmd_id):
    return (
        await session.execute(text("SELECT * FROM sync_commands WHERE id = :i"), {"i": cmd_id})
    ).mappings().first()


async def _confirm_resync_command(app_client, uid, ws, *, scope="company", fy_start=None):
    body = {"type": "confirm_resync", "scope": scope}
    if fy_start:
        body["fy_start"] = fy_start
    r = await app_client.post(f"/api/workspaces/{ws}/sync/commands", json=body, headers=web_headers(uid))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _insert_accepted_batch(session, ws, run_id, batch_id) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_batches (id, workspace_id, run_id, batch_id, request_sha256, object_count, status) "
            "VALUES (:id, :w, :r, :b, 'sha', 1, 'accepted')"
        ),
        {"id": uuid.uuid4(), "w": ws, "r": run_id, "b": batch_id},
    )
    await session.commit()


def _fy_months(fy_start: str, count: int) -> list[str]:
    y, m = int(fy_start[:4]), int(fy_start[5:7])
    out = []
    for i in range(count):
        mm = m + i
        yy = y + (mm - 1) // 12
        mm = ((mm - 1) % 12) + 1
        out.append(f"{yy:04d}-{mm:02d}")
    return out


async def _ack(app_client, headers, ws, fy_start, month, run_id=None):
    body = {"fy_start": fy_start, "month": month}
    if run_id:
        body["run_id"] = run_id
    r = await app_client.patch(f"/api/sync/{ws}/coverage", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- §7.8 first_sync open / resume / kind gating --------------------------------------------------------------


async def test_first_sync_run_opens_and_moves_state_to_first_sync(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "first_sync"
    assert body["status"] == "running"
    assert len(body["coverage"]) == 5

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "first_sync"

    run = await _run_row(session, body["run_id"])
    assert run["status"] == "running"
    assert run["device_id"] == uuid.UUID(device_id)
    assert run["counters_at_start"] == {"alt_vch_id": 965, "alt_mst_id": 412}
    assert run["started_at"] is not None


async def test_second_first_sync_returns_the_open_one_with_coverage(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r1 = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id_1 = r1.json()["run_id"]

    r2 = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 9, "alt_mst_id": 9}},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id_1  # resume: the SAME run, not a new one
    assert len(r2.json()["coverage"]) == 5

    rows = (
        await session.execute(text("SELECT count(*) FROM sync_runs WHERE workspace_id = :w"), {"w": ws})
    ).scalar_one()
    assert rows == 1  # no second row was ever created


async def test_first_sync_refused_outside_awaiting_or_first_sync(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "ready")

    r = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "run_kind_not_allowed"

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"  # untouched


async def test_first_sync_allowed_from_error_when_cursors_null(app_client, session, clock):
    """F12 (controller ruling): a failed first_sync leaves `error`; a NEW first_sync must still be openable
    there while the cursors are still NULL (a first sync has never completed) — otherwise the workspace is
    stuck forever."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "error")

    r = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "first_sync"


async def test_first_sync_refused_from_error_when_cursors_already_set(app_client, session, clock):
    """F12's guard is NULL-cursors only — once a first sync has actually completed (cursors set), a LATER
    `error` (e.g. a failed incremental/full_resync) must NOT be treated as "never completed"."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "error")

    r = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "run_kind_not_allowed"


# --- §7.8 full_resync gating (D16) ------------------------------------------------------------------------


async def test_full_resync_without_confirmed_command_409_resync_not_confirmed(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "full_resync", "scope": {"company": True}}, headers=headers
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "resync_not_confirmed"


async def test_full_resync_with_pending_resync_command_allowed(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 5, "alt_mst_id": 5}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    run = await _run_row(session, r.json()["run_id"])
    assert run["status"] == "running" and run["kind"] == "full_resync"
    assert run["command_id"] == uuid.UUID(cmd_id)


# --- §8.6 restore_detected gating (carried from Task 6 review) ---------------------------------------------


async def test_incremental_refused_in_restore_detected_409_restore_detected(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "restore_detected")

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "restore_detected"


async def test_full_resync_without_command_in_restore_detected_is_resync_not_confirmed_not_restore_detected(
    app_client, session, clock
):
    """§8.6: restore_detected doesn't gate full_resync directly — only the confirmed-command check does (that
    check IS the allowed recovery path), so the missing-command case reports `resync_not_confirmed`, not
    `restore_detected`."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "restore_detected")

    r = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "full_resync", "scope": {"company": True}}, headers=headers
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "resync_not_confirmed"


async def test_full_resync_with_confirmed_command_allowed_in_restore_detected(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "restore_detected")
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 7, "alt_mst_id": 7}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    run = await _run_row(session, r.json()["run_id"])
    assert run["status"] == "running"


# --- §14.13 whole-company resync moves coverage; edges/backfill persisted ------------------------------------


async def test_company_resync_start_moves_complete_to_resyncing_and_running_to_pending(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    await session.execute(
        text("UPDATE sync_fy_coverage SET state='complete' WHERE workspace_id=:w AND fy_start='2022-04-01'"),
        {"w": ws},
    )
    await session.execute(
        text(
            "UPDATE sync_fy_coverage SET state='running', months_done='[\"2023-04\"]', months_complete=1 "
            "WHERE workspace_id=:w AND fy_start='2023-04-01'"
        ),
        {"w": ws},
    )
    await session.commit()

    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")
    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    rows = await _coverage_rows(session, ws)
    assert rows[date(2022, 4, 1)]["state"] == "resyncing"
    assert rows[date(2023, 4, 1)]["state"] == "pending"
    assert rows[date(2023, 4, 1)]["months_done"] == []
    assert rows[date(2023, 4, 1)]["months_complete"] == 0
    assert rows[date(2024, 4, 1)]["state"] == "pending"  # untouched, stays pending


# --- §7.10 coverage ack (idempotent) + edges + backfill persisted on sync_workspaces --------------------------


async def test_coverage_patch_idempotent_and_returns_edges(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r1 = await _ack(app_client, headers, ws, "2022-04-01", "2022-04")
    assert r1["state"] == "running"
    assert r1["months_done"] == ["2022-04"]
    assert r1["months_complete"] == 1

    r2 = await _ack(app_client, headers, ws, "2022-04-01", "2022-04")  # replay of the SAME month
    assert r2["state"] == "running"  # unchanged — idempotent
    assert r2["months_done"] == ["2022-04"]
    assert r2["months_complete"] == 1

    row = await _coverage_row(session, ws, date(2022, 4, 1))
    assert row["months_done"] == ["2022-04"]
    assert row["months_complete"] == 1
    assert row["state"] == "running"
    # Current FY (2026-04-01) is still `pending` -> edges are both None (edges() walks from the current FY).
    assert r2["edges"] == {"available": None, "verified": None}


async def test_edges_and_backfill_persisted_after_full_bind_window_acked(app_client, session, clock):
    """The refresh round-trip for the coverage-driven denormalised copy (§8.3 "backfill copy"): fully ack
    every FY's months, then re-read `sync_workspaces` from a fresh session and assert the WHOLE persisted
    state, not just one field."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    for fy_start, total in (
        ("2022-04-01", 12), ("2023-04-01", 12), ("2024-04-01", 12), ("2025-04-01", 12), ("2026-04-01", 6),
    ):
        for month in _fy_months(fy_start, total):
            await _ack(app_client, headers, ws, fy_start, month)

    rows = await _coverage_rows(session, ws)
    assert all(r["state"] == "complete" for r in rows.values())

    sw = await _sw_row(session, ws)
    assert sw["oldest_available_fy"] == date(2022, 4, 1)
    assert sw["oldest_complete_fy"] == date(2022, 4, 1)
    assert sw["backfill_state"] == "complete"
    assert sw["backfill_percent"] == Decimal("100.00")


async def test_add_fy_creates_complete_row(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await app_client.patch(
        f"/api/sync/{ws}/coverage", json={"fy_start": "2027-04-01", "action": "add_fy"}, headers=headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "complete"
    assert body["months_total"] == 0
    assert body["months_done"] == []

    row = await _coverage_row(session, ws, date(2027, 4, 1))
    assert row is not None
    assert row["state"] == "complete"
    assert row["months_total"] == 0
    assert row["months_done"] == []
    assert row["fy_end"] == date(2028, 3, 31)
    assert row["completed_at"] is not None


# --- §7.8 PATCH /runs/{id}: batches_missing, cursor rules (§15.3), completion transitions ---------------------


async def test_complete_run_with_missing_batches_409_batches_missing(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]
    await _insert_accepted_batch(session, ws, run_id, "b1")
    await _insert_accepted_batch(session, ws, run_id, "b2")

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "batches_declared": 3, "cursor_after": {"alt_vch_id": 2, "alt_mst_id": 2}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "batches_missing"
    assert r.json()["missing"] == 1

    run = await _run_row(session, run_id)
    assert run["status"] == "running"  # not completed — the rejection must not have mutated the run


async def test_incremental_completion_sets_cursor_after(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "ready")
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 10, "alt_mst_id": 5}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1,
              "cursor_after": {"alt_vch_id": 20, "alt_mst_id": 15}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 20 and sw["cursor_alt_mst_id"] == 15  # NOT counters_at_start (10, 5)
    assert sw["cursor_set_at"] is not None
    assert sw["sync_state"] == "ready"  # unaffected by an ordinary incremental completion

    run = await _run_row(session, run_id)
    assert run["status"] == "completed"
    assert run["cursor_after"] == {"alt_vch_id": 20, "alt_mst_id": 15}
    assert run["finished_at"] is not None


async def test_backfill_completion_leaves_cursor(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "ready")

    r_open = await app_client.post(f"/api/sync/{ws}/runs", json={"kind": "backfill"}, headers=headers)
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "progress_done": 1, "progress_total": 1},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 100 and sw["cursor_alt_mst_id"] == 50  # unchanged

    run = await _run_row(session, run_id)
    assert run["status"] == "completed"
    assert run["cursor_after"] is None


async def test_single_fy_resync_leaves_cursor(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "ready")
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="fy", fy_start="2023-04-01")

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"fy_start": "2023-04-01"}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 200, "alt_mst_id": 150}},
        headers=headers,
    )
    assert r_open.status_code == 200, r_open.text
    run_id = r_open.json()["run_id"]

    row_before = await _coverage_row(session, ws, date(2023, 4, 1))
    assert row_before["state"] == "pending"  # fy_resync_start on an already-pending row: still pending (A10)

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "progress_done": 1, "progress_total": 1},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 100 and sw["cursor_alt_mst_id"] == 50  # unchanged (NOT 200/150)
    assert sw["sync_state"] == "ready"  # single-FY resync never touches sync_state

    run = await _run_row(session, run_id)
    assert run["status"] == "completed" and run["cursor_after"] is None


async def test_company_resync_sets_counters_at_start_and_ready(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 5, 5)
    await _set_state_row(session, ws, "restore_detected")
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 50, "alt_mst_id": 40}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "progress_done": 1, "progress_total": 1},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 50 and sw["cursor_alt_mst_id"] == 40  # counters_at_start, D15
    assert sw["sync_state"] == "ready"

    run = await _run_row(session, run_id)
    assert run["status"] == "completed"
    assert run["cursor_after"] == {"alt_vch_id": 50, "alt_mst_id": 40}

    cmd = await _command_row(session, cmd_id)
    assert cmd["status"] == "done" and cmd["done_at"] is not None


# --- §8.2 first_sync completion / A8 fatal codes ------------------------------------------------------------


async def test_first_sync_scenario_24_months_to_ready(app_client, session, clock):
    """§14 scenario 3: for a September-IST bind/start (this fixture's fixed clock), the window is 18 months —
    FY 2025-26 (12, already fully past) + FY 2026-27 (6, Apr..Sep) — NOT the scenario's literal "24 months"
    (that count implies a start earlier in the FY calendar; A17: `months_total` is taken from
    `fy_rows_for_bind` for the fixed clock, not hard-coded)."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    counters = {"alt_vch_id": 965, "alt_mst_id": 412}

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": counters}, headers=headers
    )
    assert r_open.status_code == 200, r_open.text
    run_id = r_open.json()["run_id"]
    coverage_by_fy = {c["fy_start"]: c for c in r_open.json()["coverage"]}
    window_total = coverage_by_fy["2025-04-01"]["months_total"] + coverage_by_fy["2026-04-01"]["months_total"]
    assert window_total == 18  # A17 (not 24)

    for fy_start, total in (("2025-04-01", 12), ("2026-04-01", 6)):
        for month in _fy_months(fy_start, total):
            await _ack(app_client, headers, ws, fy_start, month, run_id=run_id)

    rows = await _coverage_rows(session, ws)
    assert rows[date(2025, 4, 1)]["state"] == "complete"
    assert rows[date(2026, 4, 1)]["state"] == "complete"
    # Older, pre-window FYs are untouched — still `pending`.
    assert rows[date(2022, 4, 1)]["state"] == "pending"

    r_complete = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 18, "progress_total": 18, "batches_declared": 0},
        headers=headers,
    )
    assert r_complete.status_code == 200, r_complete.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"
    assert sw["cursor_alt_vch_id"] == 965 and sw["cursor_alt_mst_id"] == 412
    assert sw["cursor_set_at"] is not None

    run = await _run_row(session, run_id)
    assert run["status"] == "completed"
    assert run["cursor_after"] == counters


async def test_first_sync_recomputes_window_months_when_started_a_month_later(app_client, session, clock):
    """F15 (controller ruling): window `months_total` is fixed at bind time to the bind month. If the first
    sync actually starts a month later, opening it must recompute the current FY's `months_total` to the run's
    OWN start month — otherwise the FY would go `complete` before its true last month is ever acked.

    Exercises ``runs.open_run`` directly with a locally-advanced ``FixedClock`` (rather than driving the app's
    OWN clock — and thus every device token's ``iat`` — forward): the fixture clock is already near "today", so
    advancing it far enough to cross an IST month boundary would push minted JWTs' ``iat`` into the future
    relative to the real wall clock PyJWT validates it against, an unrelated auth-layer artifact this test has
    no business tripping over. The subsequent coverage acks go through the real HTTP route (the app's own,
    UNCHANGED clock), so the persistence path itself is still exercised end-to-end.
    """
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    later_clock = FixedClock(clock.now() + timedelta(days=10))  # 2026-09-25 -> 2026-10-05 IST: a new month
    sw = await session.get(SyncWorkspace, ws)
    device = await session.get(AgentDevice, uuid.UUID(device_id))
    run = await runs.open_run(
        session, sw, device,
        runs.RunCreate(kind="first_sync", counters_at_start={"alt_vch_id": 1, "alt_mst_id": 1}),
        later_clock,
    )
    await session.commit()
    run_id = str(run.id)

    row = await _coverage_row(session, ws, date(2026, 4, 1))
    assert row["months_total"] == 7  # Apr..Oct, recomputed — NOT the bind-time 6 (Apr..Sep)

    # Ack only the original 6 (bind-time) months, through the real HTTP route -> the FY must NOT be complete yet.
    for month in _fy_months("2026-04-01", 6):
        await _ack(app_client, headers, ws, "2026-04-01", month, run_id=run_id)
    row_partial = await _coverage_row(session, ws, date(2026, 4, 1))
    assert row_partial["state"] == "running"
    assert row_partial["months_complete"] == 6

    # Ack the extra (7th) month -> now complete.
    await _ack(app_client, headers, ws, "2026-04-01", "2026-10", run_id=run_id)
    row_full = await _coverage_row(session, ws, date(2026, 4, 1))
    assert row_full["state"] == "complete"
    assert row_full["months_complete"] == 7


async def test_failed_first_sync_with_fatal_code_sets_error(app_client, session, clock):
    """A8 (controller ruling): `FATAL_RUN_CODES = {"company_mismatch", "unrecoverable"}`."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "failed", "error_code": "company_mismatch"}, headers=headers
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "error"

    run = await _run_row(session, run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "company_mismatch"
    assert run["finished_at"] is not None


async def test_failed_first_sync_with_other_code_keeps_first_sync(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "failed", "error_code": "missing_master"}, headers=headers
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "first_sync"  # unchanged — not a fatal code

    run = await _run_row(session, run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "missing_master"


# --- §8.1 cross-tenant / another device's run (run_closed / wrong_workspace) ---------------------------------


async def test_patch_run_closed_run_409(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]
    r1 = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "progress_done": 1, "progress_total": 1},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text

    r2 = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "progress_done": 1, "progress_total": 1},
        headers=headers,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"] == "run_closed"
