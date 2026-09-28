"""Runs (§7.8), server-side cursors (D15), FY coverage (§7.10, §8.3, §15.4) and the two edges + backfill copy
(S1 spec §7.8, §7.10, §8.2 run-driven transitions, §8.3, §15.3, §15.4, D15, D16, §14 scenarios 3, 12, 13, 14
restore half). Every test re-reads from a fresh session (``session`` — a separate ``AsyncSession`` from the one
the API request used) and asserts the whole persisted state (§14).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text

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


async def _login_second_device(app_client, email, device_name):
    r = await app_client.post(
        "/api/agent/auth/login",
        json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": device_name,
              "agent_version": "0.1.0"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


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


# --- I1 (task-7 review): take-over during a first_sync must not wedge the workspace -----------------------


async def test_takeover_during_first_sync_interrupts_old_run_new_device_resumes_to_ready(app_client, session, clock):
    """Device A opens first_sync -> B takes over (the REAL Task 5 `/api/sync/company` route, `takeover=true`)
    -> B opens first_sync and gets a NEW run. The old run is `interrupted` and coverage is intact. B completes
    it, and `sync_state` becomes `ready`."""
    uid, ws, device_a, headers_a = await _login_and_bind(app_client, session, device_name="ACCOUNTS-PC")
    r_open_a = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers_a,
    )
    assert r_open_a.status_code == 200, r_open_a.text
    old_run_id = r_open_a.json()["run_id"]
    coverage_before = await _coverage_rows(session, ws)

    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    body_b, headers_b = await _login_second_device(app_client, email, "LAPTOP")
    r_takeover = await app_client.post(
        "/api/sync/company", json={**BIND_BODY, "workspace_id": str(ws), "takeover": True}, headers=headers_b
    )
    assert r_takeover.status_code == 200, r_takeover.text

    r_open_b = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}},
        headers=headers_b,
    )
    assert r_open_b.status_code == 200, r_open_b.text
    new_run_id = r_open_b.json()["run_id"]
    assert new_run_id != old_run_id  # B does NOT get A's run back (resume is same-device only)

    old_run = await _run_row(session, old_run_id)
    assert old_run["status"] == "interrupted"
    assert old_run["finished_at"] is not None

    new_run = await _run_row(session, new_run_id)
    assert new_run["status"] == "running"
    assert new_run["device_id"] == uuid.UUID(body_b["device_id"])
    assert new_run["counters_at_start"] == {"alt_vch_id": 965, "alt_mst_id": 412}

    coverage_after = await _coverage_rows(session, ws)
    assert set(coverage_after) == set(coverage_before)  # coverage rows intact — same 5 FYs

    for fy_start, total in (("2025-04-01", 12), ("2026-04-01", 6)):
        for month in _fy_months(fy_start, total):
            await _ack(app_client, headers_b, ws, fy_start, month, run_id=new_run_id)

    r_complete = await app_client.patch(
        f"/api/sync/{ws}/runs/{new_run_id}",
        json={"status": "completed", "progress_done": 18, "progress_total": 18, "batches_declared": 0},
        headers=headers_b,
    )
    assert r_complete.status_code == 200, r_complete.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"
    assert sw["cursor_alt_vch_id"] == 965 and sw["cursor_alt_mst_id"] == 412


async def test_takeover_interrupts_a_non_first_sync_run_too(app_client, session, clock):
    """I1 is kind-agnostic: a take-over during an `incremental` also interrupts it, not only `first_sync`."""
    uid, ws, device_a, headers_a = await _login_and_bind(app_client, session, device_name="ACCOUNTS-PC")
    await _set_cursors(session, ws, 1, 1)
    await _set_state_row(session, ws, "ready")
    r_open_a = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers_a,
    )
    assert r_open_a.status_code == 200, r_open_a.text
    old_run_id = r_open_a.json()["run_id"]

    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    body_b, headers_b = await _login_second_device(app_client, email, "LAPTOP")
    r_takeover = await app_client.post(
        "/api/sync/company", json={**BIND_BODY, "workspace_id": str(ws), "takeover": True}, headers=headers_b
    )
    assert r_takeover.status_code == 200, r_takeover.text

    r_open_b = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "backfill"}, headers=headers_b
    )
    assert r_open_b.status_code == 200, r_open_b.text

    old_run = await _run_row(session, old_run_id)
    assert old_run["status"] == "interrupted"


# --- I2 (task-7 review): incremental needs a completed first sync's cursors --------------------------------


async def test_incremental_refused_with_null_cursors_409_run_kind_not_allowed(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)  # bind default: cursors NULL

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "run_kind_not_allowed"

    rows = (
        await session.execute(text("SELECT count(*) FROM sync_runs WHERE workspace_id = :w"), {"w": ws})
    ).scalar_one()
    assert rows == 0  # nothing was created


async def test_incremental_completion_from_error_with_cursors_set_moves_to_ready(app_client, session, clock):
    """I2: the §8.2 `error -> next run completed -> ready (or first_sync)` row applies to ANY run kind once the
    cursors exist — not only `first_sync`."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "error")
    # Every FY row complete -> the window is complete -> `error` -> `ready` (not `first_sync`).
    await session.execute(text("UPDATE sync_fy_coverage SET state = 'complete' WHERE workspace_id = :w"), {"w": ws})
    await session.commit()

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 100, "alt_mst_id": 50}},
        headers=headers,
    )
    assert r_open.status_code == 200, r_open.text
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "batches_declared": 0, "cursor_after": {"alt_vch_id": 120, "alt_mst_id": 60}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"
    assert sw["cursor_alt_vch_id"] == 120 and sw["cursor_alt_mst_id"] == 60


