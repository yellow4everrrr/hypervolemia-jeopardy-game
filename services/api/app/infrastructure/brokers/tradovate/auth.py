"""Tradovate access-token lifecycle.

Three behaviours make this more than a POST:

**Success is not the status code.** ``auth/accesstokenrequest`` can answer HTTP 200
with an ``errorText`` and no token. Treating 200 as success would leave the client
holding ``None`` and failing much later, somewhere less informative.

**Rate limiting is a timed penalty, not a 429.** Too many auth attempts return
``p-ticket``/``p-time``: the request was not handled, and it may be retried after
``p-time`` seconds *with the ticket echoed in the body*. When ``p-captcha`` is set,
a third-party application cannot recover at all and the user must wait — roughly an
hour, per Tradovate's documentation.

**Tokens expire and renewal is cheap.** We renew ahead of expiry rather than on
failure, so a transient renewal error has time to be retried while the current token
still works.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.core.errors import AuthenticationError, ExternalServiceError, RateLimitedError
from app.core.logging import get_logger
from app.infrastructure.brokers.base import response_json
from app.infrastructure.brokers.tradovate.config import (
    TOKEN_RENEWAL_MARGIN_SECONDS,
    Endpoint,
)
from app.infrastructure.brokers.tradovate.models import AccessTokenResponse, TimePenalty

logger = get_logger(__name__)

#: Auth penalties are waited out in-process only when short. Anything longer is
#: surfaced to the caller so the sync scheduler can back off instead of holding a
#: worker hostage.
MAX_INLINE_PENALTY_WAIT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class TradovateCredentials:
    """What Tradovate needs to mint a token.

    Loaded from a secret store at use time and never persisted by this application:
    ``broker_connections.credential_ref`` stores a pointer, not these values.
    """

    username: str
    password: str
    app_id: str
    app_version: str
    #: Tradovate API key pair: numeric client id and secret.
    cid: str
    secret: str
    device_id: str | None = None

    def to_payload(self, penalty_ticket: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.username,
            "password": self.password,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "cid": self.cid,
            "sec": self.secret,
        }
        if self.device_id:
            payload["deviceId"] = self.device_id
        if penalty_ticket:
            payload["p-ticket"] = penalty_ticket
        return payload

    def __repr__(self) -> str:  # pragma: no cover — keeps secrets out of tracebacks
        return f"TradovateCredentials(username={self.username!r}, app_id={self.app_id!r}, ...)"


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime
    user_id: int
    market_data_token: str | None = None
    name: str | None = None
    has_live: bool = False

    def needs_renewal(
        self, now: datetime, margin_seconds: int = TOKEN_RENEWAL_MARGIN_SECONDS
    ) -> bool:
        return now >= self.expires_at - timedelta(seconds=margin_seconds)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class TradovateAuthenticator:
    """Acquires and renews access tokens, serialising concurrent requests.

    A lock guards token acquisition so that ten concurrent syncs waking to an expired
    token produce one auth request rather than ten — which would itself trip the
    penalty this class exists to avoid.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: TradovateCredentials,
        *,
        # Injected so that penalty handling can be tested without really waiting.
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self._sleep = sleep

    @property
    def current_token(self) -> AccessToken | None:
        return self._token

    async def token(self, now: datetime | None = None) -> AccessToken:
        """Return a valid token, acquiring or renewing it if needed."""
        moment = now or datetime.now(UTC)

        async with self._lock:
            if self._token is None:
                self._token = await self._request_token()
                return self._token

            if self._token.is_expired(moment):
                self._token = await self._request_token()
                return self._token

            if self._token.needs_renewal(moment):
                try:
                    self._token = await self._renew(self._token)
                except (ExternalServiceError, AuthenticationError) as exc:
                    # The current token is still valid; a failed renewal is not yet a
                    # failed sync. Log and carry on — the next call retries.
                    logger.warning("tradovate.renew_failed", error=str(exc))
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token, forcing a fresh request. Called on a 401."""
        self._token = None

    # --- internals -------------------------------------------------------------

    async def _request_token(self, penalty_ticket: str | None = None) -> AccessToken:
        response = await self._client.post(
            Endpoint.ACCESS_TOKEN.value,
            json=self._credentials.to_payload(penalty_ticket),
        )
        return await self._handle_token_response(response, renewing=False)

    async def _renew(self, current: AccessToken) -> AccessToken:
        response = await self._client.get(
            Endpoint.RENEW_TOKEN.value,
            headers={"Authorization": f"Bearer {current.value}"},
        )
        renewed = await self._handle_token_response(response, renewing=True)
        # Renewal responses omit the market-data token; keep the one we already hold.
        if renewed.market_data_token is None and current.market_data_token:
            return AccessToken(
                value=renewed.value,
                expires_at=renewed.expires_at,
                user_id=renewed.user_id or current.user_id,
                market_data_token=current.market_data_token,
                name=renewed.name or current.name,
                has_live=renewed.has_live or current.has_live,
            )
        return renewed

    async def _handle_token_response(
        self, response: httpx.Response, *, renewing: bool
    ) -> AccessToken:
        payload = response_json(response)

        if isinstance(payload, dict) and "p-ticket" in payload:
            return await self._handle_penalty(TimePenalty.model_validate(payload), renewing)

        if response.status_code == 401:
            raise AuthenticationError("Tradovate rejected the supplied credentials")
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"Tradovate auth failed with HTTP {response.status_code}",
                details={"status": response.status_code},
            )
        if not isinstance(payload, dict):
            raise ExternalServiceError("Tradovate auth returned an unexpected payload")

        parsed = AccessTokenResponse.model_validate(payload)
        if not parsed.succeeded:
            # HTTP 200 with errorText is Tradovate's way of rejecting a login.
            raise AuthenticationError(
                parsed.error_text or "Tradovate returned no access token",
                details={"user_status": parsed.user_status},
            )
        if parsed.expiration_time is None or parsed.user_id is None:
            raise ExternalServiceError("Tradovate token response is missing required fields")

        assert parsed.access_token is not None
        logger.info(
            "tradovate.token_acquired",
            renewing=renewing,
            user_id=parsed.user_id,
            expires_at=parsed.expiration_time.isoformat(),
        )
        return AccessToken(
            value=parsed.access_token,
            expires_at=parsed.expiration_time,
            user_id=parsed.user_id,
            market_data_token=parsed.md_access_token,
            name=parsed.name,
            has_live=bool(parsed.has_live),
        )

    async def _handle_penalty(self, penalty: TimePenalty, renewing: bool) -> AccessToken:
        if penalty.captcha:
            # Nothing a third-party application can do; retrying makes it worse.
            raise RateLimitedError(
                "Tradovate requires a captcha for this account; sign in via the Trader "
                "application and retry in about an hour",
                details={"p_time": penalty.time_seconds, "captcha": True},
            )
        if penalty.time_seconds > MAX_INLINE_PENALTY_WAIT_SECONDS:
            raise RateLimitedError(
                f"Tradovate imposed a {penalty.time_seconds}s authentication penalty",
                details={"p_time": penalty.time_seconds, "retry_after": penalty.time_seconds},
            )

        logger.warning(
            "tradovate.auth_penalty", wait_seconds=penalty.time_seconds, renewing=renewing
        )
        await self._sleep(penalty.time_seconds)
        # Retry once, echoing the ticket. A second penalty is not waited out.
        response = await self._client.post(
            Endpoint.ACCESS_TOKEN.value,
            json=self._credentials.to_payload(penalty.ticket),
        )
        payload = response_json(response)
        if isinstance(payload, dict) and "p-ticket" in payload:
            second = TimePenalty.model_validate(payload)
            raise RateLimitedError(
                "Tradovate imposed a second authentication penalty",
                details={"p_time": second.time_seconds},
            )
        return await self._handle_token_response(response, renewing=renewing)
