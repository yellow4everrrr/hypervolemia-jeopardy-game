"""Structured logging.

Logs are JSON in every deployed environment so that request id, user id and trade id
are queryable fields rather than substrings. Local development renders the same event
stream in colour for readability.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.config import Settings

__all__ = ["bind_contextvars", "clear_contextvars", "configure_logging", "get_logger"]


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route stdlib logging through it."""
    level = getattr(logging, settings.log_level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # structlog events are handed to stdlib logging *unrendered*, and rendering happens
    # exactly once, in the handler's formatter. Ending this chain with the renderer
    # instead would render here and again in the formatter, emitting each line nested
    # inside a second copy of itself.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Records from third-party libraries (uvicorn, SQLAlchemy) never passed
            # through the chain above, so they get the shared processors here.
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn duplicates access logs that our middleware already emits with more
    # context (request id, duration, tenant), so silence its own access logger.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.database_echo else logging.WARNING
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
