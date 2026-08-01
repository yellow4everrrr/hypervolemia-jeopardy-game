"""Tests for resampling: bootstrap intervals, Monte Carlo and risk of ruin.

Randomised code needs tests that assert *properties* rather than exact values, plus one
that pins reproducibility — because a confidence interval that moves between page loads
destroys trust in every number beside it.
"""

from __future__ import annotations

from decimal import Decimal

from app.analytics.montecarlo import (
    bootstrap_mean,
    bootstrap_statistic,
    expectancy_with_interval,
    from_scaled,
    monte_carlo,
    probability_of_profit,
    risk_of_ruin,
    to_scaled,
)
from app.analytics.statistics import mean
from app.analytics.types import TradeRecord
from tests.unit.analytics.conftest import make_trades

FAST = 500  # iterations; enough for property assertions, fast enough for a test suite


# --- Scaling --------------------------------------------------------------------


def test_scaling_round_trip_is_exact() -> None:
    """The optimisation must not cost precision.

    Values are scaled to integers at 10^-8 — the storage scale — so nothing is lost.
    """
    values = [Decimal("1234.56789012"), Decimal("-0.00000001"), Decimal("0")]
    assert [from_scaled(value) for value in to_scaled(values)] == values


# --- Bootstrap ------------------------------------------------------------------


def test_bootstrap_interval_brackets_the_point_estimate() -> None:
    values = [Decimal(value) for value in ["100", "-50", "200", "-50", "100", "75", "-25"]]
    result = bootstrap_mean(values, iterations=FAST)

    assert result is not None
    assert result.interval.low <= result.point_estimate <= result.interval.high


def test_bootstrap_point_estimate_equals_the_sample_mean() -> None:
    values = [Decimal(value) for value in ["100", "-50", "200", "-50", "100"]]
    result = bootstrap_mean(values, iterations=FAST)

    assert result is not None
    assert result.point_estimate == mean(values)


def test_bootstrap_is_reproducible() -> None:
    """Same trades, same seed, same interval — every time.

    A confidence interval that shifts between renders makes every number next to it
    look arbitrary.
    """
    values = [Decimal(value) for value in ["100", "-50", "200", "-50", "100", "75"]]
    first = bootstrap_mean(values, iterations=FAST, seed=99)
    second = bootstrap_mean(values, iterations=FAST, seed=99)

    assert first is not None and second is not None
    assert first.interval.low == second.interval.low
    assert first.interval.high == second.interval.high


def test_a_wider_sample_gives_a_wider_interval() -> None:
    """More dispersion means less certainty about the mean, and the interval says so."""
    tight = [Decimal(value) for value in ["10", "11", "9", "10", "11", "9", "10", "10"]]
    wide = [Decimal(value) for value in ["500", "-480", "490", "-470", "510", "-500", "20", "10"]]

    tight_result = bootstrap_mean(tight, iterations=FAST)
    wide_result = bootstrap_mean(wide, iterations=FAST)

    assert tight_result is not None and wide_result is not None
    assert wide_result.interval.width > tight_result.interval.width


def test_more_trades_narrow_the_interval() -> None:
    """The central promise of accumulating a track record."""
    pattern = ["100", "-50", "200", "-50", "100", "-75", "150", "-25"]
    small = [Decimal(value) for value in pattern]
    large = [Decimal(pattern[i % len(pattern)]) for i in range(400)]

    small_result = bootstrap_mean(small, iterations=FAST)
    large_result = bootstrap_mean(large, iterations=FAST)

    assert small_result is not None and large_result is not None
    assert large_result.interval.width < small_result.interval.width


def test_a_strong_consistent_edge_produces_an_interval_above_zero() -> None:
    values = [Decimal(200) if index % 3 else Decimal(-50) for index in range(120)]
    result = bootstrap_mean(values, iterations=2_000)

    assert result is not None
    assert result.is_significantly_positive
    assert result.interval.excludes_zero


def test_a_marginal_sample_produces_an_interval_spanning_zero() -> None:
    """The honest answer for a small, noisy sample.

    This is the case a journal must not round up to "you have an edge".
    """
    values = [Decimal(value) for value in ["300", "-280", "250", "-240", "20", "-10"]]
    result = bootstrap_mean(values, iterations=2_000)

    assert result is not None
    assert not result.interval.excludes_zero
    assert not result.is_significantly_positive


def test_bootstrap_needs_a_minimum_sample() -> None:
    assert bootstrap_mean([Decimal(1), Decimal(2)], iterations=FAST) is None


