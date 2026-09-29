"""A whole first sync of FakeBooks company B through the REAL endpoints (S1 spec §16 "API integration", §0 success
criteria 1-2, §15.3, §8.5), then a backfill of one older FY and an incremental run.

The agent side is played by ``fakeb`` (test-only): every month of the window is read from FakeBooks with the
extractor's own month request and turned into wire objects by the same transcoder the real captures use; the masters,
the mirrored ledger re-read, the counters and every Trial Balance are read the same way. Nothing is inserted into the
DB directly and nothing is dataset-built (ruling F9). Every persisted assertion re-reads from a fresh session.

Clock: the conftest ``FixedClock`` (2026-09-25 06:30 UTC, IST FY 2026-27) -> the first-sync window is FY 2025-26
(12 months) + FY 2026-27 (6 months, Apr..Sep; FakeBooks has no vouchers there). Tally's current period (F25) is FY
2025-26, so parity's as-on is 31-03-2026 and the D9 anchor (verified edge 01-04-2025) is the ledger-level TB as-on
31-03-2025.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from v2.tests.cloud import parity_fakebooks as pf
from v2.tests.cloud.conftest import login_device, make_workspace, requires_db, web_headers
from v2.tests.cloud.db.ingest_helpers import batch, fresh, one, post_batch, sw_row
from v2.tests.cloud.fakeb import FakeB, fakeb_company_b, fy_months, month_bounds

pytestmark = requires_db

UTC = timezone.utc
AS_ON = date(2026, 3, 31)
FY25, FY26, FY24 = date(2025, 4, 1), date(2026, 4, 1), date(2024, 4, 1)
CAPTURE_STARTED = datetime(2026, 9, 25, 5, 59, tzinfo=UTC)
CAPTURED = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
SNAP_AT = datetime(2026, 9, 25, 6, 1, tzinfo=UTC)


@dataclass
class Flow:
    """One bound workspace driven like the agent would drive it."""
    client: object
    fb: FakeB
    ws: uuid.UUID
    uid: uuid.UUID
    headers: dict
    run_id: str | None = None
    coverage: list[dict] = field(default_factory=list)
    batches: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def web(self) -> dict:
        return web_headers(self.uid)

    async def call(self, method: str, path: str, *, expect: int = 200, **kw):
        r = await getattr(self.client, method)(path, headers=kw.pop("headers", self.headers), **kw)
        assert r.status_code == expect, (method, path, r.status_code, r.text)
        self.log.append(f"{method.upper()} {path} {r.status_code}")
        return r.json() if r.content else None

    async def open_run(self, kind: str, counters: dict | None = None, **extra) -> dict:
        res = await self.call("post", f"/api/sync/{self.ws}/runs",
                              json={"kind": kind, "counters_at_start": counters or self.fb.counters(), **extra})
        self.run_id, self.coverage, self.batches = res["run_id"], res["coverage"], 0
        return res

    async def post_objects(self, objects: list[dict], *, chunk: tuple[date, date] | None = None,
                           size: int = 400) -> list[dict]:
        out = []
        for i in range(0, len(objects), size):
            body = batch(self.run_id, objects[i:i + size], company_guid=self.fb.guid,
                         chunk=None if chunk is None else {"from": chunk[0].isoformat(), "to": chunk[1].isoformat()})
            r = await post_batch(self.client, self.ws, self.headers, body)
            assert r.status_code == 200, r.text
            out.append(r.json())
            self.batches += 1
        return out

    async def ack(self, fy: date, month: str) -> dict:
        return await self.call("patch", f"/api/sync/{self.ws}/coverage",
                               json={"fy_start": fy.isoformat(), "month": month, "run_id": self.run_id})

    def months_total(self, fy: date) -> int:
        return next(r["months_total"] for r in self.coverage if r["fy_start"] == fy.isoformat())

    async def sync_fy(self, fy: date, *, masters_first: bool) -> int:
        """Every month of ``fy`` the coverage row expects: vouchers batch (+ the masters before the first month when
        asked), then ``PATCH /coverage``. Returns the number of vouchers posted."""
        months = fy_months(fy)[: self.months_total(fy)]
        n = 0
        for i, month in enumerate(months):
            if masters_first and i == 0:
                await self.post_objects(self.fb.masters())
            vouchers = self.fb.month_vouchers(fy, month)
            if vouchers:
                await self.post_objects(vouchers, chunk=month_bounds(month))
            n += len(vouchers)
            await self.ack(fy, month)
        return n

    async def complete(self, **extra) -> dict:
        return await self.call("patch", f"/api/sync/{self.ws}/runs/{self.run_id}", json={
            "status": "completed", "progress_done": 1, "progress_total": 1, "batches_declared": self.batches,
            **extra})

    async def snapshot(self, report_type: str, as_on: date, *, at: datetime = SNAP_AT, purpose: str = "parity"):
        return await self.call("post", f"/api/sync/{self.ws}/snapshots",
                               json=self.fb.snapshot_body(report_type, as_on, at, purpose=purpose))

    async def parity(self, *, capture_started_at: datetime = CAPTURE_STARTED, **kw) -> dict:
        c = self.fb.counters()
        body = {"scope": "daily", "as_on_date": "31-03-2026", "capture_started_at": capture_started_at.isoformat(),
                "counters_before": c, "counters_after": c, "remediation_done": []}
        body.update(kw)
        return await self.call("post", f"/api/sync/{self.ws}/parity", json=body)

    async def state(self) -> dict:
        return await self.call("get", f"/api/sync/{self.ws}/state")

    async def status(self) -> dict:
        return await self.call("get", f"/api/workspaces/{self.ws}/sync-status", headers=self.web)


async def bind_fakeb(client, session, fb: FakeB, *, device_name: str = "ACCOUNTS-PC") -> Flow:
    uid, _, headers = await login_device(client, session, device_name=device_name)
    ws = await make_workspace(session, uid)
    flow = Flow(client, fb, ws, uid, headers)
    await flow.call("post", "/api/sync/company", json={
        "workspace_id": str(ws), "company_guid": fb.guid, "company_name": pf.B_NAME, "books_from": "20220401",
        "base_currency_name": "INR", "takeover": False})
    return flow


async def first_sync(client, session, fb: FakeB) -> Flow:
    """login -> bind -> first_sync over the window (masters with the first month) -> mirrored balances ->
    snapshots (TB, ledger-level TB, anchor) -> run completed."""
    flow = await bind_fakeb(client, session, fb)
    await flow.open_run("first_sync")
    window = sorted(date.fromisoformat(r["fy_start"]) for r in flow.coverage)[-2:]
    assert window == [FY25, FY26]
    for i, fy in enumerate(window):
        await flow.sync_fy(fy, masters_first=(i == 0))
    await flow.post_objects(fb.balances(CAPTURED))
    await flow.snapshot("trial_balance", AS_ON)
    await flow.snapshot("trial_balance_ledgerwise", AS_ON)
    await flow.snapshot("trial_balance_ledgerwise", FY25 - timedelta(days=1), purpose="anchor")
    await flow.complete()
    return flow


def fb_session_fake() -> FakeB:
    """A private FakeBooks B per test (the loaded state is cached once per session by ``parity_fakebooks``)."""
    return fakeb_company_b().fresh()


async def test_whole_first_sync_of_fakebooks_b_through_real_endpoints(app_client, session, engine, clock):
    fb = fb_session_fake()
    counters = fb.counters()
    flow = await first_sync(app_client, session, fb)

    state = await flow.state()
    assert state["sync_state"] == "ready"
    assert state["cursors"] == counters                                  # §15.3: first_sync -> counters at start
    assert state["open_runs"] == [] and state["commands"] == []
    cov = {r["fy_start"]: r for r in state["coverage"]}
    assert {k: cov[k]["state"] for k in cov} == {"2022-04-01": "pending", "2023-04-01": "pending",
                                                  "2024-04-01": "pending", "2025-04-01": "complete",
                                                  "2026-04-01": "complete"}
    assert (cov["2025-04-01"]["months_complete"], cov["2026-04-01"]["months_complete"]) == (12, 6)

    res = await flow.parity()
    assert (res["status"], res["abort_reason"]) == ("ok", None), res
    assert res["summary"]["mismatches"] == 0 and res["ladder"]["state"] == "ok"

    status = await flow.status()
    assert status["sync_state"] == "ready"
    assert status["last_parity"]["state"] == "ok" and status["last_parity"]["run_id"] == res["parity_run_id"]
    assert status["last_parity"]["verified_from"] == "2025-04-01"
    assert status["backfill"]["oldest_complete_fy"] == "2025-04-01"
    synced_at = status["last_synced_at"]
    assert synced_at is not None

    # every FY 2025-26 voucher FakeBooks holds is in the cloud, lines and all
    expected = sum(len(fb.month_vouchers(FY25, m)) for m in fy_months(FY25))
    async with fresh(engine) as s:
        stored = (await one(s, "SELECT count(*) AS n FROM tally_vouchers WHERE workspace_id = :w AND date "
                               "BETWEEN '2025-04-01' AND '2026-03-31'", w=flow.ws))["n"]
        ledgers = (await one(s, "SELECT count(*) AS n FROM tally_ledgers WHERE workspace_id = :w", w=flow.ws))["n"]
    assert stored == expected and expected > 0
    assert ledgers == sum(1 for m in fb.masters() if m["kind"] == "ledger")

    # --- backfill one older FY: last_synced_at never moves, the verified edge (oldest_complete_fy) does
    clock.advance(minutes=2)
    await flow.open_run("backfill")
    posted = await flow.sync_fy(FY24, masters_first=False)
    assert posted > 0
    done = await flow.complete()
    assert done["cursors"] == counters and done["sync_state"] == "ready"   # §15.3: backfill leaves the cursor
    after = await flow.status()
    assert after["last_synced_at"] == synced_at
    assert after["backfill"]["oldest_complete_fy"] == "2024-04-01"
    assert after["backfill"]["state"] == "running"                       # FY 2022-23 / 2023-24 still pending
    async with fresh(engine) as s:
        sw = await sw_row(s, flow.ws)
    assert (sw["oldest_complete_fy"], sw["last_synced_at"].isoformat()) == (FY24, synced_at)


async def test_incremental_after_first_sync_moves_cursor_and_last_synced(app_client, session, engine, clock):
    fb = fb_session_fake()
    flow = await first_sync(app_client, session, fb)
    before = await flow.status()
    old_counters = fb.counters()

    tag = pf.add_current_fy_usd_sale(fb.books)                        # a new voucher in Tally (Sept 2025)
    new_counters = fb.counters()
    assert new_counters["alt_vch_id"] > old_counters["alt_vch_id"]
    clock.advance(minutes=3)
    await flow.open_run("incremental", counters=new_counters)
    (sale,) = [v for v in fb.month_vouchers(FY25, "2025-09") if v["data"]["narration"].startswith(tag)]
    (accepted,) = await flow.post_objects([sale], chunk=month_bounds("2025-09"))
    assert accepted["counts"]["inserted"] == 1
    await flow.post_objects(fb.balances(CAPTURED + timedelta(minutes=3)))
    done = await flow.complete(cursor_after=new_counters)
    assert done["cursors"] == new_counters and done["sync_state"] == "ready"

    after = await flow.status()
    assert after["last_synced_at"] != before["last_synced_at"]
    assert datetime.fromisoformat(after["last_synced_at"]) == clock.now()
    assert (await flow.state())["cursors"] == new_counters

    # the new data still reconciles: fresh snapshots for the new counters, parity ok with the sale revalued
    at = SNAP_AT + timedelta(minutes=3)
    await flow.snapshot("trial_balance", AS_ON, at=at)
    await flow.snapshot("trial_balance_ledgerwise", AS_ON, at=at)
    res = await flow.parity(capture_started_at=CAPTURED + timedelta(minutes=2))
    assert res["status"] == "ok", res
    async with fresh(engine) as s:
        sw = await sw_row(s, flow.ws)
    assert (sw["cursor_alt_vch_id"], sw["cursor_alt_mst_id"]) == (new_counters["alt_vch_id"],
                                                                  new_counters["alt_mst_id"])
