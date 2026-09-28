"""v2 cloud Alembic wrappers — used by ``python -m v2.cloud`` and the DB test harness (S1 task 3).

``purge`` (Q5 retention sweep) lands in Task 11; only ``migrate``/``downgrade`` exist here.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

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
