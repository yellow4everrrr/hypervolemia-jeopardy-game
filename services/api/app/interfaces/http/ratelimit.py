"""Rate limiting, with a hard distinction between cheap and expensive routes.

A single global limit is the usual implementation and it is the wrong shape here, because
this API's costs differ by three orders of magnitude. Reading a trade is a millisecond.
A pattern scan is seconds of permutation testing, and a report run can be a hundred of
them. A limit generous enough for the blotter lets one client saturate every worker; a
limit tight enough to protect the scans makes the blotter unusable.

So there are two buckets, and expensive routes consume from both.

**Failure is open, deliberately.** If Redis is unreachable the limiter allows the request
and logs. A rate limiter is a protection against load, not a security control — nothing
here is authorisation — and turning a Redis outage into a total outage trades a
degradation for a failure. That is the wrong trade, and it is worth stating explicitly
because "fail closed" is the right default for most middleware and the wrong one for this.

**The token bucket is a Lua script.** Read-modify-write from the client is a race: two
requests both read 5 tokens remaining and both proceed. Redis runs the script atomically,
which is the only version that holds under concurrency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Refill-rate and burst per bucket. The standard bucket is sized for a dashboard doing
#: a dozen calls per view; the expensive one for a human clicking "scan" a few times an
#: hour, because that is the actual usage and anything more is a mistake or a loop.
STANDARD_PER_MINUTE = 120
STANDARD_BURST = 240

EXPENSIVE_PER_MINUTE = 6
EXPENSIVE_BURST = 10

#: Routes that cost seconds. Matched by prefix against the path, after the API prefix.
EXPENSIVE_PATHS: tuple[str, ...] = (
    "/patterns/scan",
    "/predictions/train",
    "/simulator/sweep",
    "/reports/generate",
    "/reports/run-schedule",
    "/coach/",
)

#: Atomic token bucket. KEYS[1] is the bucket, ARGV is (rate_per_second, burst, now, cost).
#: Returns {allowed, tokens_remaining, retry_after_seconds}.
BUCKET_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])

if tokens == nil then
  tokens = burst
  ts = now
end

-- Refill for the elapsed time, capped at the burst size.
local elapsed = math.max(0, now - ts)
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
local retry = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry = (cost - tokens) / rate
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
-- Expire well after a full refill so idle buckets do not accumulate forever.
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)

return {allowed, math.floor(tokens), math.ceil(retry)}
"""


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int
    bucket: str

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Bucket": self.bucket,
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after))
        return headers


def is_expensive(path: str) -> bool:
    return any(marker in path for marker in EXPENSIVE_PATHS)


class RateLimiter:
    """Token buckets in Redis, keyed per user.

    Keyed on the authenticated user rather than the IP address. IP keying punishes
    everyone behind one NAT and is trivially evaded by anyone it would catch; a user id
    is the thing whose consumption actually needs bounding.
    """

    def __init__(self, redis: Any | None) -> None:
        self._redis = redis
        self._script: Any | None = None

    async def _run(self, key: str, rate: float, burst: int, cost: int) -> tuple[int, int, int]:
        if self._redis is None:
            raise RuntimeError("no redis client configured")
        if self._script is None:
            self._script = self._redis.register_script(BUCKET_SCRIPT)
        result = await self._script(
            keys=[key], args=[rate, burst, time.time(), cost]
        )
        return int(result[0]), int(result[1]), int(result[2])

    async def check(self, *, identity: str, path: str) -> Decision:
        """Consume from the standard bucket, and from the expensive one when applicable.

        An expensive request consumes from *both* so that a client cannot spend its whole
        standard allowance on scans; the expensive bucket is a sub-limit, not an
        alternative one.
        """
        expensive = is_expensive(path)

        if self._redis is None:
            return Decision(True, -1, 0, "expensive" if expensive else "standard")

        try:
            allowed, remaining, retry = await self._run(
                f"rl:std:{identity}",
                STANDARD_PER_MINUTE / 60.0,
                STANDARD_BURST,
                1,
            )
            if not allowed:
                return Decision(False, remaining, retry, "standard")

            if expensive:
                allowed, remaining, retry = await self._run(
                    f"rl:exp:{identity}",
                    EXPENSIVE_PER_MINUTE / 60.0,
                    EXPENSIVE_BURST,
                    1,
                )
                return Decision(bool(allowed), remaining, retry, "expensive")

            return Decision(True, remaining, 0, "standard")

        except Exception as exc:
            # Fail open. A rate limiter protects against load; it is not authorisation,
            # and turning a Redis outage into an API outage is the wrong trade.
            logger.warning("ratelimit.unavailable", error=str(exc), path=path)
            return Decision(True, -1, 0, "unavailable")
