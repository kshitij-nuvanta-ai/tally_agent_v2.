"""Which database Alembic migrates (review I3): the one the app itself uses, or none.

Order: ``config.attributes["url"]`` (a caller driving Alembic from Python) → the URL ``backend.config.Settings``
resolves (process environment first, then ``.env``; ``V2_DATABASE_URL`` still accepted) → otherwise an error.
There is no fallback to a URL written in ``alembic.ini``.

No database is touched: the resolver is tested on its own, and ``env.py`` only in Alembic's offline (``--sql``)
mode, which prints SQL and never connects. Every URL here is made up.
"""
import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from backend.db.migration_url import resolve_migration_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ATTR_URL = "postgresql+asyncpg://attr@localhost/tallyagent_test_from_attribute"
ENV_URL = "postgresql+asyncpg://env@localhost/tallyagent_test_from_env"
DOTENV_URL = "postgresql+asyncpg://dotenv@localhost/tallyagent_test_from_dotenv"
OLD_NAME_URL = "postgresql+asyncpg://old@localhost/tallyagent_test_from_v2_name"


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No ``DATABASE_URL`` / ``V2_DATABASE_URL`` in the environment, and a working directory with no ``.env``
    (the repo's own ``.env`` points at the dev database and must never be what these tests read)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("V2_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_attribute_url_wins_over_everything(clean, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    (clean / ".env").write_text(f"DATABASE_URL={DOTENV_URL}\n")
    assert resolve_migration_url(ATTR_URL) == ATTR_URL


def test_attribute_url_needs_no_other_setting(clean):
    assert resolve_migration_url(ATTR_URL) == ATTR_URL


def test_environment_variable_is_used(clean, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    assert resolve_migration_url(None) == ENV_URL


def test_environment_variable_wins_over_dotenv(clean, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    (clean / ".env").write_text(f"DATABASE_URL={DOTENV_URL}\n")
    assert resolve_migration_url(None) == ENV_URL


def test_dotenv_is_used_when_the_environment_variable_is_absent(clean):
    """The I3 bug: the app reads ``.env``, Alembic did not — so it migrated the ``alembic.ini`` database."""
    (clean / ".env").write_text(f"JWT_SECRET={'j' * 32}\nDATABASE_URL={DOTENV_URL}\n")
    assert resolve_migration_url(None) == DOTENV_URL


def test_old_v2_name_is_accepted_and_the_new_name_wins(clean, monkeypatch):
    monkeypatch.setenv("V2_DATABASE_URL", OLD_NAME_URL)
    assert resolve_migration_url(None) == OLD_NAME_URL
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    assert resolve_migration_url(None) == ENV_URL


@pytest.mark.parametrize("attribute", [None, ""])
def test_nothing_set_fails_and_says_what_to_set(clean, attribute):
    with pytest.raises(RuntimeError, match="DATABASE_URL") as ei:
        resolve_migration_url(attribute)
    assert "No database URL for Alembic" in str(ei.value) and ".env" in str(ei.value)


def test_an_empty_database_url_counts_as_not_set(clean, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="No database URL for Alembic"):
        resolve_migration_url(None)


def test_the_same_url_the_app_resolves(clean):
    from backend.config import Settings

    (clean / ".env").write_text(f"DATABASE_URL={DOTENV_URL}\n")
    assert resolve_migration_url(None) == Settings().DATABASE_URL == DOTENV_URL


# --- env.py and alembic.ini really use it (offline mode: SQL is printed, nothing connects) ----------------------

def _offline_upgrade(attribute_url: str | None = None) -> str:
    out = io.StringIO()
    cfg = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=out)
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "db" / "migrations"))
    if attribute_url:
        cfg.attributes["url"] = attribute_url
    command.upgrade(cfg, "001", sql=True)
    return out.getvalue()


def test_alembic_ini_carries_no_database_url():
    assert not Config(str(REPO_ROOT / "alembic.ini")).get_main_option("sqlalchemy.url")


def test_env_py_fails_when_no_url_is_set_instead_of_falling_back(clean):
    with pytest.raises(RuntimeError, match="No database URL for Alembic"):
        _offline_upgrade()


@pytest.mark.parametrize("source", ["attribute", "environment", "dotenv"])
def test_env_py_offline_mode_works_with_each_source(clean, monkeypatch, source):
    if source == "environment":
        monkeypatch.setenv("DATABASE_URL", ENV_URL)
    elif source == "dotenv":
        (clean / ".env").write_text(f"DATABASE_URL={DOTENV_URL}\n")
    sql = _offline_upgrade(ATTR_URL if source == "attribute" else None)
    assert "CREATE TABLE users" in sql and "INSERT INTO alembic_version" in sql
