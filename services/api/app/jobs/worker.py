"""The worker loop.

One job per transaction. Claim, set the tenant context, run the handler, record the
outcome, commit — and if anything raises, roll back and record the failure in a *separate*
transaction, because the failing one cannot be trusted to write anything.

**The tenant context is set from the job row, not from the handler.** Every request-driven
path gets its ``user_id`` from an authenticated principal; a worker has no principal, and
this is precisely where the standing "every repository requires ``user_id``" rule is most
likely to be broken by a future handler — there is no auth object to remind anyone. So the
worker sets the row-level-security GUC from ``job.user_id`` before dispatching, and
Postgres enforces the boundary underneath whatever the handler does.

**Polling, not notification.** ``LISTEN``/``NOTIFY`` would remove the poll delay and adds
a second failure mode: a notification delivered while no worker is listening is lost, so a
correct implementation still needs the poll as a backstop. Since the poll must exist
anyway, it is the whole design, and the delay is irrelevant for work measured in seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.common.enums import JobKind
from app.infrastructure.db.models.jobs import Job
from app.infrastructure.db.session import get_sessionmaker
from app.infrastructure.db.tenancy import set_tenant
from app.jobs.handlers import handler_for
from app.jobs.queue import JobQueue

logger = get_logger(__name__)

#: How long to wait when the queue is empty. Short enough that work feels prompt, long
#: enough that an idle deployment is not running a query every few milliseconds.
IDLE_SLEEP_SECONDS = 2.0

#: Backoff when the *database* is unreachable, as opposed to the queue being empty.
#: Retrying a dead connection every two seconds turns an outage into a thundering herd.
ERROR_SLEEP_SECONDS = 10.0


@dataclass
class WorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    idle_polls: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_payload(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "idle_polls": self.idle_polls,
            "started_at": self.started_at.isoformat(),
        }


class Worker:
    """Runs jobs until told to stop.

    Args:
        kinds: Restrict to certain job kinds. Lets a deployment separate the slow work
            (training, reports) from the latency-sensitive work (broker sync) onto
            different processes without a second queue.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        kinds: tuple[JobKind, ...] | None = None,
        idle_sleep: float = IDLE_SLEEP_SECONDS,
    ) -> None:
        self.name = name or f"{os.uname().nodename}:{os.getpid()}"
        self.kinds = kinds
        self.idle_sleep = idle_sleep
        self.stats = WorkerStats()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the loop to finish the job in flight and exit.

        Deliberately cooperative. Killing a worker mid-job is survivable — the lease
        expires and another worker reclaims it — but it wastes the work and risks a
        partial write on any handler that is not perfectly idempotent, so a clean stop
        is always preferred when one is possible.
        """
        self._stopping.set()

    async def run_forever(self) -> WorkerStats:
        logger.info("worker.started", worker=self.name, kinds=[k.value for k in self.kinds or ()])
        while not self._stopping.is_set():
            try:
                worked = await self.run_once()
            except Exception as exc:  # pragma: no cover - exercised by the outage path
                logger.error("worker.loop_error", worker=self.name, error=str(exc))
                await self._sleep(ERROR_SLEEP_SECONDS)
                continue

            if not worked:
                self.stats.idle_polls += 1
                await self._sleep(self.idle_sleep)

        logger.info("worker.stopped", worker=self.name, **self.stats.to_payload())
        return self.stats

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns whether there was work.

        Split out from the loop so tests drive it directly: a test that called
        ``run_forever`` would need to race a background task against an event, which is
        how flaky suites are made.
        """
        maker = get_sessionmaker()
        async with maker() as session:
            queue = JobQueue(session)
            job = await queue.claim(worker=self.name, kinds=self.kinds)
            if job is None:
                await session.rollback()
                return False

            self.stats.claimed += 1
            job_id, kind, user_id = job.id, job.kind, job.user_id

            try:
                # The boundary: whatever the handler does, Postgres will only let it see
                # this tenant's rows.
                await set_tenant(session, user_id)
                result = await handler_for(kind)(session, job)
                await queue.succeed(job, result)
                await session.commit()
                self.stats.succeeded += 1
                return True
            except Exception as exc:
                await session.rollback()
                self.stats.failed += 1
                await self._record_failure(job_id, exc)
                logger.error(
                    "worker.job_failed",
                    worker=self.name,
                    job_id=str(job_id),
                    kind=kind.value,
                    error=str(exc),
                )
                return True

    async def _record_failure(self, job_id: UUID, exc: BaseException) -> None:
        """Write the failure in a fresh transaction.

        The transaction that raised has been rolled back and cannot be used to record
        anything — attempting it is how a failing job stays ``running`` forever with its
        error nowhere. A new session is the only way to durably mark the attempt.
        """
        maker = get_sessionmaker()
        async with maker() as session:
            try:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                await JobQueue(session).fail(job, f"{type(exc).__name__}: {exc}")
                await session.commit()
            except Exception as inner:  # pragma: no cover - database is gone
                await session.rollback()
                logger.error(
                    "worker.failure_not_recorded", job_id=str(job_id), error=str(inner)
                )

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if asked to stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return


async def run_worker(
    *, kinds: tuple[JobKind, ...] | None = None, install_signal_handlers: bool = True
) -> WorkerStats:
    """Entry point for ``python -m app.jobs.worker``.

    Handles SIGTERM and SIGINT so a container stop drains the job in flight rather than
    abandoning it — the difference between a deploy that costs one job's latency and one
    that leaves a lease to expire.
    """
    worker = Worker(kinds=kinds)

    if install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not available on every platform; a worker without them still stops, just
            # less gracefully.
            with contextlib.suppress(NotImplementedError):  # pragma: no cover - non-POSIX
                loop.add_signal_handler(sig, worker.stop)

    return await worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run_worker())
