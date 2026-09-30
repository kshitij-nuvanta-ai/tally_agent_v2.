"""``POST /api/sync/{ws}/parity`` + ``last_parity`` + the §10.1 preconditions (S1 spec §7.13, §7.14, §10, §14.16,
§15.5 every row; task 10c + its controller carries).

Data (controller ruling F9): **FakeBooks company B** (`parity_fakebooks`) -- the whole company-B dataset written by the
real loader, then read back through the agent's request builders and the cloud's transcoder -- is ingested through
the real ``/batches`` endpoint and its Trial Balances through ``/snapshots``; nothing is inserted directly, so ingest
and parity are proven together. Quiescence uses the REAL probe-19 counter captures. Every persisted assertion
re-reads from a fresh session (§14).

Clock: the conftest ``FixedClock`` (2026-09-25 06:30 UTC; IST FY 2026-27). Tally's current period (F25) is FY
2025-26, so the daily as-on is 31-03-2026.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from backend.sync.parity.engine import SUMS_SQL
from contract import transcode
from tests.sync import parity_fakebooks as pf
from tests.sync import realdata
from tests.sync.conftest import (build_sync_app, login_device, make_workspace, requires_db, restart_app_engine,
                                 web_headers)
from tests.sync.db.ingest_helpers import batch, fresh, one, post_batch, rows, sw_row

pytestmark = requires_db

UTC = timezone.utc
CAPTURED = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)            # this capture's ledger re-read
CAPTURE_STARTED = datetime(2026, 9, 25, 5, 59, tzinfo=UTC)
SNAP_AT = datetime(2026, 9, 25, 6, 1, tzinfo=UTC)
AS_ON = date(2026, 3, 31)
FY25, FY24 = date(2025, 4, 1), date(2024, 4, 1)
LADDER_KEYS = {"state", "heal_attempts", "resync_offered_fy"}


@dataclass
class B:
    ws: uuid.UUID
    uid: uuid.UUID
    headers: dict
    cap: pf.Capture
    run_id: str
    coverage: list[dict]
    counters: dict
    early: dict | None = None
    books_from: date = pf.BOOKS_FROM


def _months(fy_start: date, n: int, books_from: date = pf.BOOKS_FROM) -> list[str]:
    start = max(fy_start, books_from)
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def _ingest(app_client, b: B, objects: list[dict], *, run_id: str | None = None, size: int = 400) -> int:
    n = 0
    for i in range(0, len(objects), size):
        r = await post_batch(app_client, b.ws, b.headers,
                             batch(run_id or b.run_id, objects[i:i + size], company_guid=b.cap.guid))
        assert r.status_code == 200, r.text
        n += 1
    return n


async def _ack_fy(app_client, b: B, fy: date) -> None:
    row = next(r for r in b.coverage if r["fy_start"] == fy.isoformat())
    for month in _months(fy, row["months_total"], b.books_from):
        r = await app_client.patch(f"/api/sync/{b.ws}/coverage", json={"fy_start": fy.isoformat(), "month": month},
                                   headers=b.headers)
        assert r.status_code == 200, r.text


async def _snap(app_client, b: B, report_type: str, as_on: date, *, at: datetime = SNAP_AT, **kw) -> dict:
    body = b.cap.snapshot(report_type, as_on, at, **kw)
    body["counters"] = b.counters
    r = await app_client.post(f"/api/sync/{b.ws}/snapshots", json=body, headers=b.headers)
    assert r.status_code == 200, r.text
    return r.json()


def _anchor_as_on(edge: date, books_from: date = pf.BOOKS_FROM) -> date:
    return edge if edge == books_from else edge - timedelta(days=1)


async def setup_b(app_client, session, *, fake=None, edge: date = FY25, omit: tuple[str, ...] = (),
                  omit_guids: tuple[str, ...] = (), counters: dict | None = None, complete: bool = True,
                  ack: bool = True, snapshots: tuple[str, ...] = ("tb", "lw", "lw_anchor"),
                  early_tb: bool = False, books_from: date = pf.BOOKS_FROM) -> B:
    """Bind FakeBooks B, first-sync it with the verified edge at ``edge`` (every FY from ``edge`` acked), complete
    the run (cursors = counters), and post the as-on TBs + the D9 anchor through the real endpoints."""
    fake = fake or pf.books()
    cap = pf.Capture(fake)
    counters = counters or cap.counters
    uid, _, headers = await login_device(app_client, session)
    ws = await make_workspace(session, uid)
    r = await app_client.post("/api/sync/company", headers=headers, json={
        "workspace_id": str(ws), "company_guid": cap.guid, "company_name": pf.B_NAME,
        "books_from": books_from.strftime("%Y%m%d"),
        "base_currency_name": "INR", "takeover": False})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{ws}/runs", json={"kind": "first_sync", "counters_at_start": counters},
                              headers=headers)
    assert r.status_code == 200, r.text
    b = B(ws, uid, headers, cap, r.json()["run_id"], r.json()["coverage"], counters, books_from=books_from)
    if early_tb:                                  # the group TB captured before any master reached the cloud
        b.early = await _snap(app_client, b, "trial_balance", AS_ON, at=SNAP_AT - timedelta(minutes=30))

    batches = await _ingest(app_client, b, cap.masters())
    batches += await _ingest(app_client, b, cap.balances(CAPTURED))
    vouchers = [v for v in cap.vouchers(edge, AS_ON, omit=omit) if v["data"]["guid"] not in omit_guids]
    batches += await _ingest(app_client, b, vouchers)
    if ack:
        for row in b.coverage:
            if date.fromisoformat(row["fy_start"]) >= pf.fy_start(edge):
                await _ack_fy(app_client, b, date.fromisoformat(row["fy_start"]))
    if complete:
        r = await app_client.patch(f"/api/sync/{ws}/runs/{b.run_id}", headers=headers, json={
            "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": batches})
        assert r.status_code == 200, r.text
    if "tb" in snapshots:
        await _snap(app_client, b, "trial_balance", AS_ON)
    if "lw" in snapshots:
        await _snap(app_client, b, "trial_balance_ledgerwise", AS_ON)
    if "lw_anchor" in snapshots:
        await _snap(app_client, b, "trial_balance_ledgerwise", _anchor_as_on(edge, books_from), purpose="anchor")
    if "tb_anchor" in snapshots:
        await _snap(app_client, b, "trial_balance", _anchor_as_on(edge, books_from), purpose="anchor")
    return b


def body(b: B, **kw) -> dict:
    out = {"scope": "daily", "as_on_date": "31-03-2026", "capture_started_at": CAPTURE_STARTED.isoformat(),
           "counters_before": b.counters, "counters_after": b.counters, "remediation_done": []}
    out.update(kw)
    return out


async def parity(app_client, b: B, **kw) -> dict:
    r = await app_client.post(f"/api/sync/{b.ws}/parity", json=body(b, **kw), headers=b.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def run_row(s, run_id: str) -> dict:
    return await one(s, "SELECT * FROM parity_runs WHERE id = :i", i=uuid.UUID(run_id))


async def line_rows(s, run_id: str) -> list[dict]:
    return await rows(s, "SELECT * FROM parity_lines WHERE run_id = :i ORDER BY scope, name", i=uuid.UUID(run_id))


def by_name(lines: list[dict], scope: str = "ledger") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for l in lines:
        if l["scope"] == scope:
            out.setdefault(l["name"], []).append(l)
    return out


async def whole_state(engine, b: B) -> dict:
    """The persisted parity state a refresh would rehydrate: every run, every line, last_parity, ladder."""
    async with fresh(engine) as s:
        sw = await sw_row(s, b.ws)
        return {"runs": await rows(s, "SELECT * FROM parity_runs WHERE workspace_id = :w ORDER BY created_at, id",
                                   w=b.ws),
                "lines": await rows(s, "SELECT * FROM parity_lines WHERE workspace_id = :w ORDER BY run_id, id",
                                    w=b.ws),
                "last_parity": sw["last_parity"], "ladder": sw["ladder"], "baseline": sw["tb_imbalance_baseline"]}


def integrity_events(caplog) -> list[dict]:
    out = []
    for r in caplog.records:
        if r.name == "v2.ops.integrity":
            event = json.loads(r.getMessage())
            if "status" in event:
                out.append(event)
    return out


def _gst_sale_guid(cap: pf.Capture, start: date, end: date) -> str:
    """A countable FY 2025-26 GST sale (party + Domestic Sales + Output CGST + Output SGST)."""
    for v in cap.vouchers(start, end):
        d = v["data"]
        names = {e["ledgername"] for e in d["ledger_entries"]}
        if d["iscancelled"] == "No" and d["isoptional"] == "No" and {"Output CGST", "Output SGST",
                                                                       "Domestic Sales"} <= names:
            return d["guid"]
    raise AssertionError("no GST sale in that window")


async def _missing_one_sale(app_client, session, **kw) -> tuple[B, str]:
    fake = pf.books()
    guid = _gst_sale_guid(pf.Capture(fake), date(2025, 6, 1), date(2025, 6, 30))
    return await setup_b(app_client, session, fake=fake, omit_guids=(guid,), **kw), guid


# --- §10.1 preconditions, in order -------------------------------------------------------------------------------


def _p19(name: str) -> dict:
    c = transcode.counters_from_xml(realdata.read_capture(name))
    return {"alt_vch_id": int(c["altvchid"]), "alt_mst_id": int(c["altmstid"])}


async def test_counters_moved_aborted_moving_no_lines_ladder_unchanged(app_client, session, engine, caplog):
    """Precondition 1 (probe 19, real counters: a voucher saved mid-capture moved AltVchId 66 -> 67)."""
    b, _ = await _missing_one_sale(app_client, session)
    first = await parity(app_client, b)                              # ladder ok -> suspect, with pending ids
    assert first["status"] == "suspect"
    before = await whole_state(engine, b)
    start, end = _p19("p19_A_capture_moving_counters_start.xml"), _p19("p19_A_capture_moving_counters_end.xml")
    assert start != end
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    res = await parity(app_client, b, counters_before=start, counters_after=end)
    assert (res["status"], res["abort_reason"], res["summary"]) == ("aborted_moving", "counters_moved", None)
    assert res["ladder"] == {k: before["ladder"][k] for k in LADDER_KEYS}
    after = await whole_state(engine, b)
    run = after["runs"][-1]
    assert (run["status"], run["abort_reason"], run["lines_compared"], run["mismatch_count"]) == \
        ("aborted_moving", "counters_moved", 0, 0)
    assert (run["counters_before"], run["counters_after"]) == (start, end)
    assert [l for l in after["lines"] if l["run_id"] == run["id"]] == []
    assert (after["ladder"], after["last_parity"], after["baseline"]) == \
        (before["ladder"], before["last_parity"], before["baseline"])
    assert len(after["runs"]) == len(before["runs"]) + 1 and after["lines"] == before["lines"]


async def test_quiet_counters_pass_quiescence(app_client, session, engine):
    """Precondition 1 with probe 19's QUIET capture (counters identical start/end) passes -- the run is computed."""
    start, end = _p19("p19_A_capture_quiet_1_counters_start.xml"), _p19("p19_A_capture_quiet_1_counters_end.xml")
    assert start == end
    b = await setup_b(app_client, session, counters=start)
    res = await parity(app_client, b, counters_before=start, counters_after=end)
    assert res["status"] == "ok" and res["abort_reason"] is None
    async with fresh(engine) as s:
        assert (await run_row(s, res["parity_run_id"]))["status"] == "ok"
        assert len(await line_rows(s, res["parity_run_id"])) > 0


