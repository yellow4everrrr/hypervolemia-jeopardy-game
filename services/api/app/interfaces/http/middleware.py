"""HTTP middleware: request correlation and access logging."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import orjson
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import bind_contextvars, clear_contextvars, get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware:
    """Assign a request id, bind it to the log context, and emit one access log line.

    The id is echoed back in the response header so a user reporting "my sync failed at
    2pm" hands over something that maps to exact log lines. Bound via contextvars so
    every log written anywhere in the request — including inside a repository — carries
    it without threading a parameter through the call stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        started = time.perf_counter()
        status_code = 500

        clear_contextvars()
        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            # Health probes fire every few seconds; logging them at info level buries
            # everything else.
            level: Any = (
                logger.debug if request.url.path.startswith("/health") else logger.info
            )
            level(
                "http.request",
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
            )
            clear_contextvars()


class RateLimitMiddleware:
    """Apply per-user token buckets, with a tighter sub-limit on expensive routes.

    Runs as middleware rather than a dependency so that it applies before request body
    parsing and before any database work — a limiter that only rejects after the handler
    has already opened a session and read rows has not limited very much.

    The identity is the authenticated user where one is available. It is read from the
    same ``Authorization`` header the auth layer will verify, *without* verifying it
    here: an unverified token is fine as a bucket key, because a forged one only ever
    lets an attacker consume a bucket that is not theirs and the request still fails
    authentication a moment later. Verifying twice would double the JWT cost of every
    request to save nothing.
    """

    #: Paths that are never limited. Health probes fire continuously by design, and a
    #: limiter that can make a readiness check fail will eventually take a deployment
    #: down during an incident — exactly when the probes matter most.
    EXEMPT: tuple[str, ...] = ("/health", "/ready", "/docs", "/openapi.json", "/redoc")

    def __init__(self, app: ASGIApp, limiter: Any | None = None) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.limiter is None:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in self.EXEMPT):
            await self.app(scope, receive, send)
            return

        decision = await self.limiter.check(identity=_identity(request), path=path)

        if not decision.allowed:
            logger.warning(
                "ratelimit.rejected",
                path=path,
                bucket=decision.bucket,
                retry_after=decision.retry_after,
            )
            await _too_many_requests(send, decision)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for key, value in decision.headers().items():
                    headers.append((key.lower().encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _identity(request: Request) -> str:
    """Bucket key: the caller's token where present, else their address.

    Keyed on the token rather than the IP because IP keying punishes everyone behind one
    NAT and is trivially evaded by whoever it would catch. The address is only a fallback
    for unauthenticated traffic, which should not be reaching limited routes at all.
    """
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()
        # A hash, not the token: this becomes a Redis key and, in a debug dump, a log
        # line. Storing a live credential in either is how a bearer token leaks.
        return "t:" + hashlib.sha256(token.encode()).hexdigest()[:32]

    debug_user = request.headers.get("x-debug-user")
    if debug_user:
        return f"d:{debug_user}"

    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


async def _too_many_requests(send: Send, decision: Any) -> None:
    """Emit a 429 in the same envelope as every other error.

    Shape matters: clients parse ``error.code``, and a limiter that invents its own
    response body forces a special case into every one of them.
    """
    body = orjson.dumps(
        {
            "error": {
                "code": "rate_limited",
                "message": (
                    "too many requests; this endpoint is rate limited to protect the "
                    "analysis workers"
                ),
                "details": {
                    "bucket": decision.bucket,
                    "retry_after_seconds": decision.retry_after,
                },
            }
        }
    )
    headers = [(b"content-type", b"application/json")]
    for key, value in decision.headers().items():
        headers.append((key.lower().encode(), value.encode()))

    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body})