async def test_incremental_completion_from_error_window_incomplete_moves_to_first_sync(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "error")
    # Window FYs left `pending` (the bind default) -> the window is NOT complete -> `error` -> `first_sync`.

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 100, "alt_mst_id": 50}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "batches_declared": 0, "cursor_after": {"alt_vch_id": 110, "alt_mst_id": 55}},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "first_sync"
    assert sw["cursor_alt_vch_id"] == 110 and sw["cursor_alt_mst_id"] == 55  # cursor still moves either way


# --- Fix round 2 (controller ruling, re-review of I2): `error -> ready/first_sync` only once cursors exist ---


async def test_backfill_completion_in_error_with_null_cursors_stays_error_and_first_sync_recovers(
    app_client, session, clock
):
    """A first_sync that fails FATALLY before ever completing leaves the workspace `error` with NULL cursors.
    A `backfill` completing afterward must NOT move the workspace to `ready` with NULL cursors — `backfill`
    never sets a cursor (`cursor_on_completion` returns `None` for it), and `ready` with NULL cursors would
    refuse both a new `first_sync` (nowhere left to recover from) and every `incremental` (I2's NULL-cursor
    gate), wedging the workspace for good. It must stay `error`, exactly where F12 still allows a new
    `first_sync` to recover."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}},
        headers=headers,
    )
    first_run_id = r_open.json()["run_id"]
    for fy_start, total in (("2025-04-01", 12), ("2026-04-01", 6)):
        for month in _fy_months(fy_start, total):
            await _ack(app_client, headers, ws, fy_start, month, run_id=first_run_id)

    r_fail = await app_client.patch(
        f"/api/sync/{ws}/runs/{first_run_id}", json={"status": "failed", "error_code": "company_mismatch"},
        headers=headers,
    )
    assert r_fail.status_code == 200, r_fail.text
    sw_after_fail = await _sw_row(session, ws)
    assert sw_after_fail["sync_state"] == "error"
    assert sw_after_fail["cursor_alt_vch_id"] is None and sw_after_fail["cursor_alt_mst_id"] is None

    r_open_backfill = await app_client.post(f"/api/sync/{ws}/runs", json={"kind": "backfill"}, headers=headers)
    assert r_open_backfill.status_code == 200, r_open_backfill.text
    backfill_run_id = r_open_backfill.json()["run_id"]

    r_complete_backfill = await app_client.patch(
        f"/api/sync/{ws}/runs/{backfill_run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
        headers=headers,
    )
    assert r_complete_backfill.status_code == 200, r_complete_backfill.text

    sw_after_backfill = await _sw_row(session, ws)
    assert sw_after_backfill["sync_state"] == "error"  # NOT ready — this is the fix
    assert sw_after_backfill["cursor_alt_vch_id"] is None and sw_after_backfill["cursor_alt_mst_id"] is None

    backfill_run = await _run_row(session, backfill_run_id)
    assert backfill_run["status"] == "completed"  # the run itself still completes normally

    # F12 recovery: a NEW first_sync is still accepted from `error` (cursors NULL) — this is exactly the
    # recovery path the pre-fix bug would have closed off forever. Window coverage is already `complete` from
    # the first attempt's acks, so no re-acking is needed for it to complete straight through.
    r_open_2 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}},
        headers=headers,
    )
    assert r_open_2.status_code == 200, r_open_2.text
    second_run_id = r_open_2.json()["run_id"]
    assert second_run_id != first_run_id

    r_complete_2 = await app_client.patch(
        f"/api/sync/{ws}/runs/{second_run_id}",
        json={"status": "completed", "progress_done": 18, "progress_total": 18, "batches_declared": 0},
        headers=headers,
    )
    assert r_complete_2.status_code == 200, r_complete_2.text

    sw_final = await _sw_row(session, ws)
    assert sw_final["sync_state"] == "ready"
    assert sw_final["cursor_alt_vch_id"] == 965 and sw_final["cursor_alt_mst_id"] == 412


async def test_first_sync_completing_in_error_state_sets_cursors_and_reaches_ready(app_client, session, clock):
    """The existing F12 recovery path must stay green: a `first_sync` opened from `error` (NULL cursors) sets
    the cursors on its own completion and reaches `ready` — unaffected by the new cursors-set guard, which only
    restricts the generic `error` completion row for OTHER run kinds."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_state_row(session, ws, "error")  # a prior fatal failure; cursors already NULL by default

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}},
        headers=headers,
    )
    assert r_open.status_code == 200, r_open.text
    run_id = r_open.json()["run_id"]

    sw_mid = await _sw_row(session, ws)
    assert sw_mid["sync_state"] == "first_sync"  # F12: error -> first_sync on open

    for fy_start, total in (("2025-04-01", 12), ("2026-04-01", 6)):
        for month in _fy_months(fy_start, total):
            await _ack(app_client, headers, ws, fy_start, month, run_id=run_id)

    r_complete = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 18, "progress_total": 18, "batches_declared": 0},
        headers=headers,
    )
    assert r_complete.status_code == 200, r_complete.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"
    assert sw["cursor_alt_vch_id"] == 965 and sw["cursor_alt_mst_id"] == 412


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


