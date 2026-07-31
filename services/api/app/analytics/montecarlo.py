"""Resampling: bootstrap confidence intervals, Monte Carlo, risk of ruin.

This module answers the only question that matters about any statistic in the package:
**could this have happened by chance?**

A trader with 60 trades and a $47 expectancy wants to know whether the strategy makes
money. The point estimate cannot say — it is one draw from a noisy process. The
bootstrap can: resample the trade history, recompute, and look at the spread. If the
95% interval on expectancy runs from −$12 to +$104, the honest answer is "this sample
cannot distinguish your strategy from breakeven", and that is what the AI layer must be
told.

**Non-parametric by choice.** Every interval here comes from resampling the actual
trades, never from a normal approximation. Trading returns are skewed and fat-tailed;
a normal-theory interval on a distribution with an occasional 8R winner is wrong in the
direction that matters, understating the range of outcomes.

**Scaled-integer arithmetic.** Resampling runs 10,000 iterations over the full sample,
which is where Decimal's precision would cost real time. Values are converted once to
integers at 10⁻⁸ scale, resampled with exact integer arithmetic, and converted back. No
precision is lost — 10⁻⁸ is the storage scale — and it runs roughly forty times faster.

**Seeded.** Every routine takes a seed and defaults to a fixed one, so the same trades
produce the same interval on every run. A confidence interval that shifts between page
loads destroys trust in every number next to it.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.analytics.statistics import PRECISION, ZERO
from app.analytics.types import MIN_SAMPLE, Estimate, Interval, TradeRecord

#: Scale factor matching the database's NUMERIC(20, 8). Exact, not lossy.
SCALE = 10**8
SCALE_DECIMAL = Decimal(SCALE)

#: Enough iterations that the interval bounds are stable to the cent; more mostly buys
#: decimal places nobody reads.
DEFAULT_ITERATIONS = 10_000

#: Fixed so results are reproducible across runs, processes and machines.
DEFAULT_SEED = 20260731


def to_scaled(values: Sequence[Decimal]) -> list[int]:
    """Convert decimals to exact integers at 10⁻⁸ scale."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return [int((value * SCALE_DECIMAL).to_integral_value()) for value in values]


def from_scaled(value: int | float) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal(int(value)) / SCALE_DECIMAL


