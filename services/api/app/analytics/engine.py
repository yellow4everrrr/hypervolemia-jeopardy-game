"""The analytics engine: one call, one complete evidence payload.

This is the module the rest of the product talks to. It runs every statistic over a
sample of trades and returns a single serialisable structure — the same structure that
is stored in ``performance_metrics.metrics`` and handed to the AI layer as
``ai_analyses.input_metrics``.

That equivalence is the point. The AI contract (ADR 0002) says the model may only cite
statistics it was given, and validating that requires the given set to be a concrete,
enumerable object. :meth:`AnalyticsReport.to_payload` produces it, and
:meth:`AnalyticsReport.metric_keys` enumerates every citable key so the validator has
something to check against.

**Every number here is computed, never estimated by a model.** Every one carries its
sample size. Every one that is undefined says why, in a sentence a language model can
quote rather than a null it might paper over.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.analytics import performance
from app.analytics.drawdown import (
    DrawdownSummary,
    StreakSummary,
    build_equity_curve,
    daily_pnl,
    max_daily_drawdown,
    summarise_drawdown,
    summarise_streaks,
)
from app.analytics.montecarlo import (
    DEFAULT_SEED,
    MonteCarloResult,
    expectancy_with_interval,
    monte_carlo,
    probability_of_profit,
    risk_of_ruin,
)
from app.analytics.performance import PerformanceSummary
from app.analytics.riskadjusted import (
    KellyResult,
    RatioResult,
    kelly_fraction,
    mar_ratio,
    sharpe_ratio,
    sortino_ratio,
)
from app.analytics.segmentation import (
    DimensionAnalysis,
    StreakStateAnalysis,
    analyse_all_dimensions,
    analyse_streak_state,
)
from app.analytics.significance import EdgeTest, test_for_edge
from app.analytics.types import Estimate, Reliability, TradeRecord

#: Bumped whenever a formula changes. Stored on every persisted metric row so stale
#: results are identifiable and recomputable rather than silently mixed with new ones.
ENGINE_VERSION = 1


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """Knobs, all with defensible defaults.

    Exposed so a fast dashboard render and a thorough monthly report can trade accuracy
    against time without either forking the engine.
    """

    starting_equity: Decimal | None = None
    risk_free_rate: Decimal = Decimal(0)
    bootstrap_iterations: int = 10_000
    monte_carlo_iterations: int = 5_000
    ruin_horizon_trades: int = 1_000
    ruin_fraction: Decimal = Decimal("0.5")
    include_segmentation: bool = True
    #: Significance testing dominates runtime on a large cube. Off for interactive
    #: renders, on for reports — but ``Segment.is_actionable`` then returns False,
    #: so nothing untested can be presented as a finding either way.
    test_significance: bool = True
    segmentation_dimensions: tuple[str, ...] | None = None
    seed: int = DEFAULT_SEED


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    """Everything the engine computed for one sample."""

    sample_size: int
    period_start: date | None
    period_end: date | None
    computed_at: datetime
    engine_version: int

    core: PerformanceSummary
    expectancy: Estimate
    drawdown: DrawdownSummary
    streaks: StreakSummary
    max_daily_drawdown: Estimate
    sharpe: RatioResult
    sortino: RatioResult
    mar: Estimate
    kelly: KellyResult
    edge_test: EdgeTest
    risk_of_ruin: Estimate
    probability_of_profit: Estimate
    monte_carlo: MonteCarloResult | None
    streak_state: StreakStateAnalysis
    segments: dict[str, DimensionAnalysis] = field(default_factory=dict)
    daily_pnl: dict[date, Decimal] = field(default_factory=dict)

    @property
    def reliability(self) -> Reliability:
        return Reliability.for_sample(self.sample_size)

    @property
    def has_demonstrable_edge(self) -> bool:
        return self.edge_test.has_demonstrable_edge

    def to_payload(self) -> dict[str, Any]:
        """Serialise for storage and for the AI layer.

        Decimals become strings throughout: this is stored as JSONB and read by a
        model, and a float here would reintroduce the imprecision the engine spent its
        whole implementation avoiding.
        """
        return {
            "engine_version": self.engine_version,
            "computed_at": self.computed_at.isoformat(),
            "period": {
                "start": self.period_start.isoformat() if self.period_start else None,
                "end": self.period_end.isoformat() if self.period_end else None,
            },
            "sample": {
                "trades": self.sample_size,
                "reliability": self.reliability.value,
                "winners": self.core.counts.winners,
                "losers": self.core.counts.losers,
                "scratches": self.core.counts.scratches,
            },
            "pnl": {
                "net": _s(self.core.pnl.net_pnl),
                "gross": _s(self.core.pnl.gross_pnl),
                "commission": _s(self.core.pnl.commission),
                "fees": _s(self.core.pnl.fees),
                "gross_profit": _s(self.core.pnl.gross_profit),
                "gross_loss": _s(self.core.pnl.gross_loss),
                "cost_ratio": _s(self.core.pnl.cost_ratio),
                "largest_win": _s(self.core.pnl.largest_win),
                "largest_loss": _s(self.core.pnl.largest_loss),
                "average_win": _s(self.core.pnl.average_win),
                "average_loss": _s(self.core.pnl.average_loss),
                "median_win": _s(self.core.pnl.median_win),
                "median_loss": _s(self.core.pnl.median_loss),
            },
            "core": {
                "win_rate": _s(self.core.counts.win_rate),
                "profit_factor": _s(self.core.pnl.profit_factor),
                "payoff_ratio": _s(self.core.pnl.payoff_ratio),
                "expectancy": self.expectancy.to_payload(),
                "expectancy_r": self.core.expectancy_r.to_payload(),
                "sqn": self.core.sqn.to_payload(),
                "edge_ratio": self.core.edge_ratio.to_payload(),
                "average_hold_seconds": self.core.hold_time.to_payload(),
                "hold_seconds_winners": self.core.hold_time_by_outcome["winners"].to_payload(),
                "hold_seconds_losers": self.core.hold_time_by_outcome["losers"].to_payload(),
            },
            "distribution": {
                "pnl": _distribution(self.core.pnl_distribution),
                "r": _distribution(self.core.r_distribution),
            },
            "drawdown": {
                "max": _s(self.drawdown.max_drawdown),
                "max_pct": _s(self.drawdown.max_drawdown_pct),
                "average": _s(self.drawdown.average_drawdown),
                "current": _s(self.drawdown.current_drawdown),
                "recovery_factor": _s(self.drawdown.recovery_factor),
                "time_underwater_pct": _s(self.drawdown.time_underwater_pct),
                "longest_drawdown_trades": self.drawdown.longest_drawdown_trades,
                "longest_recovery_trades": self.drawdown.longest_recovery_trades,
                "max_daily": self.max_daily_drawdown.to_payload(),
                "periods": len(self.drawdown.periods),
            },
            "streaks": {
                "longest_wins": self.streaks.longest_win_streak,
                "longest_losses": self.streaks.longest_loss_streak,
                "current": self.streaks.current_streak,
                "current_is_wins": self.streaks.current_streak_is_wins,
                "average_win_streak": _s(self.streaks.average_win_streak),
                "average_loss_streak": _s(self.streaks.average_loss_streak),
            },
            "risk_adjusted": {
                "sharpe": _ratio(self.sharpe),
                "sortino": _ratio(self.sortino),
                "mar": self.mar.to_payload(),
            },
            "position_sizing": {
                "kelly_full": _s(self.kelly.full),
                "kelly_half": _s(self.kelly.half),
                "kelly_win_rate": _s(self.kelly.win_rate),
                "kelly_payoff_ratio": _s(self.kelly.payoff_ratio),
                "kelly_sample_size": self.kelly.sample_size,
                "kelly_warnings": list(self.kelly.warnings),
                "kelly_undefined_reason": self.kelly.undefined_reason,
            },
            "risk": {
                "risk_of_ruin": self.risk_of_ruin.to_payload(),
                "probability_of_profit_next_100": self.probability_of_profit.to_payload(),
                "monte_carlo": _monte_carlo(self.monte_carlo),
            },
            "edge_test": {
                "expectancy": _s(self.edge_test.expectancy),
                "p_value": _s(self.edge_test.p_value),
                "interval_low": _s(self.edge_test.interval_low),
                "interval_high": _s(self.edge_test.interval_high),
                "has_demonstrable_edge": self.edge_test.has_demonstrable_edge,
                "undefined_reason": self.edge_test.undefined_reason,
            },
            "streak_state": {
                "baseline": self.streak_state.baseline.to_payload(),
                "after_losses": {
                    str(streak): estimate.to_payload()
                    for streak, estimate in self.streak_state.after_losses.items()
                },
                "after_wins": {
                    str(streak): estimate.to_payload()
                    for streak, estimate in self.streak_state.after_wins.items()
                },
            },
            "segments": {
                dimension: _dimension(analysis)
                for dimension, analysis in self.segments.items()
            },
        }

    def metric_keys(self) -> set[str]:
        """Every citable metric path, as dotted keys.

        The allow-list the AI layer validates its claims against. A model that mentions
        ``core.win_rate`` is quoting evidence; one that mentions a key absent from this
        set is inventing, and the analysis is rejected before it is stored.
        """
        keys: set[str] = set()

        def walk(prefix: str, node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    keys.add(path)
                    walk(path, value)

        walk("", self.to_payload())
        return keys


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _distribution(summary: performance.DistributionSummary) -> dict[str, Any]:
    return {
        "sample_size": summary.sample_size,
        "mean": _s(summary.mean),
        "median": _s(summary.median),
        "stdev": _s(summary.stdev),
        "skewness": _s(summary.skewness),
        "excess_kurtosis": _s(summary.excess_kurtosis),
        "min": _s(summary.minimum),
        "max": _s(summary.maximum),
        "is_positively_skewed": summary.is_positively_skewed,
        "has_fat_tails": summary.has_fat_tails,
        "percentiles": {key: _s(value) for key, value in summary.percentiles.items()},
    }


def _ratio(result: RatioResult) -> dict[str, Any]:
    return {
        "value": _s(result.value),
        "basis": result.basis.value,
        "observations": result.observations,
        "periods_per_year": result.periods_per_year,
        "is_comparable": result.is_comparable,
        "undefined_reason": result.undefined_reason,
    }


def _monte_carlo(result: MonteCarloResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "iterations": result.iterations,
        "trades_per_path": result.trades_per_path,
        "probability_of_loss": _s(result.probability_of_loss),
        "probability_of_ruin": _s(result.probability_of_ruin),
        "ruin_threshold": _s(result.ruin_threshold),
        "median_final_equity": _s(result.median_final_equity),
        "worst_final_equity": _s(result.worst_final_equity),
        "best_final_equity": _s(result.best_final_equity),
        "is_survivable": result.is_survivable,
        "final_equity_percentiles": {
            key: _s(value) for key, value in result.final_equity_percentiles.items()
        },
        "max_drawdown_percentiles": {
            key: _s(value) for key, value in result.max_drawdown_percentiles.items()
        },
    }


def _dimension(analysis: DimensionAnalysis) -> dict[str, Any]:
    return {
        "total_trades": analysis.total_trades,
        "excluded_segments": list(analysis.excluded_segments),
        "actionable_count": len(analysis.actionable),
        "best": analysis.best.key if analysis.best else None,
        "worst": analysis.worst.key if analysis.worst else None,
        "segments": {
            segment.key: {
                "trades": segment.counts.total,
                "win_rate": _s(segment.counts.win_rate),
                "net_pnl": _s(segment.pnl.net_pnl),
                "profit_factor": _s(segment.pnl.profit_factor),
                "expectancy": segment.expectancy.to_payload(),
                "reliability": segment.reliability.value,
                "is_actionable": segment.is_actionable,
                "p_value": (
                    _s(segment.comparison.p_value) if segment.comparison else None
                ),
                "adjusted_p_value": (
                    _s(segment.comparison.adjusted_p_value) if segment.comparison else None
                ),
                "effect_size": (
                    _s(segment.comparison.effect_size) if segment.comparison else None
                ),
                "effect_magnitude": (
                    segment.comparison.effect_magnitude if segment.comparison else None
                ),
            }
            for segment in analysis.segments
        },
    }


def analyse(
    trades: Sequence[TradeRecord],
    config: AnalyticsConfig | None = None,
    *,
    now: datetime | None = None,
) -> AnalyticsReport:
    """Run the full analytics suite over a sample of trades.

    Pure: no database, no clock unless injected, no configuration read from the
    environment. The same trades and the same config always produce the same report,
    which is what makes the AI layer's evidence reproducible and the tests exact.
    """
    settings = config or AnalyticsConfig()
    moment = now or datetime.now(UTC)

    curve = build_equity_curve(trades)
    drawdown = summarise_drawdown(curve)
    dates = [trade.session_date for trade in trades if trade.session_date is not None]

    monte = monte_carlo(
        trades,
        iterations=settings.monte_carlo_iterations,
        starting_equity=settings.starting_equity,
        ruin_fraction=settings.ruin_fraction,
        seed=settings.seed,
    )

    ruin = (
        risk_of_ruin(
            trades,
            starting_equity=settings.starting_equity,
            ruin_fraction=settings.ruin_fraction,
            horizon_trades=settings.ruin_horizon_trades,
            iterations=settings.monte_carlo_iterations,
            seed=settings.seed,
        )
        if settings.starting_equity is not None
        else Estimate.undefined(
            len(trades),
            "risk of ruin needs a starting equity; set it on the account to enable this",
        )
    )

    mar = (
        mar_ratio(
            trades,
            starting_equity=settings.starting_equity,
            max_drawdown=drawdown.max_drawdown,
        )
        if settings.starting_equity is not None
        else Estimate.undefined(
            len(trades), "MAR needs a starting equity to express return as a percentage"
        )
    )

    return AnalyticsReport(
        sample_size=len(trades),
        period_start=min(dates) if dates else None,
        period_end=max(dates) if dates else None,
        computed_at=moment,
        engine_version=ENGINE_VERSION,
        core=performance.summarise(trades),
        expectancy=expectancy_with_interval(
            trades, iterations=settings.bootstrap_iterations, seed=settings.seed
        ),
        drawdown=drawdown,
        streaks=summarise_streaks(trades),
        max_daily_drawdown=max_daily_drawdown(trades),
        sharpe=sharpe_ratio(
            trades,
            starting_equity=settings.starting_equity,
            risk_free_rate=settings.risk_free_rate,
        ),
        sortino=sortino_ratio(trades, starting_equity=settings.starting_equity),
        mar=mar,
        kelly=kelly_fraction(trades),
        edge_test=test_for_edge(
            [trade.net_pnl for trade in trades],
            iterations=settings.bootstrap_iterations,
            seed=settings.seed,
        ),
        risk_of_ruin=ruin,
        probability_of_profit=probability_of_profit(trades, seed=settings.seed),
        monte_carlo=monte,
        streak_state=analyse_streak_state(trades),
        segments=(
            analyse_all_dimensions(
                trades,
                dimensions=settings.segmentation_dimensions,
                test_significance=settings.test_significance,
                seed=settings.seed,
            )
            if settings.include_segmentation
            else {}
        ),
        daily_pnl=daily_pnl(trades),
    )
