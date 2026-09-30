"""``POST /api/sync/company`` (S1 spec §7.5, §8.3 bind rows, §9.3, A6, D5, D7, D14 GUID-only, Q4, Q30). Every
test re-reads ``sync_workspaces``, ``agent_devices`` and ``sync_fy_coverage`` in a fresh session after the call
(§14 "each re-reads from a fresh session")."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.sync import binding
from tests.sync.conftest import login_device, make_workspace, requires_db, web_headers

pytestmark = requires_db


BIND_BODY = {
    "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
    "company_name": "Sharma & Sons' Probe Traders",
    "books_from": "20220401",
    "base_currency_name": "INR",
    "takeover": False,
}


async def _sync_workspace_row(session, workspace_id):
    return (
        await session.execute(
            text(
                "SELECT tally_company_guid, tally_company_name, books_from, sync_state, active_device_id, "
                "bound_at, updated_at FROM sync_workspaces WHERE workspace_id = :w"
            ),
            {"w": workspace_id},
        )
    ).mappings().first()


async def _device_row(session, device_id):
    return (
        await session.execute(
            text(
                "SELECT workspace_id, is_active, revoked_at, revoke_reason FROM agent_devices WHERE id = :i"
            ),
            {"i": device_id},
        )
    ).mappings().first()


async def _coverage_rows(session, workspace_id):
    return (
        await session.execute(
            text(
                "SELECT fy_start, fy_end, state, months_total FROM sync_fy_coverage WHERE workspace_id = :w "
                "ORDER BY fy_start"
            ),
            {"w": workspace_id},
        )
    ).mappings().all()


async def _bind(client, headers, workspace_id, **overrides):
    body = {**BIND_BODY, "workspace_id": str(workspace_id), **overrides}
    return await client.post("/api/sync/company", json=body, headers=headers)


async def test_bind_unbound_workspace_creates_state_coverage_and_activates_device(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)

    r = await _bind(app_client, headers, ws)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] is True
    assert body["sync_state"] == "awaiting_first_connection"
    assert len(body["coverage"]) == 5

    coverage = await _coverage_rows(session, ws)
    assert len(coverage) == 5
    assert all(row["state"] == "pending" for row in coverage)

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]
    assert str(sw["active_device_id"]) == login_body["device_id"]

    device = await _device_row(session, login_body["device_id"])
    assert device["is_active"] is True
    assert str(device["workspace_id"]) == str(ws)

    # The returned access_token decodes with `ws` == the bound workspace (settings fixture uses "d" * 32).
    import jwt as pyjwt

    payload = pyjwt.decode(body["access_token"], "d" * 32, algorithms=["HS256"], options={"verify_exp": False})
    assert payload["ws"] == str(ws)


async def test_rebind_same_guid_same_device_is_noop(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)

    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200
    before = await _sync_workspace_row(session, ws)
    before_coverage = await _coverage_rows(session, ws)

    r2 = await _bind(app_client, headers, ws)
    assert r2.status_code == 200
    assert r2.json()["sync_state"] == r1.json()["sync_state"]

    after = await _sync_workspace_row(session, ws)
    after_coverage = await _coverage_rows(session, ws)
    assert after["bound_at"] == before["bound_at"]
    assert len(after_coverage) == len(before_coverage)


async def _login_second_device(app_client, session, email, device_name):
    r = await app_client.post(
        "/api/agent/auth/login",
        json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": device_name,
              "agent_version": "0.1.0"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


async def test_other_active_device_without_takeover_409(app_client, session):
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(app_client, headers2, ws)
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"] == "takeover_required"
    assert body["active_device"]["device_name"] == "ACCOUNTS-PC"


async def test_takeover_with_fresh_login_revokes_old(app_client, session):
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(app_client, headers2, ws, takeover=True)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "cursors" in body and "coverage" in body

    old_device = await _device_row(session, login_body["device_id"])
    assert old_device["revoked_at"] is not None
    assert old_device["revoke_reason"] == "taken_over"
    assert old_device["is_active"] is False

    new_device = await _device_row(session, login_body2["device_id"])
    assert new_device["is_active"] is True


async def test_takeover_with_login_older_than_10_minutes_401_reauth_required(app_client, session, clock):
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    clock.advance(minutes=11)
    r2 = await _bind(app_client, headers2, ws, takeover=True)
    assert r2.status_code == 401
    assert r2.json()["error"] == "reauth_required"


async def test_partial_unique_index_rejects_second_active_row(app_client, session):
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE agent_devices SET workspace_id = :w, is_active = true WHERE id = :d"),
            {"w": ws, "d": login_body2["device_id"]},
        )
    await session.rollback()


class _TwoPartyBarrier:
    """Same technique as ``test_auth_api.py``'s refresh-race test: forces two coroutines to rendezvous at a
    single point before either continues past it, so a race is exercised on every run instead of only when
    the event loop happens to interleave two in-process ASGI calls that way."""

    def __init__(self, parties: int):
        self._parties = parties
        self._count = 0
        self._lock = asyncio.Lock()
        self._released = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._count += 1
            if self._count >= self._parties:
                self._released.set()
        await asyncio.wait_for(self._released.wait(), 10)


async def test_concurrent_first_binds_leave_exactly_one_winner(app_client, session, monkeypatch):
    """F22 / controller ruling: two devices of the SAME user bind the SAME unbound workspace concurrently.
    Deterministic via a barrier placed right before the ON CONFLICT insert, so both requests are guaranteed to
    attempt it at the same time on every run — the exact overlap the naive ``get(with_for_update=True)``
    returning ``None`` for both racers needs to turn into a 500. Exactly one becomes the active device; the
    loser gets the ordinary §7.5 "already bound, no takeover" outcome (409 `takeover_required`), never a 500."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")

    barrier = _TwoPartyBarrier(2)
    original = binding._first_insert_attempt

    async def _barriered(session_arg, body_arg, books_from_arg, clock_arg):
        await barrier.wait()
        return await original(session_arg, body_arg, books_from_arg, clock_arg)

    monkeypatch.setattr(binding, "_first_insert_attempt", _barriered)

    results = await asyncio.gather(
        _bind(app_client, headers, ws),
        _bind(app_client, headers2, ws),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"] == "takeover_required"

    d1 = await _device_row(session, login_body["device_id"])
    d2 = await _device_row(session, login_body2["device_id"])
    assert sorted([d1["is_active"], d2["is_active"]]) == [False, True]

    sw = await _sync_workspace_row(session, ws)
    assert sw is not None
    coverage = await _coverage_rows(session, ws)
    assert len(coverage) == 5  # coverage created exactly once, not twice


async def test_concurrent_takeovers_leave_exactly_one_active(app_client, session, monkeypatch):
    """§16: an already-bound workspace with device1 active; two OTHER devices of the same user both bind with
    ``takeover=true`` at once. Deterministic via the same barrier, placed at the row lock (`_lock_workspace`) —
    the point every branch decision hangs off — so both requests genuinely contend for the same row on every
    run. Exactly one ends up active; the other is revoked (a 200 that got immediately taken over back) or 409."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    login_body3, headers3 = await _login_second_device(app_client, session, email, "PHONE")

    barrier = _TwoPartyBarrier(2)
    original = binding._lock_workspace

    async def _barriered(session_arg, workspace_id_arg):
        await barrier.wait()
        return await original(session_arg, workspace_id_arg)

    monkeypatch.setattr(binding, "_lock_workspace", _barriered)

    results = await asyncio.gather(
        _bind(app_client, headers2, ws, takeover=True),
        _bind(app_client, headers3, ws, takeover=True),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses in ([200, 200], [200, 409]), [r.text for r in results]

    rows = [
        await _device_row(session, login_body["device_id"]),
        await _device_row(session, login_body2["device_id"]),
        await _device_row(session, login_body3["device_id"]),
    ]
    active_flags = [row["is_active"] for row in rows]
    assert active_flags.count(True) == 1


async def test_device_moving_workspaces_does_not_get_revoked_by_a_later_takeover_elsewhere(app_client, session):
    """Fix round 1 #2: device D binds W1 (active there), then D itself binds W2 (allowed — §15.1 gives an
    active device ✓ on /api/sync/company). A different device E now takes over W1 with `takeover=true`. Before
    the fix, `_activate` revoked whatever `sw.active_device_id` on W1 still pointed at — D — even though D had
    since moved to W2. After the fix, D moving to W2 clears W1's `active_device_id`, so E's take-over of W1
    finds nobody active there (re-bind-when-empty) and never touches D."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws1 = await make_workspace(session, uid, name="W1")
    ws2 = await make_workspace(session, uid, name="W2")

    r1 = await _bind(app_client, headers, ws1)
    assert r1.status_code == 200

    r2 = await _bind(app_client, headers, ws2, company_guid="ws2-guid-0000", company_name="W2 Co")
    assert r2.status_code == 200, r2.text

    d_row = await _device_row(session, login_body["device_id"])
    assert str(d_row["workspace_id"]) == str(ws2)
    assert d_row["is_active"] is True

    sw1_after_move = await _sync_workspace_row(session, ws1)
    assert sw1_after_move["active_device_id"] is None  # cleared when D moved to W2

    login_body_e, headers_e = await _login_second_device(app_client, session, email, "OFFICE-PC")
    r3 = await _bind(app_client, headers_e, ws1, takeover=True)
    assert r3.status_code == 200, r3.text

    d_row_after = await _device_row(session, login_body["device_id"])
    assert d_row_after["is_active"] is True  # D is untouched — still active, still on W2
    assert d_row_after["revoked_at"] is None
    assert str(d_row_after["workspace_id"]) == str(ws2)

    e_row = await _device_row(session, login_body_e["device_id"])
    assert e_row["is_active"] is True
    assert str(e_row["workspace_id"]) == str(ws1)


