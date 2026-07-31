"""Reading trades for analysis and persisting the results.

Two directions:

**Reading.** Trades are loaded as flat :class:`TradeRecord` objects rather than ORM
entities. The analytics engine must be runnable over a list built in a test or produced
by a what-if simulation, so it cannot depend on rows that know how to lazy-load.

**Writing.** Computed metrics land in ``performance_metrics`` (one row per slice per
period), ``equity_curve_points`` (the hypertable the equity chart scans) and
``risk_metrics`` (forward-looking estimates with their assumptions). All three are
upserted on their natural keys, so recomputation converges rather than accumulating
duplicate history.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.drawdown import EquityPoint
from app.analytics.engine import AnalyticsReport
from app.analytics.types import TradeRecord
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import MetricScope, TradeStatus
from app.infrastructure.db.models.analytics import (
    EquityCurvePoint,
    PerformanceMetric,
    RiskMetric,
)
from app.infrastructure.db.models.catalog import Setup, Strategy
from app.infrastructure.db.models.instruments import Instrument
from app.infrastructure.db.models.trading import Trade

logger = get_logger(__name__)


class SqlAlchemyAnalyticsRepository:
    """Loads trades for analysis and stores what the engine computed."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Reading ---------------------------------------------------------------

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        closed_only: bool = True,
    ) -> list[TradeRecord]:
        """Load trades as analysis records.

        Closed trades only by default. An open trade's P&L is unrealized, and including
        it would let a position that has not resolved move the expectancy, the drawdown
        and every ratio built on them.
        """
        statement: Select[Any] = (
            select(
                Trade,
                Instrument.root_symbol,
                Instrument.symbol,
                Strategy.name,
                Setup.name,
            )
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .outerjoin(Strategy, Strategy.id == Trade.strategy_id)
            .outerjoin(Setup, Setup.id == Trade.setup_id)
            .where(Trade.user_id == user_id)
            .order_by(Trade.opened_at)
        )
        if closed_only:
            statement = statement.where(Trade.status == TradeStatus.CLOSED)
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)
        if session_from is not None:
            statement = statement.where(Trade.session_date >= session_from)
        if session_to is not None:
            statement = statement.where(Trade.session_date <= session_to)

        rows = (await self._session.execute(statement)).all()
        return [
            TradeRecord(
                trade_id=trade.id,
                account_id=trade.account_id,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                direction=trade.direction,
                net_pnl=trade.net_pnl,
                gross_pnl=trade.gross_pnl,
                commission=trade.commission,
                fees=trade.fees,
                quantity=trade.quantity_opened,
                r_multiple=trade.realized_r,
                duration_seconds=trade.duration_seconds,
                session_date=trade.session_date,
                session_segment=trade.session_segment,
                entry_hour=trade.entry_hour,
                entry_weekday=trade.entry_weekday,
                instrument_symbol=symbol,
                instrument_root=root,
                strategy=strategy_name,
                setup=setup_name,
                mae_r=trade.mae_r,
                mfe_r=trade.mfe_r,
            )
            for trade, root, symbol, strategy_name, setup_name in rows
        ]

    # --- Writing ---------------------------------------------------------------

    async def save_report(
        self,
        report: AnalyticsReport,
        *,
        user_id: UUID,
        account_id: UUID | None,
        period_start: date,
        period_end: date,
    ) -> int:
        """Persist the overall metric set plus every segment as its own row.

        One row per slice rather than a single blob: the dashboard filters and sorts on
        ``expectancy`` and ``win_rate`` across segments, and a nested document would
        force it to read everything to rank anything.
        """
        rows: list[dict[str, Any]] = [
            self._metric_row(
                user_id=user_id,
                account_id=account_id,
                scope=MetricScope.OVERALL,
                scope_key="",
                period_start=period_start,
                period_end=period_end,
                sample_size=report.sample_size,
                metrics=report.to_payload(),
                net_pnl=report.core.pnl.net_pnl,
                expectancy=report.expectancy.value,
                expectancy_r=report.core.expectancy_r.value,
                win_rate=report.core.counts.win_rate,
                profit_factor=report.core.pnl.profit_factor,
            )
        ]

        for dimension, analysis in report.segments.items():
            scope = _DIMENSION_TO_SCOPE.get(dimension)
            if scope is None:
                continue
            for segment in analysis.segments:
                rows.append(
                    self._metric_row(
                        user_id=user_id,
                        account_id=account_id,
                        scope=scope,
                        scope_key=segment.key,
                        period_start=period_start,
                        period_end=period_end,
                        sample_size=segment.counts.total,
                        metrics={
                            "expectancy": segment.expectancy.to_payload(),
                            "reliability": segment.reliability.value,
                            "is_actionable": segment.is_actionable,
                            "adjusted_p_value": (
                                str(segment.comparison.adjusted_p_value)
                                if segment.comparison and segment.comparison.adjusted_p_value
                                else None
                            ),
                            "effect_size": (
                                str(segment.comparison.effect_size)
                                if segment.comparison and segment.comparison.effect_size
                                else None
                            ),
                        },
                        net_pnl=segment.pnl.net_pnl,
                        expectancy=segment.expectancy.value,
                        expectancy_r=None,
                        win_rate=segment.counts.win_rate,
                        profit_factor=segment.pnl.profit_factor,
                    )
                )

        statement = insert(PerformanceMetric).values(rows)
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_performance_metrics_slice",
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "sample_size",
                        "metrics",
                        "net_pnl",
                        "expectancy",
                        "expectancy_r",
                        "win_rate",
                        "profit_factor",
                        "computed_at",
                    )
                },
            )
        )
        logger.info(
            "analytics.metrics_saved",
            user_id=str(user_id),
            rows=len(rows),
            sample_size=report.sample_size,
        )
        return len(rows)

    def _metric_row(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None,
        scope: MetricScope,
        scope_key: str,
        period_start: date,
        period_end: date,
        sample_size: int,
        metrics: dict[str, Any],
        net_pnl: Decimal | None,
        expectancy: Decimal | None,
        expectancy_r: Decimal | None,
        win_rate: Decimal | None,
        profit_factor: Decimal | None,
    ) -> dict[str, Any]:
        from app.analytics.engine import ENGINE_VERSION

        return {
            "id": uuid7(),
            "user_id": user_id,
            "account_id": account_id,
            "scope": scope,
            "scope_key": scope_key,
            "period_start": period_start,
            "period_end": period_end,
            "sample_size": sample_size,
            "metrics": metrics,
            "net_pnl": net_pnl,
            "expectancy": expectancy,
            "expectancy_r": expectancy_r,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "engine_version": ENGINE_VERSION,
        }

    async def save_equity_curve(
        self, points: Sequence[EquityPoint], *, user_id: UUID, account_id: UUID
    ) -> int:
        """Replace the account's equity curve.

        Delete-then-insert rather than upsert: a rebuild can *renumber* the curve when a
        corrected trade changes the ordering, and upserting by timestamp would leave
        stale points from the old sequence interleaved with the new one.
        """
        await self._session.execute(
            delete(EquityCurvePoint).where(
                EquityCurvePoint.user_id == user_id,
                EquityCurvePoint.account_id == account_id,
            )
        )
        if not points:
            return 0

        await self._session.execute(
            insert(EquityCurvePoint).values(
                [
                    {
                        "user_id": user_id,
                        "account_id": account_id,
                        "recorded_at": point.at,
                        "cumulative_net_pnl": point.cumulative_pnl,
                        "cumulative_r": point.cumulative_r,
                        "peak_equity": point.peak,
                        "drawdown": point.drawdown,
                        "trade_number": point.index,
                    }
                    for point in points
                ]
            )
        )
        return len(points)

    async def save_risk_metrics(
        self, report: AnalyticsReport, *, user_id: UUID, account_id: UUID | None
    ) -> None:
        """Store forward-looking risk estimates together with their assumptions.

        The assumptions are not decoration: a risk-of-ruin figure without its horizon,
        ruin threshold and iteration count cannot be interpreted or reproduced.
        """
        from app.analytics.engine import ENGINE_VERSION

        monte = report.monte_carlo
        values = {
            "id": uuid7(),
            "user_id": user_id,
            "account_id": account_id,
            "as_of": report.computed_at,
            "sample_size": report.sample_size,
            "kelly_fraction": report.kelly.full,
            "half_kelly_fraction": report.kelly.half,
            "risk_of_ruin": report.risk_of_ruin.value,
            "max_drawdown": report.drawdown.max_drawdown,
            "max_drawdown_pct": report.drawdown.max_drawdown_pct,
            "average_drawdown": report.drawdown.average_drawdown,
            "recovery_factor": report.drawdown.recovery_factor,
            "mar_ratio": report.mar.value,
            "simulation": {
                "iterations": monte.iterations if monte else None,
                "trades_per_path": monte.trades_per_path if monte else None,
                "probability_of_loss": str(monte.probability_of_loss) if monte else None,
                "final_equity_percentiles": (
                    {key: str(value) for key, value in monte.final_equity_percentiles.items()}
                    if monte
                    else {}
                ),
            },
            "assumptions": {
                "resampling": "iid bootstrap with replacement",
                "note": (
                    "resampling assumes trades are independent draws; losing streaks "
                    "cluster in practice, so ruin estimates are if anything optimistic"
                ),
                "kelly_warnings": list(report.kelly.warnings),
            },
            "engine_version": ENGINE_VERSION,
        }

        statement = insert(RiskMetric).values(values)
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_risk_metrics_user_account_as_of",
                set_={
                    key: getattr(statement.excluded, key)
                    for key in ("risk_of_ruin", "kelly_fraction", "simulation", "assumptions")
                },
            )
        )


#: Which segmentation dimensions get their own persisted scope. Dimensions absent here
#: are still computed and returned in the payload; they simply are not indexed as rows,
#: because nothing sorts or filters on them yet.
_DIMENSION_TO_SCOPE = {
    "hour_of_day": MetricScope.HOUR_OF_DAY,
    "weekday": MetricScope.WEEKDAY,
    "month": MetricScope.MONTH,
    "session_segment": MetricScope.SESSION_SEGMENT,
    "direction": MetricScope.DIRECTION,
    "instrument": MetricScope.INSTRUMENT,
    "strategy": MetricScope.STRATEGY,
    "setup": MetricScope.SETUP,
    "market_condition": MetricScope.MARKET_CONDITION,
    "duration_bucket": MetricScope.DURATION_BUCKET,
}
