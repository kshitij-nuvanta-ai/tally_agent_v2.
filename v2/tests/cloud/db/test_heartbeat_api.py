"""Heartbeat, ``/state``, server -> agent commands, restore detection, re-link and ``sync-status`` (S1 spec
§7.6, §7.7, §7.13, §7.15, §8.2, §8.4, §8.6, D16, D17, D20 hook, D21, Q1, Q25, §14 scenario 14 restore half,
§15.1 rows 4-5). Every test re-reads from a fresh session after the call (§14).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from v2.cloud.sync import maintenance, state
from v2.contract import transcode
from v2.tests.cloud.conftest import login_device, make_workspace, requires_db, web_headers

pytestmark = requires_db

SYNC_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sync"

BIND_BODY = {
    "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
    "company_name": "Sharma & Sons' Probe Traders",
    "books_from": "20220401",
    "base_currency_name": "INR",
    "takeover": False,
}


def _read_fixture(name: str) -> str:
    return (SYNC_FIXTURES / name).read_text(encoding="utf-8")


def _fixture_counters(name: str) -> dict:
    raw = transcode.counters_from_xml(_read_fixture(name))
    return {"alt_vch_id": int(raw["altvchid"]), "alt_mst_id": int(raw["altmstid"])}


async def _login_and_bind(app_client, session, *, email=None, device_name="ACCOUNTS-PC", books_from="20220401"):
    uid, login_body, headers = await login_device(app_client, session, email=email, device_name=device_name)
    ws = await make_workspace(session, uid)
    body = {**BIND_BODY, "workspace_id": str(ws), "books_from": books_from}
    r = await app_client.post("/api/sync/company", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return uid, ws, login_body["device_id"], headers


async def _set_cursors(session, ws, vch: int, mst: int) -> None:
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


async def _device_row(session, device_id):
    return (
        await session.execute(text("SELECT last_seen_at FROM agent_devices WHERE id = :i"), {"i": device_id})
    ).mappings().first()


async def _command_row(session, cmd_id):
    return (
        await session.execute(text("SELECT status FROM sync_commands WHERE id = :i"), {"i": cmd_id})
    ).mappings().first()


def _hb_body(clock, **overrides) -> dict:
    return {
        "tally_status": "closed",
        "pc_clock": clock.now().isoformat(),
        **overrides,
    }


async def _heartbeat(client, headers, ws, clock, **overrides):
    return await client.post(f"/api/sync/{ws}/heartbeat", json=_hb_body(clock, **overrides), headers=headers)


# --- heartbeat: last_seen_at / clock_skew_s / last_synced_at untouched --------------------------------------


async def test_heartbeat_sets_last_seen_and_clock_skew_but_not_last_synced(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    skewed = (clock.now() + timedelta(seconds=3)).isoformat()

    r = await _heartbeat(app_client, headers, ws, clock, pc_clock=skewed)
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["last_seen_at"] is not None
    assert sw["last_heartbeat"]["clock_skew_s"] == 3
    assert sw["last_synced_at"] is None

    device = await _device_row(session, device_id)
    assert device["last_seen_at"] is not None


# --- D21: caught_up_at ----------------------------------------------------------------------------------------


async def test_heartbeat_counters_equal_cursors_sets_caught_up_at(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 50)
    await _set_state_row(session, ws, "ready")

    r = await _heartbeat(
        app_client, headers, ws, clock,
        tally_status="ours",
        seen_company={"guid": BIND_BODY["company_guid"], "name": BIND_BODY["company_name"]},
        counters={"alt_vch_id": 100, "alt_mst_id": 50},
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["caught_up_at"] is not None
    assert sw["sync_state"] == "ready"


# --- §8.6 restore detection (probe 13, controller-ruled fixtures) ----------------------------------------


async def test_heartbeat_counters_below_cursors_restore_detected(app_client, session, clock):
    """Controller ruling F6: cursors from ``p13_A_after_throwaway.xml`` (79/269), heartbeat counters from
    ``p13_A_after_restore_counters.xml`` (78/269) — ``alt_vch_id`` moves backwards -> ``restore_detected``."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cursor_counters = _fixture_counters("p13_A_after_throwaway.xml")
    assert cursor_counters == {"alt_vch_id": 79, "alt_mst_id": 269}
    heartbeat_counters = _fixture_counters("p13_A_after_restore_counters.xml")
    assert heartbeat_counters == {"alt_vch_id": 78, "alt_mst_id": 269}

    await _set_cursors(session, ws, cursor_counters["alt_vch_id"], cursor_counters["alt_mst_id"])
    await _set_state_row(session, ws, "ready")

    r = await _heartbeat(
        app_client, headers, ws, clock,
        tally_status="ours",
        seen_company={"guid": BIND_BODY["company_guid"], "name": BIND_BODY["company_name"]},
        counters=heartbeat_counters,
    )
    assert r.status_code == 200, r.text
    assert r.json()["sync_state"] == "restore_detected"

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "restore_detected"
    assert sw["restore_reason"] == "counters_backwards"
    assert sw["ladder"]["resync_offered"] == {"scope": "company", "reason": "restore"}


