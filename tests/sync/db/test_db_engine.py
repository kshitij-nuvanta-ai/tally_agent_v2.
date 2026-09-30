"""I1 (S1 review): a DB error must never carry bound business values into ``str(exc)`` / a logged traceback."""
import traceback

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.sync.db import make_engine
from tests.sync.conftest import TEST_DB, requires_db

pytestmark = requires_db

SECRET = "NARRATION-Being-paid-to-Sharma-Traders-123456.78"


async def test_db_error_does_not_leak_bound_parameters():
    eng = make_engine(TEST_DB)
    try:
        with pytest.raises(DBAPIError) as ei:
            async with eng.connect() as c:
                await c.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
        rendered = str(ei.value) + "".join(traceback.format_exception(ei.value))
        assert SECRET not in rendered
        assert "hidden due to hide_parameters" in rendered
    finally:
        await eng.dispose()


async def test_db_error_in_a_route_logs_no_business_value(app_client, caplog):
    """The driver's own message can quote a value (asyncpg: ``invalid input for query argument $1: '…'``), which
    ``hide_parameters`` doesn't cover — so a DB error reaching the app is answered 500 and logged by class only."""
    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.sync.db import session_dep

    async def _boom(session: AsyncSession = Depends(session_dep)) -> dict:
        await session.execute(text("SELECT CAST(:v AS numeric(18,2))"), {"v": SECRET})
        return {}

    app_client.app.add_api_route("/api/v2/_test_db_boom", _boom)
    caplog.set_level("DEBUG")
    r = await app_client.get("/api/v2/_test_db_boom")
    assert r.status_code == 500
    assert r.json()["error"] == "internal_error"
    assert SECRET[:20] not in r.text
    logged = caplog.text + "".join(
        "".join(traceback.format_exception(rec.exc_info[1])) for rec in caplog.records if rec.exc_info)
    assert SECRET[:20] not in logged
    assert "DBAPIError" in caplog.text or "Error" in caplog.text
