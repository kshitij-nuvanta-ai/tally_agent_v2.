"""Re-runnable measurement behind `maintenance.AVG_ROW_BYTES["tally_vouchers"]` (S1 Task 11 fix round 1).

NOT collected by the normal suite (no `test_` prefix). Run explicitly against the TEST database:

    TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test ANTHROPIC_API_KEY=test-key \
    PYTHONPATH=. uv run pytest tests/sync/db/measure_avg_row_bytes.py -s -q

It ingests company B's real FY2022-23 vouchers (`p21_B_fy2022_month_01..12.xml`) through the real `/batches`
endpoint twice: once OUT of the raw window (default clock = FY2026-27, so `raw` is SQL NULL -- what most stored
vouchers look like) and once IN the window (`store.raw_window_fys` pinned to FY2022-23, so `raw` is kept), then prints
`round(avg(pg_column_size(t.*)))` for each. `AVG_ROW_BYTES["tally_vouchers"]` uses the out-of-window figure.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text

from backend.sync.ingest import store
from tests.sync import realdata
from tests.sync.conftest import requires_db
from tests.sync.db.ingest_helpers import bound, fresh, post_ok

pytestmark = requires_db


async def _ingest_fy2022(app_client, session):
    ws, headers, run_id, uid = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, realdata.b_masters())
    for month in range(1, 13):
        objs = realdata.vouchers(f"p21_B_fy2022_month_{month:02d}.xml")
        if objs:
            await post_ok(app_client, ws, headers, run_id, objs)
    return ws


async def _measure(engine, ws) -> tuple[int, int, int]:
    async with fresh(engine) as s:
        row = (await s.execute(text(
            "SELECT count(*) AS n, round(avg(pg_column_size(t.*)))::int AS avg_bytes, "
            "count(*) FILTER (WHERE raw IS NULL) AS sql_null FROM tally_vouchers t WHERE workspace_id = :w"),
            {"w": ws})).mappings().one()
        return row["n"], row["avg_bytes"], row["sql_null"]


async def test_measure_tally_vouchers_avg_row_bytes(app_client, session, engine, monkeypatch):
    ws_out = await _ingest_fy2022(app_client, session)
    n, out_avg, out_null = await _measure(engine, ws_out)
    async def pinned(sess, ws_id, today_ist):
        return {date(2022, 4, 1)}

    monkeypatch.setattr(store, "raw_window_fys", pinned)
    ws_in = await _ingest_fy2022(app_client, session)
    m, in_avg, in_null = await _measure(engine, ws_in)
    print(f"\nAVG_ROW_BYTES tally_vouchers: OUT-of-window (raw SQL NULL) rows={n} sql_null={out_null} avg={out_avg} B"
          f" | IN-window (raw kept) rows={m} sql_null={in_null} avg={in_avg} B")
    assert n > 0 and out_null == n and in_null < m
