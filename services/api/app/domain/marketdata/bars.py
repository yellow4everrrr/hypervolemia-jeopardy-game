"""Bars, bar series, and excursion analysis.

Pure domain code: a bar is a value object, a series is an ordered window of them, and
MAE/MFE fall out of scanning the window a trade was open for. No database, so the
same functions serve live analysis, backfill, and the what-if simulator.

**Why excursions matter enough to build a market-data pipeline for.** Every statistic
in milestone 3 describes what a trade *did*. Excursions describe what it *could have*
done — how far price ran in favour before the exit, how far it went against before
turning. That is the only evidence that separates "my entry is good and my management
is bad" from the reverse, and no amount of fill data can supply it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.errors import DomainError
from app.domain.common.enums import Direction, Timeframe


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle.

    ``ts`` is the bar's **opening** time, which is the convention TradingView and every
    exchange feed use. Mixing open- and close-stamped bars shifts every replay by one
    interval, so the convention is stated rather than assumed.
    """

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise DomainError("bar timestamps must be timezone-aware")
        if self.high < self.low:
            raise DomainError(f"bar at {self.ts}: high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise DomainError(f"bar at {self.ts}: open {self.open} outside the high/low range")
        if not (self.low <= self.close <= self.high):
            raise DomainError(f"bar at {self.ts}: close {self.close} outside the high/low range")

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_up(self) -> bool:
        return self.close >= self.open

    @property
    def midpoint(self) -> Decimal:
        return (self.high + self.low) / 2


@dataclass(frozen=True, slots=True)
class BarSeries:
    """An ordered, gap-tolerant window of bars for one instrument and timeframe.

    Gaps are expected, not exceptional: futures sessions close, holidays happen, and a
    thin contract may not print every minute. Nothing here assumes contiguity — lookups
    are by timestamp through binary search rather than by index arithmetic.
    """

    instrument_symbol: str
    timeframe: Timeframe
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        timestamps = [bar.ts for bar in self.bars]
        if timestamps != sorted(timestamps):
            raise DomainError("bars must be in ascending time order")

    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return bool(self.bars)

    @property
    def start(self) -> datetime | None:
        return self.bars[0].ts if self.bars else None

    @property
    def end(self) -> datetime | None:
        return self.bars[-1].ts if self.bars else None

    @property
    def _timestamps(self) -> list[datetime]:
        return [bar.ts for bar in self.bars]

    def window(self, start: datetime, end: datetime) -> BarSeries:
        """Bars whose opening time falls in ``[start, end]``."""
        stamps = self._timestamps
        low = bisect_left(stamps, start)
        high = bisect_right(stamps, end)
        return BarSeries(self.instrument_symbol, self.timeframe, self.bars[low:high])

    def covering(self, start: datetime, end: datetime) -> BarSeries:
        """Bars overlapping ``[start, end]``, including the one that was open at ``start``.

        Distinct from :meth:`window` and the correct choice for excursion analysis: a
        trade entered at 09:31:40 lives inside the 09:31 bar, and excluding that bar
        would discard the excursion during the minute the trade was actually opened.
        """
        stamps = self._timestamps
        low = max(0, bisect_right(stamps, start) - 1)
        high = bisect_right(stamps, end)
        return BarSeries(self.instrument_symbol, self.timeframe, self.bars[low:high])

    def bar_at(self, moment: datetime) -> Bar | None:
        """The bar that was open at ``moment``, if any."""
        stamps = self._timestamps
        index = bisect_right(stamps, moment) - 1
        if index < 0:
            return None
        candidate = self.bars[index]
        if moment >= candidate.ts + timedelta(seconds=self.timeframe.seconds):
            return None  # the moment falls in a gap
        return candidate

    @property
    def high(self) -> Decimal | None:
        return max((bar.high for bar in self.bars), default=None)

    @property
    def low(self) -> Decimal | None:
        return min((bar.low for bar in self.bars), default=None)

    def gaps(self) -> list[tuple[datetime, datetime]]:
        """Intervals where consecutive bars are further apart than one timeframe.

        Reported rather than filled. A synthesised bar would be indistinguishable from
        a real one downstream, and an excursion computed across an invented candle is
        a fabricated statistic.
        """
        step = timedelta(seconds=self.timeframe.seconds)
        found: list[tuple[datetime, datetime]] = []
        for previous, current in zip(self.bars, self.bars[1:], strict=False):
            if current.ts - previous.ts > step:
                found.append((previous.ts + step, current.ts))
        return found


@dataclass(frozen=True, slots=True)
class Excursion:
    """Maximum adverse and favourable price movement while a trade was open."""

    highest: Decimal
    lowest: Decimal
    mae_price: Decimal
    mfe_price: Decimal
    mae_points: Decimal
    mfe_points: Decimal
    bars_observed: int
    #: True when the bar window did not fully cover the holding period. The excursion
    #: is then a lower bound, and is reported as such rather than as a measurement.
    is_partial: bool = False

    @property
    def edge_ratio(self) -> Decimal | None:
        if self.mae_points == 0:
            return None
        return self.mfe_points / self.mae_points


def compute_excursion(
    series: BarSeries,
    *,
    direction: Direction,
    entry_price: Decimal,
    opened_at: datetime,
    closed_at: datetime,
) -> Excursion | None:
    """Scan the holding period for the best and worst price the trade saw.

    Uses bar highs and lows rather than closes: a stop is hit by the low, not by the
    close, and measuring adverse excursion on closes would systematically understate
    how close a trade came to being stopped out.

    Returns ``None`` when no bars cover the period at all — undefined, not zero.
    """
    if closed_at < opened_at:
        raise DomainError("closed_at must not precede opened_at")

    covered = series.covering(opened_at, closed_at)
    if not covered:
        return None

    highest = covered.high
    lowest = covered.low
    if highest is None or lowest is None:  # pragma: no cover — guarded by the emptiness check
        return None

    # The entry price itself is part of the excursion range. A trade whose entry was
    # better than any bar extreme in the window (possible on a gap fill or a stale
    # feed) would otherwise produce a negative excursion.
    highest = max(highest, entry_price)
    lowest = min(lowest, entry_price)

    if direction is Direction.LONG:
        mae_price, mfe_price = lowest, highest
    else:
        mae_price, mfe_price = highest, lowest

    sign = direction.signed_multiplier
    return Excursion(
        highest=highest,
        lowest=lowest,
        mae_price=mae_price,
        mfe_price=mfe_price,
        mae_points=abs((mae_price - entry_price) * sign),
        mfe_points=max((mfe_price - entry_price) * sign, Decimal(0)),
        bars_observed=len(covered),
        is_partial=_is_partial(covered, opened_at, closed_at),
    )


def _is_partial(series: BarSeries, opened_at: datetime, closed_at: datetime) -> bool:
    """Does the window leave any of the holding period unobserved?"""
    if not series.bars:
        return True
    step = timedelta(seconds=series.timeframe.seconds)
    first, last = series.bars[0], series.bars[-1]
    if first.ts > opened_at:
        return True
    if last.ts + step < closed_at:
        return True
    return bool(series.gaps())


def resample(series: BarSeries, target: Timeframe) -> BarSeries:
    """Aggregate a series into a coarser timeframe.

    Lets one stored resolution serve several chart intervals — a replay can hold
    one-minute bars and render five-minute candles without a second fetch. Buckets are
    anchored to the epoch so that boundaries are stable across windows, which is what
    keeps a bar from shifting when the surrounding range changes.
    """
    if target.seconds < series.timeframe.seconds:
        raise DomainError(
            f"cannot resample {series.timeframe.value} up to {target.value}; "
            "aggregation only goes coarser"
        )
    if target.seconds == series.timeframe.seconds or not series.bars:
        return BarSeries(series.instrument_symbol, target, series.bars)

    step = target.seconds
    buckets: dict[int, list[Bar]] = {}
    for bar in series.bars:
        key = int(bar.ts.timestamp()) // step
        buckets.setdefault(key, []).append(bar)

    aggregated: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        volumes = [bar.volume for bar in group if bar.volume is not None]
        aggregated.append(
            Bar(
                # Stamped at the bucket boundary, not at the first bar in it. Using the
                # first bar's time would give the same candle a different timestamp
                # depending on where the query window happened to start, which is
                # exactly the instability epoch anchoring exists to prevent.
                ts=datetime.fromtimestamp(key * step, tz=group[0].ts.tzinfo),
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(volumes, start=Decimal(0)) if volumes else None,
            )
        )
    return BarSeries(series.instrument_symbol, target, tuple(aggregated))


def average_true_range(series: BarSeries, periods: int = 14) -> Decimal | None:
    """Wilder's ATR — the volatility unit the what-if simulator sizes stops in.

    A stop expressed in points means different things on a quiet day and a volatile
    one; expressed in ATR it means the same thing on both, which is what makes "would
    a 2-ATR stop have done better?" a coherent question.
    """
    if len(series) < periods + 1:
        return None

    true_ranges: list[Decimal] = []
    for previous, current in zip(series.bars, series.bars[1:], strict=False):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    if len(true_ranges) < periods:
        return None

    # Wilder smoothing: seed with a simple average, then apply the recursive form.
    average = sum(true_ranges[:periods], start=Decimal(0)) / Decimal(periods)
    for value in true_ranges[periods:]:
        average = (average * Decimal(periods - 1) + value) / Decimal(periods)
    return average


def price_at_or_before(series: BarSeries, moment: datetime) -> Decimal | None:
    """Last known close at or before ``moment``, for marking an open position."""
    bar = series.bar_at(moment)
    if bar is not None:
        return bar.close
    stamps = [bar.ts for bar in series.bars]
    index = bisect_right(stamps, moment) - 1
    return series.bars[index].close if index >= 0 else None


def touched(
    series: BarSeries, price: Decimal, *, start: datetime, end: datetime
) -> datetime | None:
    """First moment in ``[start, end]`` when a bar's range contained ``price``.

    The primitive behind "would this stop have been hit?" in the what-if simulator.
    Returns the bar's opening time — intrabar sequence is unknowable from OHLC, so the
    simulator treats the touch as happening somewhere within that bar rather than
    pretending to a precision the data does not have.
    """
    for bar in series.covering(start, end).bars:
        if bar.low <= price <= bar.high:
            return bar.ts
    return None


def sequence_ambiguous(bar: Bar, first: Decimal, second: Decimal) -> bool:
    """Whether one bar contains two price levels, making the order they were hit unknowable.

    The central honesty problem of OHLC backtesting: if a bar's range spans both the
    stop and the target, the data cannot say which came first. Callers must treat the
    outcome as indeterminate rather than assuming the favourable one — which is how
    backtests come to promise returns that never materialise.
    """
    return bar.low <= first <= bar.high and bar.low <= second <= bar.high