async def test_counters_before_not_cursor_aborted_behind(app_client, session, engine):
    """Precondition 2: counters equal (quiet) but not the stored cursors -> the outbox isn't drained."""
    b, _ = await _missing_one_sale(app_client, session)
    await parity(app_client, b)
    before = await whole_state(engine, b)
    ahead = {"alt_vch_id": b.counters["alt_vch_id"] + 1, "alt_mst_id": b.counters["alt_mst_id"]}
    res = await parity(app_client, b, counters_before=ahead, counters_after=ahead)
    assert (res["status"], res["abort_reason"]) == ("aborted_behind", "cursor_behind")
    after = await whole_state(engine, b)
    assert (after["runs"][-1]["status"], after["runs"][-1]["lines_compared"]) == ("aborted_behind", 0)
    assert after["lines"] == before["lines"]
    assert (after["ladder"], after["last_parity"]) == (before["ladder"], before["last_parity"])


async def _resync_fy(app_client, b: B, fy: date) -> str:
    r = await app_client.post(f"/api/workspaces/{b.ws}/sync/commands", headers=web_headers(b.uid),
                              json={"type": "confirm_resync", "scope": "fy", "fy_start": fy.isoformat()})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers, json={
        "kind": "full_resync", "scope": {"fy_start": fy.isoformat()}, "command_id": r.json()["id"],
        "counters_at_start": b.counters})
    assert r.status_code == 200, r.text
    b.coverage = r.json()["coverage"]
    return r.json()["run_id"]


async def test_no_verified_span_aborted_incomplete(app_client, session, engine):
    """Precondition 3: the current (IST) FY is being resynced -> no verified span."""
    b = await setup_b(app_client, session)
    await _resync_fy(app_client, b, date(2026, 4, 1))
    async with fresh(engine) as s:
        assert (await one(s, "SELECT state FROM sync_fy_coverage WHERE workspace_id=:w AND fy_start='2026-04-01'",
                          w=b.ws))["state"] == "resyncing"
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"], res["remediation"]) == \
        ("aborted_incomplete", "no_verified_span", [])
    st = await whole_state(engine, b)
    assert (st["runs"][-1]["status"], st["lines"], st["last_parity"]) == ("aborted_incomplete", [], None)
    assert st["ladder"] == {} and st["baseline"] is None             # precondition 6 never reached


async def test_first_sync_state_aborted_incomplete(app_client, session, engine):
    """Precondition 3: `sync_state = first_sync` (the first sync completed, the window isn't) -> incomplete,
    even though the counters equal the cursors."""
    b = await setup_b(app_client, session, ack=False)
    async with fresh(engine) as s:
        sw = await sw_row(s, b.ws)
        assert (sw["sync_state"], sw["cursor_alt_vch_id"]) == ("first_sync", b.counters["alt_vch_id"])
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "no_verified_span")
    st = await whole_state(engine, b)
    assert (st["runs"][-1]["status"], st["lines"], st["ladder"], st["last_parity"]) == \
        ("aborted_incomplete", [], {}, None)


async def test_restore_detected_aborted_incomplete(app_client, session, engine):
    """Precondition 3: `restore_detected` (a re-link) -> incomplete; the cursors are unchanged by a re-link."""
    b = await setup_b(app_client, session)
    r = await app_client.post(f"/api/sync/{b.ws}/heartbeat", headers=b.headers, json={
        "tally_status": "other_company_same_name", "pc_clock": "2026-09-25T06:30:00+00:00",
        "seen_company": {"guid": "other-guid", "name": pf.B_NAME}})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{b.ws}/relink", headers=b.headers, json={
        "new_company_guid": "other-guid", "company_name": pf.B_NAME, "password": "Passw0rd!Passw0rd"})
    assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "no_verified_span")
    assert (await whole_state(engine, b))["lines"] == []


@pytest.mark.parametrize("posted,expected", [
    (("tb", "lw"), [{"report_type": "trial_balance_ledgerwise", "as_on": "2025-03-31"}]),
    (("lw", "lw_anchor"), [{"report_type": "trial_balance", "as_on": "2026-03-31"}]),
    (("tb", "lw_anchor"), [{"report_type": "trial_balance_ledgerwise", "as_on": "2026-03-31"}]),
    ((), [{"report_type": "trial_balance", "as_on": "2026-03-31"},
          {"report_type": "trial_balance_ledgerwise", "as_on": "2026-03-31"},
          {"report_type": "trial_balance_ledgerwise", "as_on": "2025-03-31"}]),
])
async def test_missing_anchor_snapshot_aborted_incomplete_with_capture_remediation(app_client, session, engine,
                                                                                  posted, expected):
    """Precondition 4: each missing required snapshot -> `aborted_incomplete` + `capture_snapshot {report_type,
    as_on}`; the anchor is the ledger-level TB as-on E−1 (D9)."""
    b = await setup_b(app_client, session, snapshots=posted)
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "snapshot_missing")
    assert [r["action"] for r in res["remediation"]] == ["capture_snapshot"] * len(expected)
    assert [r["params"] for r in res["remediation"]] == expected
    st = await whole_state(engine, b)
    assert st["runs"][-1]["remediation"] == res["remediation"]
    assert (st["lines"], st["ladder"], st["last_parity"]) == ([], {}, None)


async def test_anchor_snapshot_not_ledgerwise_counts_as_missing(app_client, session, engine):
    """10c carry (ledgerwise guard): a `trial_balance_ledgerwise` snapshot whose request_flags are not
    `ISLEDGERWISE=Yes` can't anchor rung 1 (D29) -> the anchor is missing."""
    b = await setup_b(app_client, session, snapshots=("tb", "lw"))
    await _snap(app_client, b, "trial_balance_ledgerwise", date(2025, 3, 31), flags={"EXPLODEFLAG": "Yes"})
    res = await parity(app_client, b)
    assert res["status"] == "aborted_incomplete"
    assert res["remediation"][0]["params"] == {"report_type": "trial_balance_ledgerwise", "as_on": "2025-03-31"}


# --- D10 (precondition 6) -----------------------------------------------------------------------------------------


def _pl_row_moved(b: B, by: str) -> list[dict]:
    """The group TB with its top-level `Profit & Loss A/c` row moved by `by` -- the imbalance moves by `by` and
    no compared figure changes (the P&L A/c is D11 not_applicable)."""
    cells = b.cap.tb_cells("trial_balance", AS_ON)
    row = next(c for c in cells if c["dspdispname"] == "Profit & Loss A/c")
    row["dspclcramta"] = f"{D(row['dspclcramta']) + D(by):.2f}"
    return cells


