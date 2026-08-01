"""Tests for the Tradovate WebSocket frame codec.

The protocol is unusual enough — a SockJS descendant with a type-character prefix and
newline-delimited request frames — that each rule is pinned down here rather than
discovered against a live socket.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.errors import ExternalServiceError
from app.infrastructure.brokers.tradovate.frames import (
    EventMessage,
    FrameType,
    ResponseMessage,
    classify,
    decode_frame,
    encode_authorize,
    encode_request,
)

# --- Encoding -------------------------------------------------------------------


def test_request_has_four_newline_separated_fields() -> None:
    """Omitting a separator makes the server reject the request, so pin the shape."""
    frame = encode_request("user/syncrequest", 3, body={"users": [42]})
    assert frame == 'user/syncrequest\n3\n\n{"users":[42]}'
    assert frame.count("\n") == 3


def test_request_with_a_query_and_no_body() -> None:
    assert encode_request("tradingPermission/ldeps", 8, query="masterids=1") == (
        "tradingPermission/ldeps\n8\nmasterids=1\n"
    )


def test_request_with_neither_query_nor_body() -> None:
    assert encode_request("account/list", 4) == "account/list\n4\n\n"


def test_authorize_sends_the_bare_token_as_the_body() -> None:
    """The token is sent raw, not JSON-encoded — quoting it fails authorization."""
    assert encode_authorize("tok-123", 2) == "authorize\n2\n\ntok-123"


def test_request_id_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        encode_request("account/list", -1)


# --- Decoding -------------------------------------------------------------------


def test_open_frame() -> None:
    frame = decode_frame("o")
    assert frame.type is FrameType.OPEN
    assert frame.payload == []


def test_heartbeat_frame() -> None:
    assert decode_frame("h").type is FrameType.HEARTBEAT


def test_close_frame() -> None:
    assert decode_frame("c").type is FrameType.CLOSE


def test_message_frame_carries_an_array() -> None:
    frame = decode_frame('a[{"s":200,"i":2}]')
    assert frame.type is FrameType.MESSAGE
    assert frame.payload == [{"s": 200, "i": 2}]


def test_prices_decode_as_decimal_not_float() -> None:
    """The single most important line in this module.

    A fill price that becomes a binary float here carries rounding error into every
    downstream calculation, no matter how carefully the rest of the system uses
    Decimal. This is the only place it can be prevented.
    """
    frame = decode_frame('a[{"e":"props","d":{"entity":{"price":5000.25,"qty":3}}}]')
    price = frame.payload[0]["d"]["entity"]["price"]

    assert isinstance(price, Decimal)
    assert price == Decimal("5000.25")
    assert str(price) == "5000.25"


def test_a_price_that_has_no_exact_float_representation_survives() -> None:
    frame = decode_frame('a[{"d":{"price":0.1}}]')
    assert frame.payload[0]["d"]["price"] == Decimal("0.1")
    assert frame.payload[0]["d"]["price"] != Decimal(str(0.1 + 0.2 - 0.2))


def test_bare_object_is_wrapped_in_a_list() -> None:
    assert decode_frame('a{"s":200,"i":1}').payload == [{"s": 200, "i": 1}]


def test_empty_frame_is_rejected() -> None:
    with pytest.raises(ExternalServiceError, match="empty WebSocket frame"):
        decode_frame("")


def test_unknown_frame_type_is_rejected() -> None:
    with pytest.raises(ExternalServiceError, match="unknown WebSocket frame type"):
        decode_frame("z[]")


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(ExternalServiceError, match="malformed JSON"):
        decode_frame("a[{not json}]")


def test_non_array_payload_is_rejected() -> None:
    with pytest.raises(ExternalServiceError, match="expected a JSON array"):
        decode_frame("a42")


# --- Classification -------------------------------------------------------------


def test_response_is_recognised_by_its_request_id() -> None:
    parsed = classify({"s": 200, "i": 7, "d": {"ok": True}})
    assert isinstance(parsed, ResponseMessage)
    assert parsed.request_id == 7
    assert parsed.ok


def test_error_response_exposes_its_text() -> None:
    parsed = classify({"s": 401, "i": 2, "d": "Unauthorized"})
    assert isinstance(parsed, ResponseMessage)
    assert not parsed.ok
    assert parsed.error_text == "Unauthorized"


def test_event_is_recognised_by_its_kind() -> None:
    parsed = classify(
        {
            "e": "props",
            "d": {
                "entityType": "fill",
                "eventType": "Created",
                "entity": {"id": 1, "orderId": 2},
            },
        }
    )
    assert isinstance(parsed, EventMessage)
    assert parsed.kind == "props"
    assert parsed.entity_type == "fill"
    assert parsed.event_type == "Created"
    assert parsed.entity == {"id": 1, "orderId": 2}


def test_message_with_neither_marker_is_rejected() -> None:
    with pytest.raises(ExternalServiceError, match="neither a request id nor an event"):
        classify({"unexpected": True})


def test_documented_authorization_reply_round_trips() -> None:
    """The exact reply Tradovate's documentation shows for a successful authorize."""
    frame = decode_frame('a[{"s":200,"i":2}]')
    parsed = classify(frame.payload[0])
    assert isinstance(parsed, ResponseMessage)
    assert parsed.ok and parsed.request_id == 2


def test_documented_props_event_round_trips() -> None:
    """The order event from Tradovate's own documentation, parsed end to end."""
    raw = "a" + json.dumps(
        [
            {
                "e": "props",
                "d": {
                    "entityType": "order",
                    "eventType": "Created",
                    "entity": {
                        "id": 210518,
                        "accountId": 25,
                        "contractId": 560901,
                        "timestamp": "2016-11-04T00:02:36.626Z",
                        "action": "Sell",
                        "ordStatus": "PendingNew",
                        "admin": False,
                    },
                },
            }
        ]
    )
    event = classify(decode_frame(raw).payload[0])
    assert isinstance(event, EventMessage)
    assert event.entity_type == "order"
    assert isinstance(event.entity, dict)
    assert event.entity["accountId"] == 25
