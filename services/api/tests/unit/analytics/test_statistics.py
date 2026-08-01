"""Tests for the statistical primitives.

Expected values are computed by hand or against values a reader can verify without
running the code. Where a convention is contested — sample vs population variance, the
percentile definition, the denominator of downside deviation — there is a test pinning
which one this package uses, because switching silently would change every ratio built
on top.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.statistics import (
    divide,
    downside_deviation,
    kurtosis,
    mean,
    median,
    percentile,
    skewness,
    sqrt,
    stdev,
    total,
    variance,
)


def D(values: list[str]) -> list[Decimal]:  # noqa: N802 — reads better than `decimals`
    return [Decimal(value) for value in values]


# --- Central tendency -----------------------------------------------------------


def test_mean() -> None:
    assert mean(D(["1", "2", "3", "4"])) == Decimal("2.5")


def test_mean_of_empty_is_none() -> None:
    assert mean([]) is None


def test_mean_is_exact_where_float_would_drift() -> None:
    """The reason this module is in Decimal at all.

    Ten values of 0.1 average to exactly 0.1. In binary floating point the sum alone is
    0.9999999999999999, and every ratio computed from it inherits the error.
    """
    assert mean(D(["0.1"] * 10)) == Decimal("0.1")
    assert total(D(["0.1"] * 10)) == Decimal("1.0")


def test_median_odd_and_even() -> None:
    assert median(D(["3", "1", "2"])) == Decimal("2")
    assert median(D(["4", "1", "3", "2"])) == Decimal("2.5")


def test_median_differs_from_mean_on_skewed_data() -> None:
    """Why both are always reported: one outsized winner moves only one of them."""
    values = D(["10", "10", "10", "10", "1000"])
    assert mean(values) == Decimal("208")
    assert median(values) == Decimal("10")


# --- Dispersion -----------------------------------------------------------------


def test_variance_uses_the_sample_convention() -> None:
    """Bessel's correction, n-1.

    For [2, 4, 4, 4, 5, 5, 7, 9]: mean 5, squared deviations sum to 32.
    Sample variance 32/7 ≈ 4.571; population variance would be 32/8 = 4.
    The population form understates dispersion and flatters every ratio built on it.
    """
    values = D(["2", "4", "4", "4", "5", "5", "7", "9"])
    sample = variance(values)
    population = variance(values, sample=False)

    assert sample is not None and population is not None
    assert population == Decimal(4)
    assert sample > population
    assert round(sample, 3) == Decimal("4.571")


def test_stdev_is_the_root_of_variance() -> None:
    values = D(["2", "4", "4", "4", "5", "5", "7", "9"])
    assert stdev(values, sample=False) == Decimal(2)


def test_stdev_of_identical_values_is_zero() -> None:
    assert stdev(D(["5", "5", "5", "5"])) == Decimal(0)


def test_stdev_needs_two_observations() -> None:
    assert stdev(D(["5"])) is None
    assert variance([]) is None


def test_downside_deviation_ignores_upside() -> None:
    """The distinction that makes Sortino different from Sharpe.

    Two samples with identical downside and wildly different upside must produce the
    same downside deviation — an unusually large winner is not risk.
    """
    modest_upside = D(["-10", "5", "-10", "5"])
    huge_upside = D(["-10", "500", "-10", "500"])

    assert downside_deviation(modest_upside) == downside_deviation(huge_upside)
    assert stdev(modest_upside) != stdev(huge_upside)


def test_downside_deviation_divides_by_the_full_sample() -> None:
    """Not by the count of losers.

    Dividing by the number of below-target observations would make a strategy look
    better the fewer losses it had, inverting the meaning of the statistic.
    """
    values = D(["-10", "0", "0", "0"])
    # One shortfall of 10, squared = 100, divided by n-1 = 3 → sqrt(33.33) ≈ 5.7735
    result = downside_deviation(values)
    assert result is not None
    assert round(result, 4) == Decimal("5.7735")


def test_downside_deviation_is_zero_when_nothing_falls_below_target() -> None:
    assert downside_deviation(D(["1", "2", "3"])) == Decimal(0)


# --- Percentiles ----------------------------------------------------------------


def test_percentile_interpolates() -> None:
    """Type-7, matching NumPy and R.

    For [1, 2, 3, 4] the 50th percentile is at position 0.5 x 3 = 1.5, i.e. halfway
    between 2 and 3.
    """
    values = D(["1", "2", "3", "4"])
    assert percentile(values, Decimal("0.5")) == Decimal("2.5")
    assert percentile(values, Decimal("0")) == Decimal("1")
    assert percentile(values, Decimal("1")) == Decimal("4")


def test_percentile_of_single_value() -> None:
    assert percentile(D(["7"]), Decimal("0.25")) == Decimal("7")


def test_percentile_rejects_a_fraction_outside_zero_to_one() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile(D(["1", "2"]), Decimal("1.5"))


def test_percentile_of_empty_is_none() -> None:
    assert percentile([], Decimal("0.5")) is None


# --- Shape ----------------------------------------------------------------------


def test_skewness_sign_matches_the_tail() -> None:
    """Positive skew: many small losses, rare large wins — trend following.

    Negative skew: many small wins, rare large losses — premium selling. A journal that
    reports only Sharpe cannot tell these apart, and they carry very different ruin risk.
    """
    trend_following = D(["-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1", "-1", "20"])
    premium_selling = D(["1", "1", "1", "1", "1", "1", "1", "1", "1", "-20"])

    positive = skewness(trend_following)
    negative = skewness(premium_selling)

    assert positive is not None and positive > 0
    assert negative is not None and negative < 0


def test_skewness_of_symmetric_data_is_zero() -> None:
    result = skewness(D(["-2", "-1", "0", "1", "2"]))
    assert result is not None
    assert abs(result) < Decimal("0.0001")


def test_skewness_needs_three_observations() -> None:
    assert skewness(D(["1", "2"])) is None


def test_kurtosis_detects_fat_tails() -> None:
    """Excess kurtosis is where risk of ruin actually lives."""
    fat_tailed = D(["0"] * 20 + ["-50", "50"])
    result = kurtosis(fat_tailed)
    assert result is not None
    assert result > 1


def test_kurtosis_needs_four_observations() -> None:
    assert kurtosis(D(["1", "2", "3"])) is None


def test_shape_statistics_are_none_without_dispersion() -> None:
    assert skewness(D(["5", "5", "5", "5"])) is None
    assert kurtosis(D(["5", "5", "5", "5"])) is None


# --- Helpers --------------------------------------------------------------------


def test_sqrt_is_exact_for_perfect_squares() -> None:
    assert sqrt(Decimal(16)) == Decimal(4)
    assert sqrt(Decimal(0)) == Decimal(0)


def test_sqrt_rejects_negatives() -> None:
    with pytest.raises(ValueError, match="negative"):
        sqrt(Decimal(-1))


def test_divide_returns_none_rather_than_raising() -> None:
    assert divide(Decimal(1), Decimal(0)) is None
    assert divide(Decimal(10), Decimal(4)) == Decimal("2.5")
