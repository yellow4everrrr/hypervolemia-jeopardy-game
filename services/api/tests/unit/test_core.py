"""Tests for core primitives: money, ids, instruments."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.core.ids import uuid7, uuid7_timestamp_ms
from app.core.money import (
    quantize_money,
    quantize_price,
    safe_divide,
    to_decimal,
    weighted_average,
)
from app.domain.common.enums import AssetClass, Direction, Side
from app.domain.trading.instrument import InstrumentSpec
from tests.conftest import CL, ES, MES

# --- Money ----------------------------------------------------------------------


def test_floats_are_rejected() -> None:
    """The whole point of the module: a float can never enter a P&L calculation."""
    with pytest.raises(TypeError, match="float is not accepted"):
        to_decimal(0.1)  # type: ignore[arg-type]


def test_bool_is_rejected() -> None:
    with pytest.raises(TypeError):
        to_decimal(True)  # type: ignore[arg-type]


def test_strings_preserve_precision() -> None:
    assert to_decimal("0.1") + to_decimal("0.2") == to_decimal("0.3")


def test_malformed_input_raises() -> None:
    with pytest.raises(ValueError, match="cannot interpret"):
        to_decimal("not a number")


def test_quantize_money_uses_bankers_rounding() -> None:
    """Half-even avoids the upward bias that half-up accumulates across a large sample."""
    assert quantize_money(Decimal("1.000000005"), Decimal("0.00000001")) == Decimal("1.00000000")
    assert quantize_money(Decimal("1.000000015"), Decimal("0.00000001")) == Decimal("1.00000002")


def test_quantize_price_snaps_to_the_tick_grid() -> None:
    """A derived price must be expressible as a real order before we report it."""
    assert quantize_price(Decimal("5000.13"), Decimal("0.25")) == Decimal("5000.25000000")
    assert quantize_price(Decimal("5000.12"), Decimal("0.25")) == Decimal("5000.00000000")


def test_quantize_price_rejects_a_non_positive_tick() -> None:
    with pytest.raises(ValueError, match="tick_size must be positive"):
        quantize_price(Decimal("5000"), Decimal("0"))


def test_safe_divide_returns_none_not_infinity() -> None:
    """Profit factor with no losing trades is undefined, not infinite."""
    assert safe_divide(Decimal("100"), Decimal("0")) is None
    assert safe_divide(Decimal("100"), Decimal("4")) == Decimal("25")


def test_weighted_average() -> None:
    assert weighted_average([(Decimal("5000"), Decimal(1)), (Decimal("5004"), Decimal(3))]) == (
        Decimal("5003")
    )


def test_weighted_average_of_nothing_is_none() -> None:
    assert weighted_average([]) is None
    assert weighted_average([(Decimal("5000"), Decimal(0))]) is None


# --- IDs ------------------------------------------------------------------------


def test_uuid7_has_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_uuid7_embeds_its_timestamp() -> None:
    assert uuid7_timestamp_ms(uuid7(timestamp_ms=1_800_000_000_000)) == 1_800_000_000_000


def test_uuid7_is_monotonic_within_a_millisecond() -> None:
    """Time-ordered keys are the reason for UUIDv7; ties must not break the ordering."""
    values = [uuid7(timestamp_ms=1_800_000_000_000) for _ in range(1_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_uuid7_orders_across_milliseconds() -> None:
    earlier = uuid7(timestamp_ms=1_800_000_000_000)
    later = uuid7(timestamp_ms=1_800_000_000_001)
    assert earlier < later


def test_uuid7_rejects_a_non_uuid7() -> None:
    from uuid import uuid4

    with pytest.raises(ValueError, match="expected a UUIDv7"):
        uuid7_timestamp_ms(uuid4())


# --- Instruments ----------------------------------------------------------------


def test_point_value_is_derived_from_tick_size_and_value() -> None:
    assert ES.point_value == Decimal("50")
    assert MES.point_value == Decimal("5")
    assert CL.point_value == Decimal("1000")


def test_points_to_currency_scales_by_quantity() -> None:
    assert ES.points_to_currency(Decimal("4"), Decimal(3)) == Decimal("600.00000000")


def test_points_to_ticks() -> None:
    assert ES.points_to_ticks(Decimal("4")) == Decimal("16")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tick_size", Decimal("0"), "tick_size must be positive"),
        ("tick_value", Decimal("-1"), "tick_value must be positive"),
        ("currency", "DOLLARS", "ISO 4217"),
        ("exchange_timezone", "Mars/Olympus", "unknown exchange timezone"),
        ("symbol", "", "symbol is required"),
    ],
)
def test_instrument_validation(field: str, value: object, message: str) -> None:
    kwargs = {
        "symbol": "ESZ5",
        "exchange": "CME",
        "asset_class": AssetClass.FUTURE,
        "currency": "USD",
        "tick_size": Decimal("0.25"),
        "tick_value": Decimal("12.50"),
        field: value,
    }
    with pytest.raises(DomainError, match=message):
        InstrumentSpec(**kwargs)  # type: ignore[arg-type]


# --- Enums ----------------------------------------------------------------------


def test_side_signs() -> None:
    assert Side.BUY.signed_multiplier == 1
    assert Side.SELL.signed_multiplier == -1
    assert Side.BUY.opposite is Side.SELL


def test_direction_from_signed_quantity() -> None:
    assert Direction.from_signed_quantity(Decimal(3)) is Direction.LONG
    assert Direction.from_signed_quantity(Decimal(-3)) is Direction.SHORT
    with pytest.raises(ValueError, match="flat position"):
        Direction.from_signed_quantity(Decimal(0))


def test_timeframe_seconds() -> None:
    from app.domain.common.enums import Timeframe

    assert Timeframe.M1.seconds == 60
    assert Timeframe.H4.seconds == 14_400
    assert Timeframe.D1.seconds == 86_400
