"""Equity curve, drawdown and streak analysis.

Drawdown is the statistic traders actually experience. Expectancy describes the average
trade; drawdown describes the run of trades that makes someone abandon a profitable
system three weeks before it recovers. A journal that reports the first without the
second is describing a strategy nobody could hold.

**Trade-indexed, not calendar-indexed.** The curve advances one point per trade, so
drawdown depth is measured in trades rather than days. That matches how a trader
experiences a losing run and makes the number comparable between someone taking two
trades a day and someone taking twenty. Calendar drawdown is available separately from
the daily series.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.analytics.statistics import ZERO, divide, mean
from app.analytics.types import Estimate, TradeRecord


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One point on the cumulative curve."""

    index: int
    at: datetime
    trade_pnl: Decimal
    cumulative_pnl: Decimal
    peak: Decimal
    #: Distance below the running peak. Zero at a new high, negative while underwater.
    drawdown: Decimal
    cumulative_r: Decimal | None = None

    @property
    def is_at_peak(self) -> bool:
        return self.drawdown == 0


@dataclass(frozen=True, slots=True)
class DrawdownPeriod:
    """One underwater stretch, from the peak that preceded it to recovery."""

    start_index: int
    trough_index: int
    end_index: int | None
    start_at: datetime
    trough_at: datetime
    end_at: datetime | None
    peak_equity: Decimal
    trough_equity: Decimal

    @property
    def depth(self) -> Decimal:
        """Maximum distance below the peak. Positive."""
        return self.peak_equity - self.trough_equity

    @property
    def depth_pct(self) -> Decimal | None:
        """Depth as a fraction of the peak. ``None`` when the peak was not positive.

        A percentage drawdown from a peak of zero or below is meaningless, and reporting
        one would produce the absurd figures that appear in journals whose accounts
        started underwater.
        """
        if self.peak_equity <= 0:
            return None
        return divide(self.depth, self.peak_equity)

    @property
    def length_trades(self) -> int:
        """Trades from peak to trough — how long the decline lasted."""
        return self.trough_index - self.start_index

    @property
    def recovery_trades(self) -> int | None:
        """Trades from trough back to the old peak. ``None`` if never recovered."""
        if self.end_index is None:
            return None
        return self.end_index - self.trough_index

    @property
    def is_recovered(self) -> bool:
        return self.end_index is not None


def build_equity_curve(trades: Sequence[TradeRecord]) -> list[EquityPoint]:
    """Cumulative net P&L after each trade, ordered by close time.

    Ordered by *close*, not entry: a trade contributes to the equity curve when it is
    realized, and ordering by entry would show P&L arriving before the trade that
    produced it had finished. Open trades are skipped — unrealized P&L is not equity.
    """
    closed = sorted(
        (trade for trade in trades if trade.closed_at is not None),
        key=lambda trade: (trade.closed_at, trade.opened_at),
    )

    points: list[EquityPoint] = []
    cumulative = ZERO
    cumulative_r: Decimal | None = ZERO
    peak = ZERO

    for index, trade in enumerate(closed):
        cumulative += trade.net_pnl
        if trade.r_multiple is not None and cumulative_r is not None:
            cumulative_r += trade.r_multiple
        elif trade.r_multiple is None:
            # One trade without R makes the running R total unrepresentative; better to
            # report nothing than a total that silently omits trades.
            cumulative_r = None

        peak = max(peak, cumulative)
        points.append(
            EquityPoint(
                index=index,
                at=trade.closed_at,  # type: ignore[arg-type]
                trade_pnl=trade.net_pnl,
                cumulative_pnl=cumulative,
                peak=peak,
                drawdown=cumulative - peak,
                cumulative_r=cumulative_r,
            )
        )
    return points


