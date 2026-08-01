"""Application settings.

Configuration is read once at import time from the environment (and, in local
development, from ``.env``). Every setting is typed; a malformed environment fails
fast at boot rather than at the first request that happens to touch it.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
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

    # --- AI coach ----------------------------------------------------------------
    #: Read from the environment like every other credential. Absent means the coach
    #: endpoints refuse rather than silently returning nothing — an unconfigured model
    #: is a deployment error, not a trader with no advice.
    anthropic_api_key: SecretStr | None = None
    ai_model: str = "claude-opus-5"
    #: Cap on analyses per user per day. The coach is the most expensive path in the
    #: product and the one a bored user will click repeatedly.
    ai_daily_analysis_limit: Annotated[int, Field(ge=1, le=1000)] = 50

    # --- Secret storage ------------------------------------------------------------
    #: Fernet keys for broker credentials at rest, **newest first**, comma-separated.
    #:
    #: A list rather than one key so rotation is a prepend: `MultiFernet` decrypts with
    #: any of them and encrypts with the first, so a new key can be added, secrets
    #: re-written, and the old key dropped with no window where a stored credential
    #: cannot be read.
    #:
    #: Generate one with::
    #:
    #:     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    #:
    #: Absent outside local and test environments is fatal at startup — see
    #: `require_secret_encryption`. Falling back to plaintext would be the one failure
    #: nobody notices.
    secret_encryption_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Observability -----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    @field_validator("cors_origins", "secret_encryption_keys", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        """Accept either a JSON list or a comma-separated string from the env.

        Both fields are annotated `NoDecode`, and that annotation is what makes this
        validator run at all. pydantic-settings treats any `list[str]` env var as a
        "complex" value and JSON-decodes it *inside the settings source*, before field
        validators execute — so `LEDGERLINE_SECRET_ENCRYPTION_KEYS=abc,def` raised a
        `SettingsError` about a malformed JSON document, which is a confusing thing to be
        told while holding a Fernet key.

        `cors_origins` carried this validator for a comma-separated form it could never
        actually receive from the environment: every caller had to pass JSON, and the
        validator only ever saw values from code. Annotating both suppresses the source's
        decoding and lets this run, so the documented form is the working one.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if text.startswith("["):
            # `NoDecode` suppresses the source's JSON parsing for *every* value, not just
            # the comma-separated ones, so the JSON form has to be handled here or it
            # stops working — which `web-ci.yml` and `docker-compose.yml` both rely on.
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"expected a JSON list or comma-separated string: {exc}") from exc
        return [item.strip() for item in text.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def requires_secret_encryption(self) -> bool:
        """Whether broker credentials must be encrypted at rest here.

        Local and test run without a key so that linking a demo account needs no
        provisioning; everywhere else it is mandatory and its absence stops the process.
        The failure has to be loud, because the alternative — a store that silently
        writes plaintext — keeps working perfectly until the day the dump leaks.
        """
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

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
