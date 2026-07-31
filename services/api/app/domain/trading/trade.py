"""The trade aggregate — a round turn assembled from fills.

A *trade* is the interval between a position leaving flat and returning to flat, on a
single instrument in a single account. Scale-ins and scale-outs belong to the same
trade; a position flip ends one trade and begins another.

This definition is chosen deliberately. Per-fill statistics reward a trader for
slicing orders and punish them for adding to a winner, and per-day statistics hide
which decision produced the result. The round turn is the smallest unit that
corresponds to a *decision*, which is the thing the journal exists to evaluate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.errors import DomainError
from app.core.money import ZERO, quantize_money, safe_divide, weighted_average
from app.domain.common.enums import Direction, ExecutionRole, TradeStatus
from app.domain.trading.instrument import InstrumentSpec


@dataclass(frozen=True, slots=True)
class TradeLeg:
    """One fill's contribution to one trade.

    A leg is not the same as a fill: when a fill straddles a position flip it produces
    an exit leg on the closing trade and an entry leg on the opening one, splitting
    quantity and costs between them.
    """

    execution_external_id: str
    role: ExecutionRole
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    commission: Decimal = ZERO
    fees: Decimal = ZERO
    sequence: int = 0
    #: Points realized by this leg against the lots it closed (exit legs only).
    #: ``None`` on entry legs, where the concept does not apply.
    realized_points: Decimal | None = None

    @property
    def notional_points(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class ReconstructedTrade:
    """A round turn, closed or still open.

    Attributes:
        realized_points: Sum over FIFO-matched pairs of ``Δprice × quantity``, signed by
            direction. Units are "point-contracts", so a 2-contract trade that gained
            4 points has ``realized_points == 8``. Multiply by the instrument's point
            value to get currency; divide by closed quantity for points per contract.
        max_position_size: Peak absolute exposure reached inside the trade. Used by
            risk analytics, which must size against the peak rather than the average.
    """

    account_key: str
    instrument_symbol: str
    direction: Direction
    status: TradeStatus
    opened_at: datetime
    legs: tuple[TradeLeg, ...]
    realized_points: Decimal
    max_position_size: Decimal
    point_value: Decimal
    tick_size: Decimal
    currency: str
    closed_at: datetime | None = None
    #: Identifier of the fill that opened the trade. Stable across re-syncs, so it is
    #: the natural idempotency key for persisted trades.
    origin_execution_id: str = ""
    metadata: dict[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.legs:
            raise DomainError("a trade must have at least one leg")
        if self.status is TradeStatus.CLOSED and self.closed_at is None:
            raise DomainError("a closed trade must have closed_at")
        if self.status is TradeStatus.OPEN and self.closed_at is not None:
            raise DomainError("an open trade must not have closed_at")

    # --- Legs ------------------------------------------------------------------

    @property
    def entry_legs(self) -> tuple[TradeLeg, ...]:
        return tuple(leg for leg in self.legs if leg.role is ExecutionRole.ENTRY)

    @property
    def exit_legs(self) -> tuple[TradeLeg, ...]:
        return tuple(leg for leg in self.legs if leg.role is ExecutionRole.EXIT)

    @property
    def quantity_opened(self) -> Decimal:
        return sum((leg.quantity for leg in self.entry_legs), start=ZERO)

    @property
    def quantity_closed(self) -> Decimal:
        return sum((leg.quantity for leg in self.exit_legs), start=ZERO)

    @property
    def quantity_open(self) -> Decimal:
        return self.quantity_opened - self.quantity_closed

    @property
    def is_scaled_in(self) -> bool:
        return len(self.entry_legs) > 1

    @property
    def is_scaled_out(self) -> bool:
        return len(self.exit_legs) > 1

    # --- Prices ----------------------------------------------------------------

    @property
    def avg_entry_price(self) -> Decimal | None:
        return weighted_average([(leg.price, leg.quantity) for leg in self.entry_legs])

    @property
    def avg_exit_price(self) -> Decimal | None:
        return weighted_average([(leg.price, leg.quantity) for leg in self.exit_legs])

    @property
    def points_per_contract(self) -> Decimal | None:
        """Realized move per contract — the number a trader recognises as "I made 4 points"."""
        return safe_divide(self.realized_points, self.quantity_closed)

    @property
    def ticks_per_contract(self) -> Decimal | None:
        points = self.points_per_contract
        return None if points is None else points / self.tick_size

    # --- Money -----------------------------------------------------------------

    @property
    def gross_pnl(self) -> Decimal:
        """Realized P&L before costs, in the instrument's currency."""
        return quantize_money(self.realized_points * self.point_value)

    @property
    def commission(self) -> Decimal:
        return quantize_money(sum((leg.commission for leg in self.legs), start=ZERO))

    @property
    def fees(self) -> Decimal:
        return quantize_money(sum((leg.fees for leg in self.legs), start=ZERO))

    @property
    def total_costs(self) -> Decimal:
        return self.commission + self.fees

    @property
    def net_pnl(self) -> Decimal:
        """Realized P&L after commissions and fees — the only P&L that is reported.

        Gross P&L flatters every strategy, and the gap between gross and net is where
        overtrading hides. Ledgerline reports net by default everywhere.
        """
        return quantize_money(self.gross_pnl - self.total_costs)

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def is_scratch(self) -> bool:
        return self.net_pnl == 0

    # --- Time ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> int | None:
        if self.closed_at is None:
            return None
        return int((self.closed_at - self.opened_at).total_seconds())

    @property
    def last_activity_at(self) -> datetime:
        return max(leg.executed_at for leg in self.legs)

    def signed_quantity_at(self, moment: datetime) -> Decimal:
        """Net position held at ``moment``, for replay and MAE/MFE windowing."""
        position = ZERO
        for leg in sorted(self.legs, key=lambda leg: (leg.executed_at, leg.sequence)):
            if leg.executed_at > moment:
                break
            delta = leg.quantity if leg.role is ExecutionRole.ENTRY else -leg.quantity
            position += delta * self.direction.signed_multiplier
        return position

    def with_costs_from(self, spec: InstrumentSpec) -> ReconstructedTrade:
        """Re-derive currency conversion against a (possibly corrected) instrument spec."""
        return ReconstructedTrade(
            account_key=self.account_key,
            instrument_symbol=self.instrument_symbol,
            direction=self.direction,
            status=self.status,
            opened_at=self.opened_at,
            legs=self.legs,
            realized_points=self.realized_points,
            max_position_size=self.max_position_size,
            point_value=spec.point_value,
            tick_size=spec.tick_size,
            currency=spec.currency,
            closed_at=self.closed_at,
            origin_execution_id=self.origin_execution_id,
            metadata=self.metadata,
        )
