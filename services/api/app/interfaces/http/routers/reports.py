"""Periodic report endpoints.

Generation composes the analytics engine and a pattern scan, so it costs seconds and is
always an explicit ``POST``. Milestone 13 moves it behind a worker on a real schedule;
until then ``/reports/run-schedule`` is the schedule, invoked rather than triggered, and
it is idempotent so calling it repeatedly is safe.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.application.use_cases.generate_report import MAX_PER_RUN, GenerateReport
from app.core.errors import NotFoundError, ValidationError
from app.domain.common.enums import ReportType
from app.infrastructure.db.repositories.reports import SqlAlchemyReportRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep
from app.reports.builder import REPORT_VERSION
from app.reports.comparison import COMPARED_METRICS
from app.reports.periods import MIN_SESSIONS_FOR_CONCLUSIONS, PERIODIC_TYPES

router = APIRouter(prefix="/reports", tags=["reports"])


class GenerateRequest(BaseModel):
    report_type: ReportType
    #: Any session date inside the period. The boundary is derived rather than supplied,
    #: so a caller cannot ask for "the month of the 3rd to the 28th".
    session: date
    account_id: UUID | None = None
    persist: bool = True


class ScheduleRequest(BaseModel):
    account_id: UUID | None = None
    report_types: Annotated[list[ReportType] | None, Field(max_length=5)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PER_RUN)] = MAX_PER_RUN


@router.post("/generate", summary="Build the report for one period")
async def generate(
    payload: GenerateRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Compose one periodic report.

    The period is derived from any date inside it, so its boundaries always come from
    the same arithmetic the scheduler uses. An unfinished period is refused: a month
    report generated on the 9th describes nine days while carrying the month's label.
    """
    if payload.report_type not in PERIODIC_TYPES:
        raise ValidationError(
            f"{payload.report_type.value} is not a calendar period",
            details={"supported": [item.value for item in PERIODIC_TYPES]},
        )

    use_case = GenerateReport(
        repository=SqlAlchemyReportRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
    try:
        report = await use_case.preview(
            user_id=user.id,
            report_type=payload.report_type,
            session=payload.session,
            account_id=payload.account_id,
            today=datetime.now(UTC).date(),
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    if payload.persist:
        report = await use_case.execute(
            user_id=user.id,
            period=report.period,
            account_id=payload.account_id,
            persist=True,
        )
    return report.to_payload()


@router.post("/run-schedule", summary="Build every completed period with no report yet")
async def run_schedule(
    payload: ScheduleRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The scheduler, invoked rather than triggered.

    Idempotent: it asks which ``(type, period_start)`` pairs already exist and skips
    them, matching the table's unique constraint. Safe to call repeatedly, which matters
    because it will be re-run after every failure.
    """
    outcome = await GenerateReport(
        repository=SqlAlchemyReportRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).run_schedule(
        user_id=user.id,
        today=datetime.now(UTC).date(),
        account_id=payload.account_id,
        types=tuple(payload.report_types) if payload.report_types else None,
        limit=payload.limit,
    )
    return outcome.to_payload()


@router.get("/latest/{report_type}", summary="The most recent report of a type")
async def latest(
    report_type: ReportType,
    user: CurrentUserDep,
    session: SessionDep,
    account_id: UUID | None = None,
) -> dict[str, Any]:
    stored = await SqlAlchemyReportRepository(session).latest(
        user.id, report_type=report_type, account_id=account_id
    )
    if stored is None:
        raise NotFoundError(f"no {report_type.value} report has been generated yet")
    return stored


@router.get("", summary="List generated reports")
async def listing(
    user: CurrentUserDep,
    session: SessionDep,
    report_type: ReportType | None = None,
    account_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await SqlAlchemyReportRepository(session).listing(
        user.id, report_type=report_type, account_id=account_id, limit=limit
    )
    return {"reports": items, "count": len(items)}


@router.get("/definition", summary="What a report contains and when it concludes")
async def definition() -> dict[str, Any]:
    """The rules a report is built under, published.

    Included because the most important thing about these reports is what they refuse to
    say, and a trader who sees "no change established" needs to know that is a result
    rather than a missing feature.
    """
    return {
        "version": REPORT_VERSION,
        "periods": [item.value for item in PERIODIC_TYPES],
        "sessions_needed_before_concluding": {
            report_type.value: minimum
            for report_type, minimum in MIN_SESSIONS_FOR_CONCLUSIONS.items()
        },
        "compared_metrics": [
            {"key": metric.key, "label": metric.label, "unit": metric.unit}
            for metric in COMPARED_METRICS
        ],
        "notes": [
            "A period-over-period change is reported as a change only when it survives "
            "a permutation test corrected across every metric compared. On a typical "
            "month almost nothing clears that bar, which is the accurate result rather "
            "than a missing feature: two samples of twenty trades cannot distinguish a "
            "real shift from the variation an unchanged process produces.",
            "Leak costs are attributed per trade, not per detector. Detectors overlap — "
            "a trader down on the day, late in the session, after two losses is caught "
            "by three at once — so adding their estimates bills the same money three "
            "times. Both figures are returned so the gap is visible.",
            "Quarterly and annual reports are computed from their own trades, never "
            "aggregated from the shorter reports inside them. Their figures will not "
            "equal the sum of those, and significance does not add up at all.",
        ],
    }
