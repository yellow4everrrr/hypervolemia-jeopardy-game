"""Tradovate real-time connection.

The single most important property of this module is stated up front because it is
easy to get wrong and expensive to discover late:

    **The WebSocket is an optimisation, never a source of truth.**

Fills that arrive over the stream make the journal feel live. But a stream can drop
silently, a load balancer can time it out, and Tradovate itself disconnects the oldest
connection when a customer exceeds their simultaneous-connection allowance — which
happens routinely the moment the trader opens the Trader app. Any design where a
missed frame means a missing trade is a design that loses trades.

So every reconnect triggers a REST catch-up before the stream is trusted again, and the
scheduled REST sync runs regardless. The stream reduces latency from minutes to
milliseconds; it never carries sole responsibility for a fill.

The transport is injected. ``websockets`` is the production implementation, but the
connection logic — framing, authorization, heartbeats, request/response correlation,
reconnect backoff — is testable against an in-memory fake.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.infrastructure.brokers.base import backoff_delays
from app.infrastructure.brokers.tradovate.config import (
    HEARTBEAT_INTERVAL_SECONDS,
    RECONNECT_BASE_DELAY_SECONDS,
    RECONNECT_MAX_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)
from app.infrastructure.brokers.tradovate.frames import (
    HEARTBEAT_FRAME,
    EventMessage,
    FrameType,
    ResponseMessage,
    classify,
    decode_frame,
    encode_authorize,
    encode_request,
)

logger = get_logger(__name__)


class WebSocketTransport(Protocol):
    """Minimal socket surface. Implemented by ``websockets`` and by test fakes."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


TransportFactory = Callable[[str], Awaitable[WebSocketTransport]]
EventHandler = Callable[[EventMessage], Awaitable[None]]


@dataclass
class _PendingRequest:
    future: asyncio.Future[ResponseMessage]


@dataclass
class ConnectionStats:
    """Observability for a connection that is expected to churn."""

    connects: int = 0
    disconnects: int = 0
    events_received: int = 0
    heartbeats_sent: int = 0
    #: Incremented whenever a reconnect requires a REST catch-up, which is the metric
    #: that tells you whether the stream is healthy enough to be worth trusting.
    gap_recoveries: int = 0
    last_error: str | None = None


