"""The background work queue.

**Why a table and not Redis.** Redis is already a dependency and a list-based queue would
be less code, and it would be wrong here for a reason worth stating precisely: a job's
completion and the rows it writes must commit *together*. With an external broker they
cannot. The worker either acknowledges the job before its Postgres transaction commits —
and loses the work when the transaction rolls back — or commits first and then fails to
acknowledge, and the job runs twice. That is the dual-write problem, and it has no
solution at the application layer.

Claiming a job with ``SELECT ... FOR UPDATE SKIP LOCKED`` inside the same transaction as
the work removes the second system entirely. The job's state transition and the report it
generated are one commit, and there is nothing to reconcile.

The costs are real and accepted. Postgres is not a message broker: this design polls, it
holds a row lock for the duration of a job, and it will not scale to a million jobs a
minute. The workload here is a handful of jobs per trader per day, each taking seconds,
and correctness of the *result* matters far more than queue throughput.

**Leases, not just locks.** A worker that is killed mid-job holds its row lock until the
connection dies, which under a proxy can be a long time. Every claim therefore also
writes ``leased_until``; a job whose lease has expired is claimable again regardless of
what happened to the worker that took it.

**At-least-once, so handlers must be idempotent.** A worker can complete the work, lose
its connection before committing, and have the job re-run. ``idempotency_key`` is the
defence: a job that has already produced its output recognises it and returns rather than
producing a second one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.common.enums import JobKind, JobState
from app.infrastructure.db.base import (
    Base,
    TimestampMixin,
    UserScopedMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class Job(UUIDPrimaryKeyMixin, UserScopedMixin, TimestampMixin, Base):
    """One unit of background work.

    Carries ``user_id`` like every other tenant-owned row, and for an additional reason:
    the worker sets the row-level-security context from it before running the handler, so
    a job physically cannot touch another tenant's data even if its handler is wrong.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # The claim query: pending or expired-lease jobs, oldest first. This index is
        # the difference between a queue and a sequential scan of every job ever run.
        Index(
            "ix_jobs_claimable",
            "state",
            "run_after",
            postgresql_where=text("state IN ('pending', 'running')"),
        ),
        Index("ix_jobs_user_kind", "user_id", "kind"),
        # Idempotency is enforced by the database, not by a check-then-insert in the
        # application — two workers racing would both pass the check.
        Index(
            "uq_jobs_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    kind: Mapped[JobKind] = mapped_column(pg_enum(JobKind, "job_kind"), nullable=False)
    state: Mapped[JobState] = mapped_column(
        pg_enum(JobState, "job_state"),
        nullable=False,
        server_default=JobState.PENDING.value,
    )
    account_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )

    #: Handler arguments. JSONB rather than columns because each job kind takes different
    #: arguments and a schema with a column per kind's parameters stops being extended.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    #: Set by the caller when re-running the job would produce a duplicate. Enforced by a
    #: partial unique index, so a duplicate enqueue fails rather than queueing twice.
    idempotency_key: Mapped[str | None] = mapped_column(String(200))

    #: Earliest time this job may run. Retries push it forward with exponential backoff;
    #: a caller can also use it to schedule work.
    run_after: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )
    #: While running, when the claim expires. A job past this is claimable again however
    #: the worker holding it died.
    leased_until: Mapped[datetime | None]
    leased_by: Mapped[str | None] = mapped_column(String(120))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    #: The last failure, kept on the row. A dead-lettered job whose error lives only in a
    #: log line is a job nobody will diagnose.
    last_error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    @property
    def is_terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.DEAD)
