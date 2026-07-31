"""Tests for the segmentation cube.

The theme: a segment is an *observation* until it has passed sample gating, an interval
that excludes zero, and a significance test adjusted for the size of the scan. Only then
is it a finding. These tests pin every one of those gates.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.segmentation import (
    SEGMENT_DIMENSIONS,
    analyse_dimension,
    analyse_streak_state,
    duration_bucket,
    group_by,
)
from app.analytics.types import Reliability, TradeRecord
from tests.unit.analytics.conftest import make_trade, make_trades

# --- Grouping -------------------------------------------------------------------


def test_group_by_weekday() -> None:
    trades = [
        make_trade("100", index=0, entry_weekday=1),
        make_trade("-50", index=1, entry_weekday=1),
        make_trade("200", index=2, entry_weekday=5),
    ]
    groups = group_by(trades, "weekday")

    assert set(groups) == {"monday", "friday"}
    assert len(groups["monday"]) == 2


def test_group_by_hour_pads_to_two_digits() -> None:
    """So that lexical ordering matches clock ordering in every UI that renders it."""
    trades = [make_trade("100", index=0, entry_hour=9), make_trade("100", index=1, entry_hour=14)]
    assert sorted(group_by(trades, "hour_of_day")) == ["09", "14"]


def test_trades_missing_a_dimension_are_excluded_not_bucketed_as_unknown() -> None:
    """An "unknown setup" segment would be a meaningless mixture shown as a category."""
    trades = [
        make_trade("100", index=0, setup="breakout"),
        make_trade("-50", index=1, setup=None),
    ]
    groups = group_by(trades, "setup")

    assert set(groups) == {"breakout"}
    assert sum(len(group) for group in groups.values()) == 1


def test_unknown_dimension_raises() -> None:
    with pytest.raises(ValueError, match="unknown segmentation dimension"):
        group_by([], "phase_of_moon")


def test_duration_buckets() -> None:
    assert duration_bucket(make_trade("1", duration_seconds=30)) == "under_1m"
    assert duration_bucket(make_trade("1", duration_seconds=120)) == "1m_to_5m"
    assert duration_bucket(make_trade("1", duration_seconds=20_000)) == "over_4h"
    assert duration_bucket(make_trade("1", duration_seconds=None)) is None


def test_every_declared_dimension_is_usable() -> None:
    """A dimension in the registry that cannot be grouped would fail only in production."""
    trades = make_trades(
        ["100", "-50"], setup="breakout", strategy="trend", market_condition="calm"
    )
    for dimension in SEGMENT_DIMENSIONS:
        group_by(trades, dimension)


# --- Segment analysis -----------------------------------------------------------


def test_segments_carry_their_own_statistics() -> None:
    monday = [make_trade("100", index=i, entry_weekday=1) for i in range(10)]
    friday = [make_trade("-50", index=i + 10, entry_weekday=5) for i in range(10)]

    analysis = analyse_dimension([*monday, *friday], "weekday", test_significance=False)
    by_key = {segment.key: segment for segment in analysis.segments}

    assert by_key["monday"].pnl.net_pnl == Decimal(1000)
    assert by_key["friday"].pnl.net_pnl == Decimal(-500)
    assert by_key["monday"].counts.total == 10


def test_thin_segments_are_reported_but_flagged() -> None:
    """Shown, so the trader sees the breakdown is thin — never ranked."""
    trades = [
        *[make_trade("100", index=i, entry_weekday=1) for i in range(40)],
        *[make_trade("100", index=i + 40, entry_weekday=5) for i in range(2)],
    ]
    analysis = analyse_dimension(trades, "weekday", test_significance=False)
    friday = next(s for s in analysis.segments if s.key == "friday")

    assert "friday" in analysis.excluded_segments
    assert friday.reliability is Reliability.INSUFFICIENT
    assert not friday.is_actionable


def test_a_segment_without_significance_testing_is_never_actionable() -> None:
    """Untested means unproven, however good the numbers look.

    A journal that ranks segments without this is ranking noise.
    """
    trades = [make_trade("100", index=i, entry_weekday=1) for i in range(60)]
    analysis = analyse_dimension(trades, "weekday", test_significance=False)

    assert analysis.segments[0].counts.total == 60
    assert not analysis.segments[0].is_actionable
    assert analysis.best is None


def test_a_genuinely_different_segment_becomes_actionable() -> None:
    """A large, consistent, well-sampled difference must survive every gate.

    Otherwise the defences would be so strict that nothing real ever surfaces.
    """
    good = [make_trade("200", index=i, entry_weekday=1) for i in range(50)]
    bad = [make_trade("-150", index=i + 50, entry_weekday=5) for i in range(50)]

    analysis = analyse_dimension([*good, *bad], "weekday", bootstrap_iterations=500)
    monday = next(s for s in analysis.segments if s.key == "monday")

    assert monday.is_actionable
    assert analysis.best is not None
    assert analysis.best.key == "monday"


def test_segments_from_noise_are_not_actionable() -> None:
    """The case that matters most: 24 hourly buckets over data with no hourly effect.

    Every bucket will show a different expectancy. None of them means anything, and
    none may be presented as a finding.
    """
    import random

    rng = random.Random(20260731)
    trades = [
        make_trade(
            str(round(rng.gauss(0, 100), 2)), index=i, entry_hour=i % 24
        )
        for i in range(480)
    ]
    analysis = analyse_dimension(trades, "hour_of_day", bootstrap_iterations=400)

    assert analysis.actionable == ()
    assert analysis.best is None


def test_expectancy_interval_is_attached_to_each_segment() -> None:
    trades = [make_trade("100", index=i, entry_weekday=1) for i in range(20)]
    analysis = analyse_dimension(
        trades, "weekday", test_significance=False, bootstrap_iterations=300
    )

    assert analysis.segments[0].expectancy.interval is not None


# --- Streak state ---------------------------------------------------------------


def test_streak_state_detects_degradation_after_losses() -> None:
    """Tilt, measured.

    The pattern is built so that any trade following two losses is itself a large loss,
    while the baseline is positive. That is what a journal should be able to detect and
    quantify rather than merely suspect.
    """
    pnls: list[str] = []
    for _ in range(20):
        pnls += ["100", "-50", "-50", "-300", "100"]
    trades = make_trades(pnls)

    analysis = analyse_streak_state(trades)

    assert analysis.baseline.value is not None
    after_two = analysis.after_losses.get(2)
    assert after_two is not None
    assert after_two.value == Decimal(-300)
    degradation = analysis.degradation_after_losses(2)
    assert degradation is not None and degradation < 0


def test_streak_state_baseline_covers_every_closed_trade(
    many_trades: list[TradeRecord],
) -> None:
    analysis = analyse_streak_state(many_trades)
    assert analysis.baseline.sample_size == len(many_trades)


def test_streak_state_handles_an_empty_sample() -> None:
    analysis = analyse_streak_state([])
    assert analysis.baseline.value is None
    assert analysis.after_losses == {}
