"""Error translation — the single place where domain errors become HTTP responses.

Every error the API emits has the same shape::

    {"error": {"code": "...", "message": "...", "details": {...}}}

A stable envelope means the frontend writes one error handler, not one per endpoint.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import LedgerlineError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _envelope(code: str, message: str, details: dict | None = None) -> dict:  # type: ignore[type-arg]
    return {"error": {"code": code, "message": message, "details": details or {}}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerlineError)
    async def handle_ledgerline_error(_: Request, exc: LedgerlineError) -> JSONResponse:
        logger.warning("request.domain_error", code=exc.code, message=exc.message)
        return JSONResponse(status_code=exc.http_status, content=_envelope(**exc.to_payload()))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "validation_error",
                "Request payload failed validation",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Log the traceback, return nothing internal: stack traces and driver messages
        # leak schema details and are useless to the caller anyway.
        logger.exception("request.unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "An unexpected error occurred"),
        )
