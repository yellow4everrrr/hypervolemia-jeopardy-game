"""The job queue against a real database.

Almost nothing here can be tested without Postgres. ``FOR UPDATE SKIP LOCKED`` is the
whole design, the partial unique index is what makes enqueueing idempotent under
concurrency, and lease recovery depends on real transaction boundaries. A fake would
assert that the fake works.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.ids import uuid7
from app.domain.common.enums import JobKind, JobState
from app.infrastructure.db.models.identity import User
from app.infrastructure.db.models.jobs import Job
from app.jobs.queue import JobQueue, backoff_for

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("LEDGERLINE_TEST_DATABASE_URL"),
        reason="set LEDGERLINE_TEST_DATABASE_URL to run database-backed tests",
    ),
]


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A real engine, because the concurrency tests need independent connections.

    The other database suites run inside one rolled-back transaction, which is faster and
    cleaner — but two workers competing for a job is precisely a two-connection scenario,
    and a savepoint on a single connection cannot express it.
    """
    created = create_async_engine(os.environ["LEDGERLINE_TEST_DATABASE_URL"])
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def user_id(engine: AsyncEngine) -> AsyncIterator[UUID]:
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    user = User(id=uuid7(), clerk_user_id=f"user_jobs_{uuid4().hex[:10]}", email="j@example.com")
    async with maker() as session:
        session.add(user)
        await session.commit()

    yield user.id

    # Explicit cleanup: these tests commit, so nothing rolls them back.
    async with maker() as session:
        await session.execute(
            Job.__table__.delete().where(Job.user_id == user.id)
        )
        await session.execute(User.__table__.delete().where(User.id == user.id))
        await session.commit()


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_a_job_starts_pending(self, engine: AsyncEngine, user_id: UUID) -> None:
        async with sessions(engine)() as session:
            result = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS
            )
            await session.commit()

            job = await session.get(Job, result.job_id)
            assert job is not None
            assert job.state is JobState.PENDING
            assert job.attempts == 0
            assert result.created

    @pytest.mark.asyncio
    async def test_the_same_idempotency_key_returns_the_existing_job(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """Enforced by a partial unique index rather than a check-then-insert.

        Two requests racing would both pass an application-level check and both insert;
        catching the integrity error is the only version correct under concurrency.
        """
        async with sessions(engine)() as session:
            queue = JobQueue(session)
            first = await queue.enqueue(
                user_id=user_id, kind=JobKind.TRAIN_MODELS, idempotency_key="nightly-2026-05-04"
            )
            await session.commit()

        async with sessions(engine)() as session:
            second = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.TRAIN_MODELS, idempotency_key="nightly-2026-05-04"
            )
            await session.commit()

        assert first.created
        assert not second.created
        assert first.job_id == second.job_id

    @pytest.mark.asyncio
    async def test_jobs_without_a_key_are_never_deduplicated(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """The partial index excludes NULLs, so "run it again" stays possible."""
        async with sessions(engine)() as session:
            queue = JobQueue(session)
            first = await queue.enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            second = await queue.enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        assert first.job_id != second.job_id


class TestClaiming:
    @pytest.mark.asyncio
    async def test_two_workers_never_claim_the_same_job(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """``SKIP LOCKED`` in action, on two real connections.

        One job, two concurrent claims. The second must come back empty rather than
        blocking on the first's lock or, worse, taking the same row.
        """
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        maker = sessions(engine)
        async with maker() as first_session, maker() as second_session:
            first = await JobQueue(first_session).claim(worker="w1")
            second = await JobQueue(second_session).claim(worker="w2")

            assert first is not None
            assert second is None

            await first_session.commit()
            await second_session.rollback()

    @pytest.mark.asyncio
    async def test_claiming_counts_the_attempt(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        async with sessions(engine)() as session:
            job = await JobQueue(session).claim(worker="w1")
            assert job is not None
            assert job.attempts == 1
            assert job.state is JobState.RUNNING
            assert job.leased_by == "w1"
            assert job.leased_until is not None
            await session.commit()

    @pytest.mark.asyncio
    async def test_a_job_scheduled_for_later_is_not_claimed(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(
                user_id=user_id,
                kind=JobKind.DETECT_PATTERNS,
                run_after=datetime.now(UTC) + timedelta(hours=1),
            )
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w1") is None
            await session.rollback()

    @pytest.mark.asyncio
    async def test_a_worker_can_restrict_itself_to_certain_kinds(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """Lets a deployment separate slow work from latency-sensitive work without a
        second queue."""
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.TRAIN_MODELS)
            await session.commit()

        async with sessions(engine)() as session:
            assert (
                await JobQueue(session).claim(
                    worker="w1", kinds=(JobKind.GENERATE_REPORTS,)
                )
                is None
            )
            await session.rollback()

        async with sessions(engine)() as session:
            claimed = await JobQueue(session).claim(worker="w1", kinds=(JobKind.TRAIN_MODELS,))
            assert claimed is not None
            await session.commit()


class TestLeaseRecovery:
    @pytest.mark.asyncio
    async def test_an_expired_lease_makes_a_running_job_claimable_again(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """The recovery path for a worker that died mid-job.

        Without it the job sits in ``running`` forever, owned by a process that no
        longer exists, and nothing notices until somebody reads the table.
        """
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS
            )
            await session.commit()

        async with sessions(engine)() as session:
            job = await JobQueue(session).claim(worker="doomed", lease_seconds=1)
            assert job is not None
            await session.commit()

        async with sessions(engine)() as session:
            reclaimed = await JobQueue(session).claim(
                worker="survivor", now=datetime.now(UTC) + timedelta(seconds=30)
            )
            assert reclaimed is not None
            assert reclaimed.id == enqueued.job_id
            assert reclaimed.leased_by == "survivor"
            assert reclaimed.attempts == 2
            await session.commit()

    @pytest.mark.asyncio
    async def test_a_live_lease_is_left_alone(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """The failure the design must not have: two workers on one job concurrently."""
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w1", lease_seconds=900) is not None
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w2") is None
            await session.rollback()


class TestOutcomes:
    @pytest.mark.asyncio
    async def test_success_records_the_result_and_duration(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        async with sessions(engine)() as session:
            queue = JobQueue(session)
            job = await queue.claim(worker="w1")
            assert job is not None
            await queue.succeed(job, {"findings": 3})
            await session.commit()

            assert job.state is JobState.SUCCEEDED
            assert job.result == {"findings": 3}
            assert job.duration_ms is not None
            assert job.leased_until is None

    @pytest.mark.asyncio
    async def test_a_failure_is_retried_with_backoff(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(user_id=user_id, kind=JobKind.DETECT_PATTERNS)
            await session.commit()

        async with sessions(engine)() as session:
            queue = JobQueue(session)
            job = await queue.claim(worker="w1")
            assert job is not None
            await queue.fail(job, "broker timed out")
            await session.commit()

            assert job.state is JobState.PENDING
            assert job.run_after > datetime.now(UTC)
            assert job.last_error == "broker timed out"

    @pytest.mark.asyncio
    async def test_exhausting_attempts_dead_letters_the_job(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """A queue that retries forever turns one poison job into an outage."""
        async with sessions(engine)() as session:
            await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS, max_attempts=2
            )
            await session.commit()

        for attempt in range(2):
            async with sessions(engine)() as session:
                queue = JobQueue(session)
                job = await queue.claim(
                    worker="w1", now=datetime.now(UTC) + timedelta(hours=attempt + 1)
                )
                assert job is not None, f"attempt {attempt + 1} could not claim"
                await queue.fail(job, "still broken")
                await session.commit()
                state = job.state

        assert state is JobState.DEAD

    @pytest.mark.asyncio
    async def test_a_dead_job_is_never_claimed_again(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS, max_attempts=1
            )
            await session.commit()

        async with sessions(engine)() as session:
            queue = JobQueue(session)
            job = await queue.claim(worker="w1")
            assert job is not None
            await queue.fail(job, "poison")
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w2") is None
            found = await session.get(Job, enqueued.job_id)
            assert found is not None and found.state is JobState.DEAD
            await session.rollback()

    @pytest.mark.asyncio
    async def test_the_error_is_stored_on_the_row_not_only_logged(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """A dead job whose cause lives in a log line is a job nobody will diagnose."""
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS, max_attempts=1
            )
            await session.commit()

        async with sessions(engine)() as session:
            queue = JobQueue(session)
            job = await queue.claim(worker="w1")
            assert job is not None
            await queue.fail(job, "ValueError: instrument specification missing")
            await session.commit()

        async with sessions(engine)() as session:
            found = await session.get(Job, enqueued.job_id)
            assert found is not None
            assert "instrument specification missing" in (found.last_error or "")


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_pending_job_can_be_cancelled(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS
            )
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).cancel(user_id, enqueued.job_id)
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w1") is None
            await session.rollback()

    @pytest.mark.asyncio
    async def test_a_running_job_cannot_be_cancelled(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        """Setting a flag no handler checks would report a cancellation that did not
        happen, and the caller would stop waiting for work still writing rows."""
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS
            )
            await session.commit()

        async with sessions(engine)() as session:
            assert await JobQueue(session).claim(worker="w1") is not None
            await session.commit()

        async with sessions(engine)() as session:
            assert not await JobQueue(session).cancel(user_id, enqueued.job_id)
            await session.rollback()

    @pytest.mark.asyncio
    async def test_another_tenant_cannot_cancel_your_job(
        self, engine: AsyncEngine, user_id: UUID
    ) -> None:
        async with sessions(engine)() as session:
            enqueued = await JobQueue(session).enqueue(
                user_id=user_id, kind=JobKind.DETECT_PATTERNS
            )
            await session.commit()

        async with sessions(engine)() as session:
            assert not await JobQueue(session).cancel(uuid7(), enqueued.job_id)
            await session.rollback()


class TestBackoff:
    def test_it_grows_geometrically_and_is_capped(self) -> None:
        """Geometric because retryable failures are mostly transient; capped so a run of
        them cannot push a job a week into the future."""
        delays = [backoff_for(n).total_seconds() for n in range(1, 8)]

        assert delays == sorted(delays)
        assert delays[0] == 30
        assert max(delays) <= 7200
