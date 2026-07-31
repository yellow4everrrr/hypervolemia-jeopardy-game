"""Leak attribution: the headline figure, and the bug that inflates it.

The failure this module prevents is not subtle once seen, and it is invisible until
somebody checks. Detectors overlap — a trader down on the day, late in the session, after
two losses is caught by three of them — and each estimates the *full* cost of the trades
it names. Summing the estimates bills the same money once per detector.

The sharpest demonstration is at the bottom: on a real scan the naive sum claims the
leaks cost more than the trader lost across every losing trade in the sample. Not "a bit
high" — arithmetically impossible, and shipped as the report's headline.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.behaviours import BehaviourFinding, Unit
from app.analytics.significance import ComparisonResult
from app.analytics.types import Estimate
from app.reports.leaks import quantify


def finding(
    kind: str,
    trade_ids: tuple[str, ...],
    cost: str,
    *,
    significant: bool = True,
    unaffected: int = 60,
    unit: Unit = Unit.CURRENCY,
    worse: bool = True,
) -> BehaviourFinding:
    """A finding shaped exactly as the detector suite produces one."""
    comparison = ComparisonResult(
        label_a="present",
        label_b="absent",
        mean_a=Decimal(-10) if worse else Decimal(10),
        mean_b=Decimal(0),
        difference=Decimal(-10) if worse else Decimal(10),
        sample_a=len(trade_ids),
        sample_b=unaffected,
        p_value=Decimal("0.001"),
        effect_size=Decimal("0.4"),
        permutations=1000,
        adjusted_p_value=Decimal("0.004") if significant else Decimal("0.9"),
    )
    return BehaviourFinding(
        kind=kind,
        label=kind.replace("_", " "),
        description="",
        affected=len(trade_ids),
        unaffected=unaffected,
        mean_when_present=Decimal(-10) if worse else Decimal(10),
        mean_when_absent=Decimal(0),
        expectancy_when_present=Estimate.of(Decimal(-10), len(trade_ids)),
        comparison=comparison,
        estimated_cost=Decimal(cost),
        detail={},
        unit=unit,
        affected_trade_ids=trade_ids,
    )


def pnl_for(count: int) -> dict[str, Decimal]:
    return {f"t{index}": Decimal(-100) for index in range(count)}


class TestDeduplication:
    def test_three_detectors_on_the_same_trades_are_not_billed_three_times(self) -> None:
        """The defining case, in its simplest form.

        Three detectors, one identical set of twenty trades, each estimating -$2,000.
        The naive sum is -$6,000. The money is -$2,000.
        """
        shared = tuple(f"t{index}" for index in range(20))
        findings = [
            finding("overtrading", shared, "-2000"),
            finding("trading_while_down", shared, "-2000"),
            finding("after_losing_streak", shared, "-2000"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(100))

        assert summary.naive_sum == Decimal(-6000)
        assert summary.total_cost == Decimal(-2000)
        assert summary.trades_touched == 20

    def test_disjoint_detectors_are_summed_normally(self) -> None:
        """Deduplication must not become a discount.

        Two leaks on entirely separate trades genuinely do cost the sum of their
        estimates, and an implementation that shrank that total would understate the
        problem — the opposite error, equally wrong.
        """
        findings = [
            finding("overtrading", tuple(f"t{i}" for i in range(10)), "-1000"),
            finding("revenge_trading", tuple(f"t{i}" for i in range(10, 20)), "-1500"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(100))

        assert summary.naive_sum == Decimal(-2500)
        assert summary.total_cost == Decimal(-2500)
        assert summary.overstatement_factor == Decimal(1)

    def test_partial_overlap_counts_the_shared_trades_once(self) -> None:
        first = tuple(f"t{index}" for index in range(20))
        second = tuple(f"t{index}" for index in range(10, 30))
        findings = [
            finding("overtrading", first, "-2000"),
            finding("trading_while_down", second, "-2000"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(100))

        # 30 distinct trades, each carrying the larger of the claims against it. Both
        # detectors claim -100/trade here, so the union is 30 x -100.
        assert summary.trades_touched == 30
        assert summary.total_cost == Decimal(-3000)
        assert summary.naive_sum == Decimal(-4000)

    def test_a_shared_trade_keeps_the_largest_claim_not_the_sum(self) -> None:
        """Summing per-trade claims would reintroduce the bug one level down."""
        shared = ("t0", "t1", "t2", "t3", "t4")
        findings = [
            finding("mild", shared, "-500"),
            finding("severe", shared, "-2500"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(50))

        assert summary.total_cost == Decimal(-2500)


class TestAttribution:
    def test_exclusive_cost_isolates_what_only_this_leak_explains(self) -> None:
        """What would survive fixing every other leak first — the thing worth ranking on."""
        findings = [
            finding("overtrading", tuple(f"t{i}" for i in range(20)), "-2000"),
            finding("trading_while_down", tuple(f"t{i}" for i in range(10, 20)), "-1000"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(100))
        by_kind = {item.kind: item for item in summary.attributions}

        assert by_kind["overtrading"].exclusive_trades == 10
        assert by_kind["overtrading"].exclusive_cost == Decimal(-1000)
        assert by_kind["trading_while_down"].exclusive_trades == 0
        assert by_kind["trading_while_down"].is_wholly_shared

    def test_a_wholly_shared_leak_is_flagged_rather_than_hidden(self) -> None:
        """It may be the better description of the same behaviour, but it is not extra
        work for the trader, and presenting it as a separate item to fix implies it is."""
        shared = tuple(f"t{index}" for index in range(15))
        summary = quantify(
            [finding("a", shared, "-900"), finding("b", shared, "-900")],
            net_pnl_by_trade=pnl_for(60),
        )

        assert any(item.is_wholly_shared for item in summary.attributions)

    def test_leaks_are_ordered_by_what_they_uniquely_cost(self) -> None:
        findings = [
            finding("small_exclusive", tuple(f"t{i}" for i in range(5)), "-500"),
            finding("large_exclusive", tuple(f"t{i}" for i in range(10, 40)), "-6000"),
        ]

        summary = quantify(findings, net_pnl_by_trade=pnl_for(100))

        assert summary.attributions[0].kind == "large_exclusive"


class TestWhatIsExcluded:
    def test_an_unestablished_finding_never_reaches_the_headline(self) -> None:
        """A report is read as settled fact, so the loudest coincidence in the scan must
        not be priced into the number at the top of it."""
        summary = quantify(
            [finding("noise", ("t0", "t1", "t2"), "-5000", significant=False)],
            net_pnl_by_trade=pnl_for(50),
        )

        assert summary.total_cost is None
        assert summary.undefined_reason is not None
        assert "established" in summary.undefined_reason

    def test_an_edge_is_not_counted_as_a_leak(self) -> None:
        """A behaviour associated with *better* results is not a cost."""
        summary = quantify(
            [finding("good_habit", ("t0", "t1", "t2"), "5000", worse=False)],
            net_pnl_by_trade=pnl_for(50),
        )

        assert summary.total_cost is None

    def test_a_non_currency_finding_carries_no_cost(self) -> None:
        """Holding time is measured in seconds. Multiplying seconds by a trade count
        produces a number with no meaning that a reader will nonetheless take as dollars."""
        summary = quantify(
            [finding("holding_time", ("t0", "t1"), "-100", unit=Unit.SECONDS)],
            net_pnl_by_trade=pnl_for(50),
        )

        assert summary.total_cost is None

    def test_no_findings_at_all_is_a_reason_not_a_zero(self) -> None:
        """Zero would read as "your leaks cost you nothing", which is a finding nobody
        established."""
        summary = quantify([], net_pnl_by_trade=pnl_for(50))

        assert summary.total_cost is None
        assert summary.undefined_reason


class TestAgainstTheRealScan:
    """The demonstration, on output from the actual detector suite."""

    @pytest.mark.parametrize("seed", [11, 12, 13])
    def test_the_naive_sum_overstates_a_real_scan(self, seed: int) -> None:
        from app.analytics.discovery import DiscoveryConfig, discover_patterns
        from tests.unit.analytics.test_patterns import build_trades

        trades = build_trades(400, seed, leak_after=5)
        config = DiscoveryConfig(permutations=400, include_clustering=False)
        scan = discover_patterns(trades, config)
        summary = quantify(
            scan.behaviours,
            net_pnl_by_trade={str(trade.trade_id): trade.net_pnl for trade in trades},
        )

        assert summary.total_cost is not None and summary.naive_sum is not None
        # Costs are negative, so the naive sum is *further* from zero.
        assert abs(summary.naive_sum) > abs(summary.total_cost)
        assert summary.overstatement_factor is not None
        assert summary.overstatement_factor > Decimal("1.5")

    def test_the_naive_sum_can_exceed_every_dollar_the_trader_lost(self) -> None:
        """The clearest possible evidence that summing is wrong, not merely imprecise.

        On this sample the detectors' estimates add up to more money than the trader
        lost across every losing trade in the period. A report headlining that figure is
        not overstating a real cost — it is quoting an impossible one. The deduplicated
        total stays inside what was actually lost.
        """
        from app.analytics.discovery import DiscoveryConfig, discover_patterns
        from tests.unit.analytics.test_patterns import build_trades

        trades = build_trades(400, 13, leak_after=5)
        config = DiscoveryConfig(permutations=400, include_clustering=False)
        scan = discover_patterns(trades, config)
        summary = quantify(
            scan.behaviours,
            net_pnl_by_trade={str(trade.trade_id): trade.net_pnl for trade in trades},
        )

        total_lost = sum((t.net_pnl for t in trades if t.net_pnl < 0), Decimal(0))

        assert summary.naive_sum is not None and summary.total_cost is not None
        assert abs(summary.naive_sum) > abs(total_lost)
        assert abs(summary.total_cost) < abs(total_lost)