async def test_tb_imbalance_changed_same_altmstid_discarded_stale(app_client, session, engine):
    b = await setup_b(app_client, session)
    first = await parity(app_client, b)
    assert first["status"] == "ok"
    before = await whole_state(engine, b)
    assert before["baseline"]["alt_mst_id"] == b.counters["alt_mst_id"]
    assert D(before["baseline"]["imbalance"]) == D("0.00")
    await _snap(app_client, b, "trial_balance", AS_ON, at=SNAP_AT + timedelta(minutes=5), cells=_pl_row_moved(b, "5"))
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("discarded_stale", "stale_tally")
    assert [(r["action"], r["params"]) for r in res["remediation"]] == [("tally_notice", {"notice": "restart"})]
    after = await whole_state(engine, b)
    run = after["runs"][-1]
    assert (run["status"], run["tb_imbalance"], run["lines_compared"]) == ("discarded_stale", D("5.00"), 0)
    assert (after["ladder"], after["last_parity"], after["baseline"], after["lines"]) == \
        (before["ladder"], before["last_parity"], before["baseline"], before["lines"])


async def test_imbalance_moved_with_new_altmstid_rebaselines_and_runs(app_client, session, engine):
    b = await setup_b(app_client, session)
    assert (await parity(app_client, b))["status"] == "ok"
    moved = {"alt_vch_id": b.counters["alt_vch_id"], "alt_mst_id": b.counters["alt_mst_id"] + 1}
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": moved})
    assert r.status_code == 200, r.text
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{r.json()['run_id']}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": 0, "cursor_after": moved})
    assert r.status_code == 200, r.text
    b.counters = moved
    await _snap(app_client, b, "trial_balance", AS_ON, at=SNAP_AT + timedelta(minutes=5), cells=_pl_row_moved(b, "5"))
    res = await parity(app_client, b)
    assert res["status"] == "ok"
    st = await whole_state(engine, b)
    assert (st["baseline"]["alt_mst_id"], D(st["baseline"]["imbalance"])) == (moved["alt_mst_id"], D("5.00"))
    assert st["runs"][-1]["tb_imbalance"] == D("5.00") and st["last_parity"]["run_id"] == res["parity_run_id"]


async def test_tb_imbalance_recomputed_from_cells_with_the_current_masters(app_client, session, engine):
    """10c carry: the snapshot's stored imbalance can be stale -- a custom top-level group whose master arrived after
    the snapshot changes the right value. Parity recomputes it from the cells + the CURRENT masters."""
    fake = pf.books()

    def custom_primary(s):
        s["groups"]["Promoter Funds"] = {"parent": ""}             # a wholly custom top-level group
        s["ledgers"]["Capital Account"]["parent"] = "Promoter Funds"
    fake.edit_state(custom_primary)
    b = await setup_b(app_client, session, fake=fake, early_tb=True, snapshots=("lw", "lw_anchor"))
    # stored before the masters knew `Promoter Funds`: the reserved-name fallback leaves its row out
    assert D(b.early["imbalance"]) == D("-1000000.00")
    res = await parity(app_client, b)
    assert res["status"] == "ok", res
    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
        stored = await one(s, "SELECT imbalance FROM tally_report_snapshots WHERE workspace_id=:w AND "
                              "report_type='trial_balance' AND as_on_date='2026-03-31'", w=b.ws)
        assert (run["tb_imbalance"], stored["imbalance"]) == (D("0.00"), D("-1000000.00"))


async def _ingest_new_run(app_client, b: B, objects: list[dict]) -> None:
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": b.counters})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    n = await _ingest(app_client, b, objects, run_id=run_id) if objects else 0
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n,
        "cursor_after": b.counters})
    assert r.status_code == 200, r.text


async def test_null_tb_imbalance_is_a_precondition_failure_never_ok(app_client, session, engine):
    """10c carry: an imbalance of NULL (no top-level group row -- an unusable TB) is a precondition failure."""
    b = await setup_b(app_client, session, snapshots=("lw", "lw_anchor"))
    sub_only = [c for c in b.cap.tb_cells("trial_balance", AS_ON) if c["dspdispname"] == "Sundry Debtors"]
    stored = await _snap(app_client, b, "trial_balance", AS_ON, cells=sub_only)
    assert stored["imbalance"] is None
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "tb_imbalance_unknown")
    st = await whole_state(engine, b)
    assert (st["lines"], st["last_parity"], st["ladder"], st["baseline"]) == ([], None, {}, None)


# --- §15.5 rows ---------------------------------------------------------------------------------------------------


async def test_all_match_ok_and_last_parity_persisted(app_client, session, engine):
    """§14.16 / §15.5 rows 1 and 11: FakeBooks B, verified edge FY 2025-26 (mid-backfill: FY 2022-25 never
    ingested, so all-time balances are far off; the anchor is right) -> `ok`. One `parity_runs` row, one
    `parity_lines` row per compared ledger/group, `last_parity` = the run; re-read in fresh sessions."""
    b = await setup_b(app_client, session)
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"], res["remediation"]) == ("ok", None, [])
    assert res["ladder"] == {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None}
    assert res["summary"]["mismatches"] == 0 and res["summary"]["groups_compared"] >= 5

    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
        lines = await line_rows(s, res["parity_run_id"])
        sw = await sw_row(s, b.ws)
        ledgers = await rows(s, "SELECT guid, name FROM tally_ledgers WHERE workspace_id=:w AND NOT is_deleted",
                             w=b.ws)
    assert (run["status"], run["scope"], run["rung"], run["as_on_date"]) == ("ok", "daily", 2, AS_ON)
    assert (run["verified_from"], run["anchor_as_on"], run["tb_imbalance"]) == (FY25, date(2025, 3, 31), D("0.00"))
    assert (run["lines_compared"], run["mismatch_count"], run["abort_reason"]) == (len(lines), 0, None)
    assert run["remediation"] == [] and run["finished_at"] is not None
    assert {l["as_on_date"] for l in lines} == {AS_ON} and {l["verdict"] for l in lines} <= \
        {"match", "match_revalued", "not_applicable"}

    led = by_name(lines)
    # every live ledger has exactly one rung-1 (or forex) line, the nominal ones a second (rung 2)
    assert set(led) == {l["name"] for l in ledgers}
    nominal = {"Domestic Sales", "Export Sales", "Local Purchases", "Bank Charges", "Freight & Forwarding",
               "Office Rent"} & set(led)
    for name, ls in led.items():
        verdicts = sorted((l["verdict"], l["cause"]) for l in ls)
        if name == "Profit & Loss A/c":
            assert verdicts == [("not_applicable", "pl_account")]
        elif name in nominal:
            assert verdicts == [("match", None), ("not_applicable", "nominal")], name
        else:
            assert verdicts == [("match", None)], (name, verdicts)
    groups = by_name(lines, "group")
    assert {g: ls[0]["verdict"] for g, ls in groups.items()} == {g: "match" for g in groups}
    assert {"Capital Account", "Current Assets", "Current Liabilities", "Sales Accounts"} <= set(groups)
    # Current Assets includes the same response's Opening Stock (LESSONS rule 19)
    ca = groups["Current Assets"][0]
    assert ca["our_amount"] == ca["tally_amount"]

    assert sw["last_parity"] == {"state": "ok", "checked_at": "2026-09-25T06:30:00+00:00", "as_on": "2026-03-31",
                                 "mismatch_count": 0, "verified_from": "2025-04-01",
                                 "run_id": res["parity_run_id"]}
    assert {k: sw["ladder"][k] for k in ("state", "heal_attempts", "resync_offered_fy", "pending_remediation_ids",
                                         "last_run_id")} == \
        {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": [],
         "last_run_id": res["parity_run_id"]}
    r = await app_client.get(f"/api/workspaces/{b.ws}/sync-status", headers=web_headers(b.uid))
    assert r.json()["last_parity"] == sw["last_parity"]


async def test_forex_revaluation_ok_with_match_revalued_18387(app_client, session, engine):
    """§15.5 row 2 on FakeBooks B, E = books_from: the USD party is `match_revalued` +183.87 (the live number:
    -$1609.71 @ 82.58 = -1,32,929.85 against bases -1,33,113.72), accepted by §10.5(b) against the TB's
    `Unadjusted Forex Gain/Loss` -183.87 -- and every other ledger matches -> `ok`."""
    b = await setup_b(app_client, session, edge=pf.BOOKS_FROM)
    res = await parity(app_client, b)
    assert res["status"] == "ok", res
    assert (res["summary"]["match_revalued"], res["summary"]["forex_unrealised_total"]) == (1, "183.87")
    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
        lines = await line_rows(s, res["parity_run_id"])
    assert (run["verified_from"], run["anchor_as_on"], run["forex_unrealised_total"]) == \
        (pf.BOOKS_FROM, pf.BOOKS_FROM, D("183.87"))
    (usd,) = by_name(lines)[pf.USD_PARTY]
    assert (usd["verdict"], usd["our_amount"], usd["tally_amount"], usd["unrealised_diff"]) == \
        ("match_revalued", D("-133113.72"), D("-132929.85"), D("183.87"))
    assert (usd["our_fx_amount"], usd["tally_fx_amount"]) == (D("-1609.71"), D("-1609.71"))
    export = {l["verdict"] for l in by_name(lines)["Export Sales"]}
    assert export == {"match", "not_applicable"}
    assert not [l for l in lines if l["verdict"] in ("mismatch", "missing_in_db", "missing_in_tally")]


