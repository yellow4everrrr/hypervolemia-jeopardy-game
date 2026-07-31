"""Tradovate WebSocket frame codec.

Tradovate's real-time protocol is inherited from SockJS. A server frame is a single
type character followed by an optional JSON payload:

* ``o`` — open. Sent once when the session is established.
* ``h`` — heartbeat. Sent roughly every 2.5 seconds *unless* the server is actively
  streaming data, in which case it sends none at all.
* ``a[...]`` — an array of JSON messages: either responses to our requests or
  server-pushed events.
* ``c`` — close.

Client requests are plain text with four newline-separated fields::

    <endpoint>\\n<request id>\\n<query string>\\n<json body>

The blank fields matter — ``authorize\\n2\\n\\n<token>`` has an empty query field, and
omitting the newline makes the server reject the request.

This module is pure: encoding and decoding are functions over strings, with no socket
in sight. The protocol is fiddly enough that it deserves to be testable without a
network, and the connection manager in ``websocket.py`` deals only with parsed frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.errors import ExternalServiceError


class FrameType(StrEnum):
    OPEN = "o"
    HEARTBEAT = "h"
    MESSAGE = "a"
    CLOSE = "c"


#: The client's own heartbeat. Tradovate expects a stringified empty array at least
#: every 2.5 seconds or a load balancer drops the connection.
HEARTBEAT_FRAME = "[]"


@dataclass(frozen=True, slots=True)
class ServerFrame:
    """A decoded server frame."""

    type: FrameType
    payload: list[dict[str, Any]]

    @property
    def is_data(self) -> bool:
        return self.type is FrameType.MESSAGE


@dataclass(frozen=True, slots=True)
class ResponseMessage:
    """A reply to one of our requests: ``{"s": 200, "i": 2, "d": {...}}``."""

    request_id: int
    status: int
    data: Any = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error_text(self) -> str | None:
        """For non-2xx replies Tradovate puts the error string in ``d``."""
        if self.ok:
            return None
        return self.data if isinstance(self.data, str) else json.dumps(self.data)


@dataclass(frozen=True, slots=True)
class EventMessage:
    """A server-pushed event: ``{"e": "props", "d": {...}}``.

    ``props`` events carry ``entityType``/``eventType``/``entity``, where the entity
    JSON is identical to what the corresponding REST endpoint returns — which is what
    lets the live stream and the REST backfill share one mapping layer.
    """

    kind: str
    data: dict[str, Any]

    @property
    def entity_type(self) -> str | None:
        value = self.data.get("entityType")
        return str(value) if value is not None else None

    @property
    def event_type(self) -> str | None:
        value = self.data.get("eventType")
        return str(value) if value is not None else None

    @property
    def entity(self) -> dict[str, Any] | list[dict[str, Any]] | None:
        entity = self.data.get("entity")
        if isinstance(entity, dict | list):
            return entity
        return None


def encode_request(
    endpoint: str,
    request_id: int,
    query: str = "",
    body: Any = None,
) -> str:
    """Build a client request frame.

    Args:
        endpoint: REST-style endpoint name, e.g. ``user/syncrequest``.
        request_id: Unique within the connection; the reply echoes it as ``i``.
        query: Query-string fragment, e.g. ``masterids=1``.
        body: JSON-serialisable body, or a bare string (used by ``authorize``).
    """
    if request_id < 0:
        raise ValueError("request_id must be non-negative")

    if body is None:
        body_text = ""
    elif isinstance(body, str):
        body_text = body
    else:
        body_text = json.dumps(body, separators=(",", ":"))

    return f"{endpoint}\n{request_id}\n{query}\n{body_text}"


def encode_authorize(access_token: str, request_id: int) -> str:
    """Authorize the connection. Done once per connection, not per request."""
    return encode_request("authorize", request_id, body=access_token)


def decode_frame(raw: str) -> ServerFrame:
    """Parse a server frame.

    Numbers are parsed as :class:`~decimal.Decimal`, never ``float``. Fill prices
    arrive as JSON doubles, and letting ``5000.25`` become a binary float here would
    put rounding error into P&L at the very first hop — precisely what the rest of the
    system is built to avoid.
    """
    if not raw:
        raise ExternalServiceError("received an empty WebSocket frame")

    try:
        frame_type = FrameType(raw[0])
    except ValueError as exc:
        raise ExternalServiceError(
            f"unknown WebSocket frame type {raw[0]!r}", details={"frame": raw[:64]}
        ) from exc

    body = raw[1:]
    if not body:
        return ServerFrame(type=frame_type, payload=[])

    try:
        parsed = json.loads(body, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError(
            "malformed JSON in WebSocket frame", details={"frame": raw[:256]}
        ) from exc

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ExternalServiceError(
            "expected a JSON array in WebSocket frame", details={"frame": raw[:256]}
        )
    return ServerFrame(type=frame_type, payload=parsed)


def classify(message: dict[str, Any]) -> ResponseMessage | EventMessage:
    """Split a decoded message into a response or an event.

    Responses carry ``i`` (the id of the request they answer); events carry ``e``.
    """
    if "i" in message:
        return ResponseMessage(
            request_id=int(message["i"]),
            status=int(message.get("s", 0)),
            data=message.get("d"),
        )
    if "e" in message:
        data = message.get("d")
        return EventMessage(kind=str(message["e"]), data=data if isinstance(data, dict) else {})
    raise ExternalServiceError(
        "WebSocket message has neither a request id nor an event kind",
        details={"message": str(message)[:256]},
    )
