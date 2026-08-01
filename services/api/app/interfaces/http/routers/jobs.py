"""Background job endpoints.

The three expensive surfaces — pattern scanning, model training and report generation —
have always been explicit ``POST``s that a caller waited seconds for. These endpoints are
the asynchronous half: enqueue the same work, get an id back immediately, poll for the
result.

The synchronous endpoints are deliberately kept. They are what the tests drive, they are
what a single-process local setup uses, and having both means the queue is an
*optimisation* rather than a dependency — a deployment with no worker running still
functions, it is just slower.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.errors import NotFoundError, ValidationError
from app.domain.common.enums import JobKind, JobState, ReportType
from app.infrastructure.db.models.jobs import Job
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.interfaces.http.deps import CurrentUserDep, SessionDep
from app.jobs.handlers import HANDLERS
from app.jobs.queue import JobQueue

router = APIRouter(prefix="/jobs", tags=["jobs"])

#: Kinds a client may enqueue. Deliberately narrower than ``JobKind``: broker sync and
#: excursion measurement are triggered by the sync pipeline itself, and letting a client
#: queue them directly would allow it to schedule broker traffic outside the rate limiter.
ENQUEUEABLE: tuple[JobKind, ...] = (
    JobKind.DETECT_PATTERNS,
    JobKind.TRAIN_MODELS,
    JobKind.GENERATE_REPORTS,
    JobKind.RUN_SIMULATION,
)


class EnqueueRequest(BaseModel):
    kind: JobKind
    account_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Supply to make re-submission safe. A duplicate returns the queued job's id rather
    #: than an error — "this work is already scheduled" is a success for the caller.
    idempotency_key: Annotated[str | None, Field(max_length=200)] = None


def _serialise(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "kind": job.kind.value,
        "state": job.state.value,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_after": job.run_after.isoformat(),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_ms": job.duration_ms,
        "result": job.result,
        # Surfaced rather than logged-only. A dead job whose cause lives in a log line
        # somewhere is a job nobody will diagnose.
        "last_error": job.last_error,
    }


@router.post("", summary="Queue background work")
async def enqueue(
    payload: EnqueueRequest, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Queue one of the expensive operations and return its job id."""
    if payload.kind not in ENQUEUEABLE:
        raise ValidationError(
            f"{payload.kind.value} cannot be queued directly",
            details={"enqueueable": [item.value for item in ENQUEUEABLE]},
        )
    if payload.kind not in HANDLERS:
        raise ValidationError(
            f"{payload.kind.value} has no handler in this deployment",
        )

    body = dict(payload.payload)
    if payload.kind is JobKind.GENERATE_REPORTS:
        raw = body.get("report_types")
        if isinstance(raw, list):
            try:
                body["report_types"] = [ReportType(value).value for value in raw]
            except ValueError as exc:
                raise ValidationError(f"unknown report type: {exc}") from exc
        body.setdefault("today", datetime.now(UTC).date().isoformat())

    queue = JobQueue(session)
    enqueued = await queue.enqueue(
        user_id=user.id,
        kind=payload.kind,
        payload=body,
        account_id=payload.account_id,
        idempotency_key=payload.idempotency_key,
    )
    await SqlAlchemyUnitOfWork(session).commit()
    return enqueued.to_payload()


@router.get("", summary="List jobs")
async def listing(
    user: CurrentUserDep,
    session: SessionDep,
    state: JobState | None = None,
    kind: JobKind | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    jobs = await JobQueue(session).listing(user.id, state=state, kind=kind, limit=limit)
    return {"jobs": [_serialise(job) for job in jobs], "count": len(jobs)}


@router.get("/queue", summary="Queue depth by state")
async def queue_health(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """Per-state counts, plus the two numbers worth alerting on.

    ``dead`` is work that will never run without intervention. ``pending`` climbing while
    ``running`` stays at zero means no worker is consuming the queue — the failure mode a
    depth metric alone would not distinguish from "busy".
    """
    counts = await JobQueue(session).counts(user.id)
    return {
        "counts": counts,
        "dead": counts.get(JobState.DEAD.value, 0),
        "pending": counts.get(JobState.PENDING.value, 0),
        "running": counts.get(JobState.RUNNING.value, 0),
    }


@router.get("/{job_id}", summary="One job")
async def get_job(job_id: UUID, user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    job = await JobQueue(session).get(user.id, job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found")
    return _serialise(job)


@router.post("/{job_id}/cancel", summary="Cancel a job that has not started")
async def cancel(job_id: UUID, user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """Cancel a pending job.

    A *running* job is deliberately not cancellable. Setting a flag no handler checks
    would report a cancellation that did not happen, and the caller would stop waiting
    for work that is still writing rows.
    """
    cancelled = await JobQueue(session).cancel(user.id, job_id)
    if not cancelled:
        raise ValidationError(
            "only a job that has not started can be cancelled",
            details={"job_id": str(job_id)},
        )
    await SqlAlchemyUnitOfWork(session).commit()
    return {"id": str(job_id), "state": JobState.CANCELLED.value}
