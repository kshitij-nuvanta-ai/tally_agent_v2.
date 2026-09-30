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

from backend.sync import maintenance, state
from contract import transcode
from tests.sync.conftest import login_device, make_workspace, requires_db, web_headers

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

    r = await _heartbeat(
        app_client, headers, ws, clock, pc_clock=skewed,
        agent_version="0.1.0", tally_version="TallyPrime 7.0", last_error_code=None, breaker="closed",
        outbox_depth=0,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_time"] == clock.now().isoformat()
    assert body["sync_state"] == "awaiting_first_connection"
    assert body["cursors"] == {"alt_vch_id": None, "alt_mst_id": None}
    assert body["commands"] == []

    sw = await _sw_row(session, ws)
    assert sw["last_seen_at"] is not None
    assert sw["last_synced_at"] is None
    assert sw["sync_state"] == "awaiting_first_connection"
    # Fix round 1 / Important 3: assert the WHOLE stored last_heartbeat, not just clock_skew_s (per §4.2 — minus
    # counters, which this heartbeat didn't send).
    assert sw["last_heartbeat"] == {
        "agent_version": "0.1.0",
        "tally_version": "TallyPrime 7.0",
        "tally_status": "closed",
        "seen_company": None,
        "counters": None,
        "last_error_code": None,
        "breaker": "closed",
        "outbox_depth": 0,
        "clock_skew_s": 3,
    }

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
    # Fix round 1 / Important 3: restore detection must never move the cursors themselves, never set
    # caught_up_at (counters are BELOW cursors, not equal), and never touch last_synced_at (§8.5 — only an
    # accepted batch moves it).
    assert sw["cursor_alt_vch_id"] == cursor_counters["alt_vch_id"]
    assert sw["cursor_alt_mst_id"] == cursor_counters["alt_mst_id"]
    assert sw["caught_up_at"] is None
    assert sw["last_synced_at"] is None


async def test_heartbeat_after_restore_detected_keeps_returning_200_and_advances_last_seen(app_client, session, clock):
    """Critical 1 (fix round 1): once `restore_detected`, cursors don't move until a resync completes, so
    EVERY later `ours` heartbeat still reports backwards counters. Before the fix, `transition()` was called
    unconditionally and raised on the undefined `(restore_detected, counters_backwards)` pair — a 500 that also
    killed `last_seen_at` updates and command delivery for the rest of the request. Two more heartbeats after
    the first restore-detecting one must each return 200 and keep advancing `last_seen_at`."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cursor_counters = _fixture_counters("p13_A_after_throwaway.xml")
    heartbeat_counters = _fixture_counters("p13_A_after_restore_counters.xml")
    await _set_cursors(session, ws, cursor_counters["alt_vch_id"], cursor_counters["alt_mst_id"])
    await _set_state_row(session, ws, "ready")

    seen_company = {"guid": BIND_BODY["company_guid"], "name": BIND_BODY["company_name"]}
    r1 = await _heartbeat(
        app_client, headers, ws, clock, tally_status="ours", seen_company=seen_company,
        counters=heartbeat_counters,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["sync_state"] == "restore_detected"
    sw1 = await _sw_row(session, ws)
    first_last_seen = sw1["last_seen_at"]
    assert first_last_seen is not None

    clock.advance(minutes=1)
    r2 = await _heartbeat(
        app_client, headers, ws, clock, tally_status="ours", seen_company=seen_company,
        counters=heartbeat_counters,
    )
    assert r2.status_code == 200, r2.text  # no 500 — this is the regression this test guards
    assert r2.json()["sync_state"] == "restore_detected"
    sw2 = await _sw_row(session, ws)
    assert sw2["sync_state"] == "restore_detected"
    assert sw2["restore_reason"] == "counters_backwards"
    assert sw2["last_seen_at"] > first_last_seen  # still advancing, not frozen by a swallowed exception

    clock.advance(minutes=1)
    r3 = await _heartbeat(
        app_client, headers, ws, clock, tally_status="ours", seen_company=seen_company,
        counters=heartbeat_counters,
    )
    assert r3.status_code == 200, r3.text
    sw3 = await _sw_row(session, ws)
    assert sw3["last_seen_at"] > sw2["last_seen_at"]


async def test_confirm_resync_delivered_while_restore_detected(app_client, session, clock):
    """Critical 1 (fix round 1): the recovery path — a `confirm_resync` command queued while the workspace is
    `restore_detected` — must still be delivered on the next `ours` heartbeat that (still) reports backwards
    counters. Before the fix, that heartbeat 500'd before `commands.deliver_pending` ever ran, so the only way
    out of `restore_detected` was unreachable."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    cursor_counters = _fixture_counters("p13_A_after_throwaway.xml")
    heartbeat_counters = _fixture_counters("p13_A_after_restore_counters.xml")
    await _set_cursors(session, ws, cursor_counters["alt_vch_id"], cursor_counters["alt_mst_id"])
    await _set_state_row(session, ws, "restore_detected")

    r_cmd = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands",
        json={"type": "confirm_resync", "scope": "company"},
        headers=web_headers(uid),
    )
    assert r_cmd.status_code == 200, r_cmd.text
    cmd_id = r_cmd.json()["id"]

    r = await _heartbeat(
        app_client, headers, ws, clock, tally_status="ours",
        seen_company={"guid": BIND_BODY["company_guid"], "name": BIND_BODY["company_name"]},
        counters=heartbeat_counters,
    )
    assert r.status_code == 200, r.text
    assert r.json()["commands"] == [{"id": cmd_id, "type": "confirm_resync", "params": {"scope": "company"}}]
    assert (await _command_row(session, cmd_id))["status"] == "delivered"


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
    assert sw["sync_state"] == "awaiting_first_connection"  # unchanged — a prompt is not itself a transition


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


