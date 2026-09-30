"""The sync routes' async SQLAlchemy engine + session dependency. Temporary: the routes move onto the one engine
in ``backend/db/engine.py`` in v2 merge T5. The schema is the one Alembic chain's (revision ``006``)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    """``hide_parameters=True``: a DB error's ``str()``/traceback never carries bound business values (narration,
    party/ledger names, amounts) into the server logs (Global Constraint "no business data in logs"; S1 review I1)."""
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)


async def session_dep(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield an ``AsyncSession`` from ``request.app.state.sessionmaker``."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
