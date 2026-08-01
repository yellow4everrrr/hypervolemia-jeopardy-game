"""Statistical primitives, in exact decimal arithmetic.

Everything here operates on :class:`~decimal.Decimal` and stays there. That is unusual
for a statistics module and worth justifying: the alternative is converting to ``float``
for anything involving a square root, which reintroduces — inside the very code that
decides whether a trader has an edge — the imprecision the rest of the system is built
to avoid. Python's ``decimal`` module provides ``sqrt``, ``ln`` and ``exp`` at
arbitrary precision, so the conversion is unnecessary.

The cost is speed. A bootstrap over 10,000 iterations of Decimal arithmetic is roughly
an order of magnitude slower than the NumPy equivalent. Milestone 3 accepts that: the
resampling routines in ``montecarlo.py`` sidestep it by working in scaled integers, and
if a hot path ever needs NumPy it can be swapped in behind these same signatures.

**Sample vs population.** Standard deviation uses the sample convention (Bessel's
correction, ``n - 1``). A trader's trade history is a sample of the process that
generated it, not the population of every trade they will ever take, and the population
formula understates dispersion — which flatters Sharpe and every other ratio built on
it.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from decimal import Context, Decimal, localcontext

#: Working precision. Generous enough that the accumulated error over a million-trade
#: sum stays far below the smallest amount anyone reports.
PRECISION = 34

ZERO = Decimal(0)
ONE = Decimal(1)


def _ctx() -> AbstractContextManager[Context]:
    """Arithmetic at :data:`PRECISION`, scoped so it cannot leak into caller context."""
    return localcontext()


def total(values: Sequence[Decimal]) -> Decimal:
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return sum(values, start=ZERO)


def mean(values: Sequence[Decimal]) -> Decimal | None:
    """Arithmetic mean, or ``None`` for an empty sample."""
    if not values:
        return None
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return sum(values, start=ZERO) / Decimal(len(values))


def median(values: Sequence[Decimal]) -> Decimal | None:
    """Median.

    Reported alongside the mean everywhere, because trading distributions are skewed:
    one outsized winner drags the mean far above what a typical trade looks like, and a
    trader planning around "my average winner" needs to know which of the two they are
    being shown.
    """
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint]
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def variance(values: Sequence[Decimal], sample: bool = True) -> Decimal | None:
    """Variance. ``None`` when the sample is too small to have one."""
    count = len(values)
    divisor = count - 1 if sample else count
    if divisor <= 0:
        return None

    average = mean(values)
    if average is None:  # pragma: no cover — guarded by the divisor check
        return None

    with _ctx() as ctx:
        ctx.prec = PRECISION
        squares = sum(((value - average) ** 2 for value in values), start=ZERO)
        return squares / Decimal(divisor)


def stdev(values: Sequence[Decimal], sample: bool = True) -> Decimal | None:
    """Standard deviation in exact decimal arithmetic."""
    var = variance(values, sample=sample)
    if var is None:
        return None
    if var == 0:
        return ZERO
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return var.sqrt()


def downside_deviation(
    values: Sequence[Decimal], target: Decimal = ZERO, sample: bool = True
) -> Decimal | None:
    """Deviation of returns *below* a target — the denominator of Sortino.

    Differs from standard deviation in a way that matters: upside dispersion is not
    risk. A strategy whose winners vary wildly and whose losers are uniform is penalised
    by Sharpe and correctly rewarded by Sortino.

    Deviations are summed over the full sample count, not just the below-target
    observations. That is the standard construction: dividing by the count of losers
    would make a strategy look better the fewer losses it had, which inverts the meaning.
    """
    count = len(values)
    divisor = count - 1 if sample else count
    if divisor <= 0:
        return None

    with _ctx() as ctx:
        ctx.prec = PRECISION
        shortfalls = sum(
            ((value - target) ** 2 for value in values if value < target), start=ZERO
        )
        if shortfalls == 0:
            return ZERO
        return (shortfalls / Decimal(divisor)).sqrt()


def percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    """Linear-interpolated percentile (the type-7 definition used by NumPy and R).

    Interpolating rather than picking the nearest observation matters for small samples,
    where the difference between the 5th percentile of 40 trades under the two
    conventions can be an entire trade's worth of P&L.
    """
    if not values:
        return None
    if not (ZERO <= fraction <= ONE):
        raise ValueError("fraction must be between 0 and 1")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    with _ctx() as ctx:
        ctx.prec = PRECISION
        position = fraction * Decimal(len(ordered) - 1)
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(ordered) - 1)
        weight = position - Decimal(lower_index)
        return ordered[lower_index] + weight * (ordered[upper_index] - ordered[lower_index])


def quantiles(values: Sequence[Decimal], fractions: Sequence[str]) -> dict[str, Decimal]:
    """Several percentiles at once, keyed by their fraction as a string."""
    return {
        fraction: value
        for fraction in fractions
        if (value := percentile(values, Decimal(fraction))) is not None
    }


def skewness(values: Sequence[Decimal]) -> Decimal | None:
    """Sample skewness (Fisher–Pearson, adjusted).

    Trading distributions are rarely symmetric, and the direction of the asymmetry says
    what kind of strategy is being run: trend following is positively skewed (many small
    losses, rare large wins), premium selling negatively skewed (many small wins, rare
    large losses). Reporting Sharpe without skew hides the difference between them.
    """
    count = len(values)
    if count < 3:
        return None
    deviation = stdev(values)
    average = mean(values)
    if deviation is None or average is None or deviation == 0:
        return None

    with _ctx() as ctx:
        ctx.prec = PRECISION
        cubes = sum((((value - average) / deviation) ** 3 for value in values), start=ZERO)
        n = Decimal(count)
        return (n / ((n - 1) * (n - 2))) * cubes


def kurtosis(values: Sequence[Decimal]) -> Decimal | None:
    """Excess kurtosis (population form, normal = 0).

    High excess kurtosis means the tails are fatter than a normal distribution —
    which is where risk of ruin actually lives, and precisely what a Sharpe ratio
    assumes away.
    """
    count = len(values)
    if count < 4:
        return None
    deviation = stdev(values, sample=False)
    average = mean(values)
    if deviation is None or average is None or deviation == 0:
        return None

    with _ctx() as ctx:
        ctx.prec = PRECISION
        fourth = sum((((value - average) / deviation) ** 4 for value in values), start=ZERO)
        return fourth / Decimal(count) - Decimal(3)


def sqrt(value: Decimal) -> Decimal:
    """Square root at working precision. Raises on a negative input."""
    if value < 0:
        raise ValueError("cannot take the square root of a negative value")
    if value == 0:
        return ZERO
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return value.sqrt()


def divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Division that returns ``None`` rather than raising or producing infinity."""
    if denominator == 0:
        return None
    with _ctx() as ctx:
        ctx.prec = PRECISION
        return numerator / denominator


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))
