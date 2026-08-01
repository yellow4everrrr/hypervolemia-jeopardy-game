"""Shared broker-integration machinery: rate limiting and Decimal-safe JSON.

Broker-agnostic on purpose. A second broker in a later milestone reuses this and
supplies only its own auth, endpoints and mapping.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import httpx

from app.core.errors import ExternalServiceError


class TokenBucketLimiter:
    """Async token bucket.

    Client-side rate limiting is not politeness, it is self-defence: Tradovate answers
    overuse with a timed penalty that blocks the *next* request too, so a burst that
    trips the limit costs more than the requests it saved. Staying under the limit is
    strictly cheaper than recovering from it.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available, then consume them."""
        if tokens > self._capacity:
            raise ValueError("cannot acquire more tokens than the bucket holds")

        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated_at) * self._rate
                )
                self._updated_at = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_for = deficit / self._rate
            await asyncio.sleep(wait_for)

    @property
    def available(self) -> float:
        """Tokens available right now, without consuming any. For tests and metrics."""
        elapsed = time.monotonic() - self._updated_at
        return min(self._capacity, self._tokens + elapsed * self._rate)


def parse_json_decimal(text: str) -> Any:
    """Parse JSON with numbers as ``Decimal``.

    Broker payloads carry prices as JSON doubles. ``httpx``'s ``.json()`` would turn
    ``5000.25`` into a binary float, and every downstream ``Decimal`` conversion would
    then be preserving an error that was introduced here — at the first hop, before
    any of the system's careful decimal arithmetic gets a chance to matter.

    Parsing the response *text* ourselves is the only place this can be prevented.
    """
    if not text.strip():
        return None
    try:
        return json.loads(text, parse_float=Decimal, parse_int=int)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError(
            "broker returned malformed JSON", details={"body": text[:256]}
        ) from exc


def response_json(response: httpx.Response) -> Any:
    """Decimal-safe replacement for ``response.json()``."""
    return parse_json_decimal(response.text)


def backoff_delays(
    base: float, maximum: float, attempts: int, jitter: float = 0.25
) -> list[float]:
    """Exponential backoff schedule with deterministic proportional jitter.

    Jitter is derived from the attempt number rather than a random source so that the
    schedule is reproducible in tests. Spreading reconnects matters because every
    client of a broker outage otherwise retries in the same instant and the recovering
    service is knocked over again by its own clients.

    Because jitter perturbs each delay in both directions, the sequence is not strictly
    increasing near the cap — that is the point of jitter. Every value is clamped into
    ``[base, maximum]`` *after* jitter is applied, so ``maximum`` is a real ceiling
    rather than a nominal one.
    """
    delays: list[float] = []
    for attempt in range(attempts):
        nominal = min(maximum, base * (2**attempt))
        offset = jitter * nominal * (1 if attempt % 2 else -1) * 0.5
        delays.append(round(min(maximum, max(base, nominal + offset)), 3))
    return delays
