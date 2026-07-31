"""Tests for the core performance metrics.

Every expected value is arithmetic a reader can check in their head against the
``simple_trades`` fixture: +100, −50, +200, −50, +100.
"""

from __future__ import annotations

from decimal import Decimal

from app.analytics.performance import (
    average_hold_time,
    count_trades,
    describe,
    edge_ratio,
    expectancy,
    expectancy_r,
    hold_time_by_outcome,
    summarise,
    summarise_pnl,
    system_quality_number,
)
from app.analytics.types import Reliability, TradeRecord
from tests.unit.analytics.conftest import make_trade, make_trades

# --- Counts ---------------------------------------------------------------------


def test_counts(simple_trades: list[TradeRecord]) -> None:
    counts = count_trades(simple_trades)
    assert counts.total == 5
    assert counts.winners == 3
    assert counts.losers == 2
    assert counts.scratches == 0


def test_win_rate_excludes_scratches_from_the_denominator() -> None:
    """Three wins, one loss, one scratch.

    Win rate is 3/4 = 75%, not 3/5 = 60%. A trader who scratched out of a trade did not
    lose it, and counting it as a loss understates the discipline of cutting a trade
    that stopped working.
    """
    trades = make_trades(["100", "100", "100", "-50", "0"])
    counts = count_trades(trades)

    assert counts.scratches == 1
    assert counts.decided == 4
    assert counts.win_rate == Decimal("0.75")


def test_win_rate_is_undefined_with_no_decided_trades() -> None:
    assert count_trades(make_trades(["0", "0"])).win_rate is None


# --- P&L ------------------------------------------------------------------------


def test_pnl_summary(simple_trades: list[TradeRecord]) -> None:
    """Net +300, gross profit 400, gross loss 100, profit factor 4."""
    summary = summarise_pnl(simple_trades)

    assert summary.net_pnl == Decimal(300)
    assert summary.gross_profit == Decimal(400)
    assert summary.gross_loss == Decimal(100)
    assert summary.profit_factor == Decimal(4)
    assert summary.largest_win == Decimal(200)
    assert summary.largest_loss == Decimal(-50)


def test_average_and_median_win_differ_on_skewed_samples(
    simple_trades: list[TradeRecord],
) -> None:
    """Winners are 100, 200, 100: mean 133.33, median 100."""
    summary = summarise_pnl(simple_trades)
    assert summary.median_win == Decimal(100)
    assert summary.average_win is not None
    assert round(summary.average_win, 2) == Decimal("133.33")


def test_payoff_ratio() -> None:
    """Average win 100, average loss −50 → payoff 2.0."""
    summary = summarise_pnl(make_trades(["100", "100", "-50", "-50"]))
    assert summary.payoff_ratio == Decimal(2)


def test_profit_factor_is_undefined_without_losses() -> None:
    """Undefined, not infinite.

    A sample with no losing trades means the sample is short or the strategy has not
    met its bad regime yet. Reporting infinity would let that read as an achievement.
    """
    assert summarise_pnl(make_trades(["100", "200"])).profit_factor is None


def test_cost_ratio_quantifies_overtrading() -> None:
    """$50 of commission against $150 of net winnings is a 33% cost ratio.

    The number that answers "am I trading too much?" more directly than trade count.
    """
    trades = [
        make_trade("75", index=0, commission="25"),
        make_trade("75", index=1, commission="25"),
    ]
    summary = summarise_pnl(trades)

    assert summary.gross_profit == Decimal(150)
    assert summary.commission == Decimal(50)
    assert summary.cost_ratio is not None
    assert round(summary.cost_ratio, 4) == Decimal("0.3333")


# --- Expectancy -----------------------------------------------------------------


def test_expectancy_is_mean_net_pnl(simple_trades: list[TradeRecord]) -> None:
    """(100 - 50 + 200 - 50 + 100) / 5 = 60."""
    result = expectancy(simple_trades)
    assert result.value == Decimal(60)
    assert result.sample_size == 5


def test_expectancy_of_an_empty_sample_is_undefined() -> None:
    result = expectancy([])
    assert result.value is None
    assert result.undefined_reason is not None


def test_expectancy_r(r_trades: list[TradeRecord]) -> None:
    """(2 - 1 + 3 - 1 + 1) / 5 = 0.8R."""
    result = expectancy_r(r_trades)
    assert result.value == Decimal("0.8")
    assert result.sample_size == 5


def test_expectancy_r_excludes_trades_without_a_stop() -> None:
    """Trades with no recorded stop are skipped, not counted as zero R.

    Counting them as zero would drag the average toward zero and describe a strategy
    nobody traded. The reported sample size is the number that actually had a stop, so
    thin R coverage is visible rather than hidden.
    """
    trades = [
        make_trade("200", index=0, r_multiple="2"),
        make_trade("100", index=1, r_multiple=None),
        make_trade("200", index=2, r_multiple="2"),
    ]
    result = expectancy_r(trades)

    assert result.value == Decimal(2)
    assert result.sample_size == 2


