"""Decimal helpers for money and price arithmetic.

Floating point is banned anywhere near P&L. ``0.1 + 0.2`` is a rounding curiosity in
most software and a reconciliation failure in a trading journal: a million-row
aggregate accumulates error that will not match the broker statement. Every monetary
and price quantity in Ledgerline is a :class:`decimal.Decimal`, stored as
``NUMERIC(20, 8)``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Final

#: Working precision for intermediate calculations. 28 significant digits comfortably
#: covers 10^12 notional at 8 decimal places.
DECIMAL_PRECISION: Final[int] = 28

#: Storage scale — matches ``NUMERIC(20, 8)`` in the schema.
MONEY_SCALE: Final[Decimal] = Decimal("0.00000001")

#: Display/reporting scale for currency amounts.
CURRENCY_SCALE: Final[Decimal] = Decimal("0.01")

ZERO: Final[Decimal] = Decimal(0)


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Coerce a value to ``Decimal``, rejecting floats and malformed input.

    ``float`` is deliberately not accepted: silently absorbing binary floating point
    is exactly the bug this module exists to prevent. Callers holding a float (for
    example from a broker's JSON payload) must pass the original string.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; almost certainly a mistake
        raise TypeError("bool is not a valid monetary value")
    if isinstance(value, float):
        raise TypeError("float is not accepted; pass the original string to preserve precision")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"cannot interpret {value!r} as a decimal") from exc


def quantize_money(value: Decimal, scale: Decimal = MONEY_SCALE) -> Decimal:
    """Round to the storage scale using banker's rounding.

    ``ROUND_HALF_EVEN`` is the correct default for aggregating many values: unlike
    half-up it does not introduce a systematic upward bias across a large sample.
    """
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return value.quantize(scale, rounding=ROUND_HALF_EVEN)


def quantize_price(value: Decimal, tick_size: Decimal) -> Decimal:
    """Snap a price to the instrument's tick grid.

    Broker fills already sit on the grid; this is for *derived* prices — stop
    suggestions, what-if simulations, ATR offsets — which must be expressible as real
    orders before we report them as achievable.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        ticks = (value / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return quantize_money(ticks * tick_size)


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Divide, returning ``None`` instead of raising on a zero denominator.

    Ratio metrics (profit factor with no losers, R-multiple with no defined risk) are
    genuinely undefined rather than infinite or zero. Returning ``None`` forces the
    caller — and ultimately the AI layer — to say "not defined for this sample"
    instead of reporting a fabricated number.
    """
    if denominator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return numerator / denominator


def weighted_average(pairs: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Quantity-weighted average of ``(value, weight)`` pairs.

    Returns ``None`` when the total weight is zero, which is the honest answer for an
    average entry price across zero contracts.
    """
    total_weight = sum((weight for _, weight in pairs), start=ZERO)
    if total_weight == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        total = sum((value * weight for value, weight in pairs), start=ZERO)
        return total / total_weight
