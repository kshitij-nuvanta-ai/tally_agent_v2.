"""Shared setup for the S1 task 8c ingest DB tests (`test_ingest_api.py`, `test_ingest_scenarios.py`): bind a
workspace to company A or B, open a run, post a gzipped batch, and re-read state from a fresh session."""
from __future__ import annotations

import copy
import gzip
import json
import uuid

from sqlalchemy import text

from v2.tests.cloud import realdata
from v2.tests.cloud.conftest import login_device, make_workspace, web_headers

B_BIND = {
    "company_guid": realdata.COMPANY_B_GUID,
    "company_name": "Sharma & Sons' Probe Traders",
    "books_from": "20220401",
    "base_currency_name": "INR",
    "takeover": False,
}
A_BIND = {
    "company_guid": realdata.COMPANY_A_GUID,
    "company_name": "Bharat Traders Probe Copy",
    "books_from": "20250401",
    "base_currency_name": "INR",
    "takeover": False,
}
COUNTERS = {"alt_vch_id": 965, "alt_mst_id": 412}


async def bind(app_client, session, company: str = "B", *, device_name: str = "ACCOUNTS-PC"):
    uid, login_body, headers = await login_device(app_client, session, device_name=device_name)
    ws = await make_workspace(session, uid)
    body = {**(B_BIND if company == "B" else A_BIND), "workspace_id": str(ws)}
    r = await app_client.post("/api/sync/company", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return uid, ws, headers


async def open_run(app_client, ws, headers, kind: str = "first_sync", **extra) -> str:
    body = {"kind": kind, "counters_at_start": COUNTERS, **extra}
    r = await app_client.post(f"/api/sync/{ws}/runs", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


async def bound(app_client, session, company: str = "B"):
    """user + workspace + login + bind + an open `first_sync` run -> (ws, headers, run_id, uid)."""
    uid, ws, headers = await bind(app_client, session, company)
    run_id = await open_run(app_client, ws, headers)
    return ws, headers, run_id, uid


def batch(run_id: str, objects: list[dict], *, company_guid: str = realdata.COMPANY_B_GUID,
          batch_id: str | None = None, quarantine: list[dict] | None = None, chunk: dict | None = None) -> dict:
    body = {"batch_id": batch_id or uuid.uuid4().hex, "run_id": run_id, "company_guid": company_guid,
            "objects": objects, "quarantine": quarantine or []}
    if chunk is not None:
        body["chunk"] = chunk
    return body


def gz(body: dict) -> bytes:
    return gzip.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"))


async def post_batch(app_client, ws, headers, body: dict, *, compress: bool = True):
    if compress:
        return await app_client.post(
            f"/api/sync/{ws}/batches", content=gz(body),
            headers={**headers, "Content-Encoding": "gzip", "Content-Type": "application/json"})
    return await app_client.post(f"/api/sync/{ws}/batches", content=json.dumps(body).encode("utf-8"),
                                 headers={**headers, "Content-Type": "application/json"})


async def post_ok(app_client, ws, headers, run_id, objects, **kw) -> dict:
    company_guid = kw.pop("company_guid", realdata.COMPANY_B_GUID)
    r = await post_batch(app_client, ws, headers, batch(run_id, objects, company_guid=company_guid, **kw))
    assert r.status_code == 200, r.text
    return r.json()


def voucher_by_guid(objs: list[dict], guid_suffix: str) -> dict:
    return copy.deepcopy(next(o for o in objs if o["data"]["guid"].endswith(guid_suffix)))


# --- fresh-session re-reads (§14: every scenario re-reads the whole state) ------------------------------------

async def rows(session, sql: str, **params) -> list[dict]:
    return [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]


async def one(session, sql: str, **params) -> dict | None:
    r = (await session.execute(text(sql), params)).mappings().first()
    return dict(r) if r else None


async def count(session, table: str, ws) -> int:
    return (await session.execute(text(f"SELECT count(*) FROM {table} WHERE workspace_id = :w"), {"w": ws})).scalar_one()


DATA_TABLES = ("tally_currencies", "tally_groups", "tally_voucher_types", "tally_units", "tally_stock_groups",
               "tally_ledgers", "tally_stock_items", "tally_vouchers", "tally_voucher_ledger_lines",
               "tally_voucher_inventory_lines", "tally_bill_allocations", "sync_quarantine")


async def table_counts(session, ws) -> dict[str, int]:
    return {t: await count(session, t, ws) for t in DATA_TABLES}


async def voucher_state(session, ws, guid: str) -> dict:
    """The whole stored state of one voucher: its row, its lines, inventory lines and bills (ordered)."""
    v = await one(session, "SELECT * FROM tally_vouchers WHERE workspace_id = :w AND guid = :g", w=ws, g=guid)
    if v is None:
        return {}
    vid = v["id"]
    return {
        "voucher": v,
        "lines": await rows(session, "SELECT * FROM tally_voucher_ledger_lines WHERE voucher_id = :v ORDER BY line_no",
                            v=vid),
        "inventory": await rows(session, "SELECT * FROM tally_voucher_inventory_lines WHERE voucher_id = :v "
                                         "ORDER BY line_no", v=vid),
        "bills": await rows(session, "SELECT * FROM tally_bill_allocations WHERE voucher_id = :v "
                                     "ORDER BY ledger_line_no, bill_name", v=vid),
    }


async def sw_row(session, ws) -> dict:
    return await one(session, "SELECT * FROM sync_workspaces WHERE workspace_id = :w", w=ws)


__all__ = ["web_headers"]


def fresh(engine):
    """A brand-new session on the test engine (never the one the API request used) for whole-state re-reads."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)()
