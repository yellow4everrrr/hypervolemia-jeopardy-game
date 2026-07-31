"""Core performance statistics: counts, expectancy, profit factor, SQN.

These are the numbers a trader checks first, and the ones most often computed in a way
that flatters. Three conventions applied throughout, each of which changes the answer:

**Everything is net.** Gross P&L makes every strategy look viable. The gap between
gross and net is where overtrading hides, and on a one-tick scalp it is the whole
result. Gross is reported only so that gap can be quantified.

**Scratches are their own category.** A break-even trade is not a win and not a loss.
Folding them into either shifts the win rate, and the choice of which way to fold is
usually made by whichever makes the number look better.

**Win rate is not an edge.** It appears here because traders ask for it, but it is
reported next to expectancy and payoff ratio, because a 70% win rate with a 0.3 payoff
ratio is a losing strategy and the win rate alone will not say so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.analytics.statistics import (
    PRECISION,
    ZERO,
    divide,
    kurtosis,
    mean,
    median,
    percentile,
    skewness,
    sqrt,
    stdev,
    total,
)
from app.analytics.types import Estimate, Reliability, TradeRecord

#: Van Tharp caps the sample multiplier in System Quality Number at 100. Without the cap
#: SQN grows without bound as trades accumulate, which turns a quality measure into a
#: trade-count measure.
SQN_SAMPLE_CAP = 100


@dataclass(frozen=True, slots=True)
class TradeCounts:
    total: int
    winners: int
    losers: int
    scratches: int

    @property
    def decided(self) -> int:
        """Trades that were either a win or a loss. The denominator for win rate."""
        return self.winners + self.losers

    @property
    def win_rate(self) -> Decimal | None:
        """Fraction of *decided* trades that won.

        Scratches are excluded from the denominator rather than counted as losses. A
        trader who scratches out of ten trades has not lost ten times, and treating
        them as losses would understate the win rate of anyone disciplined about
        cutting a trade that stops working.
        """
        return divide(Decimal(self.winners), Decimal(self.decided))

    @property
    def loss_rate(self) -> Decimal | None:
        return divide(Decimal(self.losers), Decimal(self.decided))


def count_trades(trades: Sequence[TradeRecord]) -> TradeCounts:
    winners = sum(1 for trade in trades if trade.is_winner)
    losers = sum(1 for trade in trades if trade.is_loser)
    return TradeCounts(
        total=len(trades),
        winners=winners,
        losers=losers,
        scratches=len(trades) - winners - losers,
    )


@dataclass(frozen=True, slots=True)
class PnlSummary:
    """The money view of a sample."""

    net_pnl: Decimal
    gross_pnl: Decimal
    commission: Decimal
    fees: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    largest_win: Decimal | None
    largest_loss: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    median_win: Decimal | None
    median_loss: Decimal | None

    @property
    def total_costs(self) -> Decimal:
        return self.commission + self.fees

    @property
    def cost_ratio(self) -> Decimal | None:
        """Costs as a fraction of gross profit.

        The number that answers "am I trading too much?" more directly than trade count
        does. A strategy handing 40% of its gross to commissions has a frequency
        problem, not a strategy problem.
        """
        return divide(self.total_costs, self.gross_profit)

    @property
    def profit_factor(self) -> Decimal | None:
        """Gross profit divided by gross loss.

        ``None`` when there are no losses — that is *undefined*, not infinite. A sample
        with no losing trades tells you the sample is too small or the strategy has not
        met its bad regime yet; reporting infinity would let it read as an achievement.
        """
        return divide(self.gross_profit, self.gross_loss)

    @property
    def payoff_ratio(self) -> Decimal | None:
        """Average win divided by average loss. The other half of the win-rate story."""
        if self.average_win is None or self.average_loss is None:
            return None
        return divide(self.average_win, abs(self.average_loss))


def summarise_pnl(trades: Sequence[TradeRecord]) -> PnlSummary:
    wins = [trade.net_pnl for trade in trades if trade.is_winner]
    losses = [trade.net_pnl for trade in trades if trade.is_loser]
    all_pnl = [trade.net_pnl for trade in trades]

    return PnlSummary(
        net_pnl=total(all_pnl),
        gross_pnl=total([trade.gross_pnl for trade in trades]),
        commission=total([trade.commission for trade in trades]),
        fees=total([trade.fees for trade in trades]),
        gross_profit=total(wins),
        gross_loss=abs(total(losses)),
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        average_win=mean(wins),
        average_loss=mean(losses),
        median_win=median(wins),
        median_loss=median(losses),
    )


def expectancy(trades: Sequence[TradeRecord]) -> Estimate:
    """Mean net P&L per trade — what one trade is worth on average.

    The single most useful number in the journal, because it multiplies: expectancy
    times trade frequency is the strategy's earning rate, and it makes the comparison
    between a rare high-conviction setup and a frequent scalp meaningful.
    """
    if not trades:
        return Estimate.undefined(0, "no trades in the sample")
    value = mean([trade.net_pnl for trade in trades])
    if value is None:  # pragma: no cover — guarded above
        return Estimate.undefined(0, "no trades in the sample")
    return Estimate.of(value, len(trades))


def expectancy_r(trades: Sequence[TradeRecord]) -> Estimate:
    """Mean R-multiple — expectancy in units of risk.

    Preferred to dollar expectancy for anything comparative: R is invariant to position
    size and instrument, so a 0.2R edge on ES and on CL are the same edge, while their
    dollar expectancies differ by an order of magnitude.

    Trades without a recorded stop have no R and are **excluded**, not counted as zero.
    The sample size reported is the number that actually had one, so a strategy where
    only a third of trades recorded a stop cannot masquerade as a full sample.
    """
    values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    if not values:
        return Estimate.undefined(
            0, "no trades in this sample have a recorded initial stop, so R is undefined"
        )
    value = mean(values)
    if value is None:  # pragma: no cover
        return Estimate.undefined(0, "no R-multiples available")
    return Estimate.of(value, len(values))


def system_quality_number(trades: Sequence[TradeRecord]) -> Estimate:
    """Van Tharp's SQN: ``sqrt(min(N, 100)) x mean(R) / stdev(R)``.

    A signal-to-noise measure of the strategy rather than of the account: it rewards
    consistency of R and penalises dispersion. The sample multiplier is capped at 100
    because otherwise SQN rises with trade count alone, and a mediocre system traded
    often would outscore a good one traded rarely.
    """
    values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    if len(values) < 2:
        return Estimate.undefined(
            len(values), "SQN needs at least two trades with a recorded initial stop"
        )

    average = mean(values)
    deviation = stdev(values)
    if average is None or deviation is None:
        return Estimate.undefined(len(values), "insufficient data for SQN")
    if deviation == 0:
        return Estimate.undefined(
            len(values), "every trade returned exactly the same R; dispersion is zero"
        )

    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = PRECISION
        multiplier = sqrt(Decimal(min(len(values), SQN_SAMPLE_CAP)))
        return Estimate.of(multiplier * average / deviation, len(values))


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    """Shape of the P&L or R distribution.

    Reported because the ratios everyone quotes — Sharpe above all — assume a shape
    that trading results do not have. Skew and kurtosis are how that assumption is made
    visible rather than silently relied upon.
    """

    sample_size: int
    mean: Decimal | None
    median: Decimal | None
    stdev: Decimal | None
    skewness: Decimal | None
    excess_kurtosis: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    percentiles: dict[str, Decimal]

    @property
    def is_positively_skewed(self) -> bool | None:
        if self.skewness is None:
            return None
        return self.skewness > 0

    @property
    def has_fat_tails(self) -> bool | None:
        """Excess kurtosis above 1 — materially fatter tails than a normal distribution.

        When true, risk-of-ruin estimates built on a normal assumption understate the
        danger, and this package's simulation-based estimate should be preferred.
        """
        if self.excess_kurtosis is None:
            return None
        return self.excess_kurtosis > 1


DEFAULT_PERCENTILES = ("0.05", "0.25", "0.5", "0.75", "0.95")


def describe(values: Sequence[Decimal]) -> DistributionSummary:
    """Full shape summary of a numeric sample."""
    return DistributionSummary(
        sample_size=len(values),
        mean=mean(values),
        median=median(values),
        stdev=stdev(values),
        skewness=skewness(values),
        excess_kurtosis=kurtosis(values),
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        percentiles={
            fraction: value
            for fraction in DEFAULT_PERCENTILES
            if (value := percentile(values, Decimal(fraction))) is not None
        },
    )


def average_hold_time(trades: Sequence[TradeRecord]) -> Estimate:
    """Mean holding period in seconds, over trades that have closed."""
    durations = [
        Decimal(trade.duration_seconds)
        for trade in trades
        if trade.duration_seconds is not None
    ]
    if not durations:
        return Estimate.undefined(0, "no closed trades with a recorded duration")
    value = mean(durations)
    if value is None:  # pragma: no cover
        return Estimate.undefined(0, "no durations available")
    return Estimate.of(value, len(durations))


def hold_time_by_outcome(trades: Sequence[TradeRecord]) -> dict[str, Estimate]:
    """Holding period split by winners and losers.

    One of the most reliably diagnostic comparisons in a journal. Losers held markedly
    longer than winners is the signature of cutting winners early and letting losers
    run — the single most common way a positive-expectancy entry is turned into a
    losing strategy by its management.
    """
    winners = [t for t in trades if t.is_winner]
    losers = [t for t in trades if t.is_loser]
    return {
        "winners": average_hold_time(winners),
        "losers": average_hold_time(losers),
    }


def edge_ratio(trades: Sequence[TradeRecord]) -> Estimate:
    """Mean MFE divided by mean MAE, in R.

    Tharp's E-ratio. Measures the *entry* in isolation: durably above 1.0 means price
    moved further in favour after entry than against it, which is evidence the entry
    has an edge independent of how the trade was managed. Below 1.0 with positive
    expectancy means the money is coming from management, not from entry timing — a
    materially different thing to know.

    Requires excursion data from the market-data engine; undefined until milestone 4
    populates it.
    """
    mfe = [t.mfe_r for t in trades if t.mfe_r is not None]
    mae = [t.mae_r for t in trades if t.mae_r is not None]
    if not mfe or not mae:
        return Estimate.undefined(
            0, "excursion data is not available for this sample (requires bar data)"
        )

    mean_mfe, mean_mae = mean(mfe), mean(mae)
    if mean_mfe is None or mean_mae is None or mean_mae == 0:
        return Estimate.undefined(
            len(mfe), "no adverse excursion recorded, so the ratio is undefined"
        )
    value = divide(mean_mfe, mean_mae)
    if value is None:  # pragma: no cover
        return Estimate.undefined(len(mfe), "adverse excursion is zero")
    return Estimate.of(value, min(len(mfe), len(mae)))


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """Everything in this module, computed once over one sample."""

    counts: TradeCounts
    pnl: PnlSummary
    expectancy: Estimate
    expectancy_r: Estimate
    sqn: Estimate
    pnl_distribution: DistributionSummary
    r_distribution: DistributionSummary
    hold_time: Estimate
    hold_time_by_outcome: dict[str, Estimate]
    edge_ratio: Estimate

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(self.counts.total)


def summarise(trades: Sequence[TradeRecord]) -> PerformanceSummary:
    """Compute the full core metric set for a sample of trades."""
    r_values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    return PerformanceSummary(
        counts=count_trades(trades),
        pnl=summarise_pnl(trades),
        expectancy=expectancy(trades),
        expectancy_r=expectancy_r(trades),
        sqn=system_quality_number(trades),
        pnl_distribution=describe([trade.net_pnl for trade in trades]),
        r_distribution=describe(r_values),
        hold_time=average_hold_time(trades),
        hold_time_by_outcome=hold_time_by_outcome(trades),
        edge_ratio=edge_ratio(trades),
    )


__all__ = [
    "ZERO",
    "DistributionSummary",
    "PerformanceSummary",
    "PnlSummary",
    "TradeCounts",
    "average_hold_time",
    "count_trades",
    "describe",
    "edge_ratio",
    "expectancy",
    "expectancy_r",
    "hold_time_by_outcome",
    "summarise",
    "summarise_pnl",
    "system_quality_number",
]
