"""The segmentation cube: performance sliced by every dimension that might matter.

This is the module a trader spends the most time in and the one most likely to mislead
them, so its design is defensive throughout.

Slicing 500 trades by hour of day produces 24 groups averaging 20 trades each. Every one
of those groups will show a different expectancy, purely because 20 trades is not enough
to estimate one. The best-looking hour will look excellent. It means nothing, and a
journal that presents it as a finding has actively harmed its user.

Three defences:

1. **Minimum sample gating.** Segments below the threshold are computed and returned but
   flagged ``INSUFFICIENT``, never ranked or recommended.
2. **Every segment carries a confidence interval,** so "best hour" is visibly a range
   that overlaps most of the other hours.
3. **Ranking requires significance testing against the rest of the sample,** with
   false-discovery-rate control across the whole scan — see ``significance.py``.

The dimensions themselves are open: ``SEGMENT_DIMENSIONS`` maps a name to a key
function, and adding one is a dictionary entry, not a schema change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.analytics.montecarlo import DEFAULT_SEED, bootstrap_mean
from app.analytics.performance import PnlSummary, TradeCounts, count_trades, summarise_pnl
from app.analytics.significance import (
    ComparisonResult,
    compare_samples,
    control_false_discovery_rate,
)
from app.analytics.statistics import mean
from app.analytics.types import MIN_SAMPLE, Estimate, Reliability, TradeRecord

#: Duration buckets in seconds. Chosen to separate genuinely different trading
#: behaviours — a scalp, an intraday hold, a swing — rather than round numbers.
DURATION_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("under_1m", 0, 60),
    ("1m_to_5m", 60, 300),
    ("5m_to_15m", 300, 900),
    ("15m_to_1h", 900, 3_600),
    ("1h_to_4h", 3_600, 14_400),
    ("over_4h", 14_400, 10**9),
)

WEEKDAY_NAMES = {
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
    7: "sunday",
}


def duration_bucket(trade: TradeRecord) -> str | None:
    if trade.duration_seconds is None:
        return None
    for name, low, high in DURATION_BUCKETS:
        if low <= trade.duration_seconds < high:
            return name
    return None  # pragma: no cover — the final bucket is unbounded


def _weekday(trade: TradeRecord) -> str | None:
    if trade.entry_weekday is None:
        return None
    return WEEKDAY_NAMES.get(trade.entry_weekday)


def _hour(trade: TradeRecord) -> str | None:
    if trade.entry_hour is None:
        return None
    return f"{trade.entry_hour:02d}"


def _month(trade: TradeRecord) -> str | None:
    if trade.session_date is None:
        return None
    return trade.session_date.strftime("%Y-%m")


#: Every dimension the cube can slice by. A key function returning ``None`` excludes the
#: trade from that dimension entirely rather than bucketing it as "unknown" — an
#: "unknown setup" segment would be a meaningless mixture presented as a category.
SEGMENT_DIMENSIONS: dict[str, Callable[[TradeRecord], str | None]] = {
    "hour_of_day": _hour,
    "weekday": _weekday,
    "month": _month,
    "session_segment": lambda trade: (
        trade.session_segment.value if trade.session_segment else None
    ),
    "direction": lambda trade: trade.direction.value,
    "instrument": lambda trade: trade.instrument_root or trade.instrument_symbol,
    "strategy": lambda trade: trade.strategy,
    "setup": lambda trade: trade.setup,
    "market_condition": lambda trade: trade.market_condition,
    "duration_bucket": duration_bucket,
}


@dataclass(frozen=True, slots=True)
class Segment:
    """Performance within one slice."""

    dimension: str
    key: str
    counts: TradeCounts
    pnl: PnlSummary
    expectancy: Estimate
    #: Permutation test of this segment against every *other* trade in the sample.
    #: Populated by :func:`analyse_dimension`, which can then FDR-adjust the family.
    comparison: ComparisonResult | None = None

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(self.counts.total)

    @property
    def is_actionable(self) -> bool:
        """Whether this segment is a finding rather than an observation.

        Requires all three: enough trades to estimate anything, a confidence interval
        that excludes zero, and significance against the rest of the sample after
        multiple-comparison adjustment. Any journal that reports a "best setup" without
        the third is reporting the loudest coincidence in its own scan.
        """
        if self.reliability is not Reliability.RELIABLE:
            return False
        if self.expectancy.interval is None or not self.expectancy.interval.excludes_zero:
            return False
        return bool(self.comparison and self.comparison.is_significant)


@dataclass(frozen=True, slots=True)
class DimensionAnalysis:
    """Every segment along one dimension, plus what can honestly be said about them."""

    dimension: str
    segments: tuple[Segment, ...]
    total_trades: int
    #: Segments too small to interpret. Reported so a trader can see that the
    #: breakdown is thin rather than concluding those hours are bad.
    excluded_segments: tuple[str, ...] = ()

    @property
    def actionable(self) -> tuple[Segment, ...]:
        """Segments that survived every test, best first."""
        return tuple(
            sorted(
                (segment for segment in self.segments if segment.is_actionable),
                key=lambda segment: segment.expectancy.value or Decimal(0),
                reverse=True,
            )
        )

    @property
    def best(self) -> Segment | None:
        """Highest expectancy among *actionable* segments only.

        Deliberately not "highest expectancy overall". The overall maximum in a scan of
        24 hours is a noise statistic, and naming it is how a journal invents an edge.
        """
        actionable = self.actionable
        return actionable[0] if actionable else None

    @property
    def worst(self) -> Segment | None:
        actionable = self.actionable
        return actionable[-1] if actionable and actionable[-1].is_actionable else None


def group_by(
    trades: Sequence[TradeRecord], dimension: str
) -> dict[str, list[TradeRecord]]:
    """Bucket trades along one dimension, skipping those it does not apply to."""
    key_function = SEGMENT_DIMENSIONS.get(dimension)
    if key_function is None:
        raise ValueError(f"unknown segmentation dimension {dimension!r}")

    groups: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        key = key_function(trade)
        if key is None:
            continue
        groups.setdefault(key, []).append(trade)
    return groups


def analyse_dimension(
    trades: Sequence[TradeRecord],
    dimension: str,
    *,
    min_sample: int = MIN_SAMPLE,
    bootstrap_iterations: int = 2_000,
    test_significance: bool = True,
    seed: int = DEFAULT_SEED,
) -> DimensionAnalysis:
    """Compute every segment along one dimension, with significance control.

    Each segment is tested against *the rest of the sample* rather than against another
    segment. "Is Friday worse than everything else?" is the question a trader actually
    has, and pairwise comparison between 24 hours would be 276 tests instead of 24.

    The family of tests is then FDR-adjusted together, so the reported significance
    accounts for how many hypotheses this scan examined.
    """
    groups = group_by(trades, dimension)
    segments: list[Segment] = []
    excluded: list[str] = []
    comparisons: list[ComparisonResult] = []

    for key, group in sorted(groups.items()):
        if len(group) < min_sample:
            excluded.append(key)

        values = [trade.net_pnl for trade in group]
        bootstrap = (
            bootstrap_mean(values, iterations=bootstrap_iterations, seed=seed)
            if len(group) >= MIN_SAMPLE
            else None
        )
        point = mean(values)
        expectancy = (
            Estimate.of(bootstrap.point_estimate, len(group), bootstrap.interval)
            if bootstrap is not None
            else Estimate(
                value=point,
                sample_size=len(group),
                reliability=Reliability.for_sample(len(group)),
                undefined_reason=(
                    f"only {len(group)} trades in this segment; too few for an interval"
                ),
            )
        )

        comparison: ComparisonResult | None = None
        if test_significance:
            # Membership by trade id, not by value. `trade not in group` would be a
            # linear scan comparing every field, making the whole dimension O(n^2) —
            # at 10,000 trades across 24 hourly segments that is minutes, not
            # milliseconds.
            member_ids = {trade.trade_id for trade in group}
            rest = [
                trade.net_pnl for trade in trades if trade.trade_id not in member_ids
            ]
            comparison = compare_samples(
                values,
                rest,
                label_a=f"{dimension}={key}",
                label_b=f"{dimension}!={key}",
                permutations=1_000,
                seed=seed,
            )
            comparisons.append(comparison)

        segments.append(
            Segment(
                dimension=dimension,
                key=key,
                counts=count_trades(group),
                pnl=summarise_pnl(group),
                expectancy=expectancy,
                comparison=comparison,
            )
        )

    if test_significance and comparisons:
        adjusted = {
            (result.label_a, result.label_b): result
            for result in control_false_discovery_rate(comparisons)
        }
        segments = [
            Segment(
                dimension=segment.dimension,
                key=segment.key,
                counts=segment.counts,
                pnl=segment.pnl,
                expectancy=segment.expectancy,
                comparison=adjusted.get(
                    (f"{dimension}={segment.key}", f"{dimension}!={segment.key}"),
                    segment.comparison,
                ),
            )
            for segment in segments
        ]

    return DimensionAnalysis(
        dimension=dimension,
        segments=tuple(segments),
        total_trades=len(trades),
        excluded_segments=tuple(excluded),
    )


def analyse_all_dimensions(
    trades: Sequence[TradeRecord],
    *,
    dimensions: Sequence[str] | None = None,
    test_significance: bool = True,
    seed: int = DEFAULT_SEED,
) -> dict[str, DimensionAnalysis]:
    """Run the full cube.

    Note that significance is FDR-controlled *within* each dimension, not across the
    whole cube. Controlling across every dimension at once would be more conservative
    still, and is the right thing to do when a single "top finding" is surfaced — which
    is why the report layer in milestone 12 re-adjusts across dimensions before ranking.
    """
    selected = dimensions or list(SEGMENT_DIMENSIONS)
    return {
        dimension: analyse_dimension(
            trades, dimension, test_significance=test_significance, seed=seed
        )
        for dimension in selected
    }


@dataclass(frozen=True, slots=True)
class StreakStateAnalysis:
    """Performance conditioned on the current run of wins or losses.

    Directly measures tilt. If expectancy after two consecutive losses is materially
    below baseline, the trader's decision quality is degrading with the streak — one of
    the few genuinely behavioural findings a journal can produce, and one that is
    invisible in every aggregate statistic.
    """

    after_wins: dict[int, Estimate]
    after_losses: dict[int, Estimate]
    baseline: Estimate

    def degradation_after_losses(self, streak: int) -> Decimal | None:
        """How far expectancy falls below baseline after ``streak`` losses."""
        estimate = self.after_losses.get(streak)
        if estimate is None or estimate.value is None or self.baseline.value is None:
            return None
        return estimate.value - self.baseline.value


def analyse_streak_state(
    trades: Sequence[TradeRecord], *, max_streak: int = 3
) -> StreakStateAnalysis:
    """Expectancy conditioned on the preceding run of outcomes.

    Trades are ordered by close time and each is labelled with the streak state *before*
    it was taken. Scratches neither extend nor reset a run.
    """
    ordered = sorted(
        (trade for trade in trades if trade.closed_at is not None),
        key=lambda trade: (trade.closed_at, trade.opened_at),
    )

    after_wins: dict[int, list[Decimal]] = {}
    after_losses: dict[int, list[Decimal]] = {}
    streak = 0
    streak_is_win = True

    for trade in ordered:
        if streak > 0:
            bucket = after_wins if streak_is_win else after_losses
            bucket.setdefault(min(streak, max_streak), []).append(trade.net_pnl)

        if trade.is_scratch:
            continue
        if trade.is_winner == streak_is_win and streak > 0:
            streak += 1
        else:
            streak_is_win = trade.is_winner
            streak = 1

    def estimates(source: dict[int, list[Decimal]]) -> dict[int, Estimate]:
        return {
            count: (
                Estimate.of(value, len(values))
                if (value := mean(values)) is not None
                else Estimate.undefined(len(values), "no trades in this state")
            )
            for count, values in sorted(source.items())
        }

    all_pnl = [trade.net_pnl for trade in ordered]
    baseline_value = mean(all_pnl)
    baseline = (
        Estimate.of(baseline_value, len(all_pnl))
        if baseline_value is not None
        else Estimate.undefined(0, "no closed trades")
    )

    return StreakStateAnalysis(
        after_wins=estimates(after_wins),
        after_losses=estimates(after_losses),
        baseline=baseline,
    )
