"""Application error taxonomy.

Errors are raised as domain-meaningful exceptions and translated to HTTP exactly once,
at the interface boundary (``app.interfaces.http.errors``). No layer below the router
imports FastAPI or knows what a status code is.
"""

from __future__ import annotations

from typing import Any


class LedgerlineError(Exception):
    """Base class for every error this application raises deliberately."""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class DomainError(LedgerlineError):
    """A rule of the trading domain was violated (bad fill, impossible position)."""

    code = "domain_error"
    http_status = 422


class ValidationError(LedgerlineError):
    """Caller-supplied input is structurally invalid."""

    code = "validation_error"
    http_status = 400


class NotFoundError(LedgerlineError):
    """The requested resource does not exist, or is not visible to this tenant.

    Deliberately indistinguishable from "exists but belongs to someone else": a 403
    would confirm the existence of another tenant's row.
    """

    code = "not_found"
    http_status = 404


class ConflictError(LedgerlineError):
    """The request conflicts with current state (duplicate natural key, stale write)."""

    code = "conflict"
    http_status = 409


class AuthenticationError(LedgerlineError):
    code = "unauthenticated"
    http_status = 401


class AuthorizationError(LedgerlineError):
    code = "forbidden"
    http_status = 403


class RateLimitedError(LedgerlineError):
    code = "rate_limited"
    http_status = 429


class ExternalServiceError(LedgerlineError):
    """An upstream dependency (broker, market data, model provider) failed."""

    code = "external_service_error"
    http_status = 502
