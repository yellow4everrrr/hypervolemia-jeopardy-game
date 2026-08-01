"""Loading trades for a report, and storing what came out.

Reuses the pattern repository's loader, for the third and final time in this codebase and
for the same reason each time: two loaders would eventually disagree about which trades
count, and the monthly report's win rate would differ from the dashboard's by a trade
with nothing to say which is right.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.types import TradeRecord
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import ReportStatus, ReportType
from app.infrastructure.db.models.analytics import Report
from app.infrastructure.db.models.trading import Trade
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.reports.builder import BuiltReport

logger = get_logger(__name__)


class SqlAlchemyReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trades = SqlAlchemyPatternRepository(session)

    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]:
        return await self._trades.load_trades(
            user_id,
            account_id=account_id,
            session_from=session_from,
            session_to=session_to,
        )

    async def session_bounds(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> tuple[date | None, date | None]:
        """First and last session the trader has any activity in.

        The scheduler needs these to avoid generating reports for every period since the
        epoch. Computed in the database rather than by loading trades, because on a
        multi-year history this runs on every scheduler pass and the alternative would
        pull the whole table to read two dates.
        """
        statement = select(
            func.min(Trade.session_date), func.max(Trade.session_date)
        ).where(Trade.user_id == user_id, Trade.session_date.is_not(None))
        if account_id is not None:
            statement = statement.where(Trade.account_id == account_id)

        row = (await self._session.execute(statement)).one()
        return row[0], row[1]

    async def existing_periods(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> set[tuple[ReportType, date]]:
        """Which ``(type, period_start)`` pairs already have a report.

        Keyed to match the table's unique constraint, so the scheduler's idea of
        "already generated" and the database's cannot drift apart.
        """
        statement = select(Report.report_type, Report.period_start).where(
            Report.user_id == user_id
        )
        if account_id is not None:
            statement = statement.where(Report.account_id == account_id)

        rows = (await self._session.execute(statement)).all()
        return {(row[0], row[1]) for row in rows}

    async def save_report(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None,
        report: BuiltReport,
        payload: dict[str, Any],
    ) -> None:
        """Store a report as an immutable record of what was believed on a date.

        ``metrics_snapshot`` holds the evidence the report was built from rather than a
        reference to live data. Re-running analytics next month would otherwise change
        last month's report, and a record whose contents move is not a record.
        """
        sections = {section.key: section for section in report.sections}
        performance = sections.get("performance")
        leaks = sections.get("leaks")

        row = Report(
            id=uuid7(),
            user_id=user_id,
            account_id=account_id,
            report_type=report.period.report_type,
            status=ReportStatus.READY,
            period_start=report.period.start,
            period_end=report.period.end,
            metrics_snapshot=performance.payload if performance is not None else {},
            findings=payload,
            summary=report.headline,
            generated_at=datetime.now(UTC),
        )
        self._session.add(row)

        logger.info(
            "reports.stored",
            user_id=str(user_id),
            report_type=report.period.report_type.value,
            period_start=report.period.start.isoformat(),
            leak_total=(leaks.payload.get("total_cost") if leaks is not None else None),
        )

    async def latest(
        self,
        user_id: UUID,
        *,
        report_type: ReportType,
        account_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        statement = (
            select(Report)
            .where(Report.user_id == user_id, Report.report_type == report_type)
            .order_by(desc(Report.period_start), desc(Report.generated_at))
            .limit(1)
        )
        if account_id is not None:
            statement = statement.where(Report.account_id == account_id)

        row = (await self._session.execute(statement)).scalars().first()
        return _to_payload(row) if row is not None else None

    async def listing(
        self,
        user_id: UUID,
        *,
        report_type: ReportType | None = None,
        account_id: UUID | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        statement = (
            select(Report)
            .where(Report.user_id == user_id)
            .order_by(desc(Report.period_start))
            .limit(limit)
        )
        if report_type is not None:
            statement = statement.where(Report.report_type == report_type)
        if account_id is not None:
            statement = statement.where(Report.account_id == account_id)

        rows = (await self._session.execute(statement)).scalars().all()
        return [_summary(row) for row in rows]


def _summary(row: Report) -> dict[str, Any]:
    """List view: enough to choose a report, without its whole evidence base."""
    return {
        "id": str(row.id),
        "report_type": row.report_type.value,
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "status": row.status.value,
        "summary": row.summary,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
    }


def _to_payload(row: Report) -> dict[str, Any]:
    return {**_summary(row), "report": row.findings}
