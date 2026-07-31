"""Enqueueing and claiming background work.

The claim is the only interesting part, and it is one statement:

``SELECT ... WHERE claimable ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1``

``SKIP LOCKED`` is what makes this a queue rather than a bottleneck. Without it, ten
workers all block on the same oldest row and the pool serialises; with it, each worker
takes the oldest row nobody else holds. ``FOR UPDATE`` is what makes the claim atomic —
two workers cannot take the same job, because the second one's lock request is skipped
rather than queued.

**A job is claimable when it is pending, or when it is running with an expired lease.**
The second clause is the recovery path. A worker killed mid-job leaves a row in
``running`` that no live process owns, and without lease expiry that job is stuck until
somebody notices. With it, the job simply becomes claimable again — which is also why
handlers must be idempotent, since the original worker may have completed the work before
dying.

**Failure is not the same as death.** A handler that raises gets its attempt counted and
``run_after`` pushed forward with exponential backoff; only after ``max_attempts`` does
the job go to ``dead``, where it stops consuming worker time and is visible for a human to
look at. A queue that retries forever turns one poison job into an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.common.enums import JobKind, JobState
from app.infrastructure.db.models.jobs import Job

logger = get_logger(__name__)

#: How long a claim is good for. Long enough for the slowest handler — a pattern scan
#: over a multi-year history — with headroom, because a lease that expires *during* a
#: healthy job lets a second worker start it concurrently, which is the one failure this
#: design must not have.
DEFAULT_LEASE_SECONDS = 900

#: Backoff between attempts: 30s, 2m, 8m, 32m, 2h. Geometric rather than linear because
#: the failures worth retrying are mostly transient (a broker rate limit, a dropped
#: connection) and the ones that are not should reach ``dead`` quickly rather than
#: retrying at a fixed interval all day.
BACKOFF_BASE_SECONDS = 30
BACKOFF_FACTOR = 4

DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class Enqueued:
    """The result of an enqueue, including the case where it was already queued."""

    job_id: UUID
    kind: JobKind
    created: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "kind": self.kind.value,
            #: False when an identical job was already queued. The caller gets the
            #: existing job's id rather than an error, because "this work is already
            #: scheduled" is a success from their point of view.
            "created": self.created,
        }


def backoff_for(attempts: int) -> timedelta:
    """Delay before the next attempt. Capped so a long-lived job cannot be pushed a
    week into the future by a run of failures."""
    seconds = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** max(0, attempts - 1))
    return timedelta(seconds=min(seconds, 7200))


class JobQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        user_id: UUID,
        kind: JobKind,
        payload: dict[str, Any] | None = None,
        account_id: UUID | None = None,
        idempotency_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Enqueued:
        """Queue a job, or return the existing one with the same idempotency key.

        The duplicate check is the database's partial unique index rather than a
        select-then-insert: two requests racing would both pass an application check and
        both insert. Catching the ``IntegrityError`` is the only version that is correct
        under concurrency.
        """
        job = Job(
            id=uuid7(),
            user_id=user_id,
            account_id=account_id,
            kind=kind,
            state=JobState.PENDING,
            payload=payload or {},
            idempotency_key=idempotency_key,
            run_after=run_after or datetime.now(UTC),
            max_attempts=max_attempts,
        )
        self._session.add(job)

        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            if idempotency_key is None:
                raise
            existing = await self.find_by_idempotency_key(user_id, idempotency_key)
            if existing is None:
                raise
            logger.info(
                "jobs.enqueue_deduplicated",
                user_id=str(user_id),
                kind=kind.value,
                job_id=str(existing.id),
            )
            return Enqueued(job_id=existing.id, kind=kind, created=False)

        logger.info(
            "jobs.enqueued", user_id=str(user_id), kind=kind.value, job_id=str(job.id)
        )
        return Enqueued(job_id=job.id, kind=kind, created=True)

    async def find_by_idempotency_key(self, user_id: UUID, key: str) -> Job | None:
        return (
            await self._session.execute(
                select(Job).where(Job.user_id == user_id, Job.idempotency_key == key)
            )
        ).scalar_one_or_none()

    async def claim(
        self,
        *,
        worker: str,
        kinds: tuple[JobKind, ...] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> Job | None:
        """Take the oldest claimable job, atomically.

        Returns ``None`` when there is nothing to do — the normal case, and not an error.
        The caller must commit for the claim to hold; until then the row lock is what
        stops another worker taking it.
        """
        moment = now or datetime.now(UTC)

        statement = (
            select(Job)
            .where(
                Job.run_after <= moment,
                or_(
                    Job.state == JobState.PENDING,
                    # Recovery: a job whose worker died still says "running" and holds
                    # no live lock. Its lease is what makes it claimable again.
                    (Job.state == JobState.RUNNING) & (Job.leased_until < moment),
                ),
            )
            .order_by(Job.run_after, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if kinds:
            statement = statement.where(Job.kind.in_(kinds))

        job = (await self._session.execute(statement)).scalar_one_or_none()
        if job is None:
            return None

        reclaimed = job.state is JobState.RUNNING
        job.state = JobState.RUNNING
        job.attempts += 1
        job.leased_until = moment + timedelta(seconds=lease_seconds)
        job.leased_by = worker
        job.started_at = moment
        await self._session.flush()

        logger.info(
            "jobs.claimed",
            job_id=str(job.id),
            kind=job.kind.value,
            attempt=job.attempts,
            worker=worker,
            reclaimed=reclaimed,
        )
        return job

    async def heartbeat(self, job: Job, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        """Extend a lease mid-job.

        Needed for handlers whose runtime is unbounded in principle — generating reports
        for three years of imported history. Without it a long job's lease expires and a
        second worker starts it alongside the first.
        """
        job.leased_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        await self._session.flush()

    async def succeed(self, job: Job, result: dict[str, Any] | None = None) -> None:
        job.state = JobState.SUCCEEDED
        job.result = result or {}
        job.finished_at = datetime.now(UTC)
        job.leased_until = None
        job.leased_by = None
        if job.started_at is not None:
            job.duration_ms = int(
                (job.finished_at - job.started_at).total_seconds() * 1000
            )
        job.last_error = None
        await self._session.flush()
        logger.info(
            "jobs.succeeded",
            job_id=str(job.id),
            kind=job.kind.value,
            duration_ms=job.duration_ms,
        )

    async def fail(self, job: Job, error: str) -> None:
        """Record a failure, and retire the job if it has run out of attempts.

        The error is stored on the row rather than only logged. A dead-lettered job whose
        cause lives in a log line somewhere is a job nobody will ever diagnose.
        """
        job.last_error = error[:4000]
        job.finished_at = datetime.now(UTC)
        job.leased_until = None
        job.leased_by = None

        if job.attempts >= job.max_attempts:
            job.state = JobState.DEAD
            logger.error(
                "jobs.dead",
                job_id=str(job.id),
                kind=job.kind.value,
                attempts=job.attempts,
                error=error[:500],
            )
        else:
            job.state = JobState.PENDING
            job.run_after = datetime.now(UTC) + backoff_for(job.attempts)
            logger.warning(
                "jobs.retrying",
                job_id=str(job.id),
                kind=job.kind.value,
                attempt=job.attempts,
                next_run=job.run_after.isoformat(),
                error=error[:500],
            )
        await self._session.flush()

    async def get(self, user_id: UUID, job_id: UUID) -> Job | None:
        return (
            await self._session.execute(
                select(Job).where(Job.id == job_id, Job.user_id == user_id)
            )
        ).scalar_one_or_none()

    async def listing(
        self,
        user_id: UUID,
        *,
        state: JobState | None = None,
        kind: JobKind | None = None,
        limit: int = 50,
    ) -> list[Job]:
        statement = (
            select(Job)
            .where(Job.user_id == user_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        if state is not None:
            statement = statement.where(Job.state == state)
        if kind is not None:
            statement = statement.where(Job.kind == kind)
        return list((await self._session.execute(statement)).scalars().all())

    async def cancel(self, user_id: UUID, job_id: UUID) -> bool:
        """Cancel a job that has not started.

        A running job is deliberately *not* cancellable. Setting a flag a handler never
        checks would report a cancellation that did not happen, which is worse than
        refusing — the caller would stop waiting for work that is still writing rows.
        """
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.user_id == user_id,
                Job.state == JobState.PENDING,
            )
            .values(state=JobState.CANCELLED, finished_at=datetime.now(UTC))
            .returning(Job.id)
        )
        # RETURNING rather than rowcount: the async result object does not expose a row
        # count, and "which rows changed" is the question being asked anyway.
        return result.scalar_one_or_none() is not None

    async def counts(self, user_id: UUID) -> dict[str, int]:
        """Per-state totals, for the queue health endpoint."""
        rows = (
            await self._session.execute(
                select(Job.state, func.count())
                .where(Job.user_id == user_id)
                .group_by(Job.state)
            )
        ).all()
        return {row[0].value: row[1] for row in rows}