async def test_workspace_of_other_user_404_workspace_not_found(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    other_uid, _, _ = await login_device(app_client, session)
    ws = await make_workspace(session, other_uid)

    r = await _bind(app_client, headers, ws)
    assert r.status_code == 404 and r.json()["error"] == "workspace_not_found"


async def test_deleted_workspace_410_workspace_deleted(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid, is_deleted=True)

    r = await _bind(app_client, headers, ws)
    assert r.status_code == 410 and r.json()["error"] == "workspace_deleted"


async def test_company_bound_elsewhere_409_names_the_workspace(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws1 = await make_workspace(session, uid, name="W1")
    ws2 = await make_workspace(session, uid, name="W2")

    r1 = await _bind(app_client, headers, ws1)
    assert r1.status_code == 200

    r2 = await _bind(app_client, headers, ws2)  # W2 is unbound, but the GUID is already bound to W1
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"] == "company_bound_elsewhere"
    assert body["workspace_id"] == str(ws1)


async def test_rebind_different_guid_allowed_while_no_batch_accepted(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    r2 = await _bind(
        app_client, headers, ws,
        company_guid="different-guid-0000", company_name="Different Co", books_from="20230401",
    )
    assert r2.status_code == 200, r2.text

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] == "different-guid-0000"
    assert sw["tally_company_name"] == "Different Co"

    coverage = await _coverage_rows(session, ws)
    assert all(row["fy_start"].isoformat() >= "2023-04-01" for row in coverage)


async def test_rebind_different_guid_with_other_active_device_no_takeover_409(app_client, session):
    """Fix round 1 #3 (controller ruling): D7's take-over guard applies to ANY displacement of a live active
    device, whatever GUID is being bound — not just the same-GUID branch. Device B tries to re-bind W1
    (currently active on device A) to a DIFFERENT guid without `takeover` — 409 `takeover_required`, and the
    binding must not be touched at all (no silent re-bind, no revoke)."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(app_client, headers2, ws, company_guid="different-guid-0000", company_name="Different Co")
    assert r2.status_code == 409
    assert r2.json()["error"] == "takeover_required"

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]  # unchanged — never silently re-bound
    device1 = await _device_row(session, login_body["device_id"])
    assert device1["is_active"] is True and device1["revoked_at"] is None


async def test_rebind_different_guid_with_other_active_device_stale_login_401(app_client, session, clock):
    """Same guard, `takeover=true` but the login is >10 min old — 401 `reauth_required`, binding untouched."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    clock.advance(minutes=11)
    r2 = await _bind(
        app_client, headers2, ws, takeover=True,
        company_guid="different-guid-0000", company_name="Different Co",
    )
    assert r2.status_code == 401
    assert r2.json()["error"] == "reauth_required"

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]
    device1 = await _device_row(session, login_body["device_id"])
    assert device1["is_active"] is True and device1["revoked_at"] is None


async def test_rebind_different_guid_with_other_active_device_fresh_takeover_200(app_client, session):
    """Same guard, `takeover=true` with a fresh login — the old device is revoked `taken_over`, the new one
    is activated, AND the different-GUID re-bind itself goes through (guid/name replaced, coverage recreated)."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(
        app_client, headers2, ws, takeover=True,
        company_guid="different-guid-0000", company_name="Different Co",
    )
    assert r2.status_code == 200, r2.text

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] == "different-guid-0000"

    old_device = await _device_row(session, login_body["device_id"])
    assert old_device["is_active"] is False and old_device["revoke_reason"] == "taken_over"

    new_device = await _device_row(session, login_body2["device_id"])
    assert new_device["is_active"] is True


async def test_concurrent_first_binds_different_guids_loser_hits_the_guard(app_client, session, monkeypatch):
    """Fix round 1 #3: two devices of the SAME user race to bind the SAME unbound workspace with DIFFERENT
    GUIDs, deterministically via the same barrier technique as `test_concurrent_first_binds_leave_exactly_one_winner`.
    The loser must land on the D7 guard (409 `takeover_required`, since it defaults `takeover=false`) — never
    silently re-bind over the winner's row and revoke it."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")

    barrier = _TwoPartyBarrier(2)
    original = binding._first_insert_attempt

    async def _barriered(session_arg, body_arg, books_from_arg, clock_arg):
        await barrier.wait()
        return await original(session_arg, body_arg, books_from_arg, clock_arg)

    monkeypatch.setattr(binding, "_first_insert_attempt", _barriered)

    results = await asyncio.gather(
        _bind(app_client, headers, ws),
        _bind(app_client, headers2, ws, company_guid="different-guid-0000", company_name="Different Co"),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"] == "takeover_required"

    sw = await _sync_workspace_row(session, ws)
    assert sw["tally_company_guid"] in (BIND_BODY["company_guid"], "different-guid-0000")

    d1 = await _device_row(session, login_body["device_id"])
    d2 = await _device_row(session, login_body2["device_id"])
    actives = [d1["is_active"], d2["is_active"]]
    assert actives.count(True) == 1
    # The loser hit the guard BEFORE any mutation (it's not an actual take-over) — it must never be revoked.
    loser_row = d1 if not d1["is_active"] else d2
    assert loser_row["revoked_at"] is None


async def test_rebind_different_guid_refused_once_data_exists_409(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    run_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sync_runs (id, workspace_id, device_id, kind, status, created_at) "
            "VALUES (:id, :w, :d, 'incremental', 'completed', now())"
        ),
        {"id": run_id, "w": ws, "d": login_body["device_id"]},
    )
    await session.execute(
        text(
            "INSERT INTO sync_batches (workspace_id, run_id, batch_id, request_sha256, object_count, status) "
            "VALUES (:w, :r, 'b1', 'sha', 1, 'accepted')"
        ),
        {"w": ws, "r": run_id},
    )
    await session.commit()

    r2 = await _bind(app_client, headers, ws, company_guid="different-guid-0000", company_name="Different Co")
    assert r2.status_code == 409 and r2.json()["error"] == "workspace_bound_to_other_company"


async def test_rebind_when_no_active_device_after_web_delete(app_client, session):
    """Fix round 1 #1: re-bind-when-empty through the REAL revocation path — `DELETE /api/devices/{id}` (web
    JWT) only ever touches `agent_devices`, never `sync_workspaces.active_device_id`. Before the fix, the
    dangling pointer meant the next bind fell into the "other active device" branch and got 409
    `takeover_required` naming an already-revoked device. `_effective_active_device` must resolve the revoked
    device as "no active device", so the second device binds freely, no `takeover` needed, and the revoked
    device keeps its ORIGINAL `revoke_reason` (`user_removed`), not `taken_over`."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    r_delete = await app_client.delete(f"/api/devices/{login_body['device_id']}", headers=web_headers(uid))
    assert r_delete.status_code == 204

    sw_after_delete = await _sync_workspace_row(session, ws)
    assert str(sw_after_delete["active_device_id"]) == login_body["device_id"]  # the dangling pointer, pre-fix
    before_coverage = await _coverage_rows(session, ws)

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(app_client, headers2, ws)  # no takeover flag needed — the "active" device is dead
    assert r2.status_code == 200, r2.text

    after_coverage = await _coverage_rows(session, ws)
    assert len(after_coverage) == len(before_coverage)  # coverage not recreated

    device1 = await _device_row(session, login_body["device_id"])
    assert device1["revoke_reason"] == "user_removed"  # unchanged — NOT overwritten to "taken_over"

    device2 = await _device_row(session, login_body2["device_id"])
    assert device2["is_active"] is True

    sw = await _sync_workspace_row(session, ws)
    assert str(sw["active_device_id"]) == login_body2["device_id"]


async def test_rebind_when_no_active_device_after_logout(app_client, session):
    """Same as above, through the agent's own `POST /api/agent/auth/logout` (device token) instead of the web
    DELETE route — another revocation writer that only touches `agent_devices`."""
    uid, login_body, headers = await login_device(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    ws = await make_workspace(session, uid)
    r1 = await _bind(app_client, headers, ws)
    assert r1.status_code == 200

    r_logout = await app_client.post("/api/agent/auth/logout", headers=headers)
    assert r_logout.status_code == 204

    login_body2, headers2 = await _login_second_device(app_client, session, email, "LAPTOP")
    r2 = await _bind(app_client, headers2, ws)  # no takeover flag needed
    assert r2.status_code == 200, r2.text

    device1 = await _device_row(session, login_body["device_id"])
    assert device1["revoke_reason"] == "logout"  # unchanged — NOT overwritten to "taken_over"

    device2 = await _device_row(session, login_body2["device_id"])
    assert device2["is_active"] is True


async def test_bind_never_writes_workspaces_table(app_client, session):
    uid, login_body, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)

    before = (
        await session.execute(text("SELECT * FROM workspaces ORDER BY id"))
    ).mappings().all()

    r = await _bind(app_client, headers, ws)
    assert r.status_code == 200

    after = (
        await session.execute(text("SELECT * FROM workspaces ORDER BY id"))
    ).mappings().all()
    assert [dict(row) for row in before] == [dict(row) for row in after]
