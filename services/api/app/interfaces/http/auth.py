"""Clerk JWT verification.

Clerk issues RS256-signed session tokens. Verification is local: fetch the issuer's
JWKS once, cache it, and validate signature, issuer, audience and expiry on every
request. No network call per request, no shared secret, and a compromised API server
cannot mint tokens.

Key rotation is handled by re-fetching the JWKS when a token presents an unknown
``kid`` — bounded by the cache TTL so a malformed token cannot be used to hammer
Clerk's endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """The verified caller.

    ``clerk_user_id`` is the subject claim; the local ``users`` row is resolved from it
    on first use. Nothing downstream trusts a user id supplied in a request body.
    """

    clerk_user_id: str
    email: str | None = None
    claims: dict[str, Any] | None = None


class ClerkTokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwk_client: PyJWKClient | None = None
        self._jwk_fetched_at: float = 0.0

    def _client(self) -> PyJWKClient:
        if not self._settings.clerk_jwks_url:
            raise AuthenticationError("authentication is not configured")

        age = time.monotonic() - self._jwk_fetched_at
        expired = age > self._settings.clerk_jwks_cache_seconds
        if self._jwk_client is None or expired:
            self._jwk_client = PyJWKClient(
                self._settings.clerk_jwks_url,
                cache_keys=True,
                lifespan=self._settings.clerk_jwks_cache_seconds,
            )
            self._jwk_fetched_at = time.monotonic()
        return self._jwk_client

    def verify(self, token: str) -> AuthenticatedPrincipal:
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._settings.clerk_issuer,
                audience=self._settings.clerk_audience,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "require": ["exp", "sub"],
                    # Clerk omits `aud` unless a template configures it; only enforce
                    # the claim when this deployment actually expects one.
                    "verify_aud": self._settings.clerk_audience is not None,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("session token has expired") from exc
        except (jwt.InvalidTokenError, httpx.HTTPError) as exc:
            logger.warning("auth.token_rejected", error=str(exc))
            raise AuthenticationError("invalid session token") from exc

        subject = claims.get("sub")
        if not subject:
            raise AuthenticationError("token is missing a subject claim")

        return AuthenticatedPrincipal(
            clerk_user_id=str(subject),
            email=claims.get("email"),
            claims=claims,
        )
