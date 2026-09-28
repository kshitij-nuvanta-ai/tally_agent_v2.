"""Base declarative class + own ``MetaData`` for the v2 cloud app's ORM models (spec §4.1, §6.2).

A distinct ``MetaData`` from the current app's ``backend.db.models.Base`` so the v2 Alembic chain
(``alembic_version_v2``) only ever targets v2's own tables, never the current app's. ``current.py``'s read-only
reflections of ``users``/``workspaces`` also live in this metadata (so v2 tables can declare bare FKs into them),
but are excluded from DDL via ``info={"v2_readonly": True}`` + the ``include_object`` filter in ``alembic/env.py``.
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = MetaData()
