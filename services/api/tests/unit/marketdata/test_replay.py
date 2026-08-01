"""Tests for replay window, markers and screenshot storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.core.errors import DomainError, ValidationError
from app.domain.common.enums import Direction, ScreenshotKind, Timeframe
from app.domain.marketdata.replay import (
    LegInput,
    MarkerKind,
    build_markers,
    build_risk_box,
    build_window,
    choose_timeframe,
    max_window_padding,
)
from app.infrastructure.storage.objects import (
    InMemoryObjectStore,
    checksum_of,
    screenshot_key,
    validate_image,
)

OPENED = datetime(2026, 3, 5, 14, 30, tzinfo=UTC)
USER = UUID("00000000-0000-7000-8000-000000000001")
TRADE = UUID("00000000-0000-7000-8000-000000000002")


# --- Timeframe selection --------------------------------------------------------


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (40, Timeframe.S15),
        (600, Timeframe.M1),
        (3_000, Timeframe.M2),
        (10_000, Timeframe.M5),
        (80_000, Timeframe.M15),
        (500_000, Timeframe.H1),
        (5_000_000, Timeframe.H1),
    ],
)
def test_timeframe_scales_with_holding_period(duration: int, expected: Timeframe) -> None:
    """A 40-second scalp on 5-minute candles is one candle and teaches nothing."""
    primary, higher = choose_timeframe(duration)
    assert primary is expected
    assert higher.seconds > primary.seconds


# --- Window ---------------------------------------------------------------------


def test_window_includes_context_on_both_sides() -> None:
    """Before, to see the setup. After, to see what the exit gave up."""
    window = build_window(
        instrument_symbol="ESM6",
        opened_at=OPENED,
        closed_at=OPENED + timedelta(minutes=10),
        duration_seconds=600,
    )

    assert window.primary_timeframe is Timeframe.M1
    assert window.window_start == OPENED - timedelta(minutes=120)
    assert window.window_end == OPENED + timedelta(minutes=70)


def test_open_trade_window_runs_to_now() -> None:
    now = OPENED + timedelta(minutes=5)
    window = build_window(
        instrument_symbol="ESM6",
        opened_at=OPENED,
        closed_at=None,
        duration_seconds=None,
        now=now,
    )
    assert window.trade_end == now


def test_window_rejects_a_close_before_the_open() -> None:
    with pytest.raises(DomainError, match="cannot close before"):
        build_window(
            instrument_symbol="ESM6",
            opened_at=OPENED,
            closed_at=OPENED - timedelta(minutes=1),
            duration_seconds=None,
        )


def test_window_rejects_a_naive_timestamp() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        build_window(
            instrument_symbol="ESM6",
            opened_at=datetime(2026, 3, 5, 14, 30),  # noqa: DTZ001
            closed_at=None,
            duration_seconds=None,
        )


# --- Markers --------------------------------------------------------------------


def test_scale_ins_get_their_own_marker_kind() -> None:
    """Seeing *where* size was added is most of the point of replaying a scaled trade."""
    markers = build_markers(
        direction=Direction.LONG,
        legs=[
            LegInput(OPENED, Decimal(5000), Decimal(1), is_entry=True),
            LegInput(OPENED + timedelta(minutes=2), Decimal(5002), Decimal(2), is_entry=True),
            LegInput(OPENED + timedelta(minutes=9), Decimal(5010), Decimal(3), is_entry=False),
        ],
    )
    kinds = [marker.kind for marker in markers]

    assert kinds == [MarkerKind.ENTRY, MarkerKind.SCALE_IN, MarkerKind.EXIT]


def test_scale_outs_are_distinguished_from_the_final_exit() -> None:
    markers = build_markers(
        direction=Direction.LONG,
        legs=[
            LegInput(OPENED, Decimal(5000), Decimal(3), is_entry=True),
            LegInput(OPENED + timedelta(minutes=5), Decimal(5004), Decimal(1), is_entry=False),
            LegInput(OPENED + timedelta(minutes=9), Decimal(5010), Decimal(2), is_entry=False),
        ],
    )
    kinds = [marker.kind for marker in markers]

    assert kinds == [MarkerKind.ENTRY, MarkerKind.SCALE_OUT, MarkerKind.EXIT]


def test_stop_and_target_span_the_trade() -> None:
    """They are horizontal lines, not point events."""
    markers = build_markers(
        direction=Direction.LONG,
        legs=[LegInput(OPENED, Decimal(5000), Decimal(1), is_entry=True)],
        stop_price=Decimal(4990),
        target_price=Decimal(5020),
    )
    spanning = [marker for marker in markers if marker.spans_trade]

    assert {marker.kind for marker in spanning} == {MarkerKind.STOP, MarkerKind.TARGET}
    assert all(marker.at is None for marker in spanning)


def test_markers_serialise_decimals_as_strings() -> None:
    markers = build_markers(
        direction=Direction.LONG,
        legs=[LegInput(OPENED, Decimal("5000.25"), Decimal(1), is_entry=True)],
    )
    payload = markers[0].to_payload()

    assert payload["price"] == "5000.25"
    assert isinstance(payload["price"], str)


# --- Risk box -------------------------------------------------------------------


def test_risk_box_from_entry_to_stop() -> None:
    box = build_risk_box(
        direction=Direction.LONG,
        entry_price=Decimal(5000),
        stop_price=Decimal(4990),
        opened_at=OPENED,
        closed_at=OPENED + timedelta(minutes=10),
    )

    assert box is not None
    assert box["risk_points"] == "10"


def test_no_risk_box_without_a_stop() -> None:
    """An invented box would put a plan the trader never had on their chart."""
    box = build_risk_box(
        direction=Direction.LONG,
        entry_price=Decimal(5000),
        stop_price=None,
        opened_at=OPENED,
        closed_at=None,
    )
    assert box is None


def test_no_risk_box_when_the_stop_is_on_the_wrong_side() -> None:
    """A long stop above entry is a data error, not a tiny risk."""
    box = build_risk_box(
        direction=Direction.LONG,
        entry_price=Decimal(5000),
        stop_price=Decimal(5010),
        opened_at=OPENED,
        closed_at=None,
    )
    assert box is None


# --- Screenshot storage ---------------------------------------------------------


async def test_object_store_round_trip() -> None:
    store = InMemoryObjectStore()
    data = b"fake-webp-bytes"
    key = screenshot_key(
        user_id=USER,
        trade_id=TRADE,
        kind=ScreenshotKind.ENTRY,
        timeframe=Timeframe.M1,
        checksum=checksum_of(data),
    )

    stored = await store.put(key, data, content_type="image/webp")

    assert stored.byte_size == len(data)
    assert await store.get(key) == data


async def test_keys_are_content_addressed_under_a_tenant_prefix() -> None:
    """Tenant prefix for access policy; checksum so a re-upload is a no-op."""
    data = b"chart"
    key = screenshot_key(
        user_id=USER,
        trade_id=TRADE,
        kind=ScreenshotKind.BEFORE_ENTRY,
        timeframe=Timeframe.M5,
        checksum=checksum_of(data),
    )

    assert key.startswith(f"screenshots/{USER}/{TRADE}/before_entry/5m/")
    same = screenshot_key(
        user_id=USER,
        trade_id=TRADE,
        kind=ScreenshotKind.BEFORE_ENTRY,
        timeframe=Timeframe.M5,
        checksum=checksum_of(data),
    )
    assert key == same


def test_non_image_content_is_refused() -> None:
    with pytest.raises(ValidationError, match="unsupported content type"):
        validate_image(b"data", "application/pdf")


def test_empty_and_oversized_objects_are_refused() -> None:
    with pytest.raises(ValidationError, match="empty object"):
        validate_image(b"", "image/webp")
    with pytest.raises(ValidationError, match="over the"):
        validate_image(b"x" * (9 * 1024 * 1024), "image/webp")


async def test_missing_object_raises_not_found() -> None:
    from app.core.errors import NotFoundError

    store = InMemoryObjectStore()
    with pytest.raises(NotFoundError):
        await store.get("screenshots/nope")


@pytest.mark.parametrize(
    "held_seconds",
    [1, 59, 120, 121, 900, 3_600, 14_400, 86_400, 604_800, 604_801, 5_000_000],
    ids=lambda seconds: f"{seconds}s",
)
def test_the_padding_bound_covers_every_window_it_claims_to(held_seconds: int) -> None:
    """`max_window_padding` must be an upper bound, not an average.

    The screenshot sweep uses it to discard trades whose windows cannot contain a bar.
    That filter is only safe while the bound is genuinely wider than every window
    `build_window` produces — one duration where the real lead exceeds the claimed lead
    turns the filter into a silent data-loss bug: the trade is dropped from the candidate
    list, no capture runs, and the sweep reports success.

    Parametrised across the timeframe thresholds and their boundaries because the
    execution timeframe is chosen by a lookup table, so the interesting durations are the
    ones either side of each step.
    """
    lead, trail = max_window_padding()
    window = build_window(
        instrument_symbol="ESU6",
        opened_at=OPENED,
        closed_at=OPENED + timedelta(seconds=held_seconds),
        duration_seconds=held_seconds,
    )

    assert OPENED - lead <= window.window_start
    assert window.window_end <= OPENED + timedelta(seconds=held_seconds) + trail