async def test_commands_ack_skips_malformed_id_but_acks_valid_ones(app_client, session, clock):
    """Controller ruling (fix round 1): one malformed id in `acked_commands` must not cancel acking the OTHER,
    well-formed ids in the same list."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)

    r_cmd = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "recheck_now"}, headers=web_headers(uid)
    )
    cmd_id = r_cmd.json()["id"]
    r1 = await _heartbeat(app_client, headers, ws, clock)
    assert r1.json()["commands"] == [{"id": cmd_id, "type": "recheck_now", "params": {}}]
    assert (await _command_row(session, cmd_id))["status"] == "delivered"

    r2 = await _heartbeat(app_client, headers, ws, clock, acked_commands=["not-a-uuid", cmd_id])
    assert r2.status_code == 200, r2.text
    assert (await _command_row(session, cmd_id))["status"] == "done"  # the valid id still got acked


# --- Important 2 (fix round 1): web command validation (§7.15) ---------------------------------------------


async def test_web_command_unknown_type_422(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "delete_everything"}, headers=web_headers(uid)
    )
    assert r.status_code == 422, r.text


async def test_web_command_confirm_resync_missing_scope_422(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "confirm_resync"}, headers=web_headers(uid)
    )
    assert r.status_code == 422, r.text


async def test_web_command_confirm_resync_fy_scope_without_fy_start_422(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "confirm_resync", "scope": "fy"},
        headers=web_headers(uid),
    )
    assert r.status_code == 422, r.text


async def test_web_command_confirm_resync_fy_scope_with_fy_start_enqueues(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands",
        json={"type": "confirm_resync", "scope": "fy", "fy_start": "2024-04-01"},
        headers=web_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["params"] == {"scope": "fy", "fy_start": "2024-04-01"}


async def test_web_command_confirm_relink_without_password_422(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands", json={"type": "confirm_relink"}, headers=web_headers(uid)
    )
    assert r.status_code == 422, r.text


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

    # Fix round 1 / Important 3: a failed attempt must leave the WHOLE row untouched, not just the GUID.
    sw_untouched = await _sw_row(session, ws)
    assert sw_untouched["tally_company_guid"] == BIND_BODY["company_guid"]
    assert sw_untouched["tally_company_name"] == BIND_BODY["company_name"]
    assert sw_untouched["relink_prompt"] == {"guid": "new-guid-0000", "name": "New Co"}
    assert sw_untouched["sync_state"] == "awaiting_first_connection"
    assert (sw_untouched["ladder"] or {}).get("resync_offered") is None
    assert sw_untouched["previous_company_guids"] == []

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
    assert sw["ladder"]["resync_offered"] == {"scope": "company", "reason": "relink"}


async def _login_second_device(app_client, email, device_name):
    r = await app_client.post(
        "/api/agent/auth/login",
        json={"email": email, "password": "Passw0rd!Passw0rd", "device_name": device_name,
              "agent_version": "0.1.0"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


async def test_relink_device_409_when_new_guid_bound_to_another_live_workspace(app_client, session, clock):
    """Important 1 (fix round 1): relink must not bypass the `company_bound_elsewhere` invariant binding
    enforces (§7.5/A6) — the new GUID is already bound (live) to a DIFFERENT workspace of the same user."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()

    # A second device of the SAME user, binding a DIFFERENT workspace of that same user to a different company —
    # this becomes the "elsewhere" target for the relink below.
    _, other_headers = await _login_second_device(app_client, email, "OTHER-PC")
    other_ws = await make_workspace(session, uid, name="Other")
    other_guid = "elsewhere-guid-0000"
    r_bind_other = await app_client.post(
        "/api/sync/company",
        json={**BIND_BODY, "workspace_id": str(other_ws), "company_guid": other_guid,
              "company_name": "Elsewhere Co"},
        headers=other_headers,
    )
    assert r_bind_other.status_code == 200, r_bind_other.text

    await session.execute(
        text("UPDATE sync_workspaces SET relink_prompt = :p WHERE workspace_id = :w"),
        {"p": f'{{"guid": "{other_guid}", "name": "Elsewhere Co"}}', "w": ws},
    )
    await session.commit()

    r = await app_client.post(
        f"/api/sync/{ws}/relink",
        json={"new_company_guid": other_guid, "company_name": "Elsewhere Co", "password": "Passw0rd!Passw0rd"},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "company_bound_elsewhere"
    assert r.json()["workspace_id"] == str(other_ws)

    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]  # untouched
    assert sw["sync_state"] == "awaiting_first_connection"


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

    # Fix round 1 / Important 3: a failed attempt must leave the WHOLE row untouched.
    sw_untouched = await _sw_row(session, ws)
    assert sw_untouched["tally_company_guid"] == BIND_BODY["company_guid"]
    assert sw_untouched["relink_prompt"] == {"guid": "web-new-guid", "name": "Web New Co"}
    assert sw_untouched["sync_state"] == "awaiting_first_connection"

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
    assert sw["relink_prompt"] is None
    assert sw["ladder"]["resync_offered"] == {"scope": "company", "reason": "relink"}


