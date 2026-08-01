"""Generating periodic reports, and deciding which ones are owed.

Two operations, deliberately separate.

``execute`` builds one report for one period. ``run_schedule`` works out which completed
periods have no report yet and builds them oldest-first.

The scheduler is idempotent by construction: it asks the repository which
``(type, period_start)`` pairs already exist and skips them, matching the unique
constraint on the table. That matters because the scheduler will be re-run after every
failure, and a retry that duplicated December would leave two Decembers disagreeing.

**A stored report is immutable.** Regenerating a period produces a new row rather than
updating the old one, for the same reason a trained model does: a report is a record of
what was believed on a date, and "the November report said the overtrading was costing
$3,000" has to stay answerable after the analytics engine changes. Reports carry
``report_version`` so a reader can see where the break is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from app.analytics.types import TradeRecord
from app.core.logging import get_logger
from app.domain.common.enums import ReportType
from app.reports.builder import BuildConfig, BuiltReport, annual_note, build
from app.reports.periods import Period, due_periods, is_complete, period_containing, previous

logger = get_logger(__name__)

#: Cap on how many reports one scheduler run will build. A trader importing three years
#: of history at once has ~800 due periods across five report types, and building them
#: all in one request would time out somewhere in the middle with no record of how far it
#: got. The run reports what it skipped and the next run continues.
MAX_PER_RUN = 25


class ReportRepository(Protocol):
    async def load_trades(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None = None,
        session_from: date | None = None,
        session_to: date | None = None,
    ) -> list[TradeRecord]: ...

    async def session_bounds(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> tuple[date | None, date | None]: ...

    async def existing_periods(
        self, user_id: UUID, *, account_id: UUID | None = None
    ) -> set[tuple[ReportType, date]]: ...

    async def save_report(
        self,
        user_id: UUID,
        *,
        account_id: UUID | None,
        report: BuiltReport,
        payload: dict[str, Any],
    ) -> None: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None: ...


@dataclass
class ScheduleOutcome:
    """What one scheduler run produced."""

    generated: list[dict[str, Any]] = field(default_factory=list)
    skipped_existing: int = 0
    deferred: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "generated": self.generated,
            "generated_count": len(self.generated),
            "skipped_existing": self.skipped_existing,
            "deferred": self.deferred,
        }
        if self.deferred:
            payload["note"] = (
                f"{self.deferred} more period(s) are due and were not built in this run. "
                f"A run builds at most {MAX_PER_RUN}; call again to continue."
            )
        return payload


class GenerateReport:
    """Build periodic reports from the engines that already computed the numbers."""

    def __init__(
        self,
        *,
        repository: ReportRepository,
        uow: UnitOfWork,
        config: BuildConfig | None = None,
    ) -> None:
        self._repository = repository
        self._uow = uow
        self._config = config or BuildConfig()

    async def execute(
        self,
        *,
        user_id: UUID,
        period: Period,
        account_id: UUID | None = None,
        persist: bool = True,
    ) -> BuiltReport:
        """Build — and optionally store — the report for one period.

        The previous period's trades are loaded too, because the comparison is part of
        the report rather than a separate call. Loading them here keeps both samples on
        the same loader, so the two periods cannot be assembled by subtly different
        rules.
        """
        trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=period.start,
            session_to=period.end,
        )
        prior = previous(period)
        prior_trades = await self._repository.load_trades(
            user_id,
            account_id=account_id,
            session_from=prior.start,
            session_to=prior.end,
        )

        report = build(
            period, trades, previous_trades=prior_trades, config=self._config
        )
        note = annual_note(period.report_type)
        if note:
            report.notes.append(note)

        if persist:
            await self._repository.save_report(
                user_id,
                account_id=account_id,
                report=report,
                payload=report.to_payload(),
            )
            await self._uow.commit()

        logger.info(
            "reports.generated",
            user_id=str(user_id),
            report_type=period.report_type.value,
            period_start=period.start.isoformat(),
            trades=report.trades,
            draws_conclusions=report.draws_conclusions,
        )
        return report

    async def run_schedule(
        self,
        *,
        user_id: UUID,
        today: date,
        account_id: UUID | None = None,
        types: tuple[ReportType, ...] | None = None,
        limit: int = MAX_PER_RUN,
    ) -> ScheduleOutcome:
        """Build every completed period that has no report yet, oldest first.

        Oldest-first is not cosmetic. A report records what was believed at a point in
        time, and producing December before November inverts that ordering in the one
        artefact that exists to preserve it.
        """
        earliest, latest = await self._repository.session_bounds(
            user_id, account_id=account_id
        )
        existing = await self._repository.existing_periods(user_id, account_id=account_id)

        kwargs: dict[str, Any] = {
            "today": today,
            "earliest_session": earliest,
            "latest_session": latest,
            "already_generated": existing,
        }
        if types is not None:
            kwargs["types"] = types
        due = due_periods(**kwargs)

        outcome = ScheduleOutcome(skipped_existing=len(existing))
        for period in due[:limit]:
            report = await self.execute(
                user_id=user_id, period=period, account_id=account_id, persist=True
            )
            outcome.generated.append(
                {
                    "period": period.to_payload(),
                    "trades": report.trades,
                    "draws_conclusions": report.draws_conclusions,
                }
            )

        outcome.deferred = max(0, len(due) - limit)
        logger.info(
            "reports.schedule_run",
            user_id=str(user_id),
            generated=len(outcome.generated),
            deferred=outcome.deferred,
        )
        return outcome

    async def preview(
        self,
        *,
        user_id: UUID,
        report_type: ReportType,
        session: date,
        account_id: UUID | None = None,
        today: date,
    ) -> BuiltReport:
        """Build a report without storing it.

        Refuses an incomplete period. A month report generated on the 9th describes nine
        days while carrying the month's label, and every figure in it reads as a claim
        about the whole — including the comparison against last month, which would be
        comparing nine days against thirty.
        """
        period = period_containing(report_type, session)
        if not is_complete(period, today=today):
            raise ValueError(
                f"{period.label} has not finished yet. A report on a partial period "
                "labels nine days' trading as a month and compares it against a full one."
            )
        return await self.execute(
            user_id=user_id, period=period, account_id=account_id, persist=False
        )
