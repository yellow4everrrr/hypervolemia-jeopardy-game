"""Instrument specification.

A price difference is meaningless until it is multiplied by the instrument's point
value. One point on ES is $50; on MES it is $5; on CL it is $1,000. Every P&L
computation in the system goes through :class:`InstrumentSpec` so that this conversion
lives in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.errors import DomainError
from app.core.money import quantize_money
from app.domain.common.enums import AssetClass


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Immutable contract specification.

    Attributes:
        symbol: Broker-facing symbol, e.g. ``ESZ5`` or ``MNQ``.
        exchange: Listing exchange, e.g. ``CME``.
        asset_class: Product family, which selects the P&L convention.
        currency: Settlement currency (ISO 4217).
        tick_size: Minimum price increment, e.g. ``0.25`` for ES.
        tick_value: Currency value of one tick for one contract, e.g. ``12.50``.
        exchange_timezone: IANA zone used to derive session dates and segments.
        price_precision: Decimal places used when displaying prices.
    """

    symbol: str
    exchange: str
    asset_class: AssetClass
    currency: str
    tick_size: Decimal
    tick_value: Decimal
    exchange_timezone: str = "America/Chicago"
    price_precision: int = 2

    def __post_init__(self) -> None:
        if not self.symbol:
            raise DomainError("instrument symbol is required")
        if self.tick_size <= 0:
            raise DomainError(f"tick_size must be positive for {self.symbol}")
        if self.tick_value <= 0:
            raise DomainError(f"tick_value must be positive for {self.symbol}")
        if len(self.currency) != 3:
            raise DomainError(f"currency must be an ISO 4217 code, got {self.currency!r}")
        try:
            ZoneInfo(self.exchange_timezone)
        except Exception as exc:
            raise DomainError(f"unknown exchange timezone {self.exchange_timezone!r}") from exc

    @property
    def point_value(self) -> Decimal:
        """Currency value of a one-point move in one contract."""
        return self.tick_value / self.tick_size

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.exchange_timezone)

    def points_to_currency(self, points: Decimal, quantity: Decimal) -> Decimal:
        """Convert a signed point move on ``quantity`` contracts into currency."""
        return quantize_money(points * self.point_value * quantity)

    def points_to_ticks(self, points: Decimal) -> Decimal:
        """Express a point move in ticks — the unit traders actually reason in."""
        return points / self.tick_size

    def price_difference(self, from_price: Decimal, to_price: Decimal) -> Decimal:
        return to_price - from_price
