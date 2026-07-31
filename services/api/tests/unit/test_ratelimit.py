"""Rate limiting: bucket selection, identity keying, and the fail-open decision."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.interfaces.http.middleware import _identity
from app.interfaces.http.ratelimit import (
    EXPENSIVE_BURST,
    EXPENSIVE_PER_MINUTE,
    STANDARD_BURST,
    RateLimiter,
    is_expensive,
)


class FakeScript:
    """Stands in for a registered Lua script, tracking what it was asked."""

    def __init__(self, store: dict[str, float]) -> None:
        self.store = store
        self.calls: list[tuple[str, float, int]] = []

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        key = keys[0]
        rate, burst, _now, cost = float(args[0]), int(args[1]), float(args[2]), int(args[3])
        self.calls.append((key, rate, burst))

        tokens = self.store.get(key, float(burst))
        if tokens >= cost:
            self.store[key] = tokens - cost
            return [1, int(self.store[key]), 0]
        return [0, int(tokens), 5]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, float] = {}
        self.script = FakeScript(self.store)

    def register_script(self, _source: str) -> FakeScript:
        return self.script


class BrokenRedis:
    def register_script(self, _source: str) -> Any:
        raise ConnectionError("redis is unreachable")


class TestBucketSelection:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/patterns/scan",
            "/api/v1/predictions/train",
            "/api/v1/simulator/sweep",
            "/api/v1/reports/run-schedule",
            "/api/v1/coach/analyse",
        ],
    )
    def test_the_costly_routes_are_classified_expensive(self, path: str) -> None:
        assert is_expensive(path)

    @pytest.mark.parametrize(
        "path", ["/api/v1/trades", "/api/v1/analytics/summary", "/api/v1/jobs"]
    )
    def test_ordinary_routes_are_not(self, path: str) -> None:
        assert not is_expensive(path)

    @pytest.mark.asyncio
    async def test_an_expensive_request_consumes_from_both_buckets(self) -> None:
        """The expensive limit is a sub-limit, not an alternative one.

        If it consumed only its own bucket, a client could spend its entire standard
        allowance on scans and the tighter limit would bound nothing.
        """
        redis = FakeRedis()
        decision = await RateLimiter(redis).check(
            identity="u1", path="/api/v1/predictions/train"
        )

        keys = [call[0] for call in redis.script.calls]
        assert decision.allowed
        assert keys == ["rl:std:u1", "rl:exp:u1"]

    @pytest.mark.asyncio
    async def test_an_ordinary_request_touches_only_the_standard_bucket(self) -> None:
        redis = FakeRedis()
        await RateLimiter(redis).check(identity="u1", path="/api/v1/trades")

        assert [call[0] for call in redis.script.calls] == ["rl:std:u1"]

    @pytest.mark.asyncio
    async def test_the_expensive_bucket_is_much_tighter(self) -> None:
        redis = FakeRedis()
        await RateLimiter(redis).check(identity="u1", path="/api/v1/patterns/scan")

        bursts = {call[0]: call[2] for call in redis.script.calls}
        assert bursts["rl:std:u1"] == STANDARD_BURST
        assert bursts["rl:exp:u1"] == EXPENSIVE_BURST
        assert EXPENSIVE_PER_MINUTE < STANDARD_BURST


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_a_client_is_rejected_once_the_expensive_bucket_empties(self) -> None:
        limiter = RateLimiter(FakeRedis())
        path = "/api/v1/patterns/scan"

        allowed = 0
        for _ in range(EXPENSIVE_BURST + 5):
            if (await limiter.check(identity="u1", path=path)).allowed:
                allowed += 1

        assert allowed == EXPENSIVE_BURST

    @pytest.mark.asyncio
    async def test_a_rejection_names_the_bucket_and_a_retry_delay(self) -> None:
        limiter = RateLimiter(FakeRedis())
        path = "/api/v1/patterns/scan"
        for _ in range(EXPENSIVE_BURST):
            await limiter.check(identity="u1", path=path)

        decision = await limiter.check(identity="u1", path=path)

        assert not decision.allowed
        assert decision.bucket == "expensive"
        assert decision.headers()["Retry-After"]

    @pytest.mark.asyncio
    async def test_one_client_exhausting_its_bucket_does_not_affect_another(self) -> None:
        limiter = RateLimiter(FakeRedis())
        path = "/api/v1/patterns/scan"
        for _ in range(EXPENSIVE_BURST + 2):
            await limiter.check(identity="noisy", path=path)

        assert (await limiter.check(identity="quiet", path=path)).allowed


class TestFailureMode:
    @pytest.mark.asyncio
    async def test_it_fails_open_when_redis_is_unreachable(self) -> None:
        """A rate limiter protects against load; it is not authorisation.

        Turning a Redis outage into an API outage trades a degradation for a failure,
        which is the wrong direction — and worth a test, because "fail closed" is the
        right default for most middleware and the wrong one here.
        """
        decision = await RateLimiter(BrokenRedis()).check(
            identity="u1", path="/api/v1/patterns/scan"
        )

        assert decision.allowed
        assert decision.bucket == "unavailable"

    @pytest.mark.asyncio
    async def test_no_redis_configured_allows_everything(self) -> None:
        decision = await RateLimiter(None).check(identity="u1", path="/api/v1/trades")

        assert decision.allowed


class TestIdentity:
    def _request(self, headers: dict[str, str], host: str | None = "1.2.3.4") -> Any:
        class FakeClient:
            def __init__(self, address: str) -> None:
                self.host = address

        class FakeRequest:
            def __init__(self) -> None:
                self.headers = headers
                self.client = FakeClient(host) if host else None

        return FakeRequest()

    def test_the_bearer_token_is_hashed_never_stored_raw(self) -> None:
        """The identity becomes a Redis key and can reach a log line. A live credential
        in either is how a bearer token leaks."""
        token = "super-secret-jwt-value"
        identity = _identity(self._request({"authorization": f"Bearer {token}"}))

        assert token not in identity
        assert identity.startswith("t:")
        assert identity[2:] == hashlib.sha256(token.encode()).hexdigest()[:32]

    def test_the_same_token_always_produces_the_same_bucket(self) -> None:
        request = self._request({"authorization": "Bearer abc"})
        assert _identity(request) == _identity(request)

    def test_different_tokens_get_different_buckets(self) -> None:
        first = _identity(self._request({"authorization": "Bearer aaa"}))
        second = _identity(self._request({"authorization": "Bearer bbb"}))
        assert first != second

    def test_it_falls_back_to_the_address_when_unauthenticated(self) -> None:
        assert _identity(self._request({})) == "ip:1.2.3.4"

    def test_a_missing_client_does_not_crash(self) -> None:
        """ASGI does not guarantee a client address."""
        assert _identity(self._request({}, host=None)) == "ip:unknown"
