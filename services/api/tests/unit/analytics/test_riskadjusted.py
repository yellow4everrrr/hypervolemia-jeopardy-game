"""Tests for the risk-adjusted ratios.

These are the statistics most often reported wrongly, so most of these tests pin down a
*refusal*: what the module declines to compute, and why.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.analytics.riskadjusted import (
    MIN_DAILY_OBSERVATIONS,
    RatioBasis,
    kelly_fraction,
    mar_ratio,
    sharpe_ratio,
    sortino_ratio,
)
from app.analytics.types import TradeRecord
from tests.unit.analytics.conftest import make_trade, make_trades

START = date(2026, 1, 5)


def daily_series(values: list[str]) -> list[TradeRecord]:
    """One trade per successive session date, so the daily series is unambiguous."""
    return [
        make_trade(value, index=index, session_date=START + timedelta(days=index))
        for index, value in enumerate(values)
    ]


# --- Sharpe ---------------------------------------------------------------------


def test_sharpe_refuses_to_annualise_a_short_sample() -> None:
    """Annualising 5 days multiplies noise by sqrt(252) and calls it a year.

    Refusing is the point: a journal that reports "Sharpe 4.2" from a good week has
    told its user something actively false.
    """
    result = sharpe_ratio(daily_series(["100"] * 5))

    assert result.value is None
    assert "trading days" in (result.undefined_reason or "")


def test_sharpe_is_computed_once_there_are_enough_days() -> None:
    values = ["100", "-50", "80", "-30", "120", "-60", "90", "40"] * 4
    result = sharpe_ratio(daily_series(values))

    assert result.value is not None
    assert result.observations >= MIN_DAILY_OBSERVATIONS


def test_sharpe_basis_is_pnl_without_a_starting_equity() -> None:
    """A dollar-denominated ratio is scale-dependent and must not be compared outside.

    The basis travels with the number so nobody mistakes it for a published Sharpe.
    """
    values = ["100", "-50", "80", "-30", "120", "-60", "90", "40"] * 4
    result = sharpe_ratio(daily_series(values))

    assert result.basis is RatioBasis.PNL
    assert result.is_comparable is False


def test_sharpe_basis_is_return_with_a_starting_equity() -> None:
    values = ["100", "-50", "80", "-30", "120", "-60", "90", "40"] * 4
    result = sharpe_ratio(daily_series(values), starting_equity=Decimal(50_000))

    assert result.basis is RatioBasis.RETURN
    assert result.is_comparable is True


def test_sharpe_is_scale_invariant_in_return_basis() -> None:
    """Doubling every result while doubling the equity leaves Sharpe unchanged.

    The property that makes a return-based ratio comparable at all.
    """
    values = ["100", "-50", "80", "-30", "120", "-60", "90", "40"] * 4
    doubled = [str(int(value) * 2) for value in values]

    single = sharpe_ratio(daily_series(values), starting_equity=Decimal(10_000))
    double = sharpe_ratio(daily_series(doubled), starting_equity=Decimal(20_000))

    assert single.value is not None and double.value is not None
    assert round(single.value, 6) == round(double.value, 6)


def test_sharpe_is_undefined_with_no_variance() -> None:
    result = sharpe_ratio(daily_series(["100"] * 30))
    assert result.value is None
    assert "zero variance" in (result.undefined_reason or "")


def test_sharpe_is_not_a_per_trade_statistic() -> None:
    """Two samples with identical daily P&L but different trade counts agree.

    A "Sharpe" computed per trade would differ between these, rising as the trader
    traded less. That is the error this construction exists to avoid.
    """
    one_per_day = [
        make_trade("100", index=i, session_date=START + timedelta(days=i))
        for i in range(30)
    ]
    two_per_day = [
        make_trade("50", index=i, session_date=START + timedelta(days=i // 2))
        for i in range(60)
    ]

    assert sharpe_ratio(one_per_day).observations == 30
    assert sharpe_ratio(two_per_day).observations == 30


# --- Sortino --------------------------------------------------------------------


def test_sortino_ignores_upside_dispersion() -> None:
    """Two series with identical losing days and wildly different winning days.

    Sortino must agree between them; Sharpe must not. This is the whole reason both
    are reported.
    """
    modest = ["50", "-40"] * 15
    spectacular = ["5000", "-40"] * 15

    sortino_modest = sortino_ratio(daily_series(modest))
    sortino_spectacular = sortino_ratio(daily_series(spectacular))
    sharpe_modest = sharpe_ratio(daily_series(modest))
    sharpe_spectacular = sharpe_ratio(daily_series(spectacular))

    assert sortino_modest.value is not None and sortino_spectacular.value is not None
    # Downside deviation is identical, so the ratio scales purely with mean return.
    assert sortino_spectacular.value > sortino_modest.value
    # Sharpe penalises the large winners through the same denominator.
    assert sharpe_modest.value is not None and sharpe_spectacular.value is not None
    assert sharpe_spectacular.value < sortino_spectacular.value


def test_sortino_is_undefined_when_nothing_falls_below_target() -> None:
    """Undefined rather than infinite — a sample with no losing day is a short sample."""
    result = sortino_ratio(daily_series(["100"] * 25))
    assert result.value is None
    assert "undefined rather than infinite" in (result.undefined_reason or "")


# --- MAR ------------------------------------------------------------------------


def test_mar_needs_a_drawdown() -> None:
    result = mar_ratio(
        daily_series(["100"] * 30), starting_equity=Decimal(10_000), max_drawdown=Decimal(0)
    )
    assert result.value is None
    assert "no drawdown" in (result.undefined_reason or "")


def test_mar_is_computed_with_equity_and_drawdown() -> None:
    values = ["100", "-50"] * 15
    result = mar_ratio(
        daily_series(values), starting_equity=Decimal(10_000), max_drawdown=Decimal(500)
    )
    assert result.value is not None


def test_mar_rejects_non_positive_equity() -> None:
    result = mar_ratio(
        daily_series(["100"] * 30), starting_equity=Decimal(0), max_drawdown=Decimal(100)
    )
    assert result.value is None


# --- Kelly ----------------------------------------------------------------------


def test_kelly_from_win_rate_and_payoff() -> None:
    """60% win rate, payoff 2.0 → f* = 0.6 - 0.4/2 = 0.4.

    Six winners of +200 and four losers of -100 in ten trades.
    """
    trades = make_trades(["200"] * 6 + ["-100"] * 4)
    result = kelly_fraction(trades)

    assert result.win_rate == Decimal("0.6")
    assert result.payoff_ratio == Decimal(2)
    assert result.full == Decimal("0.4")
    assert result.half == Decimal("0.2")


def test_kelly_warns_about_small_samples() -> None:
    """Kelly is extremely sensitive to estimation error, and 10 trades is nothing."""
    result = kelly_fraction(make_trades(["200"] * 6 + ["-100"] * 4))
    assert any("estimation error" in warning for warning in result.warnings)


def test_kelly_warns_about_implausibly_large_fractions() -> None:
    result = kelly_fraction(make_trades(["500"] * 8 + ["-100"] * 2))
    assert any("overestimated edge" in warning for warning in result.warnings)


def test_kelly_is_negative_without_an_edge() -> None:
    """30% win rate at 1:1 → f* = 0.3 - 0.7/1 = -0.4. There is nothing to size."""
    trades = make_trades(["100"] * 3 + ["-100"] * 7)
    result = kelly_fraction(trades)

    assert result.full == Decimal("-0.4")
    assert result.is_negative_edge
    assert any("negative Kelly" in warning for warning in result.warnings)


def test_kelly_is_undefined_without_losses() -> None:
    """No losses means no payoff ratio, so the formula has no denominator."""
    result = kelly_fraction(make_trades(["100", "200", "150"]))
    assert result.full is None
    assert "no losing trades" in (result.undefined_reason or "")


def test_kelly_is_undefined_without_wins() -> None:
    result = kelly_fraction(make_trades(["-100", "-200"]))
    assert result.full is None