def find_drawdown_periods(points: Sequence[EquityPoint]) -> list[DrawdownPeriod]:
    """Every underwater stretch in the curve.

    A period opens when equity falls below its running peak and closes when it regains
    that peak. A final unrecovered period is returned with ``end_index=None`` — the
    trader is still in it, which is usually the most relevant one on the page.
    """
    periods: list[DrawdownPeriod] = []
    if not points:
        return periods

    open_start: int | None = None
    trough_index = 0

    for point in points:
        if point.drawdown < 0:
            if open_start is None:
                # The peak preceding this decline is the previous point.
                open_start = max(0, point.index - 1)
                trough_index = point.index
            elif point.cumulative_pnl < points[trough_index].cumulative_pnl:
                trough_index = point.index
        elif open_start is not None:
            periods.append(_period(points, open_start, trough_index, point.index))
            open_start = None

    if open_start is not None:
        periods.append(_period(points, open_start, trough_index, None))
    return periods


def _period(
    points: Sequence[EquityPoint], start: int, trough: int, end: int | None
) -> DrawdownPeriod:
    return DrawdownPeriod(
        start_index=start,
        trough_index=trough,
        end_index=end,
        start_at=points[start].at,
        trough_at=points[trough].at,
        end_at=points[end].at if end is not None else None,
        peak_equity=points[start].peak,
        trough_equity=points[trough].cumulative_pnl,
    )


@dataclass(frozen=True, slots=True)
class DrawdownSummary:
    """Drawdown depths as **positive magnitudes**.

    Note the sign convention, which differs deliberately from
    :attr:`EquityPoint.drawdown`. A point on the equity curve carries a *signed*
    drawdown because it is a chart series and is drawn below the zero line. A summary
    reports *depths*, and "the worst drawdown was 1,200" is how a depth is spoken about.

    Mixing the two would be the real hazard: a struct reporting ``max_drawdown = 1200``
    beside ``current_drawdown = -1200`` describes one situation with two opposite signs,
    and any consumer comparing them — "are we at the worst point ever?" — gets it wrong.
    """

    max_drawdown: Decimal
    max_drawdown_pct: Decimal | None
    average_drawdown: Decimal | None
    longest_drawdown_trades: int | None
    longest_recovery_trades: int | None
    #: Depth below the running peak right now, positive; zero at a new high.
    current_drawdown: Decimal
    time_underwater_pct: Decimal | None
    periods: tuple[DrawdownPeriod, ...]
    peak_equity: Decimal
    final_equity: Decimal

    @property
    def is_underwater(self) -> bool:
        return self.current_drawdown > 0

    @property
    def is_at_high_water_mark(self) -> bool:
        """At or above every previous peak — the complement of being underwater."""
        return self.current_drawdown == 0

    @property
    def recovery_factor(self) -> Decimal | None:
        """Net profit divided by maximum drawdown.

        How much was earned per unit of worst-case pain. A strategy returning $10,000
        through a $9,000 drawdown and one returning $10,000 through a $1,000 drawdown
        have identical P&L and are not remotely the same strategy.
        """
        if self.max_drawdown <= 0:
            return None
        return divide(self.final_equity, self.max_drawdown)


def summarise_drawdown(points: Sequence[EquityPoint]) -> DrawdownSummary:
    if not points:
        return DrawdownSummary(
            max_drawdown=ZERO,
            max_drawdown_pct=None,
            average_drawdown=None,
            longest_drawdown_trades=None,
            longest_recovery_trades=None,
            current_drawdown=ZERO,
            time_underwater_pct=None,
            periods=(),
            peak_equity=ZERO,
            final_equity=ZERO,
        )

    periods = find_drawdown_periods(points)
    depths = [period.depth for period in periods]
    underwater = sum(1 for point in points if point.drawdown < 0)
    max_period = max(periods, key=lambda period: period.depth, default=None)

    return DrawdownSummary(
        max_drawdown=max(depths) if depths else ZERO,
        max_drawdown_pct=max_period.depth_pct if max_period else None,
        average_drawdown=mean(depths),
        longest_drawdown_trades=(
            max((period.length_trades for period in periods), default=None)
        ),
        longest_recovery_trades=(
            max(
                (
                    period.recovery_trades
                    for period in periods
                    if period.recovery_trades is not None
                ),
                default=None,
            )
        ),
        # Negated into a depth, to match ``max_drawdown`` and the rest of this struct.
        current_drawdown=-points[-1].drawdown,
        time_underwater_pct=divide(Decimal(underwater), Decimal(len(points))),
        periods=tuple(periods),
        peak_equity=points[-1].peak,
        final_equity=points[-1].cumulative_pnl,
    )


