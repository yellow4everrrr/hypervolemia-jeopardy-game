"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.db.session import dispose_engine
from app.interfaces.http.errors import register_error_handlers
from app.interfaces.http.middleware import RequestContextMiddleware
from app.interfaces.http.routers import (
    analytics,
    broker,
    coach,
    health,
    patterns,
    replay,
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
    yield
    await dispose_engine()
    logger.info("app.stopped")


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

    return app
