"""HTTP middleware: request correlation and access logging."""

from __future__ import annotations

import time
import uuid
from typing import Any

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
