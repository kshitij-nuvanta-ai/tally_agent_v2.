"""V2 cloud's own async SQLAlchemy engine + session dependency — its own connection, its own Alembic chain
(``alembic_version_v2``); it never shares the current app's engine (S1 Global Constraints)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def session_dep(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield an ``AsyncSession`` from ``request.app.state.sessionmaker``."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