# --- I4 (task-7 review): full_resync scope must match its command; a command binds to one run at a time ------


async def test_fy_command_cannot_open_a_whole_company_run(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="fy", fy_start="2023-04-01")

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "resync_not_confirmed"

    rows = (
        await session.execute(text("SELECT count(*) FROM sync_runs WHERE workspace_id = :w"), {"w": ws})
    ).scalar_one()
    assert rows == 0


async def test_company_command_cannot_open_an_fy_scoped_run(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"fy_start": "2023-04-01"}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "resync_not_confirmed"


async def test_same_command_cannot_open_two_runs(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r1 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    run_id_1 = r1.json()["run_id"]

    r2 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 2, "alt_mst_id": 2}},
        headers=headers,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"] == "resync_not_confirmed"

    rows = (
        await session.execute(text("SELECT count(*) FROM sync_runs WHERE workspace_id = :w"), {"w": ws})
    ).scalar_one()
    assert rows == 1  # only run_id_1 exists
    run1 = await _run_row(session, run_id_1)
    assert run1["status"] == "running"  # untouched by the refused second open


async def test_command_reusable_after_earlier_run_failed(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r1 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id_1 = r1.json()["run_id"]
    r_fail = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id_1}", json={"status": "failed", "error_code": "missing_master"},
        headers=headers,
    )
    assert r_fail.status_code == 200, r_fail.text

    r2 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 2, "alt_mst_id": 2}},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text  # reusable once the earlier run ended `failed`
    run_id_2 = r2.json()["run_id"]
    assert run_id_2 != run_id_1

    run2 = await _run_row(session, run_id_2)
    assert run2["status"] == "running" and run2["command_id"] == uuid.UUID(cmd_id)


async def test_command_reusable_after_earlier_run_interrupted(app_client, session, clock):
    """The I1 take-over interrupt is itself one of the two ways a command becomes reusable again."""
    uid, ws, device_a, headers_a = await _login_and_bind(app_client, session, device_name="ACCOUNTS-PC")
    cmd_id = await _confirm_resync_command(app_client, uid, ws, scope="company")

    r1 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers_a,
    )
    run_id_1 = r1.json()["run_id"]

    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    body_b, headers_b = await _login_second_device(app_client, email, "LAPTOP")
    r_takeover = await app_client.post(
        "/api/sync/company", json={**BIND_BODY, "workspace_id": str(ws), "takeover": True}, headers=headers_b
    )
    assert r_takeover.status_code == 200, r_takeover.text

    r2 = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "full_resync", "scope": {"company": True}, "command_id": cmd_id,
              "counters_at_start": {"alt_vch_id": 2, "alt_mst_id": 2}},
        headers=headers_b,
    )
    assert r2.status_code == 200, r2.text
    run_id_2 = r2.json()["run_id"]
    assert run_id_2 != run_id_1

    run1 = await _run_row(session, run_id_1)
    assert run1["status"] == "interrupted"  # I1's own interrupt happened when B opened run2 above


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
    await _set_cursors(session, ws, 1, 1)  # I2: incremental needs a completed first sync's cursors
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
    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 1 and sw["cursor_alt_mst_id"] == 1  # D15: unchanged by the rejection


