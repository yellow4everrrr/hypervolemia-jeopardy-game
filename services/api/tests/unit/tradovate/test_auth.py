"""Tests for the Tradovate access-token lifecycle.

Run against ``httpx.MockTransport``, so the request/response contract is exercised end
to end — headers, body, retry behaviour — with no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import AuthenticationError, ExternalServiceError, RateLimitedError
from app.infrastructure.brokers.tradovate.auth import (
    AccessToken,
    TradovateAuthenticator,
    TradovateCredentials,
)

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)

CREDENTIALS = TradovateCredentials(
    username="trader",
    password="secret",
    app_id="Ledgerline",
    app_version="1.0",
    cid="1234",
    secret="api-secret",
)


def token_payload(expires_in_hours: int = 24, **overrides: Any) -> dict[str, Any]:
    payload = {
        "accessToken": "tok-abc",
        "mdAccessToken": "md-abc",
        "expirationTime": (NOW + timedelta(hours=expires_in_hours)).isoformat(),
        "userId": 4242,
        "name": "trader",
        "hasLive": True,
        "userStatus": "Active",
    }
    payload.update(overrides)
    return payload


def build(handler) -> tuple[TradovateAuthenticator, list[float]]:  # type: ignore[no-untyped-def]
    """Authenticator wired to a mock transport, plus a record of any sleeps."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://demo.tradovateapi.com/v1"
    )
    return TradovateAuthenticator(client, CREDENTIALS, sleep=fake_sleep), slept


# --- Acquisition ----------------------------------------------------------------


async def test_acquires_a_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/accesstokenrequest")
        body = json.loads(request.content)
        assert body["name"] == "trader"
        assert body["cid"] == "1234"
        assert body["sec"] == "api-secret"
        return httpx.Response(200, json=token_payload())

    auth, _ = build(handler)
    token = await auth.token(now=NOW)

    assert token.value == "tok-abc"
    assert token.market_data_token == "md-abc"
    assert token.user_id == 4242
    assert token.has_live


async def test_token_is_cached_between_calls() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=token_payload())

    auth, _ = build(handler)
    await auth.token(now=NOW)
    await auth.token(now=NOW)

    assert calls == 1


async def test_device_id_is_sent_when_configured() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=token_payload())

    credentials = TradovateCredentials(
        username="t", password="p", app_id="a", app_version="1", cid="1", secret="s",
        device_id="device-9",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://demo.tradovateapi.com/v1"
    )
    await TradovateAuthenticator(client, credentials).token(now=NOW)

    assert seen["deviceId"] == "device-9"


# --- Failure modes --------------------------------------------------------------


async def test_http_200_with_error_text_is_a_failure() -> None:
    """Tradovate rejects logins with a 200 and an errorText.

    Treating the status code as the verdict would leave the client holding no token and
    failing much later, somewhere far less informative.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errorText": "Invalid username or password", "userStatus": "Active"}
        )

    auth, _ = build(handler)
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        await auth.token(now=NOW)


async def test_401_is_an_authentication_error() -> None:
    auth, _ = build(lambda request: httpx.Response(401, json={}))
    with pytest.raises(AuthenticationError):
        await auth.token(now=NOW)


async def test_missing_expiration_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = token_payload()
        del payload["expirationTime"]
        return httpx.Response(200, json=payload)

    auth, _ = build(handler)
    with pytest.raises(ExternalServiceError, match="missing required fields"):
        await auth.token(now=NOW)


# --- Time penalties -------------------------------------------------------------


async def test_short_penalty_is_waited_out_and_retried_with_the_ticket() -> None:
    """The penalty response means the request was *not* handled.

    Retrying without the ticket earns another penalty, so the ticket must be echoed.
    """
    attempts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append(body)
        if len(attempts) == 1:
            return httpx.Response(200, json={"p-ticket": "tkt-1", "p-time": 5})
        return httpx.Response(200, json=token_payload())

    auth, slept = build(handler)
    token = await auth.token(now=NOW)

    assert token.value == "tok-abc"
    assert slept == [5]
    assert "p-ticket" not in attempts[0]
    assert attempts[1]["p-ticket"] == "tkt-1"


async def test_long_penalty_is_surfaced_rather_than_waited_out() -> None:
    """A worker blocked for minutes is worse than a sync that reports back and retries."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"p-ticket": "tkt", "p-time": 600})

    auth, slept = build(handler)
    with pytest.raises(RateLimitedError, match="600s authentication penalty"):
        await auth.token(now=NOW)
    assert slept == []


async def test_captcha_penalty_is_not_retried() -> None:
    """A third-party application cannot solve it; retrying only extends the lockout."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"p-ticket": "t", "p-time": 5, "p-captcha": True})

    auth, slept = build(handler)
    with pytest.raises(RateLimitedError, match="captcha"):
        await auth.token(now=NOW)
    assert slept == []


async def test_second_consecutive_penalty_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"p-ticket": "t", "p-time": 2})

    auth, slept = build(handler)
    with pytest.raises(RateLimitedError, match="second authentication penalty"):
        await auth.token(now=NOW)
    assert slept == [2]


# --- Renewal --------------------------------------------------------------------


def test_renewal_is_due_before_expiry_not_after() -> None:
    """Renewing early leaves room to retry while the current token still works."""
    token = AccessToken(value="t", expires_at=NOW + timedelta(hours=1), user_id=1)

    assert not token.needs_renewal(NOW)
    assert token.needs_renewal(NOW + timedelta(minutes=51))
    assert not token.is_expired(NOW + timedelta(minutes=59))
    assert token.is_expired(NOW + timedelta(hours=1))


async def test_renewal_preserves_the_market_data_token() -> None:
    """Renewal responses omit it, and dropping it would break market-data access."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("renewaccesstoken"):
            payload = token_payload(expires_in_hours=48)
            del payload["mdAccessToken"]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=token_payload(expires_in_hours=1))

    auth, _ = build(handler)
    first = await auth.token(now=NOW)
    assert first.market_data_token == "md-abc"

    renewed = await auth.token(now=NOW + timedelta(minutes=55))

    assert any("renewaccesstoken" in path for path in calls)
    assert renewed.market_data_token == "md-abc"


async def test_failed_renewal_keeps_the_still_valid_token() -> None:
    """A renewal blip is not yet a failed sync — the current token still works."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("renewaccesstoken"):
            return httpx.Response(500, text="upstream error")
        return httpx.Response(200, json=token_payload(expires_in_hours=1))

    auth, _ = build(handler)
    await auth.token(now=NOW)
    token = await auth.token(now=NOW + timedelta(minutes=55))

    assert token.value == "tok-abc"


async def test_expired_token_is_replaced_not_renewed() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=token_payload(expires_in_hours=1))

    auth, _ = build(handler)
    await auth.token(now=NOW)
    await auth.token(now=NOW + timedelta(hours=2))

    assert all("accesstokenrequest" in path for path in paths)
    assert len(paths) == 2


async def test_invalidate_forces_a_fresh_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=token_payload())

    auth, _ = build(handler)
    await auth.token(now=NOW)
    auth.invalidate()
    await auth.token(now=NOW)

    assert calls == 2


def test_credentials_repr_hides_the_secret() -> None:
    """Credentials end up in tracebacks and log lines; the secret must not."""
    rendered = repr(CREDENTIALS)
    assert "secret" not in rendered.replace("api_secret", "").replace("TradovateCredentials", "")
    assert "api-secret" not in rendered
    assert "trader" in rendered