async def test_forex_sale_missing_current_fy_suspect_face_mismatch(app_client, session, engine):
    """§15.5 row 3 / ruling F4: a USD sale in Tally's CURRENT period (FakeBooks, FY 2025-26) left out of ingest ->
    the face check fails (`forex_face_mismatch`, folded by §10.7 row 7 into `forex_gap` + refetch that ledger), Export
    Sales mismatches at rung 2 -> `suspect`. The positive control (the sale ingested) is `ok` with the USD party
    `match_revalued` -- which needs the anchor's own Unadjusted Forex row netted out (E−1 anchors are revalued)."""
    fake = pf.books()
    tag = pf.add_current_fy_usd_sale(fake)
    control = await setup_b(app_client, session, fake=fake)
    ok = await parity(app_client, control)
    assert ok["status"] == "ok", ok
    async with fresh(engine) as s:
        (usd,) = by_name(await line_rows(s, ok["parity_run_id"]))[pf.USD_PARTY]
    assert (usd["verdict"], usd["unrealised_diff"]) == ("match_revalued", D("-2285.79"))

    b = await setup_b(app_client, session, fake=fake, omit=(tag,))
    res = await parity(app_client, b)
    assert res["status"] == "suspect" and res["ladder"]["state"] == "suspect"
    async with fresh(engine) as s:
        lines = await line_rows(s, res["parity_run_id"])
        guid = (await one(s, "SELECT guid FROM tally_ledgers WHERE workspace_id=:w AND name=:n", w=b.ws,
                          n=pf.USD_PARTY))["guid"]
    (usd,) = by_name(lines)[pf.USD_PARTY]
    assert (usd["verdict"], usd["cause"]) == ("mismatch", "forex_gap")
    assert (usd["our_fx_amount"], usd["tally_fx_amount"]) == (D("-1609.71"), D("-2109.71"))
    rung2_export = [l for l in by_name(lines)["Export Sales"] if l["verdict"] != "not_applicable"]
    assert [(l["verdict"], l["diff"]) for l in rung2_export] == [("mismatch", D("42000.00"))]
    assert {"action": "refetch_ledger_vouchers", "params": {"ledger_guid": guid, "fy_start": "2025-04-01"}} in \
        [{k: r[k] for k in ("action", "params")} for r in res["remediation"]]


async def test_forex_sale_missing_past_fy_is_revaluation_unexplained(app_client, session, engine):
    """Ruling F4 on the past-FY side: [S0-B:101] (Sept 2022) left out of ingest, E = books_from. Its face is not
    checkable for the current FY run (the current-FY opening face already includes it), so §10.5(b) carries it:
    the set rule rejects -> `forex_revaluation_unexplained` -> `forex_gap` (never `forex_face_mismatch`)."""
    b = await setup_b(app_client, session, edge=pf.BOOKS_FROM, omit=("[S0-B:101]",))
    res = await parity(app_client, b)
    assert res["status"] == "suspect"
    async with fresh(engine) as s:
        lines = await line_rows(s, res["parity_run_id"])
    (usd,) = by_name(lines)[pf.USD_PARTY]
    assert (usd["verdict"], usd["cause"]) == ("mismatch", "forex_gap")
    assert usd["our_fx_amount"] == usd["tally_fx_amount"] == D("-1609.71")          # the face check passed
    # ours lacks the 37,216.04 base; Tally still holds its face revalued at the latest rate
    assert (usd["our_amount"], usd["tally_amount"]) == (D("-95897.68"), D("-132929.85"))
    assert by_name(lines, "group")["Current Assets"][0]["verdict"] == "mismatch"


async def test_one_voucher_missing_suspect_voucher_missed_or_duplicated(app_client, session, engine):
    """§15.5 row 4: one GST sale missing -> party, Output CGST/SGST (rung 1) and Domestic Sales (rung 2) differ by
    amounts netting to zero -> every one `voucher_missed_or_duplicated`, one `month_bisect` listing only the
    month-ends with no stored ledger-level TB; ladder ok -> suspect."""
    b, guid = await _missing_one_sale(app_client, session)
    res = await parity(app_client, b)
    assert (res["status"], res["ladder"]["state"]) == ("suspect", "suspect")
    async with fresh(engine) as s:
        lines = await line_rows(s, res["parity_run_id"])
    bad = [l for l in lines if l["verdict"] == "mismatch" and l["scope"] == "ledger"]
    names = {l["name"] for l in bad}
    assert {"Output CGST", "Output SGST", "Domestic Sales"} <= names and len(names) == 4
    assert {l["cause"] for l in bad} == {"voucher_missed_or_duplicated"}
    assert sum((l["diff"] for l in bad), D("0")) == D("0.00")
    (rem,) = res["remediation"]
    expected_ends = [d.isoformat() for d in pf.month_ends(date(2025, 4, 1), date(2026, 2, 28))]
    assert (rem["action"], rem["params"]) == ("month_bisect", {"fy_start": "2025-04-01", "month_ends": expected_ends})
    assert all(l["remediation_status"] == "issued" for l in bad)


async def test_ladder_alert_then_resync_offered_after_two_heals(app_client, session, engine):
    """§15.5 row 5 / §10.8: suspect -> (remediation reported) alert heal 1 -> (not reported: kept, same remediation
    re-issued) -> (reported) alert heal 2 + resync offered for the newest verified FY. Nothing starts (decision 12)."""
    b, _ = await _missing_one_sale(app_client, session)
    r1 = await parity(app_client, b)
    ids1 = [r["id"] for r in r1["remediation"]]
    assert r1["ladder"] == {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": None} and ids1
    r2 = await parity(app_client, b, remediation_done=ids1)
    assert r2["ladder"] == {"state": "alert", "heal_attempts": 1, "resync_offered_fy": None}
    r3 = await parity(app_client, b)                                     # remediation NOT reported
    assert r3["ladder"] == r2["ladder"] and [r["id"] for r in r3["remediation"]] == ids1
    r4 = await parity(app_client, b, remediation_done=[r["id"] for r in r3["remediation"]])
    assert r4["ladder"] == {"state": "alert", "heal_attempts": 2, "resync_offered_fy": "2025-04-01"}
    assert [r["status"] for r in (r1, r2, r3, r4)] == ["suspect", "alert", "alert", "alert"]

    st = await whole_state(engine, b)
    assert [x["status"] for x in st["runs"]] == ["suspect", "alert", "alert", "alert"]
    assert (st["ladder"]["state"], st["ladder"]["heal_attempts"], st["ladder"]["resync_offered_fy"]) == \
        ("alert", 2, "2025-04-01")
    assert st["ladder"]["pending_remediation_ids"] == ids1 and st["ladder"]["last_run_id"] == r4["parity_run_id"]
    assert st["last_parity"]["state"] == "alert" and st["last_parity"]["run_id"] == r4["parity_run_id"]
    status = (await app_client.get(f"/api/workspaces/{b.ws}/sync-status", headers=web_headers(b.uid))).json()
    assert status["resync_offered"] == {"scope": "fy", "fy_start": "2025-04-01", "reason": "parity"}
    async with fresh(engine) as s:                                       # offered, never started
        assert (await one(s, "SELECT count(*) AS n FROM sync_runs WHERE workspace_id=:w AND kind='full_resync'",
                          w=b.ws))["n"] == 0


async def test_hard_alert_after_confirmed_fy_resync_still_wrong(app_client, session, engine, clock, caplog):
    """§15.5 row 6: after the offer, a confirmed single-FY full_resync of that FY completes and the next run still
    mismatches -> `hard_alert` + one `engineering_flag` ops event. A later clean run lowers it to `ok`."""
    b, guid = await _missing_one_sale(app_client, session)
    ids = [r["id"] for r in (await parity(app_client, b))["remediation"]]
    ids = [r["id"] for r in (await parity(app_client, b, remediation_done=ids))["remediation"]]
    r = await parity(app_client, b, remediation_done=ids)
    assert r["ladder"]["resync_offered_fy"] == "2025-04-01"

    clock.advance(minutes=10)
    run_id = await _resync_fy(app_client, b, FY25)
    n = await _ingest(app_client, b, [v for v in b.cap.vouchers(FY25, AS_ON) if v["data"]["guid"] != guid],
                      run_id=run_id)
    await _ack_fy(app_client, b, FY25)
    rr = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n})
    assert rr.status_code == 200, rr.text
    clock.advance(minutes=1)
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    hard = await parity(app_client, b, scope="post_resync", remediation_done=[x["id"] for x in r["remediation"]])
    assert (hard["status"], hard["ladder"]["state"]) == ("hard_alert", "hard_alert")
    flags = [json.loads(x.getMessage()) for x in caplog.records if x.name == "v2.ops.integrity"
             and json.loads(x.getMessage()).get("event") == "engineering_flag"]
    assert flags == [{"workspace_id": str(b.ws), "run_id": hard["parity_run_id"], "event": "engineering_flag",
                      "reason": "hard_alert"}]
    st = await whole_state(engine, b)
    assert (st["runs"][-1]["status"], st["ladder"]["state"], st["last_parity"]["state"]) == \
        ("hard_alert", "hard_alert", "hard_alert")

    still = await parity(app_client, b)                                  # sticky, and no second engineering flag
    assert still["ladder"]["state"] == "hard_alert"
    await _ingest_new_run(app_client, b, [v for v in b.cap.vouchers(FY25, AS_ON) if v["data"]["guid"] == guid])
    clean = await parity(app_client, b)
    assert (clean["status"], clean["ladder"]) == ("ok", {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None})
    st = await whole_state(engine, b)
    assert (st["ladder"]["state"], st["last_parity"]["state"]) == ("ok", "ok") and "resync_offered" not in st["ladder"]


