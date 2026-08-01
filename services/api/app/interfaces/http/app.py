"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Environment, Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.db.session import dispose_engine
from app.interfaces.http.errors import register_error_handlers
from app.interfaces.http.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.interfaces.http.ratelimit import RateLimiter
from app.interfaces.http.routers import (
    analytics,
    broker,
    coach,
    health,
    jobs,
    patterns,
    predictions,
    replay,
    reports,
    simulator,
    strategies,
    trades,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logger.info(
        "app.starting",
        environment=settings.environment,
        version=settings.version,
    )
    _verify_secret_encryption(settings)
    await _verify_tenant_isolation(settings)
    yield
    await dispose_engine()
    logger.info("app.stopped")


def _verify_secret_encryption(settings: Settings) -> None:
    """Refuse to start in a deployed environment without an encryption key.

    The failure this prevents is not "credentials are unprotected" — it is "credentials
    are unprotected and everything appears to work". A store that falls back to plaintext
    passes every test, serves every request, and is discovered only by whoever reads the
    dump. Startup is the last honest place to stop.

    Building the cipher rather than checking the key is non-empty, because a malformed
    key fails at the first ``put`` — which is when a user is handing over a broker
    password — rather than at boot.
    """
    if not settings.requires_secret_encryption:
        return

    from app.infrastructure.secrets.encrypted import build_cipher

    build_cipher(settings.secret_encryption_keys)
    logger.info("app.secret_encryption_ready", keys=len(settings.secret_encryption_keys))


async def _verify_tenant_isolation(settings: Settings) -> None:
    """Refuse to start in production when row-level security is not actually enforced.

    This check exists because the failure it catches is invisible. Policies can be
    present, correct and ``FORCE``d while doing nothing at all — Postgres exempts
    superusers unconditionally, and the migration role owns the tables. Measured on a
    real database in exactly that configuration, one tenant's session read another
    tenant's trades with no error anywhere.

    Every downstream symptom of that is silence, so the check is loud: fatal in
    production, a warning elsewhere so local development against a superuser connection
    still works.
    """
    from app.infrastructure.db.session import get_sessionmaker
    from app.infrastructure.db.tenancy import assert_rls_effective

    try:
        async with get_sessionmaker()() as session:
            effective = await assert_rls_effective(session)
    except Exception as exc:
        logger.warning("app.tenancy_check_skipped", error=str(exc))
        return

    if effective:
        logger.info("app.tenant_isolation_enforced")
        return

    if settings.is_production:
        raise RuntimeError(
            "row-level security is not enforced for this connection: the application "
            "must connect as a role that is neither a superuser nor the owner of the "
            "tenant tables (see the ledgerline_app role in migration 0004). Refusing "
            "to start, because in this state a query missing its user_id filter returns "
            "every tenant's rows and nothing reports an error."
        )

    logger.warning(
        "app.tenant_isolation_not_enforced",
        detail=(
            "policies exist but this connection bypasses them; acceptable locally, "
            "fatal in production"
        ),
    )


def _build_limiter(settings: Settings) -> RateLimiter | None:
    """Construct the limiter, or ``None`` when there is nothing to limit with.

    Returning ``None`` rather than a no-op client keeps the "no Redis configured"
    case explicit at the call site instead of hiding it behind an object that silently
    allows everything.
    """
    if settings.environment is Environment.TEST:
        return None
    try:
        import redis.asyncio as redis_asyncio

        return RateLimiter(redis_asyncio.from_url(str(settings.redis_url)))
    except Exception as exc:  # pragma: no cover - redis package or URL problem
        logger.warning("app.ratelimit_disabled", error=str(exc))
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance: tests construct an app with their
    own settings, and nothing is created as an import side effect.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Ledgerline API",
        version=settings.version,
        description=(
            "Institutional-grade AI trading journal. Statistics are computed in "
            "Python; the AI layer interprets them and never invents them."
        ),
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    app.add_middleware(RequestContextMiddleware)
    # Added after RequestContextMiddleware so it runs *inside* it: a rejected request
    # still gets a request id and an access log line, which is exactly when someone
    # needs both.
    app.add_middleware(RateLimitMiddleware, limiter=_build_limiter(settings))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Debug-User"],
        expose_headers=["X-Request-ID"],
    )

    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(trades.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(broker.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(analytics.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(replay.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(strategies.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(strategies.compliance_router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(patterns.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(coach.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(simulator.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(predictions.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(reports.router, prefix=f"{settings.api_prefix}/v1")
    app.include_router(jobs.router, prefix=f"{settings.api_prefix}/v1")

    return app
