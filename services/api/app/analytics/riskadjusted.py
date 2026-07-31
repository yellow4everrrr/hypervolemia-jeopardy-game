"""Risk-adjusted return ratios: Sharpe, Sortino, MAR, Kelly, risk of ruin.

These are the most frequently misreported statistics in retail trading software, and
the errors all run the same direction — they flatter. This module is explicit about
each choice, because the same name attached to two different constructions produces
numbers that differ by several multiples.

**Sharpe is computed on the daily series, not per trade.** A "Sharpe ratio" over
per-trade P&L is not a Sharpe ratio: the denominator is dispersion per *trade*, so the
figure rises when you trade less and falls when you trade more, independent of
performance. Sharpe is defined on a periodic return series, so this module builds one
from session dates and annualises it.

**Sharpe needs an equity base.** A ratio of dollar P&L to dollar standard deviation is
scale-free by accident, not by construction, and cannot be compared against any
published Sharpe. When starting equity is supplied the result is a true return-based
Sharpe; when it is not, the result is reported with ``basis="pnl"`` and must not be
compared with anything external.

**Kelly is reported alongside half-Kelly and a warning.** Full Kelly is the growth-
optimal fraction only if the estimated edge is exactly right, and it is never exactly
right. Estimation error makes full Kelly reliably over-aggressive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from app.analytics.drawdown import daily_pnl
from app.analytics.statistics import (
    PRECISION,
    ZERO,
    divide,
    downside_deviation,
    mean,
    sqrt,
    stdev,
)
from app.analytics.types import Estimate, TradeRecord

#: US futures trading days per year. Used to annualise a daily series.
TRADING_DAYS_PER_YEAR = 252

#: Minimum daily observations before an annualised ratio means anything. Annualising
#: from a handful of days multiplies noise by sqrt(252) and presents it as a year.
MIN_DAILY_OBSERVATIONS = 20


class RatioBasis(StrEnum):
    """What the ratio was computed on.

    ``RETURN`` is comparable with published figures. ``PNL`` is internally consistent
    but scale-dependent and must not be compared against anything external — which is
    exactly why the basis travels with the number instead of being assumed.
    """

    RETURN = "return"
    PNL = "pnl"


@dataclass(frozen=True, slots=True)
class RatioResult:
    value: Decimal | None
    basis: RatioBasis
    observations: int
    periods_per_year: int
    undefined_reason: str | None = None

    @property
    def is_comparable(self) -> bool:
        """Whether this figure can be compared with an externally published one."""
        return self.value is not None and self.basis is RatioBasis.RETURN


def _daily_returns(
    trades: Sequence[TradeRecord], starting_equity: Decimal | None
) -> tuple[list[Decimal], RatioBasis, dict[date, Decimal]]:
    """Build the daily series, as returns when equity is known and P&L otherwise."""
    series = daily_pnl(trades)
    if starting_equity is not None and starting_equity > 0:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return (
                [value / starting_equity for value in series.values()],
                RatioBasis.RETURN,
                series,
            )
    return list(series.values()), RatioBasis.PNL, series


def sharpe_ratio(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal | None = None,
    risk_free_rate: Decimal = ZERO,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> RatioResult:
    """Annualised Sharpe ratio on the daily series.

    Args:
        starting_equity: Account equity the returns are measured against. Without it
            the result is P&L-based and not externally comparable.
        risk_free_rate: Annual risk-free rate, de-annualised internally. Defaults to
            zero — for an intraday futures trader holding no overnight capital at risk,
            zero is the defensible assumption rather than a simplification.
    """
    returns, basis, _ = _daily_returns(trades, starting_equity)

    if len(returns) < MIN_DAILY_OBSERVATIONS:
        return RatioResult(
            value=None,
            basis=basis,
            observations=len(returns),
            periods_per_year=periods_per_year,
            undefined_reason=(
                f"only {len(returns)} trading days; annualising fewer than "
                f"{MIN_DAILY_OBSERVATIONS} multiplies noise rather than measuring return"
            ),
        )

    average = mean(returns)
    deviation = stdev(returns)
    if average is None or deviation is None:
        return RatioResult(None, basis, len(returns), periods_per_year, "insufficient data")
    if deviation == 0:
        return RatioResult(
            None,
            basis,
            len(returns),
            periods_per_year,
            "daily results have zero variance, so the ratio is undefined",
        )

    with localcontext() as ctx:
        ctx.prec = PRECISION
        daily_rf = risk_free_rate / Decimal(periods_per_year)
        excess = average - daily_rf
        annualised = (excess / deviation) * sqrt(Decimal(periods_per_year))
        return RatioResult(annualised, basis, len(returns), periods_per_year)


def sortino_ratio(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal | None = None,
    target: Decimal = ZERO,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> RatioResult:
    """Annualised Sortino ratio — Sharpe with only downside dispersion penalised.

    The more appropriate of the two for trading strategies. Sharpe treats an unusually
    large winning day as risk; Sortino does not, which is why a strategy with a few
    outsized winners can look mediocre by Sharpe and strong by Sortino. Both are
    reported so the gap between them is visible.
    """
    returns, basis, _ = _daily_returns(trades, starting_equity)

    if len(returns) < MIN_DAILY_OBSERVATIONS:
        return RatioResult(
            value=None,
            basis=basis,
            observations=len(returns),
            periods_per_year=periods_per_year,
            undefined_reason=f"only {len(returns)} trading days",
        )

    average = mean(returns)
    downside = downside_deviation(returns, target=target)
    if average is None or downside is None:
        return RatioResult(None, basis, len(returns), periods_per_year, "insufficient data")
    if downside == 0:
        return RatioResult(
            None,
            basis,
            len(returns),
            periods_per_year,
            "no day fell below the target, so downside deviation is zero and the "
            "ratio is undefined rather than infinite",
        )

    with localcontext() as ctx:
        ctx.prec = PRECISION
        annualised = ((average - target) / downside) * sqrt(Decimal(periods_per_year))
        return RatioResult(annualised, basis, len(returns), periods_per_year)


def mar_ratio(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal,
    max_drawdown: Decimal,
) -> Estimate:
    """Annualised return divided by maximum drawdown.

    Requires both an equity base and a real drawdown. Returns undefined rather than a
    flattering number when a sample has never drawn down — which is a statement about
    the sample's length, not about the strategy's safety.
    """
    series = daily_pnl(trades)
    if len(series) < MIN_DAILY_OBSERVATIONS:
        return Estimate.undefined(
            len(series), "too few trading days to annualise a return"
        )
    if starting_equity <= 0:
        return Estimate.undefined(len(series), "starting equity must be positive")
    if max_drawdown <= 0:
        return Estimate.undefined(
            len(series), "this sample has no drawdown, so the ratio is undefined"
        )

    with localcontext() as ctx:
        ctx.prec = PRECISION
        net = sum(series.values(), start=ZERO)
        years = Decimal(len(series)) / Decimal(TRADING_DAYS_PER_YEAR)
        if years <= 0:  # pragma: no cover — guarded above
            return Estimate.undefined(len(series), "zero elapsed time")
        annualised_return = (net / starting_equity) / years
        value = divide(annualised_return, max_drawdown / starting_equity)
        if value is None:  # pragma: no cover
            return Estimate.undefined(len(series), "drawdown is zero")
        return Estimate.of(value, len(series))


# --- Position sizing -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KellyResult:
    """Growth-optimal position size, and the reasons not to use it.

    Attributes:
        full: The Kelly fraction ``W - (1 - W) / R``.
        half: Half Kelly. The fraction most practitioners actually use, because it
            gives roughly three-quarters of the growth at half the volatility and is
            far more forgiving of an overestimated edge.
        warnings: Conditions that make the estimate unreliable.
    """

    full: Decimal | None
    half: Decimal | None
    win_rate: Decimal | None
    payoff_ratio: Decimal | None
    sample_size: int
    warnings: tuple[str, ...] = ()
    undefined_reason: str | None = None

    @property
    def is_negative_edge(self) -> bool:
        """A negative Kelly fraction means the strategy has no edge to size."""
        return self.full is not None and self.full <= 0


def kelly_fraction(trades: Sequence[TradeRecord]) -> KellyResult:
    """Kelly criterion from realized win rate and payoff ratio.

    ``f* = W - (1 - W) / R`` where ``W`` is win rate and ``R`` is average win over
    average loss.

    The formula assumes binary outcomes of fixed size, which trading is not: real
    results are a continuous distribution with fat tails, so this is an approximation
    that runs optimistic. It is reported with half-Kelly and explicit warnings rather
    than as a recommendation, and the simulation-based risk of ruin below is the more
    trustworthy guide to whether a size is survivable.
    """
    winners = [trade.net_pnl for trade in trades if trade.is_winner]
    losers = [trade.net_pnl for trade in trades if trade.is_loser]
    decided = len(winners) + len(losers)

    if decided == 0:
        return KellyResult(None, None, None, None, 0, undefined_reason="no decided trades")
    if not losers:
        return KellyResult(
            None,
            None,
            divide(Decimal(len(winners)), Decimal(decided)),
            None,
            decided,
            undefined_reason=(
                "this sample contains no losing trades, so the payoff ratio — and "
                "therefore Kelly — is undefined"
            ),
        )
    if not winners:
        return KellyResult(
            None, None, ZERO, None, decided, undefined_reason="no winning trades"
        )

    average_win = mean(winners)
    average_loss = mean(losers)
    if average_win is None or average_loss is None or average_loss == 0:
        return KellyResult(None, None, None, None, decided, undefined_reason="insufficient data")

    with localcontext() as ctx:
        ctx.prec = PRECISION
        win_rate = Decimal(len(winners)) / Decimal(decided)
        payoff = average_win / abs(average_loss)
        full = win_rate - (Decimal(1) - win_rate) / payoff
        half = full / Decimal(2)

    warnings: list[str] = []
    if decided < 100:
        warnings.append(
            f"estimated from {decided} trades; Kelly is highly sensitive to estimation "
            "error and needs several hundred to be stable"
        )
    if full > Decimal("0.25"):
        warnings.append(
            "a Kelly fraction above 25% almost always reflects an overestimated edge "
            "rather than an opportunity"
        )
    if full <= 0:
        warnings.append("negative Kelly: this sample shows no edge to size")

    return KellyResult(
        full=full,
        half=half,
        win_rate=win_rate,
        payoff_ratio=payoff,
        sample_size=decided,
        warnings=tuple(warnings),
    )


__all__ = [
    "MIN_DAILY_OBSERVATIONS",
    "TRADING_DAYS_PER_YEAR",
    "KellyResult",
    "RatioBasis",
    "RatioResult",
    "kelly_fraction",
    "mar_ratio",
    "sharpe_ratio",
    "sortino_ratio",
]