async def test_resyncing_fy_excluded_from_verified_span(app_client, session, engine):
    """§15.5 row 13: FY 2024-25 is `resyncing` -> the verified edge moves to FY 2025-26 (anchor as-on
    31-03-2025); lines of FY 2024-25 are not compared."""
    b = await setup_b(app_client, session, edge=FY24, snapshots=("tb", "lw", "lw_anchor"))
    await _snap(app_client, b, "trial_balance_ledgerwise", date(2025, 3, 31), purpose="anchor")
    full = await parity(app_client, b)
    await _resync_fy(app_client, b, FY24)
    narrowed = await parity(app_client, b)
    assert (full["status"], narrowed["status"]) == ("ok", "ok")
    async with fresh(engine) as s:
        a, n = await run_row(s, full["parity_run_id"]), await run_row(s, narrowed["parity_run_id"])
        sw = await sw_row(s, b.ws)
    assert (a["verified_from"], a["anchor_as_on"]) == (FY24, date(2024, 3, 31))
    assert (n["verified_from"], n["anchor_as_on"]) == (FY25, date(2025, 3, 31))
    assert sw["last_parity"]["verified_from"] == "2025-04-01"


async def test_no_ledger_anchor_group_anchor_route(app_client, session, engine):
    """§15.5 row 12 / 10c carry: the ledger-level anchor is unavailable but the group TB as-on E−1 is stored ->
    every BS ledger `not_applicable` (`no_ledger_anchor`, never match); rung 2 carries the check at group level."""
    b = await setup_b(app_client, session, snapshots=("tb", "lw", "tb_anchor"))
    res = await parity(app_client, b)
    assert res["status"] == "ok", res
    async with fresh(engine) as s:
        lines = await line_rows(s, res["parity_run_id"])
    bs = [l for l in lines if l["scope"] == "ledger" and l["cause"] == "no_ledger_anchor"]
    assert len(bs) >= 15 and {l["verdict"] for l in bs} == {"not_applicable"}
    groups = by_name(lines, "group")
    for g in ("Capital Account", "Current Assets", "Current Liabilities"):
        assert groups[g][0]["verdict"] == "match", g


# --- §10.9 bisect ------------------------------------------------------------------------------------------------


async def test_bisect_scope_returns_first_diverging_month(app_client, session, engine):
    b, _ = await _missing_one_sale(app_client, session)                 # the missing sale is dated June 2025
    for me in pf.month_ends(FY25, date(2026, 2, 28)):
        await _snap(app_client, b, "trial_balance_ledgerwise", me, purpose="bisect")
    before = await whole_state(engine, b)
    res = await parity(app_client, b, scope="bisect", fy_start="2025-04-01")
    assert res["status"] == "suspect"
    assert res["bisect"]["first_diverging_month"] == "2025-06-30"
    assert res["bisect"]["months_evaluated"] == [d.isoformat() for d in pf.month_ends(FY25, AS_ON)]
    assert [(r["action"], r["params"]) for r in res["remediation"]] == [("refetch_month", {"month": "2025-06"})]
    async with fresh(engine) as s:
        lines = await line_rows(s, res["parity_run_id"])
        sw = await sw_row(s, b.ws)
    by_month: dict[date, set[str]] = {}
    for l in lines:
        by_month.setdefault(l["as_on_date"], set()).add(l["verdict"])
    assert "mismatch" not in by_month[date(2025, 5, 31)] and "mismatch" in by_month[date(2025, 6, 30)]
    assert sw["ladder"] == {**before["ladder"], "bisect_month": "2025-06-30"}     # never stepped
    assert sw["last_parity"] == before["last_parity"]


async def test_bisect_missing_month_ends_aborted_incomplete(app_client, session, engine):
    b = await setup_b(app_client, session)
    for me in pf.month_ends(FY25, date(2025, 12, 31)):
        await _snap(app_client, b, "trial_balance_ledgerwise", me, purpose="bisect")
    res = await parity(app_client, b, scope="bisect", fy_start="2025-04-01")
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "month_ends_missing")
    assert [r["params"] for r in res["remediation"]] == [
        {"report_type": "trial_balance_ledgerwise", "as_on": d} for d in ("2026-01-31", "2026-02-28")]
    st = await whole_state(engine, b)
    assert (st["lines"], st["ladder"], st["last_parity"]) == ([], {}, None)


async def test_bisect_first_fy_with_mid_year_books_from(app_client, session, engine):
    """Review M2a: books begin 1 October 2022 (FakeBooks B with nothing before it). Bisecting that first FY runs
    its months from books_from (Oct..Mar), anchored at the TB as-on books_from minus that day's lines (D9); a sale
    missing in December diverges from 31-12-2022 -- October and November are clean."""
    mid = date(2022, 10, 1)
    fake = pf.books()
    fake.edit_state(lambda s: s.update(vouchers={m: v for m, v in s["vouchers"].items() if v["date"] >= "20221001"}))
    guid = _gst_sale_guid(pf.Capture(fake), date(2022, 12, 1), date(2022, 12, 31))
    b = await setup_b(app_client, session, fake=fake, books_from=mid, edge=mid, omit_guids=(guid,),
                      snapshots=("lw_anchor",))
    ends = pf.month_ends(mid, date(2023, 3, 31))
    for me in ends:
        await _snap(app_client, b, "trial_balance_ledgerwise", me, purpose="bisect")
    res = await parity(app_client, b, scope="bisect", fy_start="2022-04-01")
    assert (res["status"], res["abort_reason"]) == ("suspect", None), res
    assert res["bisect"]["months_evaluated"] == [d.isoformat() for d in ends]
    assert res["bisect"]["first_diverging_month"] == "2022-12-31"
    assert [(r["action"], r["params"]) for r in res["remediation"]] == [("refetch_month", {"month": "2022-12"})]
    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
        lines = await line_rows(s, res["parity_run_id"])
    assert (run["verified_from"], run["anchor_as_on"]) == (mid, mid)
    verdicts: dict[date, set[str]] = {}
    for l in lines:
        verdicts.setdefault(l["as_on_date"], set()).add(l["verdict"])
    assert sorted(verdicts) == ends
    assert {"mismatch", "missing_in_db", "missing_in_tally"}.isdisjoint(verdicts[date(2022, 10, 31)] |
                                                                      verdicts[date(2022, 11, 30)])
    assert "mismatch" in verdicts[date(2022, 12, 31)]


# --- persistence, restart, ops signal, sync-status -----------------------------------------------------------------


async def test_parity_runs_persist_across_requests_and_ladder_survives_restart(app_client, session, engine, settings,
                                                                              clock):
    b, _ = await _missing_one_sale(app_client, session)
    r1 = await parity(app_client, b)
    before = await whole_state(engine, b)
    await restart_app_engine()
    app2 = build_sync_app(settings, clock)
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://v2") as c2:
        status = (await c2.get(f"/api/workspaces/{b.ws}/sync-status", headers=web_headers(b.uid))).json()
        assert status["last_parity"] == {**before["last_parity"], "state": "ok"}           # suspect is invisible
        r = await c2.post(f"/api/sync/{b.ws}/parity", headers=b.headers,
                          json=body(b, remediation_done=[x["id"] for x in r1["remediation"]]))
        assert r.status_code == 200, r.text
        assert r.json()["ladder"] == {"state": "alert", "heal_attempts": 1, "resync_offered_fy": None}
    after = await whole_state(engine, b)
    assert after["runs"][0] == before["runs"][0] and len(after["runs"]) == 2
    assert [l for l in after["lines"] if l["run_id"] == before["runs"][0]["id"]] == before["lines"]


def _ops_records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "v2.ops.integrity"]


def _assert_no_business_data(caplog, ledgers: list[dict]) -> None:
    text_out = " ".join(r.getMessage() for r in caplog.records if r.name == "v2.ops.integrity")
    for led in ledgers:
        assert led["guid"] not in text_out and led["name"] not in text_out


