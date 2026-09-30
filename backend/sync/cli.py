"""``purge_cli`` — the retention sweep behind ``python -m backend.sync purge`` (Q5, Task 11).

Wraps ``sync/purge.py``'s ``purge()``: builds its own short-lived engine/session_factory from ``url``
(through ``backend.db.engine.build_engine``, like the app's) so the CLI never shares a connection with the app's
own lifespan. Schema migrations are not run from here: the sync tables are part of the one Alembic chain
(``alembic upgrade head``, revision ``006``).
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import Settings
from backend.sync.clock import Clock, SystemClock
from backend.db.engine import build_engine


async def purge_cli(
    url: str, *, workspace_id: uuid.UUID | None = None, now: bool = False, clock: Clock | None = None
) -> dict[str, int]:
    """``python -m backend.sync purge`` entry point (Q5): its own engine, disposed after one run."""
    from backend.sync import purge as purge_mod  # local: avoid importing the sync package at CLI import time

    engine = build_engine(url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await purge_mod.purge(
            session_factory, workspace_id=workspace_id, now=now, clock=clock or SystemClock(),
            settings=Settings(DATABASE_URL=url),
        )
    finally:
        await engine.dispose()