async def test_complete_run_without_batches_declared_422(app_client, session, clock):
    """I3 (task-7 review): `batches_declared` is REQUIRED on completion — D15's "only if every batch the run
    declared was acked" cannot be bypassed by simply omitting the field."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 1, 1)
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "cursor_after": {"alt_vch_id": 99, "alt_mst_id": 99}},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "batches_declared_required"

    run = await _run_row(session, run_id)
    assert run["status"] == "running"  # not completed
    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 1 and sw["cursor_alt_mst_id"] == 1  # D15: cursors never moved


async def test_incremental_completion_sets_cursor_after(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 1, 1)  # I2: incremental needs a completed first sync's cursors
    await _set_state_row(session, ws, "ready")
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 10, "alt_mst_id": 5}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0,
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
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
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
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["cursor_alt_vch_id"] == 100 and sw["cursor_alt_mst_id"] == 50  # unchanged (NOT 200/150)
    assert sw["sync_state"] == "ready"  # single-FY resync never touches sync_state

    run = await _run_row(session, run_id)
    assert run["status"] == "completed" and run["cursor_after"] is None

    cmd = await _command_row(session, cmd_id)
    assert cmd["status"] == "done" and cmd["done_at"] is not None  # I4: single-FY resync also consumes its command


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
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
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

    Drives the SHARED app clock forward through the real HTTP routes (fix round 1: `decode_access` now
    validates both `exp` AND `iat` against the injected clock, so a clock advanced past real wall time no
    longer breaks device-token auth — the F15 test no longer needs to sidestep it by calling `open_run`
    directly). The original login token expires once the clock advances 10 days, so a real `/auth/refresh`
    call mints a fresh one for the SAME device — never a re-login, which would create a different device.
    """
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    ws = await make_workspace(session, uid)
    r_bind = await app_client.post(
        "/api/sync/company", json={**BIND_BODY, "workspace_id": str(ws)}, headers=headers
    )
    assert r_bind.status_code == 200, r_bind.text

    clock.advance(days=10)  # 2026-09-25 -> 2026-10-05 IST: a new month since bind
    r_refresh = await app_client.post(
        "/api/agent/auth/refresh", json={"refresh_token": login_body["refresh_token"]}
    )
    assert r_refresh.status_code == 200, r_refresh.text
    headers = {"Authorization": f"Bearer {r_refresh.json()['access_token']}"}

    r_open = await app_client.post(
        f"/api/sync/{ws}/runs",
        json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    assert r_open.status_code == 200, r_open.text
    run_id = r_open.json()["run_id"]
    current_row = next(c for c in r_open.json()["coverage"] if c["fy_start"] == "2026-04-01")
    assert current_row["months_total"] == 7  # Apr..Oct, recomputed — NOT the bind-time 6 (Apr..Sep)

    row = await _coverage_row(session, ws, date(2026, 4, 1))
    assert row["months_total"] == 7

    # Ack only the original 6 (bind-time) months -> the FY must NOT be complete yet.
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
    await _set_cursors(session, ws, 1, 1)  # I2: incremental needs a completed first sync's cursors
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "incremental", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers,
    )
    run_id = r_open.json()["run_id"]
    r1 = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text

    r2 = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}",
        json={"status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0},
        headers=headers,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"] == "run_closed"


async def test_require_open_run_404_when_run_not_found(app_client, session, clock):
    """403/404 branches of `_load_run_for_device` (consumed by `require_open_run`, Task 8's own interface) —
    flagged as untested in the review (Minor/Concern 2)."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{uuid.uuid4()}", json={"status": "completed", "batches_declared": 0}, headers=headers
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "run_not_found"


async def test_patch_run_403_wrong_workspace_when_another_devices_run(app_client, session, clock):
    """The new active device (post take-over) directly PATCHing the OLD device's run_id WITHOUT first calling
    `POST /runs` (which would have interrupted it, I1) — `_load_run_for_device`'s own 403 guard."""
    uid, ws, device_a, headers_a = await _login_and_bind(app_client, session, device_name="ACCOUNTS-PC")
    r_open = await app_client.post(
        f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": {"alt_vch_id": 1, "alt_mst_id": 1}},
        headers=headers_a,
    )
    run_id = r_open.json()["run_id"]

    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    body_b, headers_b = await _login_second_device(app_client, email, "LAPTOP")
    r_takeover = await app_client.post(
        "/api/sync/company", json={**BIND_BODY, "workspace_id": str(ws), "takeover": True}, headers=headers_b
    )
    assert r_takeover.status_code == 200, r_takeover.text

    r = await app_client.patch(
        f"/api/sync/{ws}/runs/{run_id}", json={"status": "completed", "batches_declared": 0}, headers=headers_b
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "wrong_workspace"