async def test_ops_signal_emitted_once_per_non_ok_run(app_client, session, engine, caplog):
    """§10.10 / review I1 ruling: one integrity event per COMPUTED non-ok run (suspect, alert, hard_alert) -- an
    `ok` run emits none, and so does a precondition abort (§10.1 "no alert")."""
    b, _ = await _missing_one_sale(app_client, session)
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    suspect = await parity(app_client, b)
    behind = {"alt_vch_id": 1, "alt_mst_id": 1}
    aborted = await parity(app_client, b, counters_before=behind, counters_after=behind)
    alert = await parity(app_client, b, remediation_done=[r["id"] for r in suspect["remediation"]])
    assert (suspect["status"], aborted["status"], alert["status"]) == ("suspect", "aborted_behind", "alert")
    events = integrity_events(caplog)
    assert [(e["run_id"], e["status"]) for e in events] == \
        [(suspect["parity_run_id"], "suspect"), (alert["parity_run_id"], "alert")]
    assert _ops_records(caplog) == events                                    # nothing else on the channel
    for e in events:
        assert e["cause_counts"] == {"voucher_missed_or_duplicated": 4}
        assert e["rung"] == 2 and e["max_abs_diff_bucket"] in ("<₹1k", "<₹1L", "<₹1Cr", "≥₹1Cr")
    async with fresh(engine) as s:
        _assert_no_business_data(caplog, await rows(s, "SELECT guid, name FROM tally_ledgers WHERE workspace_id=:w",
                                                    w=b.ws))

    ok = await setup_b(app_client, session)
    caplog.clear()
    assert (await parity(app_client, ok))["status"] == "ok"
    assert _ops_records(caplog) == []


async def test_computed_suspect_run_emits_exactly_one_ops_event(app_client, session, engine, caplog):
    b, _ = await _missing_one_sale(app_client, session)
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    res = await parity(app_client, b)
    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
    assert _ops_records(caplog) == [{
        "workspace_id": str(b.ws), "run_id": res["parity_run_id"], "rung": 2, "status": "suspect",
        "cause_counts": {"voucher_missed_or_duplicated": 4}, "mismatch_count": run["mismatch_count"],
        "max_abs_diff_bucket": _ops_records(caplog)[0]["max_abs_diff_bucket"]}]
    assert run["status"] == "suspect" and run["mismatch_count"] > 0


async def test_every_abort_kind_emits_no_ops_event(app_client, session, engine, caplog):
    """Review I1 ruling: `aborted_moving`, `aborted_behind`, `aborted_incomplete` and `discarded_stale` are "no
    alert" (§10.1) -- zero events on `v2.ops.integrity` for any of them."""
    b = await setup_b(app_client, session)
    assert (await parity(app_client, b))["status"] == "ok"                     # records the D10 baseline
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    moved = {"alt_vch_id": b.counters["alt_vch_id"] + 1, "alt_mst_id": b.counters["alt_mst_id"]}
    statuses = [
        (await parity(app_client, b, counters_before=b.counters, counters_after=moved))["status"],
        (await parity(app_client, b, counters_before=moved, counters_after=moved))["status"],
        (await parity(app_client, b, scope="bisect", fy_start="2025-04-01"))["status"],     # month-ends missing
    ]
    await _snap(app_client, b, "trial_balance", AS_ON, at=SNAP_AT + timedelta(minutes=5), cells=_pl_row_moved(b, "5"))
    statuses.append((await parity(app_client, b))["status"])
    assert statuses == ["aborted_moving", "aborted_behind", "aborted_incomplete", "discarded_stale"]
    assert _ops_records(caplog) == []


async def test_no_balance_sheet_verified_abort_leaves_the_d10_baseline(app_client, session, engine, caplog,
                                                                       monkeypatch):
    """Review M1 + M6: the engine's `no_balance_sheet_verified` guard (driven by making `bs_verified` report
    nothing verified) is an abort like any other: its status and capture remediation, no lines, no ops event, and
    the D10 baseline -- which this first run would have recorded -- stays unset; ladder / last_parity untouched."""
    from backend.sync.parity import engine as engine_mod
    b = await setup_b(app_client, session)
    monkeypatch.setattr(engine_mod, "bs_verified", lambda lines: False)
    caplog.set_level(logging.INFO, logger="v2.ops.integrity")
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "no_balance_sheet_verified")
    assert [(r["action"], r["params"]) for r in res["remediation"]] == \
        [("capture_snapshot", {"report_type": "trial_balance_ledgerwise", "as_on": "2025-03-31"})]
    assert _ops_records(caplog) == []
    st = await whole_state(engine, b)
    assert (st["runs"][-1]["status"], st["runs"][-1]["lines_compared"], st["lines"]) == ("aborted_incomplete", 0, [])
    assert (st["baseline"], st["ladder"], st["last_parity"]) == (None, {}, None)


async def test_sync_status_shows_ok_for_suspect(app_client, session, engine, clock):
    """§7.13 / review I2 ruling: `suspect` is invisible. The run row says suspect; `last_parity` is written in its
    `ok` form -- `checked_at` moves to this run, `mismatch_count` 0, and `run_id` stays the previous VISIBLE run's."""
    b = await setup_b(app_client, session)
    ok = await parity(app_client, b)
    async with fresh(engine) as s:
        before = (await sw_row(s, b.ws))["last_parity"]
    assert (before["state"], before["run_id"]) == ("ok", ok["parity_run_id"])

    clock.advance(minutes=5)
    cells = b.cap.tb_cells("trial_balance_ledgerwise", AS_ON)          # Tally now shows ₹50 more Domestic Sales
    row = next(c for c in cells if c["dspdispname"] == "Domestic Sales")
    row["dspclcramta"] = f"{D(row['dspclcramta']) + 50:.2f}"
    await _snap(app_client, b, "trial_balance_ledgerwise", AS_ON, at=SNAP_AT + timedelta(minutes=5), cells=cells)
    res = await parity(app_client, b)
    assert res["status"] == "suspect"

    async with fresh(engine) as s:
        run = await run_row(s, res["parity_run_id"])
        sw = await sw_row(s, b.ws)
    assert (run["status"], run["mismatch_count"]) == ("suspect", 1)
    assert sw["ladder"]["state"] == "suspect"
    assert sw["last_parity"] == {"state": "ok", "checked_at": "2026-09-25T06:35:00+00:00", "as_on": "2026-03-31",
                                 "mismatch_count": 0, "verified_from": "2025-04-01", "run_id": ok["parity_run_id"]}
    assert sw["last_parity"]["checked_at"] != before["checked_at"]
    status = (await app_client.get(f"/api/workspaces/{b.ws}/sync-status", headers=web_headers(b.uid))).json()
    assert status["last_parity"] == sw["last_parity"]


async def test_parity_sums_use_the_covering_index(app_client, session, engine):
    """Review checkpoint: parity's line sums come from `ix_lines_cover`. EXPLAIN of the engine's own statement:
    ``Index Only Scan using ix_lines_cover on tally_voucher_ledger_lines`` -- the statement is answered from the index
    alone (every column it reads is a key, an INCLUDE column or the partial predicate). On this tiny test table the
    planner would happily heap-scan, so seq / bitmap / plain index scans are switched off: what is left can only be
    an index-only scan, and the only index that covers the statement is `ix_lines_cover`."""
    b = await setup_b(app_client, session)
    async with engine.connect() as c:                      # VACUUM can't run inside a transaction block
        await (await c.execution_options(isolation_level="AUTOCOMMIT")).execute(
            text("VACUUM ANALYZE tally_voucher_ledger_lines"))
    async with fresh(engine) as s:
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        await s.execute(text("SET LOCAL enable_bitmapscan = off"))
        await s.execute(text("SET LOCAL enable_indexscan = off"))           # only index-ONLY scans remain
        plan = "\n".join(r[0] for r in (await s.execute(text("EXPLAIN " + SUMS_SQL.text),
                                                        {"w": b.ws, "a": FY25, "b": AS_ON})).all())
    # EXPLAIN (PostgreSQL 16, 2026-09-28):
    #   GroupAggregate                       -- no Sort: the index is already in ledger_guid order
    #     Group Key: ledger_guid
    #     ->  Index Only Scan using ix_lines_cover on tally_voucher_ledger_lines
    #           Index Cond: ((workspace_id = '…'::uuid) AND (voucher_date >= '2025-04-01'::date)
    #                        AND (voucher_date <= '2026-03-31'::date))
    #   (no heap Filter: `countable` is the partial index's own predicate; amount / fx_amount are INCLUDE columns)
    assert "Index Only Scan using ix_lines_cover" in plan, plan


async def test_parity_request_validation(app_client, session):
    b = await setup_b(app_client, session, snapshots=())
    for bad, code in (({"scope": "weekly"}, "invalid_scope"), ({"as_on_date": "2026-03-31"}, "bad_as_on_date"),
                      ({"scope": "bisect"}, "fy_start_required"),
                      ({"scope": "bisect", "fy_start": "2025-05-01"}, "fy_start_required")):
        r = await app_client.post(f"/api/sync/{b.ws}/parity", json=body(b, **bad), headers=b.headers)
        assert (r.status_code, r.json()["error"]) == (422, code), bad


# --- S1 review I7: a ledger rename after the anchor TB was stored --------------------------------------------------


def _renamed_cells(b: B, report_type: str, as_on: date, old: str, new: str) -> list[dict]:
    """Tally exports a rename retroactively (probe 8, §12 step 9): a TB captured AFTER the rename shows the new name
    on every date."""
    return [{**c, "dspdispname": new} if c.get("dspdispname") == old else c
            for c in b.cap.tb_cells(report_type, as_on)]


