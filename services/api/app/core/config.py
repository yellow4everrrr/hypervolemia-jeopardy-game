"""Application settings.

Configuration is read once at import time from the environment (and, in local
development, from ``.env``). Every setting is typed; a malformed environment fails
fast at boot rather than at the first request that happens to touch it.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEDGERLINE_",
        extra="ignore",
        frozen=True,
    )

    # --- Runtime -----------------------------------------------------------------
    environment: Environment = Environment.LOCAL
    service_name: str = "ledgerline-api"
    version: str = "0.1.0"
    debug: bool = False

    # --- HTTP --------------------------------------------------------------------
    api_prefix: str = "/api"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    request_timeout_seconds: float = 30.0

    # --- Persistence -------------------------------------------------------------
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://ledgerline:ledgerline@localhost:5432/ledgerline")
    )
    database_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    database_max_overflow: Annotated[int, Field(ge=0, le=100)] = 20
    database_pool_recycle_seconds: int = 1800
    database_echo: bool = False

    redis_url: RedisDsn = Field(default=RedisDsn("redis://localhost:6379/0"))

    # --- Authentication (Clerk) --------------------------------------------------
    clerk_jwks_url: str | None = None
    clerk_issuer: str | None = None
    clerk_audience: str | None = None
    clerk_jwks_cache_seconds: int = 3600
    # Local-only escape hatch: trust an ``X-Debug-User`` header instead of a JWT.
    auth_dev_bypass: bool = False

    # --- Object storage ----------------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "ledgerline-media"
    s3_region: str = "us-east-1"

    # --- Observability -----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept either a JSON list or a comma-separated string from the env."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def sync_database_url(self) -> str:
        """Driver-less URL for tools (Alembic) that use the sync psycopg/asyncpg split."""
        return str(self.database_url).replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules never re-parses the environment. Tests that need
    different settings call ``get_settings.cache_clear()``.
    """
    settings = Settings()
    if settings.is_production and settings.auth_dev_bypass:
        raise ValueError("auth_dev_bypass must never be enabled in production")
    return settings
