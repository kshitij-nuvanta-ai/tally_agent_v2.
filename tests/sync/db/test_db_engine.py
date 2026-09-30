"""I1 (S1 review): a DB error must never carry bound business values into ``str(exc)`` / a logged traceback.
The engine under test is the backend's one engine (``backend/db/engine.py``, v2 merge M3)."""
import traceback

import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import engine as app_engine
from backend.db.engine import build_engine, get_db
from backend.sync.errors import SyncRoute
from tests.sync.conftest import TEST_DB, requires_db

pytestmark = requires_db

SECRET = "NARRATION-Being-paid-to-Sharma-Traders-123456.78"


async def test_db_error_does_not_leak_bound_parameters():
    eng = build_engine(TEST_DB)
    try:
        with pytest.raises(DBAPIError) as ei:
            async with eng.connect() as c:
                await c.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
        rendered = str(ei.value) + "".join(traceback.format_exception(ei.value))
        assert SECRET not in rendered
        assert "hidden due to hide_parameters" in rendered
    finally:
        await eng.dispose()


async def test_the_apps_own_engine_hides_parameters_and_pre_pings(app_factory):
    """M3: the engine every route uses (``init_engine``, here through ``app_factory``) is built by the same
    helper — a DB error in a session from ``get_db`` hides its parameters too."""
    assert app_engine.engine.sync_engine.hide_parameters is True
    assert app_engine.engine.pool._pre_ping is True
    with pytest.raises(DBAPIError) as ei:
        async for session in get_db():
            await session.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
    rendered = str(ei.value) + "".join(traceback.format_exception(ei.value))
    assert SECRET not in rendered and "hidden due to hide_parameters" in rendered


async def test_db_error_in_a_route_logs_no_business_value(app_client, caplog):
    """The driver's own message can quote a value (asyncpg: ``invalid input for query argument $1: '…'``), which
    ``hide_parameters`` doesn't cover — so a DB error reaching a sync route is answered 500 and logged by class
    only. The probe route is a sync route: it is on a router with the sync route class (``SyncRoute``), which is
    what scopes this handling to the sync routes (M9; the other half is in ``test_main_app.py``)."""
    probe = APIRouter(route_class=SyncRoute)

    @probe.get("/api/sync/_test_db_boom")
    async def _boom(session: AsyncSession = Depends(get_db)) -> dict:
        await session.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
        return {}

    app_client.app.include_router(probe)
    caplog.set_level("DEBUG")
    r = await app_client.get("/api/sync/_test_db_boom")
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error", "detail": ""}
    assert SECRET[:20] not in r.text
    logged = caplog.text + "".join(
        "".join(traceback.format_exception(rec.exc_info[1])) for rec in caplog.records if rec.exc_info)
    assert SECRET[:20] not in logged
    assert "DBAPIError" in caplog.text or "Error" in caplog.text
