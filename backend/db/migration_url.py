"""Which database Alembic migrates: the one the app serves, never a different one by accident.

Used by ``backend/db/migrations/env.py``. Kept here, outside the Alembic script directory, so it can be imported
and tested without running a migration.
"""
from __future__ import annotations


def resolve_migration_url(attribute_url: str | None) -> str:
    """The database URL for an Alembic run. First one set wins:

    1. ``attribute_url`` — ``config.attributes["url"]``, set by a caller driving Alembic from Python (the
       migration tests, the sync test harness);
    2. the URL the app itself would use: ``Settings().DATABASE_URL``, which reads the process environment first,
       then ``.env`` in the working directory, and still accepts the old ``V2_DATABASE_URL`` name.

    Nothing set: ``RuntimeError``. There is deliberately no fallback to a URL written in ``alembic.ini`` — that
    is how ``alembic upgrade head`` once migrated a different database than the one the app was serving.
    """
    if attribute_url:
        return attribute_url
    from backend.config import Settings  # a fresh read, not the import-time ``settings`` singleton

    url = Settings().DATABASE_URL
    if url:
        return url
    raise RuntimeError(
        "No database URL for Alembic. Set DATABASE_URL — in the environment, or in the .env file of the "
        "directory you run alembic from (the same place the app reads it) — and run again. alembic.ini "
        "deliberately carries no URL, so Alembic cannot migrate a different database than the app uses."
    )
