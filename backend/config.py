from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


def _also_v2(name: str) -> AliasChoices:
    """Env names a sync setting is read from: the new name, then the old ``V2_``-prefixed one (v2 merge M2).

    The new name wins when both are set. The first choice is the field's own name, so ``Settings(NAME=...)``
    keeps working as a keyword argument.
    """
    return AliasChoices(name, f"V2_{name}")


class Settings(BaseSettings):
    TALLY_HOST: str = "localhost"
    TALLY_PORT: int = 9000
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_CLASSIFIER_MODEL: str = "claude-haiku-4-5-20251001"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    SESSION_TTL_MINUTES: int = 60
    TALLY_MODE: str = "live"  # "live" or "mock"
    CODE_EXECUTION_ENABLED: bool = True  # kill switch: False reverts to analysis tools
    ANALYSIS_CONTEXT_MESSAGES: int = 8  # Number of prior session messages passed to AnalysisAgent
    CHARTS_ENABLED: bool = True  # kill switch for chart rendering
    CORS_ORIGINS: str = ""  # Comma-separated allowed origins; empty/"*" = allow all (legacy default)
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"

    # Database & Auth (Set A1)
    # Both also accept the name the separate v2 sync app used (M2); the new name wins when both are set.
    DATABASE_URL: str | None = Field(default=None, validation_alias=AliasChoices("DATABASE_URL", "V2_DATABASE_URL"))
    JWT_SECRET: str | None = Field(default=None, validation_alias=AliasChoices("JWT_SECRET", "V2_WEB_JWT_SECRET"))
    JWT_ACCESS_TOKEN_EXPIRY_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRY_DAYS: int = 7

    # File upload (Set B1)
    FILE_STORAGE_PATH: str = "./uploads"
    FILE_MAX_SIZE_MB: int = 10

    # Tally write (Set B1)
    TALLY_WRITE_ENABLED: bool = False
    TALLY_DRY_RUN: bool = False

    # Inventory line items (invoice entry Phase 2)
    DEFAULT_STOCK_GROUP: str = "AI Imported Items"  # parent group for auto-created stock items (non-reserved; created on demand)

    # FX → INR conversion (write-flow Group B, Slice A)
    FX_DEFAULT_RATES: str = ""  # per-currency defaults, e.g. "USD:83.5,EUR:90"
    FX_DEFAULT_RATE: float = 0.0  # global fallback rate (0.0 = unknown)

    # --- Sync (agent → cloud). Each also accepts its old V2_-prefixed env name (M2). ---
    # Device tokens. The secret is separate from JWT_SECRET (D6) so a device token can never pass as a web token.
    DEVICE_TOKEN_SECRET: str = Field(default="", validation_alias=_also_v2("DEVICE_TOKEN_SECRET"))
    DEVICE_ACCESS_MINUTES: int = Field(default=15, validation_alias=_also_v2("DEVICE_ACCESS_MINUTES"))
    DEVICE_REFRESH_DAYS: int = Field(default=90, validation_alias=_also_v2("DEVICE_REFRESH_DAYS"))
    TAKEOVER_LOGIN_MAX_AGE_MINUTES: int = Field(default=10, validation_alias=_also_v2("TAKEOVER_LOGIN_MAX_AGE_MINUTES"))
    # Ingest limits: gzip body / decompressed size / object count.
    INGEST_MAX_GZIP_BYTES: int = Field(default=5_242_880, validation_alias=_also_v2("INGEST_MAX_GZIP_BYTES"))
    INGEST_MAX_DECOMPRESSED_BYTES: int = Field(
        default=52_428_800, validation_alias=_also_v2("INGEST_MAX_DECOMPRESSED_BYTES")
    )
    INGEST_MAX_OBJECTS: int = Field(default=500, validation_alias=_also_v2("INGEST_MAX_OBJECTS"))
    # Parity engine.
    PARITY_TOLERANCE_PAISE: int = Field(default=100, validation_alias=_also_v2("PARITY_TOLERANCE_PAISE"))
    QUARANTINE_ERROR_THRESHOLD: int = Field(default=50, validation_alias=_also_v2("QUARANTINE_ERROR_THRESHOLD"))
    # Ops / storage.
    STORAGE_ALERT_BYTES: int = Field(default=5_368_709_120, validation_alias=_also_v2("STORAGE_ALERT_BYTES"))
    # Rate limits.
    LOGIN_RATE_MAX: int = Field(default=5, validation_alias=_also_v2("LOGIN_RATE_MAX"))
    LOGIN_RATE_WINDOW_S: int = Field(default=900, validation_alias=_also_v2("LOGIN_RATE_WINDOW_S"))
    DEVICE_RATE_MAX: int = Field(default=600, validation_alias=_also_v2("DEVICE_RATE_MAX"))
    DEVICE_RATE_WINDOW_S: int = Field(default=60, validation_alias=_also_v2("DEVICE_RATE_WINDOW_S"))
    # Lazy maintenance / purge.
    MAINTENANCE_SLICE_SECONDS: float = Field(default=2.0, validation_alias=_also_v2("MAINTENANCE_SLICE_SECONDS"))
    MAINTENANCE_SLICE_ROWS: int = Field(default=5000, validation_alias=_also_v2("MAINTENANCE_SLICE_ROWS"))
    # The per-table count(*) walk behind the storage estimate must not run on every heartbeat (D20).
    STORAGE_ESTIMATE_INTERVAL_SECONDS: int = Field(
        default=3600, validation_alias=_also_v2("STORAGE_ESTIMATE_INTERVAL_SECONDS")
    )
    PURGE_GRACE_DAYS: int = Field(default=30, validation_alias=_also_v2("PURGE_GRACE_DAYS"))

    def validate_for_serving(self) -> None:
        """Raise ``ValueError`` if this configuration cannot serve the sync routes safely.

        Called from an app's lifespan startup, never at import, so the app stays importable with no database
        configured.
        """
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required to serve")
        for name in ("JWT_SECRET", "DEVICE_TOKEN_SECRET"):
            if len(getattr(self, name) or "") < 32:
                raise ValueError(f"{name} must be at least 32 characters")
        if self.JWT_SECRET == self.DEVICE_TOKEN_SECRET:
            # D6: device tokens are signed with a secret separate from the web JWT secret, so a device token can
            # never pass as a web token (or the reverse) even if the `typ`/`type` check were ever bypassed.
            raise ValueError("DEVICE_TOKEN_SECRET must differ from JWT_SECRET (D6)")

    @property
    def db_mode(self) -> bool:
        """True when DATABASE_URL is set — enables auth + persistence."""
        return self.DATABASE_URL is not None

    @property
    def TALLY_URL(self) -> str:
        return f"http://{self.TALLY_HOST}:{self.TALLY_PORT}"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