class TradovateWebSocket:
    """One authorized real-time connection.

    Tradovate permits a single simultaneous connection per customer by default and
    disconnects the oldest when that is exceeded, so this class owns exactly one
    socket and treats a close frame as an ordinary event.
    """

    def __init__(
        self,
        url: str,
        access_token: str,
        transport_factory: TransportFactory,
        *,
        on_event: EventHandler | None = None,
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._token = access_token
        self._factory = transport_factory
        self._on_event = on_event
        #: Called after every successful reconnect. This is where the REST catch-up
        #: goes — the callback that keeps a dropped stream from becoming lost trades.
        self._on_reconnect = on_reconnect
        self._heartbeat_interval = heartbeat_interval
        self._sleep = sleep

        self._transport: WebSocketTransport | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._request_id = 0
        self._closed = False
        self._authorized = asyncio.Event()
        #: Set by whichever loop notices the connection has gone: the reader on a close
        #: frame or read error, the heartbeat loop on a failed send.
        self._disconnected = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.stats = ConnectionStats()

    @property
    def is_connected(self) -> bool:
        return self._transport is not None and not self._closed

    async def connect(self) -> None:
        """Open the socket, wait for the open frame, and authorize."""
        self._transport = await self._factory(self._url)
        self._closed = False
        self._authorized.clear()
        self._disconnected.clear()
        self.stats.connects += 1

        self._tasks = [
            asyncio.create_task(self._read_loop(), name="tradovate-ws-read"),
            asyncio.create_task(self._heartbeat_loop(), name="tradovate-ws-heartbeat"),
        ]

        await self._authorize()

    async def close(self) -> None:
        self._closed = True
        # Wake `run_forever` if it is parked waiting for a disconnect, otherwise a
        # shutdown leaves the reconnect loop blocked forever on an event nobody sets.
        self._disconnected.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()
            self._transport = None
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(ExternalServiceError("connection closed"))
        self._pending.clear()

    # The `timeout` parameter is protocol-level rather than a cancellation scope: a
    # Tradovate request whose reply never arrives must fail rather than hang, and
    # different endpoints warrant different patience. Hence the ASYNC109 exemption.
    async def request(
        self,
        endpoint: str,
        query: str = "",
        body: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ResponseMessage:
        """Send a request and await its correlated reply."""
        if self._transport is None:
            raise ExternalServiceError("WebSocket is not connected")

        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[ResponseMessage] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(future=future)

        await self._transport.send(encode_request(endpoint, request_id, query, body))
        try:
            return await asyncio.wait_for(future, timeout or REQUEST_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ExternalServiceError(
                f"Tradovate did not answer {endpoint} in time", details={"endpoint": endpoint}
            ) from exc

    async def subscribe_user(self, user_id: int) -> ResponseMessage:
        """Subscribe to every change on the user's data.

        ``user/syncrequest`` returns a full snapshot and then streams incremental
        ``props`` events — orders, fills, positions, cash balances. It cannot be
        unsubscribed except by closing the connection, which is fine: we want it for
        the lifetime of the connection.
        """
        return await self.request("user/syncrequest", body={"users": [user_id]})

    async def run_forever(self, max_attempts: int | None = None) -> None:
        """Maintain the connection, reconnecting with backoff until closed.

        Every successful reconnect invokes ``on_reconnect`` *before* the stream is
        treated as authoritative again, so the gap is closed by REST rather than
        assumed away.
        """
        delays = backoff_delays(
            RECONNECT_BASE_DELAY_SECONDS, RECONNECT_MAX_DELAY_SECONDS, attempts=12
        )
        attempt = 0

        while not self._closed:
            try:
                await self.connect()
                attempt = 0
                if self._on_reconnect is not None:
                    self.stats.gap_recoveries += 1
                    await self._on_reconnect()
                await self._wait_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.last_error = str(exc)
                logger.warning("tradovate.ws_error", error=str(exc), attempt=attempt)

            if self._closed:
                break

            attempt += 1
            if max_attempts is not None and attempt > max_attempts:
                raise ExternalServiceError(
                    "exhausted WebSocket reconnect attempts",
                    details={"attempts": attempt, "last_error": self.stats.last_error},
                )
            await self._sleep(delays[min(attempt - 1, len(delays) - 1)])

    # --- internals -------------------------------------------------------------

    async def _authorize(self) -> None:
        if self._transport is None:  # pragma: no cover — guarded by callers
            raise ExternalServiceError("WebSocket is not connected")

        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[ResponseMessage] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(future=future)

        await self._transport.send(encode_authorize(self._token, request_id))
        response = await asyncio.wait_for(future, REQUEST_TIMEOUT_SECONDS)
        if not response.ok:
            raise ExternalServiceError(
                f"Tradovate refused the WebSocket authorization: {response.error_text}",
                details={"status": response.status},
            )
        self._authorized.set()
        logger.info("tradovate.ws_authorized")

    async def _read_loop(self) -> None:
        transport = self._transport
        if transport is None:  # pragma: no cover
            return
        try:
            while not self._closed:
                raw = await transport.recv()
                await self._handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = str(exc)
            self.stats.disconnects += 1
            self._disconnected.set()
            logger.info("tradovate.ws_disconnected", error=str(exc))

    async def _handle_raw(self, raw: str) -> None:
        frame = decode_frame(raw)

        if frame.type is FrameType.OPEN:
            return
        if frame.type is FrameType.HEARTBEAT:
            return
        if frame.type is FrameType.CLOSE:
            # Expected when the customer's connection allowance is exceeded elsewhere.
            self.stats.disconnects += 1
            self._disconnected.set()
            raise ExternalServiceError("Tradovate closed the WebSocket")

        for message in frame.payload:
            parsed = classify(message)
            if isinstance(parsed, ResponseMessage):
                pending = self._pending.pop(parsed.request_id, None)
                if pending is not None and not pending.future.done():
                    pending.future.set_result(parsed)
                continue

            self.stats.events_received += 1
            if self._on_event is not None:
                await self._on_event(parsed)

    async def _heartbeat_loop(self) -> None:
        """Beat unconditionally.

        A server that is streaming data sends no heartbeats of its own, so client
        liveness cannot be inferred from inbound traffic. Beating on a fixed interval
        regardless of what is arriving is the only scheme that survives an active feed.
        """
        try:
            while not self._closed:
                await self._sleep(self._heartbeat_interval)
                if self._transport is None or self._closed:
                    return
                await self._transport.send(HEARTBEAT_FRAME)
                self.stats.heartbeats_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = str(exc)
            self._disconnected.set()

    async def _wait_until_disconnected(self) -> None:
        await self._disconnected.wait()
        await self._teardown_transport()

    async def _teardown_transport(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()
            self._transport = None


async def stream_events(  # pragma: no cover — adapter over the callback interface
    socket: TradovateWebSocket,
) -> AsyncIterator[EventMessage]:
    """Adapter for callers that prefer iteration over a callback."""
    queue: asyncio.Queue[EventMessage] = asyncio.Queue()

    async def enqueue(event: EventMessage) -> None:
        await queue.put(event)

    socket._on_event = enqueue
    while True:
        yield await queue.get()
