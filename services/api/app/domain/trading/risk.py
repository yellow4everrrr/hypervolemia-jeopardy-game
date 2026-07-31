"""Per-trade risk arithmetic.

Only quantities that a *single trade* defines live here. Portfolio-level statistics
(expectancy, Sharpe, Kelly, risk of ruin, Monte Carlo) belong to the analytics engine
in milestone 3, which operates over collections of trades.

Every function returns ``None`` rather than a placeholder when the input does not
define the quantity. A trade with no recorded stop has no R-multiple; reporting ``0``
or ``inf`` there would poison the R distribution and any AI commentary drawn from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import DomainError
from app.core.money import ZERO, quantize_money, safe_divide
from app.domain.common.enums import Direction
from app.domain.trading.instrument import InstrumentSpec


def risk_per_contract(
    direction: Direction, entry_price: Decimal, stop_price: Decimal
) -> Decimal | None:
    """Points at risk per contract, or ``None`` if the stop is on the wrong side.

    A long stop above entry (or a short stop below) is not a stop — it is a data error
    or a broker field we misread. Returning ``None`` keeps it out of the statistics
    instead of recording negative risk.
    """
    points = (entry_price - stop_price) * direction.signed_multiplier
    return points if points > 0 else None


def planned_risk_amount(
    spec: InstrumentSpec,
    direction: Direction,
    entry_price: Decimal,
    stop_price: Decimal,
    quantity: Decimal,
) -> Decimal | None:
    """Currency at risk if the initial stop is hit on the full position."""
    points = risk_per_contract(direction, entry_price, stop_price)
    if points is None or quantity <= 0:
        return None
    return spec.points_to_currency(points, quantity)


def planned_reward_amount(
    spec: InstrumentSpec,
    direction: Direction,
    entry_price: Decimal,
    target_price: Decimal,
    quantity: Decimal,
) -> Decimal | None:
    """Currency gained if the initial target is hit on the full position."""
    points = (target_price - entry_price) * direction.signed_multiplier
    if points <= 0 or quantity <= 0:
        return None
    return spec.points_to_currency(points, quantity)


def reward_risk_ratio(risk_amount: Decimal | None, reward_amount: Decimal | None) -> Decimal | None:
    """Planned R:R. ``None`` unless both legs of the plan are known and positive."""
    if risk_amount is None or reward_amount is None or risk_amount <= 0:
        return None
    return safe_divide(reward_amount, risk_amount)


def r_multiple(net_pnl: Decimal, risk_amount: Decimal | None) -> Decimal | None:
    """Result expressed in units of initial risk.

    R is the only P&L unit comparable across instruments and position sizes, which is
    why the analytics engine prefers it to dollars for distribution work. It is
    computed from **net** P&L: a strategy that is +0.1R gross and −0.2R net after
    commissions is a losing strategy, and R must say so.
    """
    if risk_amount is None or risk_amount <= 0:
        return None
    return safe_divide(net_pnl, risk_amount)


@dataclass(frozen=True, slots=True)
class ExcursionResult:
    """Maximum adverse/favourable excursion for one trade.

    MAE and MFE are how a journal answers "was my stop too tight?" and "did I leave
    money on the table?" with evidence instead of hindsight narrative. Both are
    reported in points, currency and R.
    """

    mae_price: Decimal
    mfe_price: Decimal
    mae_points: Decimal
    mfe_points: Decimal
    mae_amount: Decimal
    mfe_amount: Decimal
    mae_r: Decimal | None
    mfe_r: Decimal | None

    @property
    def edge_ratio(self) -> Decimal | None:
        """MFE / MAE for this trade.

        Aggregated across a sample this is Tharp's edge ratio: a value durably above
        1.0 says entries are followed by more favourable movement than adverse, which
        is the cleanest available evidence that an *entry* has an edge independent of
        how the trade was managed.
        """
        return safe_divide(self.mfe_points, self.mae_points)


def compute_excursions(
    spec: InstrumentSpec,
    direction: Direction,
    entry_price: Decimal,
    quantity: Decimal,
    highest_price: Decimal,
    lowest_price: Decimal,
    risk_amount: Decimal | None = None,
) -> ExcursionResult:
    """Derive MAE/MFE from the price extremes observed while the trade was open.

    Args:
        highest_price: Highest traded price during the holding period.
        lowest_price: Lowest traded price during the holding period.
        risk_amount: Initial risk, used to express the excursions in R.

    Raises:
        DomainError: If the extremes are inverted or exclude the entry price, which
            would mean the bar window does not actually cover the trade.
    """
    if highest_price < lowest_price:
        raise DomainError("highest_price must not be below lowest_price")
    if not (lowest_price <= entry_price <= highest_price):
        raise DomainError("entry price must lie within the observed price range")
    if quantity <= 0:
        raise DomainError("quantity must be positive")

    if direction is Direction.LONG:
        mae_price, mfe_price = lowest_price, highest_price
    else:
        mae_price, mfe_price = highest_price, lowest_price

    sign = direction.signed_multiplier
    mae_points = abs((mae_price - entry_price) * sign)
    mfe_points = max((mfe_price - entry_price) * sign, ZERO)

    mae_amount = spec.points_to_currency(mae_points, quantity)
    mfe_amount = spec.points_to_currency(mfe_points, quantity)

    return ExcursionResult(
        mae_price=mae_price,
        mfe_price=mfe_price,
        mae_points=mae_points,
        mfe_points=mfe_points,
        mae_amount=quantize_money(mae_amount),
        mfe_amount=quantize_money(mfe_amount),
        mae_r=r_multiple(mae_amount, risk_amount),
        mfe_r=r_multiple(mfe_amount, risk_amount),
    )
