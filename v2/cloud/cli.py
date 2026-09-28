"""v2 cloud Alembic wrappers — used by ``python -m v2.cloud`` and the DB test harness (S1 task 3).

``purge_cli`` (Q5 retention sweep, Task 11) wraps ``sync/purge.py``'s ``purge()`` for ``__main__.py``: builds its
own engine/session_factory from ``url`` so the CLI never shares a connection with the app's own lifespan.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker

from v2.cloud.clock import Clock, SystemClock
from v2.cloud.config import V2Settings
from v2.cloud.db import make_engine

_BASE_DIR = Path(__file__).resolve().parent


def _config() -> Config:
    cfg = Config(str(_BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BASE_DIR / "alembic"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    """Run the v2 Alembic chain (``alembic_version_v2``) up to ``revision`` against ``url``."""
    cfg = _config()
    cfg.attributes["url"] = url
    command.upgrade(cfg, revision)


def downgrade(url: str, revision: str = "base") -> None:
    """Run the v2 Alembic chain down to ``revision`` against ``url``."""
    cfg = _config()
    cfg.attributes["url"] = url
    command.downgrade(cfg, revision)


async def purge_cli(
    url: str, *, workspace_id: uuid.UUID | None = None, now: bool = False, clock: Clock | None = None
) -> dict[str, int]:
    """``python -m v2.cloud purge`` entry point (Q5): its own engine, disposed after one run."""
    from v2.cloud.sync import purge as purge_mod  # local: avoid importing the sync package at CLI import time

    engine = make_engine(url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await purge_mod.purge(
            session_factory, workspace_id=workspace_id, now=now, clock=clock or SystemClock(),
            settings=V2Settings(database_url=url),
        )
    finally:
        await engine.dispose()
