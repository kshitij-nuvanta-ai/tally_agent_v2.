"""v2 cloud's own Alembic env — async runner, own version table ``alembic_version_v2`` (spec §4.1).

``include_object`` restricts every autogenerate/compare operation to exactly the tables in ``V2_TABLES``, and
excludes anything marked ``info={"v2_readonly": True}`` (the reflected ``users``/``workspaces`` tables in
``models/current.py``) — so this chain can never emit DDL against the current app's tables, even by accident.
"""
import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db.sync_models import V2_TABLES, Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in V2_TABLES and not (getattr(obj, "info", {}) or {}).get("v2_readonly")
    return True


def _configure(**kw):
    context.configure(
        target_metadata=target_metadata,
        version_table="alembic_version_v2",
        include_object=include_object,
        **kw,
    )


async def _online(url: str) -> None:
    eng = create_async_engine(url, hide_parameters=True)   # S1 review I1: no bound values in logs
    async with eng.connect() as conn:
        await conn.run_sync(lambda c: (_configure(connection=c), context.run_migrations()))
        await conn.commit()
    await eng.dispose()


url = context.config.attributes.get("url") or os.environ["V2_DATABASE_URL"]
if context.is_offline_mode():
    _configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online(url))