async def test_heartbeat_not_ours_never_triggers_restore(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 100, 100)
    await _set_state_row(session, ws, "ready")

    r = await _heartbeat(
        app_client, headers, ws, clock,
        tally_status="other_company",
        seen_company={"guid": "some-other-guid", "name": "Some Other Co"},
        counters={"alt_vch_id": 1, "alt_mst_id": 1},
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["sync_state"] == "ready"
    assert sw["restore_reason"] is None


async def test_no_company_status_stored_for_secured_prompt(app_client, session, clock):
    """§8.4/probe 24, LESSONS rule 26: a pending security login / TallyVault prompt answers an empty company
    list, indistinguishable from no company open — ``p24_C_security_login_pending_company_list.xml`` fixture
    justification: it is an empty ``<COLLECTION/>``, exactly what ``tally_status: no_company`` models."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await _heartbeat(app_client, headers, ws, clock, tally_status="no_company")
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["last_heartbeat"]["tally_status"] == "no_company"
    assert sw["sync_state"] == "awaiting_first_connection"  # unchanged — no restore/relink logic for this status


async def test_other_company_same_name_sets_relink_prompt(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r = await _heartbeat(
        app_client, headers, ws, clock,
        tally_status="other_company_same_name",
        seen_company={"guid": "new-guid-0000", "name": BIND_BODY["company_name"]},
    )
    assert r.status_code == 200, r.text

    sw = await _sw_row(session, ws)
    assert sw["relink_prompt"] == {"guid": "new-guid-0000", "name": BIND_BODY["company_name"]}


# --- D16: server -> agent commands ride the heartbeat -----------------------------------------------------


async def test_commands_delivered_once_then_acked_done(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r_cmd = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "recheck_now"}, headers=web_headers(uid)
    )
    assert r_cmd.status_code == 200, r_cmd.text
    cmd_id = r_cmd.json()["id"]
    assert r_cmd.json()["status"] == "pending"

    r1 = await _heartbeat(app_client, headers, ws, clock)
    assert r1.status_code == 200, r1.text
    assert r1.json()["commands"] == [{"id": cmd_id, "type": "recheck_now", "params": {}}]
    assert (await _command_row(session, cmd_id))["status"] == "delivered"

    r2 = await _heartbeat(app_client, headers, ws, clock)
    assert r2.status_code == 200, r2.text
    assert r2.json()["commands"] == []
    assert (await _command_row(session, cmd_id))["status"] == "delivered"  # still delivered, not re-sent

    r3 = await _heartbeat(app_client, headers, ws, clock, acked_commands=[cmd_id])
    assert r3.status_code == 200, r3.text
    assert r3.json()["commands"] == []
    assert (await _command_row(session, cmd_id))["status"] == "done"


# --- §7.15 re-link (device path) --------------------------------------------------------------------------


async def test_relink_requires_prompt_and_password(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r_no_prompt = await app_client.post(
        f"/api/sync/{ws}/relink",
        json={"new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "Passw0rd!Passw0rd"},
        headers=headers,
    )
    assert r_no_prompt.status_code == 409 and r_no_prompt.json()["error"] == "relink_not_prompted"

    await session.execute(
        text("UPDATE sync_workspaces SET relink_prompt = :p WHERE workspace_id = :w"),
        {"p": '{"guid": "new-guid-0000", "name": "New Co"}', "w": ws},
    )
    await session.commit()

    r_wrong_pw = await app_client.post(
        f"/api/sync/{ws}/relink",
        json={"new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "wrong-password"},
        headers=headers,
    )
    assert r_wrong_pw.status_code == 401 and r_wrong_pw.json()["error"] == "invalid_credentials"

    sw_untouched = await _sw_row(session, ws)
    assert sw_untouched["tally_company_guid"] == BIND_BODY["company_guid"]  # never touched by a failed attempt

    r_ok = await app_client.post(
        f"/api/sync/{ws}/relink",
        json={"new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "Passw0rd!Passw0rd"},
        headers=headers,
    )
    assert r_ok.status_code == 200, r_ok.text

    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == "new-guid-0000"
    assert sw["tally_company_name"] == "New Co"
    assert BIND_BODY["company_guid"] in sw["previous_company_guids"]
    assert sw["sync_state"] == "restore_detected"
    assert sw["restore_reason"] == "relink"
    assert sw["relink_prompt"] is None


async def test_web_confirm_relink_same_service_as_device(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await session.execute(
        text("UPDATE sync_workspaces SET relink_prompt = :p WHERE workspace_id = :w"),
        {"p": '{"guid": "web-new-guid", "name": "Web New Co"}', "w": ws},
    )
    await session.commit()

    r_wrong = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands",
        json={"type": "confirm_relink", "password": "wrong-password"},
        headers=web_headers(uid),
    )
    assert r_wrong.status_code == 401 and r_wrong.json()["error"] == "invalid_credentials"

    r_ok = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands",
        json={"type": "confirm_relink", "password": "Passw0rd!Passw0rd"},
        headers=web_headers(uid),
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["sync_state"] == "restore_detected"

    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == "web-new-guid"
    assert sw["tally_company_name"] == "Web New Co"
    assert BIND_BODY["company_guid"] in sw["previous_company_guids"]
    assert sw["sync_state"] == "restore_detected"
    assert sw["restore_reason"] == "relink"


# --- §7.7 state --------------------------------------------------------------------------------------------


async def test_state_endpoint_returns_cursors_coverage_open_runs_commands(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _set_cursors(session, ws, 42, 17)

    run_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sync_runs (id, workspace_id, device_id, kind, status, progress_done, progress_total) "
            "VALUES (:id, :w, :d, 'incremental', 'running', 3, 10)"
        ),
        {"id": run_id, "w": ws, "d": device_id},
    )
    await session.commit()

    r_cmd = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "recheck_now"}, headers=web_headers(uid)
    )
    assert r_cmd.status_code == 200
    cmd_id = r_cmd.json()["id"]

    r = await app_client.get(f"/api/sync/{ws}/state", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cursors"] == {"alt_vch_id": 42, "alt_mst_id": 17}
    assert len(body["coverage"]) == 5
    assert body["books_from"] == "2022-04-01"
    assert body["base_currency_name"] == "INR"
    assert [r["id"] for r in body["open_runs"]] == [str(run_id)]
    assert [c["id"] for c in body["commands"]] == [cmd_id]


# --- §7.13 sync-status --------------------------------------------------------------------------------------


async def test_sync_status_shape_and_suspect_reported_as_ok(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await session.execute(
        text("UPDATE sync_workspaces SET last_parity = :p WHERE workspace_id = :w"),
        {"p": '{"state": "suspect", "checked_at": "2026-09-25T06:30:00+00:00", "as_on": "2026-03-31", '
              '"mismatch_count": 1, "verified_from": "2022-04-01"}', "w": ws},
    )
    await session.commit()

    r = await app_client.get(f"/api/workspaces/{ws}/sync-status", headers=web_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["last_parity"]["state"] == "ok"  # suspect never surfaces on the web (Part 1 §6)
    for key in (
        "sync_state", "restore_reason", "first_sync", "last_synced_at", "caught_up_at", "agent", "backfill",
        "last_parity", "relink_prompt", "resync_offered", "quarantine_count", "storage_alert",
    ):
        assert key in body, key
    assert body["first_sync"] is None  # sync_state is awaiting_first_connection, not first_sync
    assert body["sync_state"] == "awaiting_first_connection"


async def test_sync_status_404_for_non_owner(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    other_uid, _, _ = await login_device(app_client, session)

    r = await app_client.get(f"/api/workspaces/{ws}/sync-status", headers=web_headers(other_uid))
    assert r.status_code == 404 and r.json()["error"] == "workspace_not_found"


# --- D20 hook: one maintenance slice per heartbeat -----------------------------------------------------------


async def test_heartbeat_runs_one_maintenance_slice(app_client, session, clock, monkeypatch):
    calls = []
    original = maintenance.run_slice

    async def _counting(session_arg, sw_arg, settings_arg, clock_arg):
        calls.append(1)
        return await original(session_arg, sw_arg, settings_arg, clock_arg)

    monkeypatch.setattr(state.maintenance, "run_slice", _counting)

    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await _heartbeat(app_client, headers, ws, clock)
    assert r.status_code == 200, r.text
    assert len(calls) == 1


# --- §15.1 row 4: /api/sync/{ws}/* auth matrix -----------------------------------------------------------


async def _matrix_setup(app_client, session, clock, variant: str):
    """Returns (ws, headers) for the given §15.1 row-4 auth-state variant, all built on top of a real bind so
    the "valid active" baseline is realistic, not a hand-seeded row."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session, device_name="ACCOUNTS-PC")
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()

    if variant == "expired":
        clock.advance(minutes=16)  # default device_access_minutes is 15
        return ws, headers
    if variant == "revoked":
        await session.execute(
            text("UPDATE agent_devices SET revoked_at = now(), revoke_reason = 'logout', is_active = false "
                 "WHERE id = :d"),
            {"d": device_id},
        )
        await session.commit()
        return ws, headers
    if variant == "unbound":
        r = await app_client.post(
            "/api/agent/auth/login",
            json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": "UNBOUND-PC",
                  "agent_version": "0.1.0"},
        )
        assert r.status_code == 200
        unbound_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return ws, unbound_headers
    if variant == "wrong_ws":
        other_ws = await make_workspace(session, uid, name="Other")
        return other_ws, headers
    if variant == "deleted_ws":
        await session.execute(text("UPDATE workspaces SET is_deleted = true WHERE id = :w"), {"w": ws})
        await session.commit()
        return ws, headers
    if variant == "not_active":
        r2 = await app_client.post(
            "/api/agent/auth/login",
            json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": "LAPTOP",
                  "agent_version": "0.1.0"},
        )
        assert r2.status_code == 200
        other_headers = {"Authorization": f"Bearer {r2.json()['access_token']}"}
        other_device_id = r2.json()["device_id"]
        await session.execute(
            text("UPDATE agent_devices SET workspace_id = :w, is_active = false WHERE id = :d"),
            {"w": ws, "d": other_device_id},
        )
        await session.commit()
        return ws, other_headers
    if variant == "web_jwt":
        return ws, web_headers(uid)
    raise AssertionError(variant)


@pytest.mark.parametrize(
    "variant, expected_status, expected_error",
    [
        ("expired", 401, "token_expired"),
        ("revoked", 401, "device_revoked"),
        ("unbound", 403, "wrong_workspace"),
        ("wrong_ws", 403, "wrong_workspace"),
        ("deleted_ws", 410, "workspace_deleted"),
        ("not_active", 409, "not_active_device"),
        ("web_jwt", 401, "token_invalid"),
    ],
)
async def test_sync_endpoint_matrix_row(app_client, session, clock, variant, expected_status, expected_error):
    ws, headers = await _matrix_setup(app_client, session, clock, variant)
    r = await _heartbeat(app_client, headers, ws, clock)
    assert r.status_code == expected_status, r.text
    assert r.json()["error"] == expected_error
