"""V2 cloud settings (S1 spec §9.4, §6.2, A12, A13). ``env_prefix="V2_"``.

Two secrets fall back to the current app's own env vars (F21, A12) so the cloud app can share one deployment's
Postgres URL and web JWT secret without duplicating them: ``database_url`` reads ``V2_DATABASE_URL`` first, then
``DATABASE_URL``; ``web_jwt_secret`` reads ``V2_WEB_JWT_SECRET`` first, then ``JWT_SECRET`` (the current app's own
secret — the two apps trust the same web session). ``populate_by_name=True`` (F21) so tests and fixtures can also
construct ``V2Settings(database_url=..., web_jwt_secret=...)`` by field name, not just by env/alias.
"""
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class V2Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="V2_", env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # --- Connectivity ---
    database_url: str = Field(default="", validation_alias=AliasChoices("V2_DATABASE_URL", "DATABASE_URL"))
    port: int = 8100

    # --- Auth secrets (D6: web JWT and device secret are separate settings) ---
    web_jwt_secret: str = Field(default="", validation_alias=AliasChoices("V2_WEB_JWT_SECRET", "JWT_SECRET"))
    device_token_secret: str = ""

    # --- Device tokens ---
    device_access_minutes: int = 15
    device_refresh_days: int = 90
    takeover_login_max_age_minutes: int = 10

    # --- Ingest limits (gzip body / decompressed size / object count) ---
    ingest_max_gzip_bytes: int = 5_242_880
    ingest_max_decompressed_bytes: int = 52_428_800
    ingest_max_objects: int = 500

    # --- Parity engine ---
    parity_tolerance_paise: int = 100
    quarantine_error_threshold: int = 50

    # --- Ops / storage ---
    storage_alert_bytes: int = 5_368_709_120

    # --- Rate limits ---
    login_rate_max: int = 5
    login_rate_window_s: int = 900
    device_rate_max: int = 600
    device_rate_window_s: int = 60

    # --- Lazy maintenance / purge ---
    maintenance_slice_seconds: float = 2.0
    maintenance_slice_rows: int = 5000
    purge_grace_days: int = 30

    def validate_for_serving(self) -> None:
        """Raise ``ValueError`` if this configuration cannot serve safely.

        Called from the app's lifespan startup (never at import), and only when ``database_url`` is set —
        `create_app` must stay importable with no DB configured (e.g. the health-only smoke test).
        """
        if not self.database_url:
            raise ValueError("database_url is required to serve")
        for name in ("web_jwt_secret", "device_token_secret"):
            if len(getattr(self, name)) < 32:
                raise ValueError(f"{name} must be at least 32 characters")
