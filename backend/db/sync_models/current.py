"""Read-only reflections of the current app's ``users`` / ``workspaces`` tables (spec §4.1, §6.2).

v2 never creates, alters, or drops these — they live in the current app's own Alembic chain
(``backend/db/migrations``). These ``Table`` objects exist only so v2's own tables can declare bare FKs into them,
and carry ``info={"v2_readonly": True}`` so the v2 Alembic ``include_object`` filter (``alembic/env.py``) never
emits DDL against them. No mapped ORM class for either — v2 code that ever needs to read/write ``users`` or
``workspaces`` rows (e.g. the DB test harness) does so with plain ``sqlalchemy.text()``, never through these
Table objects' ORM machinery.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, String, Table
from sqlalchemy.dialects.postgresql import UUID

from .base import Base

users_table = Table(
    "users",
    Base.metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("email", String(255), nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("name", String(255), nullable=False),
    Column("is_active", Boolean),
    info={"v2_readonly": True},
)

workspaces_table = Table(
    "workspaces",
    Base.metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("name", String(255), nullable=False),
    Column("is_deleted", Boolean),
    info={"v2_readonly": True},
)
