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

from app.application.use_cases.capture_screenshots import CaptureScreenshots
from app.application.use_cases.detect_patterns import DetectPatterns
from app.application.use_cases.generate_report import GenerateReport
from app.application.use_cases.run_simulation import RunSimulation
from app.application.use_cases.train_models import TrainModels
from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.common.enums import JobKind, ReportType
from app.infrastructure.db.models.jobs import Job
from app.infrastructure.db.repositories.ml import SqlAlchemyModelRepository
from app.infrastructure.db.repositories.patterns import SqlAlchemyPatternRepository
from app.infrastructure.db.repositories.reports import SqlAlchemyReportRepository
from app.infrastructure.db.repositories.screenshots import SqlAlchemyScreenshotRepository
from app.infrastructure.db.repositories.simulation import SqlAlchemySimulationRepository
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.storage.objects import build_object_store, screenshot_key

logger = get_logger(__name__)

Handler = Callable[[AsyncSession, Job], Awaitable[dict[str, Any]]]

#: Trades captured in one sweep. Each renders six PNGs, so this bounds the job's runtime
#: rather than its usefulness — whatever is left is picked up by the next sweep, because
#: the query selects trades that still have no screenshots.
CAPTURE_SWEEP_LIMIT = 200


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


async def run_simulation(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Sweep counterfactual rules against the trader's history.

    Queued rather than served inline because it takes roughly a minute: nine scenarios
    re-priced across a year of trades, each bootstrapped. It was a synchronous ``POST``
    until measurement showed 65 seconds on 1,447 trades, which is past most proxy
    timeouts and far past the point where a person believes the page has hung.

    Idempotent for free. The sweep writes nothing — it loads trades, re-prices them and
    returns — and every random draw is seeded, so a re-run produces a byte-identical
    result rather than converging on one.

    **The whole payload is returned**, not a summary, because it lands in ``jobs.result``
    and that is where the UI reads it from. Returning only the headline would mean either
    a second store for the detail or a second computation to recover it, and the second
    computation is the minute this job exists to avoid.
    """
    outcome = await RunSimulation(
        repository=SqlAlchemySimulationRepository(session),
        uow=SqlAlchemyUnitOfWork(session),
    ).execute(
        user_id=job.user_id,
        account_id=_account_id(job),
        permutations=job.payload.get("permutations"),
    )
    return outcome.to_payload()


async def capture_screenshots(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Render and store chart images, for one named trade or for whatever still lacks them.

    With a ``trade_id`` in the payload this captures that trade — the path the UI's
    re-capture button uses. Without one it sweeps, which is the automatic path enqueued
    after every broker sync.

    A sweep rather than a job per trade: a backfill importing six months would otherwise
    put thousands of captures in the queue ahead of every other kind of work. It is also
    idempotent by construction, since a re-run simply finds fewer trades, and it
    self-heals — a trade whose bars had not arrived yet captures nothing and is still
    outstanding for the next sweep to find.

    Idempotent per trade too, through content addressing and an upsert: the render is a
    pure function of the bars, so a re-run writes the same storage key and replaces the
    row rather than duplicating it. A worker that dies between storing the object and
    committing the row redoes both and converges.
    """
    repository = SqlAlchemyScreenshotRepository(session)
    use_case = CaptureScreenshots(
        repository=repository,
        storage=build_object_store(get_settings()),
        uow=SqlAlchemyUnitOfWork(session),
        key_builder=screenshot_key,
    )

    raw = job.payload.get("trade_id")
    if isinstance(raw, str):
        targets = [UUID(raw)]
    else:
        limit = job.payload.get("limit")
        targets = await repository.trades_needing_capture(
            job.user_id, limit=limit if isinstance(limit, int) else CAPTURE_SWEEP_LIMIT
        )

    now = datetime.now(UTC)
    captured = skipped = 0
    for trade_id in targets:
        outcome = await use_case.execute(user_id=job.user_id, trade_id=trade_id, now=now)
        if outcome.skipped:
            skipped += 1
        else:
            captured += len(outcome.captured)

    # `trades_skipped` is reported rather than swallowed: a sweep that captures nothing
    # because no bars are stored is a successful job and a useless one, and the two are
    # indistinguishable from the job state alone.
    return {"trades": len(targets), "frames": captured, "trades_skipped": skipped}


#: The dispatch table. A job kind with no handler is a configuration error rather than a
#: runtime surprise: :func:`handler_for` raises, the worker records it, and the job goes
#: to ``dead`` after its attempts rather than silently succeeding.
HANDLERS: dict[JobKind, Handler] = {
    JobKind.DETECT_PATTERNS: detect_patterns,
    JobKind.TRAIN_MODELS: train_models,
    JobKind.GENERATE_REPORTS: generate_reports,
    JobKind.RUN_SIMULATION: run_simulation,
    JobKind.CAPTURE_SCREENSHOTS: capture_screenshots,
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
