"""Tests for the equity curve, drawdown and streaks."""

from __future__ import annotations

from decimal import Decimal

from app.analytics.drawdown import (
    build_equity_curve,
    daily_pnl,
    find_drawdown_periods,
    max_daily_drawdown,
    summarise_drawdown,
    summarise_streaks,
)
from app.analytics.types import TradeRecord
from tests.unit.analytics.conftest import make_trade, make_trades

# --- Equity curve ---------------------------------------------------------------


def test_equity_curve_accumulates(simple_trades: list[TradeRecord]) -> None:
    """+100, -50, +200, -50, +100 → 100, 50, 250, 200, 300."""
    curve = build_equity_curve(simple_trades)
    assert [point.cumulative_pnl for point in curve] == [
        Decimal(100),
        Decimal(50),
        Decimal(250),
        Decimal(200),
        Decimal(300),
    ]


def test_peak_only_ratchets_upward(simple_trades: list[TradeRecord]) -> None:
    curve = build_equity_curve(simple_trades)
    assert [point.peak for point in curve] == [
        Decimal(100),
        Decimal(100),
        Decimal(250),
        Decimal(250),
        Decimal(300),
    ]


def test_drawdown_is_distance_below_the_peak(simple_trades: list[TradeRecord]) -> None:
    curve = build_equity_curve(simple_trades)
    assert [point.drawdown for point in curve] == [
        Decimal(0),
        Decimal(-50),
        Decimal(0),
        Decimal(-50),
        Decimal(0),
    ]


def test_curve_is_ordered_by_close_not_entry() -> None:
    """A trade contributes to equity when it is realized.

    Ordering by entry would show P&L arriving before the trade that produced it had
    finished, which makes every drawdown figure describe a sequence that never occurred.
    """
    early_entry_late_exit = make_trade("100", index=0, duration_seconds=100_000)
    late_entry_early_exit = make_trade("-50", index=1, duration_seconds=60)

    curve = build_equity_curve([early_entry_late_exit, late_entry_early_exit])
    assert [point.trade_pnl for point in curve] == [Decimal(-50), Decimal(100)]


def test_open_trades_are_excluded() -> None:
    """Unrealized P&L is not equity."""
    trades = [make_trade("100", index=0), make_trade("500", index=1, closed=False)]
    curve = build_equity_curve(trades)

    assert len(curve) == 1
    assert curve[0].cumulative_pnl == Decimal(100)


def test_cumulative_r_is_dropped_when_any_trade_lacks_a_stop() -> None:
    """A running R total that silently omits trades would misrepresent the curve."""
    trades = [
        make_trade("100", index=0, r_multiple="1"),
        make_trade("100", index=1, r_multiple=None),
    ]
    curve = build_equity_curve(trades)

    assert curve[0].cumulative_r == Decimal(1)
    assert curve[1].cumulative_r is None


# --- Drawdown periods -----------------------------------------------------------


def test_drawdown_period_records_depth_and_recovery() -> None:
    """+100, -80, -40, +200: one period, trough at -120 below the 100 peak."""
    curve = build_equity_curve(make_trades(["100", "-80", "-40", "200"]))
    periods = find_drawdown_periods(curve)

    assert len(periods) == 1
    period = periods[0]
    assert period.depth == Decimal(120)
    assert period.is_recovered
    assert period.recovery_trades == 1


def test_unrecovered_drawdown_is_still_reported() -> None:
    """The one the trader is currently in — usually the most relevant on the page."""
    curve = build_equity_curve(make_trades(["100", "-80"]))
    periods = find_drawdown_periods(curve)

    assert len(periods) == 1
    assert not periods[0].is_recovered
    assert periods[0].recovery_trades is None


def test_drawdown_percentage_is_undefined_from_a_non_positive_peak() -> None:
    """A percentage drawdown from a peak of zero is meaningless, not infinite."""
    curve = build_equity_curve(make_trades(["-100", "-50"]))
    summary = summarise_drawdown(curve)

    assert summary.max_drawdown == Decimal(150)
    assert summary.max_drawdown_pct is None


