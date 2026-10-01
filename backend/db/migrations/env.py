"""Alembic env.py — async migration runner for the one chain (app tables and sync tables).

Database URL, first one set wins: ``config.attributes["url"]`` (a caller driving Alembic from Python, e.g. the
migration tests), then the URL the app itself uses (``backend.config.Settings``: the ``DATABASE_URL`` environment
variable, then ``.env``). Nothing set: the run fails — there is no fallback URL in ``alembic.ini``. See
``backend/db/migration_url.py``.
"""
import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
import backend.db  # noqa: F401  (registers every model: the 9 app tables and the 21 sync tables)
from backend.db.migration_url import resolve_migration_url
from backend.db.models import Base

target_metadata = Base.metadata  # all 30 tables

def get_url() -> str:
    return resolve_migration_url(context.config.attributes.get("url"))

def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()  # also when a revision raises (006 refuses an unknown old sync chain)

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
