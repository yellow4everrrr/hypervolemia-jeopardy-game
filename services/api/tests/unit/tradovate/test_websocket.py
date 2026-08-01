"""Tests for the Tradovate real-time connection.

Driven against an in-memory transport, so authorization, heartbeats, request/response
correlation and — most importantly — gap recovery on reconnect are all exercised
without a socket.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core.errors import ExternalServiceError
from app.infrastructure.brokers.tradovate.frames import EventMessage
from app.infrastructure.brokers.tradovate.websocket import TradovateWebSocket


class FakeTransport:
    """Scriptable WebSocket.

    ``inbound`` is a queue of frames to deliver; ``sent`` records what the client wrote.
    Pushing ``None`` simulates the connection dropping.
    """

    def __init__(self, script: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self.inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        for frame in script or []:
            self.inbound.put_nowait(frame)

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("transport closed")
        self.sent.append(message)
        # Answer an authorize request the way the server documents it.
        if message.startswith("authorize\n"):
            request_id = int(message.split("\n")[1])
            await self.inbound.put(json.dumps([{"s": 200, "i": request_id}]).join(["a", ""]))

    async def recv(self) -> str:
        frame = await self.inbound.get()
        if frame is None:
            raise ConnectionError("connection lost")
        return frame

    async def close(self) -> None:
        self.closed = True
        await self.inbound.put(None)

    def push(self, frame: str) -> None:
        self.inbound.put_nowait(frame)

    @property
    def requests(self) -> list[str]:
        return [message for message in self.sent if message != "[]"]

    @property
    def heartbeats(self) -> list[str]:
        return [message for message in self.sent if message == "[]"]


def socket_for(
    transport: FakeTransport,
    *,
    on_event: Any = None,
    on_reconnect: Any = None,
    heartbeat_interval: float = 0.01,
) -> TradovateWebSocket:
    async def factory(url: str) -> FakeTransport:
        return transport

    return TradovateWebSocket(
        "wss://demo.tradovateapi.com/v1/websocket",
        "tok-123",
        factory,
        on_event=on_event,
        on_reconnect=on_reconnect,
        heartbeat_interval=heartbeat_interval,
    )


# --- Connection and authorization ------------------------------------------------


async def test_connect_authorizes_with_the_token() -> None:
    transport = FakeTransport(["o"])
    socket = socket_for(transport)

    await socket.connect()
    try:
        assert transport.requests[0].startswith("authorize\n")
        assert transport.requests[0].endswith("\n\ntok-123")
        assert socket.is_connected
        assert socket.stats.connects == 1
    finally:
        await socket.close()


async def test_rejected_authorization_fails_the_connection() -> None:
    class RejectingTransport(FakeTransport):
        async def send(self, message: str) -> None:
            self.sent.append(message)
            if message.startswith("authorize"):
                request_id = int(message.split("\n")[1])
                await self.inbound.put(f'a[{{"s":401,"i":{request_id},"d":"Unauthorized"}}]')

    transport = RejectingTransport(["o"])
    socket = socket_for(transport)

    with pytest.raises(ExternalServiceError, match="refused the WebSocket authorization"):
        await socket.connect()
    await socket.close()


async def test_open_and_heartbeat_frames_are_ignored() -> None:
    """Neither carries data; treating them as messages would be a parse error."""
    transport = FakeTransport(["o", "h", "h"])
    events: list[EventMessage] = []

    async def collect(event: EventMessage) -> None:
        events.append(event)

    socket = socket_for(transport, on_event=collect)
    await socket.connect()
    await asyncio.sleep(0.02)
    try:
        assert events == []
    finally:
        await socket.close()


# --- Requests --------------------------------------------------------------------


async def test_request_is_correlated_with_its_reply() -> None:
    transport = FakeTransport(["o"])
    socket = socket_for(transport)
    await socket.connect()

    try:
        task = asyncio.create_task(socket.subscribe_user(4242))
        await asyncio.sleep(0.01)
        sync_request = next(r for r in transport.requests if r.startswith("user/syncrequest"))
        request_id = int(sync_request.split("\n")[1])
        transport.push(f'a[{{"s":200,"i":{request_id},"d":{{"users":[]}}}}]')

        response = await asyncio.wait_for(task, timeout=1)
        assert response.ok
        assert response.request_id == request_id
        assert sync_request.endswith('\n\n{"users":[4242]}')
    finally:
        await socket.close()


async def test_out_of_order_replies_reach_the_right_caller() -> None:
    """Requests are concurrent; correlation must be by id, not arrival order."""
    transport = FakeTransport(["o"])
    socket = socket_for(transport)
    await socket.connect()

    try:
        first = asyncio.create_task(socket.request("account/list"))
        second = asyncio.create_task(socket.request("fill/list"))
        await asyncio.sleep(0.01)

        ids = [int(r.split("\n")[1]) for r in transport.requests if not r.startswith("authorize")]
        # Answer the second request first.
        transport.push(f'a[{{"s":200,"i":{ids[1]},"d":"second"}}]')
        transport.push(f'a[{{"s":200,"i":{ids[0]},"d":"first"}}]')

        assert (await asyncio.wait_for(first, 1)).data == "first"
        assert (await asyncio.wait_for(second, 1)).data == "second"
    finally:
        await socket.close()


async def test_request_times_out_rather_than_hanging() -> None:
    transport = FakeTransport(["o"])
    socket = socket_for(transport)
    await socket.connect()

    try:
        with pytest.raises(ExternalServiceError, match="did not answer"):
            await socket.request("account/list", timeout=0.05)
    finally:
        await socket.close()


async def test_request_before_connecting_is_refused() -> None:
    socket = socket_for(FakeTransport())
    with pytest.raises(ExternalServiceError, match="not connected"):
        await socket.request("account/list")


# --- Events ----------------------------------------------------------------------


async def test_props_events_reach_the_handler() -> None:
    transport = FakeTransport(["o"])
    events: list[EventMessage] = []

    async def collect(event: EventMessage) -> None:
        events.append(event)

    socket = socket_for(transport, on_event=collect)
    await socket.connect()

    try:
        transport.push(
            'a[{"e":"props","d":{"entityType":"fill","eventType":"Created",'
            '"entity":{"id":1,"orderId":7,"price":5000.25}}}]'
        )
        await asyncio.sleep(0.02)

        assert len(events) == 1
        assert events[0].entity_type == "fill"
        assert socket.stats.events_received == 1
    finally:
        await socket.close()


async def test_several_messages_in_one_frame_are_all_delivered() -> None:
    """A frame carries an array; handling only the first would silently drop fills."""
    transport = FakeTransport(["o"])
    events: list[EventMessage] = []

    async def collect(event: EventMessage) -> None:
        events.append(event)

    socket = socket_for(transport, on_event=collect)
    await socket.connect()

    try:
        transport.push(
            'a[{"e":"props","d":{"entityType":"fill","entity":{"id":1}}},'
            '{"e":"props","d":{"entityType":"fill","entity":{"id":2}}}]'
        )
        await asyncio.sleep(0.02)
        assert len(events) == 2
    finally:
        await socket.close()


# --- Heartbeats ------------------------------------------------------------------


async def test_client_heartbeats_unconditionally() -> None:
    """A server streaming data sends no heartbeats of its own.

    Liveness therefore cannot be inferred from inbound traffic; the client must beat on
    a timer regardless of what is arriving, or an active feed gets disconnected.
    """
    transport = FakeTransport(["o"])
    socket = socket_for(transport, heartbeat_interval=0.01)
    await socket.connect()

    try:
        for _ in range(5):
            transport.push('a[{"e":"props","d":{"entityType":"fill","entity":{"id":1}}}]')
        await asyncio.sleep(0.05)

        assert transport.heartbeats, "no heartbeat sent while the feed was active"
        assert socket.stats.heartbeats_sent >= 1
    finally:
        await socket.close()


# --- Gap recovery ----------------------------------------------------------------


async def test_reconnect_triggers_a_rest_catch_up() -> None:
    """The property this whole module exists to guarantee.

    A dropped stream means fills may have happened unseen. Reconnecting without a REST
    catch-up would leave those trades missing from the journal with nothing to indicate
    it — so every reconnect must run the recovery callback before the stream is trusted.
    """
    recoveries = 0

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    transports: list[FakeTransport] = []

    async def factory(url: str) -> FakeTransport:
        transport = FakeTransport(["o"])
        transports.append(transport)
        return transport

    socket = TradovateWebSocket(
        "wss://example/ws", "tok", factory, on_reconnect=recover, heartbeat_interval=0.01,
        sleep=lambda _: asyncio.sleep(0),
    )

    runner = asyncio.create_task(socket.run_forever(max_attempts=3))
    await asyncio.sleep(0.05)

    # Drop the live connection the way a load balancer or a competing session would.
    transports[0].push("c")
    await asyncio.sleep(0.1)

    assert socket.stats.connects >= 2, "the socket did not reconnect after the drop"
    assert recoveries >= 2, "reconnect did not trigger a catch-up"
    assert socket.stats.gap_recoveries >= 2
    assert socket.stats.disconnects >= 1

    await socket.close()
    transports[-1].push("c")
    await asyncio.wait_for(runner, timeout=1)


async def test_close_frame_counts_as_a_disconnect() -> None:
    """Tradovate closes the oldest connection when a customer opens another elsewhere."""
    transport = FakeTransport(["o"])
    socket = socket_for(transport)
    await socket.connect()

    transport.push("c")
    await asyncio.sleep(0.02)

    assert socket.stats.disconnects >= 1
    await socket.close()


async def test_close_fails_any_pending_requests() -> None:
    """A caller awaiting a reply on a dead socket must not wait forever."""
    transport = FakeTransport(["o"])
    socket = socket_for(transport)
    await socket.connect()

    pending = asyncio.create_task(socket.request("account/list", timeout=5))
    await asyncio.sleep(0.01)
    await socket.close()

    with pytest.raises(ExternalServiceError, match="connection closed"):
        await asyncio.wait_for(pending, timeout=1)