async def test_rename_after_anchor_asks_to_recapture_the_anchor_not_a_false_mismatch(app_client, session, engine):
    """I7: `Cash` (a balance-sheet ledger with a non-zero anchor at 31-03-2025) is renamed through a master batch
    after the anchor TB was stored. The stale anchor row no longer resolves; parity must NOT compute a false
    mismatch (which climbs the ladder to hard_alert) but abort `aborted_incomplete` / `anchor_stale` asking to
    re-capture that anchor — and after the re-capture (new name, newer counters) the run is `ok`."""
    old, new = "Cash", "Cash on Hand (Main)"
    b = await setup_b(app_client, session)
    assert (await parity(app_client, b))["status"] == "ok"
    before = await whole_state(engine, b)

    cash = next(m for m in b.cap.masters() if m["kind"] == "ledger" and m["data"]["name"] == old)
    moved = {"alt_vch_id": b.counters["alt_vch_id"], "alt_mst_id": b.counters["alt_mst_id"] + 1}
    renamed = {"kind": "ledger", "data": {**cash["data"], "name": new, "alterid": str(moved["alt_mst_id"])}}
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": b.counters})
    run_id = r.json()["run_id"]
    n = await _ingest(app_client, b, [renamed], run_id=run_id)
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n, "cursor_after": moved})
    assert r.status_code == 200, r.text
    b.counters = moved
    later = SNAP_AT + timedelta(minutes=10)
    for rt in ("trial_balance", "trial_balance_ledgerwise"):          # today's as-on captures carry the new name
        await _snap(app_client, b, rt, AS_ON, at=later, cells=_renamed_cells(b, rt, AS_ON, old, new))
    async with fresh(engine) as s:
        assert (await one(s, "SELECT name FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=b.ws,
                          g=cash["data"]["guid"]))["name"] == new

    anchor = _anchor_as_on(FY25)
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "anchor_stale"), res
    assert [(r["action"], r["params"]) for r in res["remediation"]] == [
        ("capture_snapshot", {"report_type": "trial_balance_ledgerwise", "as_on": anchor.isoformat()})]
    mid = await whole_state(engine, b)
    assert (mid["ladder"], mid["last_parity"]) == (before["ladder"], before["last_parity"])   # no false alert
    assert mid["lines"] == before["lines"]

    await _snap(app_client, b, "trial_balance_ledgerwise", anchor, at=later, purpose="anchor",
                cells=_renamed_cells(b, "trial_balance_ledgerwise", anchor, old, new))
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("ok", None), res
    async with fresh(engine) as s:
        lines = by_name(await line_rows(s, res["parity_run_id"]))
    assert [l["verdict"] for l in lines[new]] == ["match"] and old not in lines


async def test_rename_after_bisect_month_ends_asks_to_recapture_them(app_client, session, engine):
    """I7, bisect form: stored month-end TBs are reused the same way as the anchor; after a rename every stale one
    (captured before the master change, now holding an unresolvable row) is asked for again, never evaluated."""
    old, new = "Cash", "Cash on Hand (Main)"
    b = await setup_b(app_client, session)
    ends = pf.month_ends(FY25, AS_ON)
    for me in ends:
        await _snap(app_client, b, "trial_balance_ledgerwise", me, purpose="bisect")
    cash = next(m for m in b.cap.masters() if m["kind"] == "ledger" and m["data"]["name"] == old)
    moved = {"alt_vch_id": b.counters["alt_vch_id"], "alt_mst_id": b.counters["alt_mst_id"] + 1}
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": b.counters})
    run_id = r.json()["run_id"]
    n = await _ingest(app_client, b, [{"kind": "ledger", "data": {**cash["data"], "name": new,
                                                                  "alterid": str(moved["alt_mst_id"])}}],
                      run_id=run_id)
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n, "cursor_after": moved})
    assert r.status_code == 200, r.text
    b.counters = moved
    before = await whole_state(engine, b)
    res = await parity(app_client, b, scope="bisect", fy_start="2025-04-01")
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "anchor_stale"), res
    assert [r["params"]["as_on"] for r in res["remediation"]] == [_anchor_as_on(FY25).isoformat(),
                                                                   *[d.isoformat() for d in ends]]
    after = await whole_state(engine, b)
    assert (after["ladder"], after["last_parity"], after["lines"]) == \
        (before["ladder"], before["last_parity"], before["lines"])


# --- S1 review M20: a non-bisect run at an as_on outside the mirrored balances' period is refused ----------------


async def test_daily_as_on_outside_the_mirrored_period_is_422_and_nothing_stored(app_client, session, engine):
    """M20 (ruling F3/F25): the mirrored ledger balances (and their face fields) are Tally's current period — FY
    2025-26 here. A daily run at 31-03-2025 would face-check a current-period opening against another FY's lines
    (false `forex_face_mismatch`). Outside bisect it is refused 422 `as_on_not_current_period`; nothing is stored."""
    prev = date(2025, 3, 31)
    b = await setup_b(app_client, session, edge=FY24)
    await _snap(app_client, b, "trial_balance", prev)
    await _snap(app_client, b, "trial_balance_ledgerwise", prev)
    before = await whole_state(engine, b)
    r = await app_client.post(f"/api/sync/{b.ws}/parity", json=body(b, as_on_date="31-03-2025"), headers=b.headers)
    assert (r.status_code, r.json()["error"]) == (422, "as_on_not_current_period"), r.text
    assert await whole_state(engine, b) == before
    assert (await parity(app_client, b))["status"] == "ok"                    # the current period's end still runs


# --- Task 14b C1 (I7 residual): no anchor_stale loop after a restore / relink ----------------------------------------


async def _company_resync(app_client, b: B, counters: dict, during=None) -> str:
    """User confirms a whole-company resync; the agent runs it at ``counters`` (posting nothing unless ``during``
    -- ``async (run_id) -> batches posted`` -- does), re-acking every month of the FYs from FY25 (the verified
    edge stays FY25, as in ``setup_b``); completion moves the cursor to ``counters`` and the workspace back to
    ``ready``."""
    r = await app_client.post(f"/api/workspaces/{b.ws}/sync/commands", headers=web_headers(b.uid),
                              json={"type": "confirm_resync", "scope": "company"})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers, json={
        "kind": "full_resync", "scope": {"company": True}, "command_id": r.json()["id"],
        "counters_at_start": counters})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    n = await during(run_id) if during is not None else 0
    for c in r.json()["coverage"]:
        if date.fromisoformat(c["fy_start"]) < FY25:
            continue
        for month in _months(date.fromisoformat(c["fy_start"]), c["months_total"], b.books_from):
            rr = await app_client.patch(f"/api/sync/{b.ws}/coverage", headers=b.headers,
                                        json={"fy_start": c["fy_start"], "month": month, "run_id": run_id})
            assert rr.status_code == 200, rr.text
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n})
    assert r.status_code == 200, r.text
    b.counters = counters
    return run_id


@pytest.mark.parametrize("cause", ["restore", "relink"])
async def test_unresolved_anchor_row_after_restore_or_relink_is_computed_never_an_anchor_stale_loop(
        app_client, session, engine, cause):
    """C1 (I7 residual, controller ruling): after a restore (counters go backwards) or a relink (a new company's
    counter space) the mirror still holds ledgers whose `alter_id` is above the new Tally's `AltMstID`. A stored
    TB with a row that resolves to no live ledger must NOT be `anchor_stale` for ever: a snapshot captured at the
    current cursor is never stale, so parity computes a verdict (`masters_gap` on the unresolved row) — and does so
    again on the next run, never an `anchor_stale` / re-capture loop."""
    old, ghost = "Cash", "Cash on Hand (Main)"
    b = await setup_b(app_client, session)
    assert (await parity(app_client, b))["status"] == "ok"
    pre = dict(b.counters)
    if cause == "restore":
        # a post-backup rename reaches the mirror at a higher alter_id; then the backup is restored in Tally
        cash = next(m for m in b.cap.masters() if m["kind"] == "ledger" and m["data"]["name"] == old)
        moved = {"alt_vch_id": pre["alt_vch_id"], "alt_mst_id": pre["alt_mst_id"] + 5}
        r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                                  json={"kind": "incremental", "counters_at_start": pre})
        run_id = r.json()["run_id"]
        n = await _ingest(app_client, b, [{"kind": "ledger", "data": {**cash["data"], "name": ghost,
                                                                      "alterid": str(moved["alt_mst_id"])}}],
                          run_id=run_id)
        r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
            "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n,
            "cursor_after": moved})
        assert r.status_code == 200, r.text
        r = await app_client.post(f"/api/sync/{b.ws}/heartbeat", headers=b.headers, json={
            "tally_status": "ours", "pc_clock": "2026-09-25T06:30:00+00:00", "counters": pre,
            "seen_company": {"guid": b.cap.guid, "name": pf.B_NAME}})
        assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text
        now_counters = pre                                  # the restored Tally's counters (below the cursor)
        cells_name = (ghost, old)                          # restored Tally shows the pre-rename name
    else:
        r = await app_client.post(f"/api/sync/{b.ws}/heartbeat", headers=b.headers, json={
            "tally_status": "other_company_same_name", "pc_clock": "2026-09-25T06:30:00+00:00",
            "seen_company": {"guid": "other-guid", "name": pf.B_NAME}})
        assert r.status_code == 200, r.text
        r = await app_client.post(f"/api/sync/{b.ws}/relink", headers=b.headers, json={
            "new_company_guid": "other-guid", "company_name": pf.B_NAME, "password": "Passw0rd!Passw0rd"})
        assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text
        now_counters = {"alt_vch_id": 5, "alt_mst_id": 5}   # the new company's own (small) counter space
        cells_name = (old, ghost + " X")                   # a row naming a ledger the mirror doesn't hold
    await _company_resync(app_client, b, now_counters)
    async with fresh(engine) as s:
        top = (await one(s, "SELECT max(alter_id) AS m FROM tally_ledgers WHERE workspace_id=:w", w=b.ws))["m"]
        assert top > now_counters["alt_mst_id"]             # the leftover alter_ids the old I7 rule tripped on
        assert (await sw_row(s, b.ws))["sync_state"] == "ready"

    later = SNAP_AT + timedelta(minutes=10)
    anchor = _anchor_as_on(FY25)
    src, dst = cells_name
    for rt, as_on, kw in (("trial_balance", AS_ON, {}), ("trial_balance_ledgerwise", AS_ON, {}),
                          ("trial_balance_ledgerwise", anchor, {"purpose": "anchor"})):
        cells = [{**c, "dspdispname": dst} if c.get("dspdispname") == src else c for c in b.cap.tb_cells(rt, as_on)]
        await _snap(app_client, b, rt, as_on, at=later, cells=cells, **kw)

    for _ in range(2):                                      # a second run is computed too: no re-capture loop
        res = await parity(app_client, b)
        assert res["abort_reason"] is None, (res["remediation"], res)
        assert res["status"] in ("suspect", "alert"), res
        async with fresh(engine) as s:
            run = await run_row(s, res["parity_run_id"])
        assert run["lines_compared"] > 0
    assert "refetch_masters" in {r["action"] for r in res["remediation"]}, res