def test_bootstrap_statistic_handles_an_arbitrary_function() -> None:
    values = [Decimal(value) for value in ["100", "-50", "200", "-50", "100", "80"]]
    interval = bootstrap_statistic(values, mean, iterations=200)

    assert interval is not None
    assert interval.low < interval.high


# --- Expectancy with interval ---------------------------------------------------


def test_expectancy_carries_its_interval(simple_trades: list[TradeRecord]) -> None:
    result = expectancy_with_interval(simple_trades, iterations=FAST)

    assert result.value == Decimal(60)
    assert result.interval is not None


def test_expectancy_reports_when_the_sample_is_too_small_for_an_interval() -> None:
    trades = make_trades(["100", "-50"])
    result = expectancy_with_interval(trades, iterations=FAST)

    assert result.value == Decimal(25)
    assert result.interval is None
    assert "too few to estimate a confidence interval" in (result.undefined_reason or "")


# --- Monte Carlo ----------------------------------------------------------------


def test_monte_carlo_percentiles_are_ordered(many_trades: list[TradeRecord]) -> None:
    result = monte_carlo(many_trades, iterations=FAST)

    assert result is not None
    percentiles = result.final_equity_percentiles
    assert percentiles["0.05"] <= percentiles["0.5"] <= percentiles["0.95"]


def test_monte_carlo_median_tracks_expectancy_times_path_length(
    many_trades: list[TradeRecord],
) -> None:
    """A sanity check that resampling preserves the underlying distribution.

    The pattern averages +50 a trade, so 100 trades should centre near +5,000.
    """
    result = monte_carlo(many_trades, iterations=2_000, trades_per_path=100)

    assert result is not None
    assert Decimal(3_500) < result.median_final_equity < Decimal(6_500)


def test_monte_carlo_is_reproducible(many_trades: list[TradeRecord]) -> None:
    first = monte_carlo(many_trades, iterations=FAST, seed=7)
    second = monte_carlo(many_trades, iterations=FAST, seed=7)

    assert first is not None and second is not None
    assert first.median_final_equity == second.median_final_equity


def test_a_losing_strategy_mostly_loses(losing_trades: list[TradeRecord]) -> None:
    result = monte_carlo(losing_trades, iterations=FAST, trades_per_path=100)

    assert result is not None
    assert result.probability_of_loss > Decimal("0.9")


def test_monte_carlo_needs_a_minimum_sample() -> None:
    assert monte_carlo(make_trades(["100", "-50"]), iterations=FAST) is None


# --- Risk of ruin ---------------------------------------------------------------


def test_risk_of_ruin_is_low_for_a_strong_edge_and_ample_capital(
    many_trades: list[TradeRecord],
) -> None:
    result = risk_of_ruin(
        many_trades,
        starting_equity=Decimal(100_000),
        horizon_trades=200,
        iterations=FAST,
    )
    assert result.value is not None
    assert result.value < Decimal("0.05")


def test_risk_of_ruin_is_high_for_a_losing_strategy_on_thin_capital(
    losing_trades: list[TradeRecord],
) -> None:
    result = risk_of_ruin(
        losing_trades,
        starting_equity=Decimal(500),
        horizon_trades=200,
        iterations=FAST,
    )
    assert result.value is not None
    assert result.value > Decimal("0.9")


def test_thinner_capital_means_more_ruin(many_trades: list[TradeRecord]) -> None:
    """The relationship that makes the statistic worth computing at all."""
    ample = risk_of_ruin(
        many_trades, starting_equity=Decimal(100_000), horizon_trades=200, iterations=FAST
    )
    thin = risk_of_ruin(
        many_trades, starting_equity=Decimal(400), horizon_trades=200, iterations=FAST
    )

    assert ample.value is not None and thin.value is not None
    assert thin.value > ample.value


def test_risk_of_ruin_needs_a_starting_equity() -> None:
    result = risk_of_ruin(
        make_trades(["100", "-50"]), starting_equity=Decimal(0), iterations=FAST
    )
    assert result.value is None


# --- Probability of profit ------------------------------------------------------


def test_probability_of_profit_for_a_winning_strategy(
    many_trades: list[TradeRecord],
) -> None:
    result = probability_of_profit(many_trades, horizon_trades=100)
    assert result.value is not None
    assert result.value > Decimal("0.8")


def test_probability_of_profit_for_a_losing_strategy(
    losing_trades: list[TradeRecord],
) -> None:
    result = probability_of_profit(losing_trades, horizon_trades=100)
    assert result.value is not None
    assert result.value < Decimal("0.2")