def test_expectancy_r_undefined_when_no_trade_has_a_stop() -> None:
    result = expectancy_r(make_trades(["100", "200"]))
    assert result.value is None
    assert "recorded initial stop" in (result.undefined_reason or "")


# --- SQN ------------------------------------------------------------------------


def test_sqn(r_trades: list[TradeRecord]) -> None:
    """R values [2, -1, 3, -1, 1]: mean 0.8, sample stdev 1.7889.

    SQN = sqrt(5) x 0.8 / 1.7889 ≈ 1.0.
    """
    result = system_quality_number(r_trades)
    assert result.value is not None
    assert round(result.value, 2) == Decimal("1.00")


def test_sqn_caps_the_sample_multiplier() -> None:
    """Beyond 100 trades, SQN stops growing with trade count.

    The same repeating R pattern traded 100 times and 400 times describes a strategy of
    identical quality — only the duration differs. Uncapped, the sqrt(N) term would make
    the longer history score twice as high. The residual difference here is Bessel's
    correction on the standard deviation, not the sample multiplier.
    """
    pattern = ["2", "-1", "3", "-1", "1"]

    def sqn_for(count: int) -> Decimal:
        trades = [
            make_trade("100", index=i, r_multiple=pattern[i % 5]) for i in range(count)
        ]
        value = system_quality_number(trades).value
        assert value is not None
        return value

    hundred = sqn_for(100)
    four_hundred = sqn_for(400)

    # Uncapped this ratio would be sqrt(400/100) = 2.0.
    assert round(four_hundred / hundred, 2) == Decimal("1.00")


def test_sqn_undefined_with_zero_dispersion() -> None:
    trades = [make_trade("100", index=i, r_multiple="1") for i in range(10)]
    result = system_quality_number(trades)
    assert result.value is None
    assert "dispersion is zero" in (result.undefined_reason or "")


def test_sqn_needs_two_r_multiples() -> None:
    assert system_quality_number(make_trades(["100"])).value is None


# --- Distribution ---------------------------------------------------------------


def test_describe_reports_shape(simple_trades: list[TradeRecord]) -> None:
    summary = describe([trade.net_pnl for trade in simple_trades])

    assert summary.sample_size == 5
    assert summary.mean == Decimal(60)
    assert summary.minimum == Decimal(-50)
    assert summary.maximum == Decimal(200)
    assert "0.5" in summary.percentiles


def test_fat_tail_flag() -> None:
    values = [Decimal(0)] * 30 + [Decimal(-500), Decimal(500)]
    assert describe(values).has_fat_tails is True


# --- Hold time ------------------------------------------------------------------


def test_average_hold_time() -> None:
    trades = [
        make_trade("100", index=0, duration_seconds=60),
        make_trade("100", index=1, duration_seconds=120),
    ]
    assert average_hold_time(trades).value == Decimal(90)


def test_hold_time_by_outcome_exposes_cutting_winners_early() -> None:
    """Losers held four times longer than winners — the classic management leak.

    This split is one of the most diagnostic comparisons a journal can make, and it is
    invisible in any aggregate hold time.
    """
    trades = [
        make_trade("100", index=0, duration_seconds=60),
        make_trade("100", index=1, duration_seconds=60),
        make_trade("-100", index=2, duration_seconds=240),
        make_trade("-100", index=3, duration_seconds=240),
    ]
    split = hold_time_by_outcome(trades)

    assert split["winners"].value == Decimal(60)
    assert split["losers"].value == Decimal(240)


def test_hold_time_undefined_without_durations() -> None:
    trades = [make_trade("100", index=0, duration_seconds=None)]
    assert average_hold_time(trades).value is None


# --- Edge ratio -----------------------------------------------------------------


def test_edge_ratio_from_excursions() -> None:
    """Mean MFE 2R over mean MAE 1R = 2.0.

    Above 1 means price moved further in favour after entry than against it — evidence
    the entry has an edge independently of how the trade was managed.
    """
    trades = [
        make_trade("100", index=0, mae_r="1", mfe_r="2"),
        make_trade("100", index=1, mae_r="1", mfe_r="2"),
    ]
    assert edge_ratio(trades).value == Decimal(2)


def test_edge_ratio_undefined_without_bar_data(simple_trades: list[TradeRecord]) -> None:
    result = edge_ratio(simple_trades)
    assert result.value is None
    assert "excursion data" in (result.undefined_reason or "")


# --- Reliability ----------------------------------------------------------------


def test_small_samples_are_flagged_insufficient(simple_trades: list[TradeRecord]) -> None:
    assert summarise(simple_trades).reliability is Reliability.PROVISIONAL


def test_tiny_samples_are_flagged_insufficient() -> None:
    assert summarise(make_trades(["100", "-50"])).reliability is Reliability.INSUFFICIENT


def test_large_samples_are_reliable(many_trades: list[TradeRecord]) -> None:
    assert summarise(many_trades).reliability is Reliability.RELIABLE


def test_summarise_runs_over_an_empty_sample() -> None:
    """The dashboard renders for a user with no trades yet; nothing may raise."""
    summary = summarise([])
    assert summary.counts.total == 0
    assert summary.expectancy.value is None
    assert summary.pnl.net_pnl == Decimal(0)