async def test_web_confirm_relink_409_when_new_guid_bound_to_another_live_workspace(app_client, session, clock):
    """Important 1 (fix round 1), web path."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()

    _, other_headers = await _login_second_device(app_client, email, "OTHER-PC")
    other_ws = await make_workspace(session, uid, name="Other")
    other_guid = "web-elsewhere-guid-0000"
    r_bind_other = await app_client.post(
        "/api/sync/company",
        json={**BIND_BODY, "workspace_id": str(other_ws), "company_guid": other_guid,
              "company_name": "Elsewhere Co"},
        headers=other_headers,
    )
    assert r_bind_other.status_code == 200, r_bind_other.text

    await session.execute(
        text("UPDATE sync_workspaces SET relink_prompt = :p WHERE workspace_id = :w"),
        {"p": f'{{"guid": "{other_guid}", "name": "Elsewhere Co"}}', "w": ws},
    )
    await session.commit()

    r = await app_client.post(
        f"/api/workspaces/{ws}/sync/commands",
        json={"type": "confirm_relink", "password": "Passw0rd!Passw0rd"},
        headers=web_headers(uid),
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "company_bound_elsewhere"
    assert r.json()["workspace_id"] == str(other_ws)

    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]  # untouched
    assert sw["sync_state"] == "awaiting_first_connection"


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


# --- S1 review I5: the relink password re-check shares the login limiter (failed attempts only, per email) ----------


async def _prompt(session, ws, guid="new-guid-0000"):
    await session.execute(text("UPDATE sync_workspaces SET relink_prompt = :p WHERE workspace_id = :w"),
                          {"p": '{"guid": "%s", "name": "New Co"}' % guid, "w": ws})
    await session.commit()


async def test_device_relink_wrong_passwords_are_throttled_429(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _prompt(session, ws)
    body = {"new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "wrong"}
    for _ in range(app_client.app.state.settings.login_rate_max):
        r = await app_client.post(f"/api/sync/{ws}/relink", json=body, headers=headers)
        assert (r.status_code, r.json()["error"]) == (401, "invalid_credentials")
    r = await app_client.post(f"/api/sync/{ws}/relink", json={**body, "password": "Passw0rd!Passw0rd"},
                              headers=headers)
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")      # even the right one is refused now
    assert "Retry-After" in r.headers
    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"] and sw["relink_prompt"] is not None


async def test_web_confirm_relink_wrong_passwords_are_throttled_429(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _prompt(session, ws)
    for _ in range(app_client.app.state.settings.login_rate_max):
        r = await app_client.post(f"/api/workspaces/{ws}/sync/commands",
                                  json={"type": "confirm_relink", "password": "wrong"}, headers=web_headers(uid))
        assert (r.status_code, r.json()["error"]) == (401, "invalid_credentials")
    r = await app_client.post(f"/api/workspaces/{ws}/sync/commands",
                              json={"type": "confirm_relink", "password": "Passw0rd!Passw0rd"}, headers=web_headers(uid))
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")
    sw = await _sw_row(session, ws)
    assert sw["tally_company_guid"] == BIND_BODY["company_guid"]


async def test_relink_failures_share_the_login_budget(app_client, session, clock):
    """Keyed by the user's email: relink guesses and /login guesses draw on one budget, so neither path multiplies
    the other's attempts."""
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    await _prompt(session, ws)
    email = (await session.execute(text("SELECT email FROM users WHERE id = :i"), {"i": uid})).scalar_one()
    for _ in range(app_client.app.state.settings.login_rate_max):
        r = await app_client.post(f"/api/sync/{ws}/relink", headers=headers, json={
            "new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "wrong"})
        assert r.status_code == 401
    r = await app_client.post("/api/agent/auth/login", json={"email": email, "password": "Passw0rd!Passw0rd",
                                                             "device_name": "X", "agent_version": "0.1.0"})
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")


async def test_successful_relink_does_not_consume_the_budget(app_client, session, clock):
    uid, ws, device_id, headers = await _login_and_bind(app_client, session)
    for _ in range(app_client.app.state.settings.login_rate_max - 1):
        await _prompt(session, ws)
        r = await app_client.post(f"/api/sync/{ws}/relink", headers=headers, json={
            "new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "wrong"})
        assert r.status_code == 401
    r = await app_client.post(f"/api/sync/{ws}/relink", headers=headers, json={
        "new_company_guid": "new-guid-0000", "company_name": "New Co", "password": "Passw0rd!Passw0rd"})
    assert r.status_code == 200, r.text
