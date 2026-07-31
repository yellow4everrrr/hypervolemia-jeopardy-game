"""Tests for the Tradovate REST client and shared transport helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.errors import AuthenticationError, ExternalServiceError, RateLimitedError
from app.infrastructure.brokers.base import (
    TokenBucketLimiter,
    backoff_delays,
    parse_json_decimal,
)
from app.infrastructure.brokers.tradovate.auth import AccessToken, TradovateAuthenticator
from app.infrastructure.brokers.tradovate.client import TradovateClient
from app.infrastructure.brokers.tradovate.config import TradovateEnvironment, hosts_for

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)


class StubAuthenticator(TradovateAuthenticator):
    """Hands out a fixed token, recording how often it is invalidated."""

    def __init__(self) -> None:
        self.invalidations = 0
        self._token = AccessToken(
            value="tok", expires_at=NOW + timedelta(days=1), user_id=1
        )

    async def token(self, now: datetime | None = None) -> AccessToken:
        assert self._token is not None
        return self._token

    def invalidate(self) -> None:
        self.invalidations += 1


def build(handler) -> TradovateClient:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=hosts_for(TradovateEnvironment.DEMO).rest,
    )
    # A wide-open limiter: rate limiting has its own tests and should not slow these.
    return TradovateClient(
        http, StubAuthenticator(), limiter=TokenBucketLimiter(10_000, 10_000)
    )


# --- Decimal safety -------------------------------------------------------------


def test_json_numbers_parse_as_decimal() -> None:
    """A price must never pass through a binary float, not even momentarily."""
    parsed = parse_json_decimal('{"price": 5000.25, "qty": 3}')

    assert isinstance(parsed["price"], Decimal)
    assert parsed["price"] == Decimal("5000.25")
    assert isinstance(parsed["qty"], int)


def test_repeating_decimal_price_is_exact() -> None:
    parsed = parse_json_decimal('{"price": 0.1}')
    assert parsed["price"] == Decimal("0.1")
    assert parsed["price"] * 3 == Decimal("0.3")


def test_empty_body_parses_to_none() -> None:
    assert parse_json_decimal("   ") is None


def test_malformed_body_is_an_external_error() -> None:
    with pytest.raises(ExternalServiceError, match="malformed JSON"):
        parse_json_decimal("{not json")


async def test_fill_prices_survive_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '[{"id":1,"orderId":7,"contractId":9,"timestamp":"2026-03-05T15:30:00Z",'
                '"action":"Buy","qty":2,"price":5000.25,"active":true}]'
            ),
            headers={"Content-Type": "application/json"},
        )

    fills = await build(handler).list_fills()

    assert fills[0].price == Decimal("5000.25")
    assert str(fills[0].price) == "5000.25"


# --- Requests -------------------------------------------------------------------


async def test_bearer_token_is_attached() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    await build(handler).list_accounts()
    assert seen["auth"] == "Bearer tok"


async def test_batched_lookups_deduplicate_and_sort_ids() -> None:
    """Deterministic URLs make caches and recorded fixtures useful rather than incidental."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=[])

    await build(handler).get_orders([9, 3, 9, 1, 3])

    assert len(urls) == 1
    assert "ids=1%2C3%2C9" in urls[0] or "ids=1,3,9" in urls[0]


async def test_batches_are_chunked() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[])

    await build(handler).get_contracts(list(range(250)))
    assert len(calls) == 3  # 100 + 100 + 50


async def test_a_single_object_response_is_treated_as_a_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "name": "Main", "userId": 5})

    accounts = await build(handler).list_accounts()
    assert len(accounts) == 1
    assert accounts[0].name == "Main"


async def test_null_response_is_an_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="null", headers={"Content-Type": "application/json"})

    assert await build(handler).list_fills() == []


# --- Error handling -------------------------------------------------------------


async def test_401_triggers_one_retry_with_a_fresh_token() -> None:
    """A token can be invalidated mid-flight when the session is used elsewhere."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json=[])

    client = build(handler)
    await client.list_accounts()

    assert attempts == 2


async def test_persistent_401_raises_after_one_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={})

    with pytest.raises(AuthenticationError):
        await build(handler).list_accounts()
    assert attempts == 2


@pytest.mark.parametrize("status", [423, 429])
async def test_rate_limit_statuses_raise_rate_limited(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={}, headers={"Retry-After": "30"})

    with pytest.raises(RateLimitedError) as excinfo:
        await build(handler).list_fills()
    assert excinfo.value.details["retry_after"] == "30"


async def test_error_text_in_a_200_is_still_an_error() -> None:
    """Tradovate signals business-level rejections with errorText and a 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorText": "Account not found"})

    with pytest.raises(ExternalServiceError, match="Account not found"):
        await build(handler).get_cash_balance_snapshot(1)


async def test_network_failure_becomes_an_external_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ExternalServiceError, match="request failed"):
        await build(handler).list_accounts()


async def test_unparsable_order_is_skipped_not_fatal() -> None:
    """One malformed record must not cost a trader their whole sync."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": 1, "accountId": 25, "contractId": 9},
                {"id": 2},  # missing accountId
            ],
        )

    orders = await build(handler).list_orders()
    assert [order.id for order in orders] == [1]


# --- Rate limiting --------------------------------------------------------------


async def test_limiter_allows_a_burst_then_throttles() -> None:
    limiter = TokenBucketLimiter(rate_per_second=1000, burst=3)
    for _ in range(3):
        await limiter.acquire()
    assert limiter.available < 1


async def test_limiter_refills_over_time() -> None:
    import asyncio

    limiter = TokenBucketLimiter(rate_per_second=1000, burst=2)
    await limiter.acquire()
    await limiter.acquire()
    await asyncio.sleep(0.01)
    assert limiter.available > 1


def test_limiter_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="rate_per_second must be positive"):
        TokenBucketLimiter(0, 5)
    with pytest.raises(ValueError, match="burst must be at least 1"):
        TokenBucketLimiter(1, 0)


async def test_limiter_rejects_an_impossible_request() -> None:
    limiter = TokenBucketLimiter(1, 2)
    with pytest.raises(ValueError, match="more tokens than the bucket holds"):
        await limiter.acquire(5)


# --- Backoff --------------------------------------------------------------------


def test_backoff_never_exceeds_the_ceiling() -> None:
    """The cap must survive jitter, or a "60s max" reconnect can sleep 75 seconds."""
    delays = backoff_delays(1.0, 30.0, attempts=12)
    assert all(1.0 <= delay <= 30.0 for delay in delays)


def test_backoff_grows_exponentially_without_jitter() -> None:
    delays = backoff_delays(1.0, 30.0, attempts=8, jitter=0.0)
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_backoff_is_deterministic() -> None:
    """Reproducible schedules keep reconnect tests from being flaky."""
    assert backoff_delays(1.0, 60.0, 5) == backoff_delays(1.0, 60.0, 5)


def test_backoff_jitter_spreads_reconnects() -> None:
    """Without jitter every client of an outage retries in the same instant."""
    delays = backoff_delays(2.0, 60.0, 6)
    assert any(delay % 2 != 0 for delay in delays)


# --- Configuration --------------------------------------------------------------


def test_demo_and_live_hosts_differ() -> None:
    demo = hosts_for(TradovateEnvironment.DEMO)
    live = hosts_for(TradovateEnvironment.LIVE)

    assert demo.rest != live.rest
    assert demo.rest.startswith("https://")
    assert demo.websocket.startswith("wss://")
    assert "demo" in demo.rest and "live" in live.rest
