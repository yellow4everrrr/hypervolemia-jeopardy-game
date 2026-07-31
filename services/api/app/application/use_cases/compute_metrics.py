"""Compute and persist performance metrics for a user or account.

Thin by design. All the substance is in ``app.analytics``, which is pure; this use case
only loads trades, calls the engine, and stores what came back. That split is what lets
the statistics be tested exhaustively without a database and the persistence be tested
without recomputing a bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.analytics.drawdown import build_equity_curve
from app.analytics.engine import AnalyticsConfig, AnalyticsReport, analyse
from app.analytics.types import TradeRecord
from app.core.logging import get_logger

logger = get_logger(__name__)


class AnalyticsRepository(Protocol):
    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        closed_only: bool = True,
    ) -> list[TradeRecord]: ...

    async def save_report(
        self,
        report: AnalyticsReport,
        *,
        user_id: UUID,
        account_id: UUID | None,
        period_start: date,
        period_end: date,
    ) -> int: ...

    async def save_equity_curve(
        self, points: Any, *, user_id: UUID, account_id: UUID
    ) -> int: ...

    async def save_risk_metrics(
        self, report: AnalyticsReport, *, user_id: UUID, account_id: UUID | None
    ) -> None: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ComputeMetricsResult:
    user_id: UUID
    account_id: UUID | None
    trades_analysed: int
    metric_rows_written: int
    equity_points_written: int
    report: AnalyticsReport

    @property
    def was_noop(self) -> bool:
        return self.trades_analysed == 0


class ComputePerformanceMetrics:
    """Load trades, run the analytics engine, persist the results."""

    def __init__(
        self,
        *,
        repository: AnalyticsRepository,
        uow: UnitOfWork,
    ) -> None:
        self._repository = repository
        self._uow = uow

    async def execute(
        self,
        *,
        user_id: UUID,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
        starting_equity: Decimal | None = None,
        config: AnalyticsConfig | None = None,
        persist: bool = True,
        now: datetime | None = None,
    ) -> ComputeMetricsResult:
        moment = now or datetime.now(UTC)
        trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
        )

        settings = config or AnalyticsConfig(starting_equity=starting_equity)
        report = analyse(trades, settings, now=moment)

        if not persist or not trades:
            logger.info(
                "analytics.computed",
                user_id=str(user_id),
                trades=len(trades),
                persisted=False,
            )
            return ComputeMetricsResult(
                user_id=user_id,
                account_id=account_id,
                trades_analysed=len(trades),
                metric_rows_written=0,
                equity_points_written=0,
                report=report,
            )

        # The period is taken from the trades themselves rather than the request, so a
        # metric row is always keyed to the data it actually describes.
        period_start = report.period_start or moment.date()
        period_end = report.period_end or moment.date()

        rows = await self._repository.save_report(
            report,
            user_id=user_id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
        await self._repository.save_risk_metrics(
            report, user_id=user_id, account_id=account_id
        )

        points = 0
        if account_id is not None:
            # The equity curve is per account: summing curves across accounts of
            # different sizes produces a line that describes no real account.
            points = await self._repository.save_equity_curve(
                build_equity_curve(trades), user_id=user_id, account_id=account_id
            )

        await self._uow.commit()

        logger.info(
            "analytics.computed",
            user_id=str(user_id),
            account_id=str(account_id) if account_id else None,
            trades=len(trades),
            metric_rows=rows,
            equity_points=points,
            has_edge=report.has_demonstrable_edge,
        )
        return ComputeMetricsResult(
            user_id=user_id,
            account_id=account_id,
            trades_analysed=len(trades),
            metric_rows_written=rows,
            equity_points_written=points,
            report=report,
        )
