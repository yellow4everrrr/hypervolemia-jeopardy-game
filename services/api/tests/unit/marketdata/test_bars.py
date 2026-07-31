"""Tests for bars, series windowing and excursion analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.domain.common.enums import Direction, Timeframe
from app.domain.marketdata.bars import (
    Bar,
    BarSeries,
    average_true_range,
    compute_excursion,
    resample,
    sequence_ambiguous,
    touched,
)

START = datetime(2026, 3, 5, 14, 30, tzinfo=UTC)


def bar(minute: int, o: str, h: str, low: str, c: str, volume: str = "100") -> Bar:
    return Bar(
        ts=START + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(volume),
    )


def series(*bars: Bar, timeframe: Timeframe = Timeframe.M1) -> BarSeries:
    return BarSeries("ESM6", timeframe, bars)


# --- Bar validation -------------------------------------------------------------


def test_bar_rejects_inverted_high_and_low() -> None:
    with pytest.raises(DomainError, match=r"high .* is below low"):
        Bar(START, Decimal(100), Decimal(90), Decimal(110), Decimal(100))


def test_bar_rejects_an_open_outside_the_range() -> None:
    """A malformed bar corrupts every excursion computed from it."""
    with pytest.raises(DomainError, match=r"open .* outside"):
        Bar(START, Decimal(200), Decimal(110), Decimal(90), Decimal(100))


def test_bar_rejects_a_naive_timestamp() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        Bar(datetime(2026, 3, 5, 14, 30), Decimal(100), Decimal(110), Decimal(90), Decimal(100))  # noqa: DTZ001


# --- Series ---------------------------------------------------------------------


def test_series_rejects_unordered_bars() -> None:
    with pytest.raises(DomainError, match="ascending time order"):
        BarSeries(
            "ESM6",
            Timeframe.M1,
            (bar(5, "1", "2", "0.5", "1"), bar(1, "1", "2", "0.5", "1")),
        )


def test_window_selects_by_opening_time() -> None:
    data = series(*[bar(i, "100", "101", "99", "100") for i in range(10)])
    selected = data.window(START + timedelta(minutes=3), START + timedelta(minutes=5))
    assert len(selected) == 3


def test_covering_includes_the_bar_open_at_the_start() -> None:
    """A trade entered mid-bar lives inside that bar.

    Excluding it would discard the excursion during the very minute the trade was
    opened — often the minute with the largest adverse move.
    """
    data = series(*[bar(i, "100", "101", "99", "100") for i in range(10)])
    entered = START + timedelta(minutes=3, seconds=40)

    assert len(data.window(entered, entered + timedelta(minutes=1))) == 1
    assert len(data.covering(entered, entered + timedelta(minutes=1))) == 2


def test_bar_at_returns_none_inside_a_gap() -> None:
    data = series(bar(0, "100", "101", "99", "100"), bar(10, "100", "101", "99", "100"))
    assert data.bar_at(START + timedelta(minutes=5)) is None
    assert data.bar_at(START + timedelta(seconds=30)) is not None


def test_gaps_are_reported_not_filled() -> None:
    """A synthesised bar is indistinguishable from a real one downstream.

    An excursion computed across an invented candle would be a fabricated statistic.
    """
    data = series(bar(0, "100", "101", "99", "100"), bar(5, "100", "101", "99", "100"))
    gaps = data.gaps()

    assert len(gaps) == 1
    assert gaps[0] == (START + timedelta(minutes=1), START + timedelta(minutes=5))


# --- Excursions -----------------------------------------------------------------


def test_excursion_for_a_long() -> None:
    """Entry 100; the window runs to 104 and down to 97.

    MFE is 4 points, MAE is 3.
    """
    data = series(
        bar(0, "100", "101", "99", "100"),
        bar(1, "100", "104", "98", "103"),
        bar(2, "103", "103", "97", "98"),
    )
    result = compute_excursion(
        data,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(minutes=2),
    )

    assert result is not None
    assert result.mfe_points == Decimal(4)
    assert result.mae_points == Decimal(3)
    assert result.edge_ratio is not None
    assert round(result.edge_ratio, 4) == Decimal("1.3333")


def test_excursion_for_a_short_inverts() -> None:
    data = series(
        bar(0, "100", "101", "99", "100"),
        bar(1, "100", "104", "96", "97"),
    )
    result = compute_excursion(
        data,
        direction=Direction.SHORT,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(minutes=1),
    )

    assert result is not None
    assert result.mfe_points == Decimal(4)  # price fell to 96
    assert result.mae_points == Decimal(4)  # and rose to 104


def test_excursion_uses_highs_and_lows_not_closes() -> None:
    """A stop is hit by the low, not by the close.

    Measuring adverse excursion on closes would systematically understate how close a
    trade came to being stopped out — the exact error that makes "my stops are fine"
    look true when it is not.
    """
    data = series(bar(0, "100", "100", "90", "100"))
    result = compute_excursion(
        data,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(seconds=59),
    )

    assert result is not None
    assert result.mae_points == Decimal(10)


def test_excursion_is_none_without_bars() -> None:
    """Undefined, not zero. Zero would assert price never moved."""
    empty = BarSeries("ESM6", Timeframe.M1, ())
    result = compute_excursion(
        empty,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(minutes=5),
    )
    assert result is None


def test_partial_coverage_is_flagged() -> None:
    """The excursion is then a lower bound, and says so."""
    data = series(bar(0, "100", "101", "99", "100"))
    result = compute_excursion(
        data,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(minutes=30),
    )

    assert result is not None
    assert result.is_partial


def test_full_coverage_is_not_flagged() -> None:
    data = series(*[bar(i, "100", "101", "99", "100") for i in range(5)])
    result = compute_excursion(
        data,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(minutes=4),
    )

    assert result is not None
    assert not result.is_partial


def test_entry_price_is_included_in_the_range() -> None:
    """A gap fill can leave the entry outside every bar extreme in the window."""
    data = series(bar(0, "105", "106", "104", "105"))
    result = compute_excursion(
        data,
        direction=Direction.LONG,
        entry_price=Decimal(100),
        opened_at=START,
        closed_at=START + timedelta(seconds=30),
    )

    assert result is not None
    assert result.mae_points >= 0
    assert result.lowest == Decimal(100)


def test_excursion_rejects_an_inverted_holding_period() -> None:
    data = series(bar(0, "100", "101", "99", "100"))
    with pytest.raises(DomainError, match="must not precede"):
        compute_excursion(
            data,
            direction=Direction.LONG,
            entry_price=Decimal(100),
            opened_at=START,
            closed_at=START - timedelta(minutes=1),
        )


# --- Resampling -----------------------------------------------------------------


def test_resample_aggregates_correctly() -> None:
    """Five one-minute bars into one five-minute bar.

    Open from the first, close from the last, high and low across all, volume summed.
    """
    data = series(
        bar(0, "100", "102", "99", "101", "10"),
        bar(1, "101", "105", "100", "104", "20"),
        bar(2, "104", "104", "98", "99", "30"),
        bar(3, "99", "103", "97", "102", "40"),
        bar(4, "102", "106", "101", "105", "50"),
    )
    aggregated = resample(data, Timeframe.M5)

    assert len(aggregated) == 1
    candle = aggregated.bars[0]
    assert candle.open == Decimal(100)
    assert candle.close == Decimal(105)
    assert candle.high == Decimal(106)
    assert candle.low == Decimal(97)
    assert candle.volume == Decimal(150)


def test_resample_refuses_to_go_finer() -> None:
    """Aggregation cannot invent detail that was never stored."""
    data = series(bar(0, "100", "101", "99", "100"), timeframe=Timeframe.M5)
    with pytest.raises(DomainError, match="only goes coarser"):
        resample(data, Timeframe.M1)


def test_resample_buckets_are_epoch_anchored() -> None:
    """So a candle does not shift when the surrounding window changes."""
    full = series(*[bar(i, "100", "101", "99", "100") for i in range(10)])
    trimmed = series(*[bar(i, "100", "101", "99", "100") for i in range(2, 10)])

    full_stamps = {candle.ts for candle in resample(full, Timeframe.M5).bars}
    trimmed_stamps = {candle.ts for candle in resample(trimmed, Timeframe.M5).bars}

    assert trimmed_stamps <= full_stamps


# --- ATR and touch detection ----------------------------------------------------


def test_atr_needs_enough_bars() -> None:
    data = series(*[bar(i, "100", "101", "99", "100") for i in range(5)])
    assert average_true_range(data, periods=14) is None


def test_atr_on_uniform_bars_equals_the_bar_range() -> None:
    data = series(*[bar(i, "100", "102", "98", "100") for i in range(30)])
    result = average_true_range(data, periods=14)

    assert result is not None
    assert round(result, 2) == Decimal("4.00")


def test_touched_finds_the_first_bar_containing_a_price() -> None:
    data = series(
        bar(0, "100", "101", "99", "100"),
        bar(1, "100", "103", "100", "102"),
        bar(2, "102", "105", "101", "104"),
    )
    hit = touched(data, Decimal("104.5"), start=START, end=START + timedelta(minutes=3))
    assert hit == START + timedelta(minutes=2)


def test_touched_returns_none_when_price_is_never_reached() -> None:
    data = series(bar(0, "100", "101", "99", "100"))
    assert touched(data, Decimal(200), start=START, end=START + timedelta(minutes=1)) is None


def test_sequence_ambiguity_is_detectable() -> None:
    """The central honesty problem of OHLC backtesting.

    When one bar spans both the stop and the target, the data cannot say which was hit
    first. The simulator must treat that as indeterminate rather than assuming the
    favourable outcome — which is how backtests promise returns that never arrive.
    """
    wide = bar(0, "100", "110", "90", "105")
    assert sequence_ambiguous(wide, Decimal(95), Decimal(108))

    narrow = bar(1, "100", "101", "99", "100")
    assert not sequence_ambiguous(narrow, Decimal(95), Decimal(108))
