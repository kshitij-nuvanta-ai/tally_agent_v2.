"""The sync settings on the one ``Settings`` class (v2 merge M2).

Every sync field is read from its new env name and from the old ``V2_``-prefixed one; the new name wins when both
are set. ``DATABASE_URL`` also accepts ``V2_DATABASE_URL`` and ``JWT_SECRET`` also accepts ``V2_WEB_JWT_SECRET``.
"""
import pytest

from backend.config import Settings

SYNC_DEFAULTS = {
    "DEVICE_TOKEN_SECRET": "",
    "DEVICE_ACCESS_MINUTES": 15,
    "DEVICE_REFRESH_DAYS": 90,
    "TAKEOVER_LOGIN_MAX_AGE_MINUTES": 10,
    "INGEST_MAX_GZIP_BYTES": 5_242_880,
    "INGEST_MAX_DECOMPRESSED_BYTES": 52_428_800,
    "INGEST_MAX_OBJECTS": 500,
    "PARITY_TOLERANCE_PAISE": 100,
    "QUARANTINE_ERROR_THRESHOLD": 50,
    "STORAGE_ALERT_BYTES": 5_368_709_120,
    "LOGIN_RATE_MAX": 5,
    "LOGIN_RATE_WINDOW_S": 900,
    "DEVICE_RATE_MAX": 600,
    "DEVICE_RATE_WINDOW_S": 60,
    "MAINTENANCE_SLICE_SECONDS": 2.0,
    "MAINTENANCE_SLICE_ROWS": 5000,
    "STORAGE_ESTIMATE_INTERVAL_SECONDS": 3600,
    "PURGE_GRACE_DAYS": 30,
}
# A value of the right type, different from the default, per field.
SAMPLE = {name: ("s" * 40 if isinstance(default, str) else default + 1) for name, default in SYNC_DEFAULTS.items()}
ALIASED = [*SYNC_DEFAULTS, "DATABASE_URL", "JWT_SECRET"]
OLD_NAME = {**{name: f"V2_{name}" for name in SYNC_DEFAULTS},
            "DATABASE_URL": "V2_DATABASE_URL", "JWT_SECRET": "V2_WEB_JWT_SECRET"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No test here sees the developer's real environment (``.env`` is switched off with ``_env_file=None``)."""
    for name in ALIASED:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(OLD_NAME[name], raising=False)
    monkeypatch.delenv("V2_PORT", raising=False)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_sync_defaults_are_the_v2_defaults():
    s = _settings()
    assert {name: getattr(s, name) for name in SYNC_DEFAULTS} == SYNC_DEFAULTS
    assert s.DATABASE_URL is None and s.JWT_SECRET is None


@pytest.mark.parametrize("name", sorted(SYNC_DEFAULTS))
def test_sync_field_reads_the_new_env_name(monkeypatch, name):
    monkeypatch.setenv(name, str(SAMPLE[name]))
    assert getattr(_settings(), name) == SAMPLE[name]


@pytest.mark.parametrize("name", sorted(SYNC_DEFAULTS))
def test_sync_field_reads_the_old_v2_env_name(monkeypatch, name):
    monkeypatch.setenv(f"V2_{name}", str(SAMPLE[name]))
    assert getattr(_settings(), name) == SAMPLE[name]


def test_device_token_secret_both_spellings_and_new_name_wins(monkeypatch):
    monkeypatch.setenv("V2_DEVICE_TOKEN_SECRET", "old" * 12)
    assert _settings().DEVICE_TOKEN_SECRET == "old" * 12
    monkeypatch.setenv("DEVICE_TOKEN_SECRET", "new" * 12)
    assert _settings().DEVICE_TOKEN_SECRET == "new" * 12
    monkeypatch.delenv("V2_DEVICE_TOKEN_SECRET")
    assert _settings().DEVICE_TOKEN_SECRET == "new" * 12


def test_database_url_both_spellings_and_new_name_wins(monkeypatch):
    monkeypatch.setenv("V2_DATABASE_URL", "postgresql+asyncpg://h/old")
    s = _settings()
    assert s.DATABASE_URL == "postgresql+asyncpg://h/old" and s.db_mode is True
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://h/new")
    assert _settings().DATABASE_URL == "postgresql+asyncpg://h/new"
    monkeypatch.delenv("V2_DATABASE_URL")
    assert _settings().DATABASE_URL == "postgresql+asyncpg://h/new"


def test_jwt_secret_both_spellings_and_new_name_wins(monkeypatch):
    monkeypatch.setenv("V2_WEB_JWT_SECRET", "o" * 32)
    assert _settings().JWT_SECRET == "o" * 32
    monkeypatch.setenv("JWT_SECRET", "n" * 32)
    assert _settings().JWT_SECRET == "n" * 32
    monkeypatch.delenv("V2_WEB_JWT_SECRET")
    assert _settings().JWT_SECRET == "n" * 32


def test_new_name_wins_in_an_env_file_too(tmp_path):
    env = tmp_path / ".env"
    env.write_text("V2_DEVICE_RATE_MAX=7\nDEVICE_RATE_MAX=8\nV2_LOGIN_RATE_MAX=9\n")
    s = Settings(_env_file=str(env))
    assert (s.DEVICE_RATE_MAX, s.LOGIN_RATE_MAX) == (8, 9)


def test_v2_port_is_ignored(monkeypatch):
    monkeypatch.setenv("V2_PORT", "8100")
    s = _settings()
    assert s.APP_PORT == 8000 and not hasattr(s, "PORT") and not hasattr(s, "port")


def test_fields_are_set_by_name_as_keyword_arguments():
    s = _settings(DATABASE_URL="postgresql+asyncpg://x/y", JWT_SECRET="w" * 32, DEVICE_TOKEN_SECRET="d" * 32,
                  DEVICE_RATE_MAX=3)
    assert (s.DATABASE_URL, s.JWT_SECRET, s.DEVICE_TOKEN_SECRET, s.DEVICE_RATE_MAX) == (
        "postgresql+asyncpg://x/y", "w" * 32, "d" * 32, 3)


def _serving(**overrides) -> Settings:
    return _settings(**{"DATABASE_URL": "postgresql+asyncpg://x/y", "JWT_SECRET": "w" * 32,
                        "DEVICE_TOKEN_SECRET": "d" * 32, **overrides})


def test_validate_for_serving_accepts_a_complete_configuration():
    assert _serving().validate_for_serving() is None


@pytest.mark.parametrize("overrides,message", [
    ({"DATABASE_URL": None}, "DATABASE_URL is required"),
    ({"DATABASE_URL": ""}, "DATABASE_URL is required"),
    ({"JWT_SECRET": None}, "JWT_SECRET must be at least 32 characters"),
    ({"JWT_SECRET": "w" * 31}, "JWT_SECRET must be at least 32 characters"),
    ({"DEVICE_TOKEN_SECRET": ""}, "DEVICE_TOKEN_SECRET must be at least 32 characters"),
    ({"DEVICE_TOKEN_SECRET": "d" * 31}, "DEVICE_TOKEN_SECRET must be at least 32 characters"),
    ({"DEVICE_TOKEN_SECRET": "w" * 32}, "DEVICE_TOKEN_SECRET must differ from JWT_SECRET"),
])
def test_validate_for_serving_refuses(overrides, message):
    with pytest.raises(ValueError, match=message):
        _serving(**overrides).validate_for_serving()


def test_construction_never_validates_for_serving():
    """A legacy-mode (no database) configuration still constructs; only the explicit call refuses it."""
    assert _settings().db_mode is False