# --- Streaks --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreakSummary:
    """Consecutive-outcome runs.

    Included because streaks drive behaviour far more than their statistical
    significance warrants: the fourth consecutive loss is when position sizing gets
    abandoned. Quantifying the historical distribution of streaks lets the coach answer
    "is this run unusual?" with a number instead of reassurance.
    """

    longest_win_streak: int
    longest_loss_streak: int
    current_streak: int
    current_streak_is_wins: bool
    average_win_streak: Decimal | None
    average_loss_streak: Decimal | None

    @property
    def current_win_streak(self) -> int:
        return self.current_streak if self.current_streak_is_wins else 0

    @property
    def current_loss_streak(self) -> int:
        return 0 if self.current_streak_is_wins else self.current_streak


def summarise_streaks(trades: Sequence[TradeRecord]) -> StreakSummary:
    """Runs of consecutive wins and losses, in close order.

    Scratches neither extend nor break a streak — they are not an outcome in the
    relevant sense, and letting them reset a losing run would understate exactly the
    streaks that matter behaviourally.
    """
    ordered = sorted(
        (trade for trade in trades if trade.closed_at is not None),
        key=lambda trade: (trade.closed_at, trade.opened_at),
    )

    win_streaks: list[int] = []
    loss_streaks: list[int] = []
    current = 0
    current_is_win = True

    for trade in ordered:
        if trade.is_scratch:
            continue
        if trade.is_winner == current_is_win and current > 0:
            current += 1
            continue
        if current > 0:
            (win_streaks if current_is_win else loss_streaks).append(current)
        current_is_win = trade.is_winner
        current = 1

    if current > 0:
        (win_streaks if current_is_win else loss_streaks).append(current)

    return StreakSummary(
        longest_win_streak=max(win_streaks, default=0),
        longest_loss_streak=max(loss_streaks, default=0),
        current_streak=current,
        current_streak_is_wins=current_is_win,
        average_win_streak=mean([Decimal(value) for value in win_streaks]),
        average_loss_streak=mean([Decimal(value) for value in loss_streaks]),
    )


# --- Daily series ----------------------------------------------------------------


def daily_pnl(trades: Sequence[TradeRecord]) -> dict[date, Decimal]:
    """Net P&L per session date.

    The basis for any calendar-anchored statistic — Sharpe, volatility, MAR — because
    those are defined on a time series and a trade-indexed series is not one. Uses the
    exchange-derived session date the reconstruction engine assigned, not the UTC date.
    """
    totals: dict[date, Decimal] = {}
    for trade in trades:
        if trade.session_date is None:
            continue
        totals[trade.session_date] = totals.get(trade.session_date, ZERO) + trade.net_pnl
    return dict(sorted(totals.items()))


def max_daily_drawdown(trades: Sequence[TradeRecord]) -> Estimate:
    """Worst peak-to-trough decline on the daily equity series.

    Distinct from trade-indexed drawdown and the one prop firms actually enforce.
    """
    series = daily_pnl(trades)
    if not series:
        return Estimate.undefined(0, "no trades with a session date")

    cumulative = ZERO
    peak = ZERO
    worst = ZERO
    for value in series.values():
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return Estimate.of(abs(worst), len(series))