def _percentile_int(ordered: Sequence[int], fraction: Decimal) -> int:
    """Type-7 percentile over pre-sorted integers."""
    if len(ordered) == 1:
        return ordered[0]
    position = float(fraction) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + weight * (ordered[upper] - ordered[lower]))


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Distribution of a statistic under resampling."""

    point_estimate: Decimal
    interval: Interval
    iterations: int
    sample_size: int
    #: Fraction of resamples where the statistic came out at or below zero. The
    #: one-sided p-value for "this strategy is not profitable".
    proportion_at_or_below_zero: Decimal
    #: Fraction at or **above** zero. Deliberately not ``1 - proportion_at_or_below_zero``:
    #: both tails include the resamples that landed exactly on zero, so the two sum to
    #: more than one whenever the distribution has an atom there.
    #:
    #: That atom is not a curiosity. A counterfactual scenario that repriced no trades
    #: produces a delta of exactly zero for every trade, so *every* resample is exactly
    #: zero, and ``1 - below`` is then 0 — which a two-sided formula reads as "no resample
    #: landed on the other side", the signature of an overwhelming effect. The scenario
    #: with the least possible evidence against the null scored the smallest possible
    #: p-value. Carrying both tails inclusively is what makes the degenerate case come
    #: out at 1 instead of at 1/iterations.
    proportion_at_or_above_zero: Decimal

    @property
    def is_significantly_positive(self) -> bool:
        """Whether the 95% interval sits entirely above zero."""
        return self.interval.low > 0

    @property
    def p_value_positive(self) -> Decimal:
        """One-sided p-value against the null that true expectancy is zero or worse."""
        return self.proportion_at_or_below_zero


def bootstrap_mean(
    values: Sequence[Decimal],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    confidence: Decimal = Decimal("0.95"),
    seed: int = DEFAULT_SEED,
) -> BootstrapResult | None:
    """Percentile bootstrap interval for the mean.

    Resamples with replacement — which is the whole point: it treats the observed
    trades as the best available estimate of the underlying distribution and asks what
    other trade histories that same process could plausibly have produced.
    """
    if len(values) < MIN_SAMPLE:
        return None

    scaled = to_scaled(values)
    count = len(scaled)
    rng = random.Random(seed)

    means: list[int] = []
    at_or_below_zero = 0
    at_or_above_zero = 0
    for _ in range(iterations):
        running = 0
        for _ in range(count):
            running += scaled[rng.randrange(count)]
        resampled_mean = running // count
        means.append(resampled_mean)
        # Both comparisons are inclusive, so a resample of exactly zero counts in each.
        # See the note on `proportion_at_or_above_zero`.
        if resampled_mean <= 0:
            at_or_below_zero += 1
        if resampled_mean >= 0:
            at_or_above_zero += 1

    means.sort()
    with localcontext() as ctx:
        ctx.prec = PRECISION
        tail = (Decimal(1) - confidence) / Decimal(2)
        low = _percentile_int(means, tail)
        high = _percentile_int(means, Decimal(1) - tail)
        point = Decimal(sum(scaled)) / Decimal(count) / SCALE_DECIMAL

    return BootstrapResult(
        point_estimate=point,
        interval=Interval(
            low=from_scaled(low),
            high=from_scaled(high),
            confidence=confidence,
            method="bootstrap_percentile",
            iterations=iterations,
        ),
        iterations=iterations,
        sample_size=count,
        proportion_at_or_below_zero=Decimal(at_or_below_zero) / Decimal(iterations),
        proportion_at_or_above_zero=Decimal(at_or_above_zero) / Decimal(iterations),
    )


def bootstrap_statistic(
    values: Sequence[Decimal],
    statistic: Callable[[Sequence[Decimal]], Decimal | None],
    *,
    iterations: int = 2_000,
    confidence: Decimal = Decimal("0.95"),
    seed: int = DEFAULT_SEED,
) -> Interval | None:
    """Bootstrap interval for an arbitrary statistic.

    Slower than :func:`bootstrap_mean` because the statistic is evaluated in Decimal on
    every iteration, hence the lower default iteration count. Used for profit factor,
    win rate and anything else that is not a simple mean.
    """
    if len(values) < MIN_SAMPLE:
        return None

    rng = random.Random(seed)
    count = len(values)
    results: list[Decimal] = []

    for _ in range(iterations):
        sample = [values[rng.randrange(count)] for _ in range(count)]
        computed = statistic(sample)
        if computed is not None:
            results.append(computed)

    if len(results) < iterations // 2:
        # More than half the resamples left the statistic undefined; an interval built
        # on the remainder would describe a different population than the one asked about.
        return None

    results.sort()
    scaled = to_scaled(results)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        tail = (Decimal(1) - confidence) / Decimal(2)
        return Interval(
            low=from_scaled(_percentile_int(scaled, tail)),
            high=from_scaled(_percentile_int(scaled, Decimal(1) - tail)),
            confidence=confidence,
            method="bootstrap_percentile",
            iterations=iterations,
        )


def expectancy_with_interval(
    trades: Sequence[TradeRecord],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> Estimate:
    """Expectancy with a bootstrap confidence interval attached.

    The form every expectancy figure should be reported in. A point estimate alone
    invites a trader to plan around a number the data does not support.
    """
    values = [trade.net_pnl for trade in trades]
    if not values:
        return Estimate.undefined(0, "no trades in the sample")

    result = bootstrap_mean(values, iterations=iterations, seed=seed)
    if result is None:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            point = sum(values, start=ZERO) / Decimal(len(values))
        return Estimate(
            value=point,
            sample_size=len(values),
            interval=None,
            reliability=Estimate.undefined(len(values), "").reliability,
            undefined_reason=(
                f"only {len(values)} trades; too few to estimate a confidence interval"
            ),
        )
    return Estimate.of(result.point_estimate, len(values), result.interval)


# --- Monte Carlo simulation ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    """Distribution of outcomes over many simulated trade sequences.

    Where the bootstrap asks "what is the true expectancy?", this asks "what could the
    next N trades actually look like?" — including the sequences that end the account.
    Same edge, different question, and the second is the one that determines position
    size.
    """

    iterations: int
    trades_per_path: int
    sample_size: int
    final_equity_percentiles: dict[str, Decimal]
    max_drawdown_percentiles: dict[str, Decimal]
    probability_of_loss: Decimal
    probability_of_ruin: Decimal | None
    ruin_threshold: Decimal | None
    median_final_equity: Decimal
    worst_final_equity: Decimal
    best_final_equity: Decimal

    @property
    def is_survivable(self) -> bool | None:
        """Ruin probability under 1% — the conventional threshold for an acceptable risk."""
        if self.probability_of_ruin is None:
            return None
        return self.probability_of_ruin < Decimal("0.01")


PERCENTILE_KEYS = ("0.05", "0.25", "0.5", "0.75", "0.95")


def monte_carlo(
    trades: Sequence[TradeRecord],
    *,
    iterations: int = 5_000,
    trades_per_path: int | None = None,
    starting_equity: Decimal | None = None,
    ruin_fraction: Decimal = Decimal("0.5"),
    seed: int = DEFAULT_SEED,
) -> MonteCarloResult | None:
    """Simulate many possible futures by resampling the trade history.

    Args:
        trades_per_path: Trades in each simulated path. Defaults to the sample size, so
            the question is "what might another run of this many trades look like?".
        starting_equity: Enables risk-of-ruin estimation. Without it, drawdown
            percentiles are still produced but ruin has no meaning.
        ruin_fraction: Fraction of starting equity whose loss counts as ruin. Defaults
            to 50% — the point at which most prop accounts are closed and most traders
            stop, rather than the theoretical zero that never occurs in practice.

    Resampling **with replacement** deliberately destroys sequence: it assumes trades
    are independent draws. That assumption is not exactly true — losing streaks cluster
    through tilt and regime — so the ruin estimate is, if anything, optimistic. That is
    stated rather than corrected, because correcting it would need a model of tilt this
    package does not have.
    """
    values = [trade.net_pnl for trade in trades]
    if len(values) < MIN_SAMPLE:
        return None

    scaled = to_scaled(values)
    count = len(scaled)
    path_length = trades_per_path or count
    rng = random.Random(seed)

    ruin_level: int | None = None
    if starting_equity is not None and starting_equity > 0:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            ruin_level = -int(starting_equity * ruin_fraction * SCALE_DECIMAL)

    finals: list[int] = []
    drawdowns: list[int] = []
    losing_paths = 0
    ruined_paths = 0

    for _ in range(iterations):
        equity = 0
        peak = 0
        worst = 0
        ruined = False

        for _ in range(path_length):
            equity += scaled[rng.randrange(count)]
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
            if ruin_level is not None and equity <= ruin_level:
                ruined = True
                break

        finals.append(equity)
        drawdowns.append(-worst)
        if equity < 0:
            losing_paths += 1
        if ruined:
            ruined_paths += 1

    finals.sort()
    drawdowns.sort()

    with localcontext() as ctx:
        ctx.prec = PRECISION
        return MonteCarloResult(
            iterations=iterations,
            trades_per_path=path_length,
            sample_size=count,
            final_equity_percentiles={
                key: from_scaled(_percentile_int(finals, Decimal(key)))
                for key in PERCENTILE_KEYS
            },
            max_drawdown_percentiles={
                key: from_scaled(_percentile_int(drawdowns, Decimal(key)))
                for key in PERCENTILE_KEYS
            },
            probability_of_loss=Decimal(losing_paths) / Decimal(iterations),
            probability_of_ruin=(
                Decimal(ruined_paths) / Decimal(iterations) if ruin_level is not None else None
            ),
            ruin_threshold=(
                starting_equity * ruin_fraction if starting_equity is not None else None
            ),
            median_final_equity=from_scaled(_percentile_int(finals, Decimal("0.5"))),
            worst_final_equity=from_scaled(finals[0]),
            best_final_equity=from_scaled(finals[-1]),
        )


def risk_of_ruin(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal,
    ruin_fraction: Decimal = Decimal("0.5"),
    horizon_trades: int = 1_000,
    iterations: int = 5_000,
    seed: int = DEFAULT_SEED,
) -> Estimate:
    """Probability of losing ``ruin_fraction`` of the account within ``horizon_trades``.

    Simulation-based rather than closed-form. The textbook risk-of-ruin formula assumes
    fixed-size binary outcomes; real trade distributions are continuous with fat tails,
    and it is exactly the tail the formula assumes away that causes ruin. Resampling
    the trader's own results keeps the real distribution, outliers included.
    """
    result = monte_carlo(
        trades,
        iterations=iterations,
        trades_per_path=horizon_trades,
        starting_equity=starting_equity,
        ruin_fraction=ruin_fraction,
        seed=seed,
    )
    if result is None or result.probability_of_ruin is None:
        return Estimate.undefined(
            len(trades), f"need at least {MIN_SAMPLE} trades and a positive starting equity"
        )
    return Estimate.of(result.probability_of_ruin, len(trades))


def probability_of_profit(
    trades: Sequence[TradeRecord], *, horizon_trades: int = 100, seed: int = DEFAULT_SEED
) -> Estimate:
    """Probability the next ``horizon_trades`` finish positive.

    A more intuitive framing of edge than expectancy for most traders: "you make money
    over the next hundred trades about seven times in ten" lands where "$47 expectancy"
    does not.
    """
    result = monte_carlo(trades, trades_per_path=horizon_trades, seed=seed)
    if result is None:
        return Estimate.undefined(len(trades), f"need at least {MIN_SAMPLE} trades")
    value = Decimal(1) - result.probability_of_loss
    return Estimate.of(value, len(trades))
