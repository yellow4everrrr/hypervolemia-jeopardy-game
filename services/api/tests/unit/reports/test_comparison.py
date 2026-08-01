"""Period-over-period comparison: the null battery for "you improved this month".

Every journal ships this feature and almost all ship it wrong — subtract, render an
arrow. On twenty trades a month that arrow is noise with a direction, and it appears
twelve times a year with the authority of a report.

The battery below generates two periods from **one unchanged process** and asserts
nothing is established. The positive control generates a genuine shift, because an
implementation that reported nothing under all circumstances would pass every null test
here while being useless.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.types import Direction, TradeRecord
from app.core.ids import uuid7
from app.domain.common.enums import ReportType
from app.reports.comparison import (
    COMPARED_METRICS,
    MetricChange,
    compare_periods,
    display_value,
)
from app.reports.periods import period_containing, previous

BASE = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)


def build_month(
    *,
    month: int,
    seed: int,
    count: int = 22,
    win_rate: float = 0.45,
    win: str = "150",
    loss: str = "-120",
) -> list[TradeRecord]:
    rng = random.Random(seed)
    trades: list[TradeRecord] = []
    for index in range(count):
        session = date(2026, month, (index % 20) + 1)
        opened = datetime.combine(session, BASE.timetz()) + timedelta(minutes=13 * index)
        won = rng.random() < win_rate
        trades.append(
            TradeRecord(
                trade_id=uuid7(),
                account_id=uuid7(),
                opened_at=opened,
                closed_at=opened + timedelta(minutes=5),
                direction=Direction.LONG if rng.random() < 0.5 else Direction.SHORT,
                net_pnl=Decimal(win) if won else Decimal(loss),
                r_multiple=Decimal("1.2") if won else Decimal("-1.0"),
                quantity=Decimal(rng.choice([1, 2, 2, 3])),
                session_date=session,
                entry_hour=opened.hour,
                entry_weekday=session.isoweekday(),
                duration_seconds=300,
            )
        )
    return trades


def compare(current_trades, previous_trades, *, permutations: int = 600):
    current = period_containing(ReportType.MONTHLY, date(2026, 5, 15))
    return compare_periods(
        current,
        previous(current),
        current_trades,
        previous_trades,
        permutations=permutations,
    )


class TestTheNullBattery:
    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    def test_an_unchanged_process_establishes_no_change(self, seed: int) -> None:
        """Two months drawn from identical parameters. The measured values will differ —
        that is what sampling does — and none of the differences is a change."""
        result = compare(
            build_month(month=5, seed=seed * 10),
            build_month(month=4, seed=seed * 10 + 1),
        )

        assert result.changes
        assert result.established == ()

    def test_the_measured_values_do_move_even_though_nothing_changed(self) -> None:
        """Guards the guard: if the two months came out identical, the battery above
        would pass while testing nothing."""
        result = compare(build_month(month=5, seed=99), build_month(month=4, seed=7))

        moved = [
            change
            for change in result.changes
            if change.difference is not None and change.difference != 0
        ]
        assert moved, "the generator produced two identical months"

    def test_an_unestablished_change_reports_direction_unchanged(self) -> None:
        """``flat`` and ``unchanged`` are different claims. ``flat`` says the values
        matched; ``unchanged`` says the measurement could not tell them apart, and a UI
        that collapses the two shows a settled fact where there is an open question."""
        result = compare(build_month(month=5, seed=31), build_month(month=4, seed=32))

        assert all(change.direction == "unchanged" for change in result.changes)

    def test_the_narrative_says_chance_rather_than_implying_a_trend(self) -> None:
        result = compare(build_month(month=5, seed=41), build_month(month=4, seed=42))
        win_rate = next(c for c in result.changes if c.key == "win_rate")

        assert "chance" in win_rate.narrate()
        assert str(win_rate.current_sample) in win_rate.narrate()


class TestThePositiveControl:
    def test_a_large_genuine_shift_is_established(self) -> None:
        """Position size trebles between periods, on plenty of trades. If this is not
        detected the comparison is inert and every null test above is vacuous."""
        # Rebuilt rather than mutated: TradeRecord is frozen with slots.
        big = [
            dataclasses.replace(t, quantity=t.quantity * Decimal(4))
            for t in build_month(month=5, seed=5, count=120)
        ]

        result = compare(big, build_month(month=4, seed=6, count=120))
        sizes = next(c for c in result.changes if c.key == "quantity")

        assert sizes.is_established
        assert sizes.direction == "up"

    def test_an_established_change_narrates_as_a_change(self) -> None:
        big = [
            dataclasses.replace(t, quantity=t.quantity * Decimal(4))
            for t in build_month(month=5, seed=5, count=120)
        ]
        result = compare(big, build_month(month=4, seed=6, count=120))
        sizes = next(c for c in result.changes if c.key == "quantity")

        assert "more than chance explains" in sizes.narrate()


class TestTheFamilyCorrection:
    def test_every_tested_metric_is_adjusted(self) -> None:
        """Comparing five metrics at p < 0.05 finds roughly one 'change' per report on
        an unchanged trader. One family, one correction."""
        result = compare(build_month(month=5, seed=17), build_month(month=4, seed=18))

        tested = [c for c in result.changes if c.comparison is not None]
        assert tested
        for change in tested:
            assert change.comparison is not None
            assert change.comparison.adjusted_p_value is not None

    def test_no_metric_receives_another_metrics_adjustment(self) -> None:
        """The correction ranks p-values internally; pairing them back positionally
        would give a flat metric the significance earned by a moved one."""
        big = [
            dataclasses.replace(t, quantity=t.quantity * Decimal(4))
            for t in build_month(month=5, seed=5, count=120)
        ]
        result = compare(big, build_month(month=4, seed=6, count=120))

        for change in result.changes:
            if change.comparison is None or change.comparison.p_value is None:
                continue
            assert change.comparison.adjusted_p_value is not None
            assert change.comparison.adjusted_p_value >= change.comparison.p_value

    def test_the_compared_set_is_kept_short(self) -> None:
        """A statistical limit, not an oversight: every added metric raises the bar for
        all the others."""
        assert len(COMPARED_METRICS) <= 6


class TestThinPeriods:
    def test_too_few_trades_yields_no_test_rather_than_a_bad_one(self) -> None:
        result = compare(build_month(month=5, seed=3, count=3), build_month(month=4, seed=4))

        win_rate = next(c for c in result.changes if c.key == "win_rate")
        assert win_rate.comparison is None
        assert not win_rate.is_established
        assert "not enough data" in win_rate.narrate()

    def test_a_metric_missing_from_a_period_is_not_compared(self) -> None:
        """Trades with no recorded stop have no R multiple. Treating the absence as zero
        would invent a period of break-even trades."""
        no_stops = [
            dataclasses.replace(t, r_multiple=None) for t in build_month(month=5, seed=21)
        ]
        result = compare(no_stops, build_month(month=4, seed=22))

        r_change = next(c for c in result.changes if c.key == "r_multiple")
        assert r_change.current is None
        assert r_change.comparison is None
        assert not r_change.is_established

    def test_the_payload_explains_why_almost_nothing_is_established(self) -> None:
        payload = compare(
            build_month(month=5, seed=51), build_month(month=4, seed=52)
        ).to_payload()

        assert payload["established_count"] == 0
        assert "permutation test" in payload["interpretation"]


def test_the_narrative_does_not_print_full_decimal_precision() -> None:
    """A mean of per-trade values carries every digit of the division that made it.

    This shipped: a monthly report read "average P&L per trade was
    178.88686131386861313868613 against 144.43359375". The sentence is not only rendered
    on screen — it is stored on the report row and handed to the AI layer as the ground
    truth it is forbidden to restate numbers without — so the noise propagated into
    everything downstream.
    """
    change = MetricChange(
        key="net_pnl_per_trade",
        label="average P&L per trade",
        current=Decimal("178.88686131386861313868613"),
        previous=Decimal("144.43359375"),
        current_sample=137,
        previous_sample=128,
        comparison=None,
        unit="currency",
    )

    narrative = change.narrate()

    assert "178.89" in narrative
    assert "144.43" in narrative
    assert "178.88686131386861313868613" not in narrative
    assert "144.43359375" not in narrative


def test_the_payload_keeps_the_exact_value_beside_the_rounded_one() -> None:
    """Rounding for display must not become rounding of the number.

    A caller that needs to recompute — a chart, a later comparison, an export — needs the
    exact value. Replacing it with the display string would fix the rendering by
    destroying the data, which is the wrong trade in a system built on exact arithmetic.
    """
    exact = Decimal("178.88686131386861313868613")
    change = MetricChange(
        key="net_pnl_per_trade",
        label="average P&L per trade",
        current=exact,
        previous=Decimal("144.43359375"),
        current_sample=137,
        previous_sample=128,
        comparison=None,
        unit="currency",
    )

    payload = change.to_payload()

    assert payload["current"] == str(exact)
    assert payload["display_current"] == "178.89"
    assert payload["unit"] == "currency"


def test_each_unit_rounds_to_a_precision_worth_reading() -> None:
    """Seconds whole, R to three places, ratios to four.

    A tenth of a second of average holding time is not a fact about trading, and a win
    rate rounded to two places loses the difference between 51.2% and 51.4% that the
    comparison is actually about.
    """
    assert display_value(Decimal("1252.912408759124087"), "seconds") == "1253s"
    assert display_value(Decimal("0.5109489051094890"), "ratio") == "0.5109"
    assert display_value(Decimal("0.4105109489051094"), "r") == "0.411R"
    assert display_value(None, "currency") == "not available"