# --- Task 14b C4 (M20 tightening): the "latest voucher" bound ignores optional and future-dated vouchers ----------


async def _add_voucher(app_client, b: B, day: str, **flags) -> None:
    """One extra copy of a June-2025 payment, re-dated to ``day`` (YYYYMMDD) with ``flags`` (e.g. isoptional="Yes"),
    ingested through a normal incremental (cursor unchanged)."""
    src = next(v for v in b.cap.vouchers(date(2025, 6, 1), date(2025, 6, 30))
               if len(v["data"]["ledger_entries"]) == 2 and v["data"]["iscancelled"] == "No"
               and v["data"]["isoptional"] == "No")
    v = json.loads(json.dumps(src))
    v["data"].update(guid=v["data"]["guid"][:-8] + "0000fffe", masterid="65534", vouchernumber="65534",
                     date=day, **flags)
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": b.counters})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    n = await _ingest(app_client, b, [v], run_id=run_id)
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n,
        "cursor_after": b.counters})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("day,flags,refused", [
    ("20260501", {}, True),                                   # control: a real FY 2026-27 voucher (<= today IST)
    ("20260501", {"isoptional": "Yes"}, False),               # optional: not books data (§10.2)
    ("20270515", {}, False),                                  # a typo'd future-FY, non-post-dated (after today)
    ("20260601", {"ispostdated": "Yes"}, False),              # post-dated
], ids=["control_real_later_fy", "optional_later_fy", "typo_future_fy", "post_dated_later_fy"])
async def test_m20_bound_ignores_optional_and_future_dated_vouchers(app_client, session, engine, day, flags,
                                                                    refused):
    """C4: the M20 lower bound is the FY of the latest non-deleted, non-optional, non-post-dated voucher dated on
    or before today (IST, clock 2026-09-25). An optional voucher, a post-dated one, or a voucher mistyped into a
    future FY must not refuse the current period's daily run (31-03-2026); a real FY 2026-27 voucher still does."""
    b = await setup_b(app_client, session)
    await _add_voucher(app_client, b, day, **flags)
    r = await app_client.post(f"/api/sync/{b.ws}/parity", json=body(b), headers=b.headers)
    if refused:
        assert (r.status_code, r.json()["error"]) == (422, "as_on_not_current_period"), r.text
    else:
        assert r.status_code == 200, r.text


async def test_m20_new_fy_without_vouchers_and_bisect_are_never_refused(app_client, session, engine):
    """C4 pins: (1) a daily run in a brand-new FY that holds no voucher yet (31-03-2027, later than the latest
    voucher's FY) is never refused by M20; (2) bisect of a past FY is exempt from the bound."""
    b = await setup_b(app_client, session, edge=FY24)
    r = await app_client.post(f"/api/sync/{b.ws}/parity", json=body(b, as_on_date="31-03-2027"), headers=b.headers)
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/sync/{b.ws}/parity", headers=b.headers,
                              json=body(b, scope="bisect", fy_start="2024-04-01", as_on_date="31-03-2025"))
    assert r.status_code == 200, r.text


# --- Task 14b fix round 1 (C1 follow-up): a snapshot from a counter space that no longer exists is stale -----------


async def test_anchor_recaptured_before_a_restore_is_stale_after_it_then_computed(app_client, session, engine):
    """C1 follow-up (controller ruling: stale = unresolved rows AND `snap.alt_mst_id != cursor`). A post-backup
    rename -> `anchor_stale` -> the anchor is re-captured under the new name at counters N -> the backup is restored
    -> a confirmed company resync restores the old name (C2) and the cursor falls to M < N. The stored anchor (new
    name, N) no longer resolves: it must be asked for again once (`anchor_stale`), not computed into a false
    `masters_gap` that no remediation fixes; after a fresh re-capture at M the run is computed (`ok`) — no loop."""
    b = await setup_b(app_client, session)
    assert (await parity(app_client, b))["status"] == "ok"
    pre = dict(b.counters)
    anchor = _anchor_as_on(FY25)
    cash = next(m for m in b.cap.masters() if m["kind"] == "ledger" and m["data"]["name"] == "Cash")
    new = "Cash on Hand (Main)"

    # post-backup rename, then the I7 re-capture of every TB at the new counters N
    n_counters = {"alt_vch_id": pre["alt_vch_id"], "alt_mst_id": pre["alt_mst_id"] + 5}
    r = await app_client.post(f"/api/sync/{b.ws}/runs", headers=b.headers,
                              json={"kind": "incremental", "counters_at_start": pre})
    run_id = r.json()["run_id"]
    n = await _ingest(app_client, b, [{"kind": "ledger", "data": {**cash["data"], "name": new,
                                                                  "alterid": str(n_counters["alt_mst_id"])}}],
                      run_id=run_id)
    r = await app_client.patch(f"/api/sync/{b.ws}/runs/{run_id}", headers=b.headers, json={
        "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": n,
        "cursor_after": n_counters})
    assert r.status_code == 200, r.text
    b.counters = n_counters
    t1 = SNAP_AT + timedelta(minutes=10)
    for rt in ("trial_balance", "trial_balance_ledgerwise"):
        await _snap(app_client, b, rt, AS_ON, at=t1, cells=_renamed_cells(b, rt, AS_ON, "Cash", new))
    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "anchor_stale"), res
    await _snap(app_client, b, "trial_balance_ledgerwise", anchor, at=t1, purpose="anchor",
                cells=_renamed_cells(b, "trial_balance_ledgerwise", anchor, "Cash", new))
    assert (await parity(app_client, b))["status"] == "ok"

    # restore -> confirmed company resync posts the restored (old-name, lower alter_id) master; cursor M < N
    r = await app_client.post(f"/api/sync/{b.ws}/heartbeat", headers=b.headers, json={
        "tally_status": "ours", "pc_clock": "2026-09-25T06:30:00+00:00", "counters": pre,
        "seen_company": {"guid": b.cap.guid, "name": pf.B_NAME}})
    assert r.status_code == 200 and r.json()["sync_state"] == "restore_detected", r.text

    async def during(rid: str) -> int:
        return await _ingest(app_client, b, [cash], run_id=rid)

    await _company_resync(app_client, b, pre, during)
    async with fresh(engine) as s:
        assert (await one(s, "SELECT name FROM tally_ledgers WHERE workspace_id=:w AND guid=:g", w=b.ws,
                          g=cash["data"]["guid"]))["name"] == "Cash"
    t2 = SNAP_AT + timedelta(minutes=20)
    for rt in ("trial_balance", "trial_balance_ledgerwise"):          # today's as-on TBs from the restored Tally
        await _snap(app_client, b, rt, AS_ON, at=t2)
    before = await whole_state(engine, b)

    res = await parity(app_client, b)
    assert (res["status"], res["abort_reason"]) == ("aborted_incomplete", "anchor_stale"), res
    assert [(r["action"], r["params"]) for r in res["remediation"]] == [
        ("capture_snapshot", {"report_type": "trial_balance_ledgerwise", "as_on": anchor.isoformat()})]
    mid = await whole_state(engine, b)
    assert (mid["ladder"], mid["last_parity"], mid["lines"]) == \
        (before["ladder"], before["last_parity"], before["lines"])      # no false alert

    await _snap(app_client, b, "trial_balance_ledgerwise", anchor, at=t2, purpose="anchor")
    for _ in range(2):                                                  # computed, and stays computed
        res = await parity(app_client, b)
        assert (res["status"], res["abort_reason"]) == ("ok", None), res
