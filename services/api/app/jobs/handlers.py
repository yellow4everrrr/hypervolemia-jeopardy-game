"""What each job kind actually does.

Handlers are thin. Every one delegates to the use case the HTTP endpoint already calls,
so a job and a request produce the same result by construction — there is no second
implementation to drift.

**Every handler must be idempotent**, because the queue is at-least-once: a worker can
finish the work, lose its connection before committing the job's state, and have it run
again. The three that append rows say below how they handle a re-run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.use_cases.detect_patterns import DetectPatterns
from app.application.use_cases.generate_report import GenerateReport
from app.application.use_cases.train_models import TrainModels
from app.core.logging import get_logger
from app.domain.common.enums import JobKind, ReportType
from app.infrastructure.db.models.jobs import Job
from app.infrastructure.db.repositories.ml import SqlAlchemyModelRepository
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.infrastructure.db.repositories.reports import SqlAlchemyReportRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork

logger = get_logger(__name__)

Handler = Callable[[AsyncSession, Job], Awaitable[dict[str, Any]]]


def _account_id(job: Job) -> UUID | None:
    if job.account_id is not None:
        return job.account_id
    raw = job.payload.get("account_id")
    return UUID(raw) if isinstance(raw, str) else None


async def detect_patterns(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Re-scan for behavioural and cluster patterns.

    Idempotent: the pattern repository replaces the previous scan's verdicts for the
    period rather than accumulating them, so a re-run converges instead of duplicating.
    """
    outcome = await DetectPatterns(
        repository=SqlAlchemyPatternRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(user_id=job.user_id, account_id=_account_id(job))
    return {
        "trades": outcome.report.sample_size,
        "findings": len(outcome.report.behaviours) + len(outcome.report.clusters),
    }


async def train_models(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Retrain the predictive models.

    ``prediction_models`` is append-only by design — "the model said 68% on the 4th" has
    to stay answerable — so a re-run *does* insert a second row. That is accepted rather
    than deduplicated: the rows are identical apart from their timestamp, the serving
    path reads the newest, and suppressing the duplicate would mean making an
    append-only audit table conditionally not append-only.
    """
    outcome = await TrainModels(
        repository=SqlAlchemyModelRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(user_id=job.user_id, account_id=_account_id(job))
    return {
        "trades": outcome.report.trades,
        "win_probability_deployable": outcome.report.win_probability.is_deployable,
        "expected_r_deployable": outcome.report.expected_r.is_deployable,
    }


async def generate_reports(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Build every completed period that has no report yet.

    Idempotent through the scheduler itself: it skips ``(type, period_start)`` pairs that
    already exist, matching the table's unique constraint. A re-run after a partial
    failure continues from where it stopped.
    """
    raw_types = job.payload.get("report_types")
    types = (
        tuple(ReportType(value) for value in raw_types)
        if isinstance(raw_types, list) and raw_types
        else None
    )
    today = job.payload.get("today")

    outcome = await GenerateReport(
        repository=SqlAlchemyReportRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).run_schedule(
        user_id=job.user_id,
        today=date.fromisoformat(today) if isinstance(today, str) else datetime.now(UTC).date(),
        account_id=_account_id(job),
        types=types,
    )
    return {
        "generated": len(outcome.generated),
        "deferred": outcome.deferred,
    }


#: The dispatch table. A job kind with no handler is a configuration error rather than a
#: runtime surprise: :func:`handler_for` raises, the worker records it, and the job goes
#: to ``dead`` after its attempts rather than silently succeeding.
HANDLERS: dict[JobKind, Handler] = {
    JobKind.DETECT_PATTERNS: detect_patterns,
    JobKind.TRAIN_MODELS: train_models,
    JobKind.GENERATE_REPORTS: generate_reports,
}


class UnknownJobKindError(RuntimeError):
    """Raised when a job's kind has no registered handler."""


def handler_for(kind: JobKind) -> Handler:
    handler = HANDLERS.get(kind)
    if handler is None:
        raise UnknownJobKindError(
            f"{kind.value} has no registered handler; it was enqueued by code that "
            "expects a worker feature which is not deployed"
        )
    return handler