def test_max_drawdown_takes_the_deepest_of_several() -> None:
    curve = build_equity_curve(make_trades(["100", "-30", "100", "-90", "200"]))
    summary = summarise_drawdown(curve)

    assert summary.max_drawdown == Decimal(90)
    assert len(summary.periods) == 2


def test_recovery_factor() -> None:
    """Final equity 300 over a max drawdown of 50 → 6.0.

    Two strategies with identical P&L and different drawdowns are not the same
    strategy, and this is the number that says so.
    """
    curve = build_equity_curve(make_trades(["100", "-50", "200", "-50", "100"]))
    summary = summarise_drawdown(curve)

    assert summary.max_drawdown == Decimal(50)
    assert summary.final_equity == Decimal(300)
    assert summary.recovery_factor == Decimal(6)


def test_time_underwater(simple_trades: list[TradeRecord]) -> None:
    """Two of five points sit below the running peak → 40%."""
    summary = summarise_drawdown(build_equity_curve(simple_trades))
    assert summary.time_underwater_pct == Decimal("0.4")


def test_empty_curve_summarises_without_raising() -> None:
    summary = summarise_drawdown([])
    assert summary.max_drawdown == Decimal(0)
    assert summary.periods == ()


# --- Streaks --------------------------------------------------------------------


def test_streaks() -> None:
    """W W W L L W → longest win 3, longest loss 2, current streak 1 win."""
    trades = make_trades(["10", "10", "10", "-10", "-10", "10"])
    streaks = summarise_streaks(trades)

    assert streaks.longest_win_streak == 3
    assert streaks.longest_loss_streak == 2
    assert streaks.current_streak == 1
    assert streaks.current_streak_is_wins is True


def test_scratches_neither_extend_nor_break_a_streak() -> None:
    """L L scratch L is a three-loss run.

    Letting a break-even trade reset the count would understate exactly the streaks
    that matter behaviourally — the run that precedes abandoning position sizing.
    """
    trades = make_trades(["-10", "-10", "0", "-10"])
    assert summarise_streaks(trades).longest_loss_streak == 3


def test_current_loss_streak_is_reported_separately() -> None:
    trades = make_trades(["10", "-10", "-10"])
    streaks = summarise_streaks(trades)

    assert streaks.current_loss_streak == 2
    assert streaks.current_win_streak == 0


def test_average_streak_lengths() -> None:
    """Win runs of 2 and 1 → average 1.5."""
    trades = make_trades(["10", "10", "-10", "10", "-10"])
    streaks = summarise_streaks(trades)
    assert streaks.average_win_streak == Decimal("1.5")


# --- Daily series ---------------------------------------------------------------


def test_daily_pnl_groups_by_session_date() -> None:
    from datetime import date

    trades = [
        make_trade("100", index=0, session_date=date(2026, 3, 2)),
        make_trade("-40", index=1, session_date=date(2026, 3, 2)),
        make_trade("60", index=2, session_date=date(2026, 3, 3)),
    ]
    series = daily_pnl(trades)

    assert series[date(2026, 3, 2)] == Decimal(60)
    assert series[date(2026, 3, 3)] == Decimal(60)


def test_max_daily_drawdown_is_measured_on_the_daily_series() -> None:
    """The drawdown a prop firm actually enforces.

    Daily totals +100, −150, +30: peak 100, trough −50 → 150 decline.
    """
    from datetime import date

    trades = [
        make_trade("100", index=0, session_date=date(2026, 3, 2)),
        make_trade("-150", index=1, session_date=date(2026, 3, 3)),
        make_trade("30", index=2, session_date=date(2026, 3, 4)),
    ]
    assert max_daily_drawdown(trades).value == Decimal(150)


def test_max_daily_drawdown_undefined_without_session_dates() -> None:
    """A trade whose session could not be resolved contributes to no daily series."""
    import dataclasses

    trades = [dataclasses.replace(make_trade("100", index=0), session_date=None)]
    result = max_daily_drawdown(trades)

    assert result.value is None
    assert "session date" in (result.undefined_reason or "")
