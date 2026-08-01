"""Add ``run_simulation`` to the job kind enum.

The what-if sweep was a synchronous ``POST`` costing **65 seconds** on a 1,447-trade
history — past most proxy and load-balancer timeouts, and long past the point where a
person concludes the page is broken. Reducing the resampling does not help: the cost is
re-pricing every trade under every scenario, not the bootstrap.

Milestone 13 built the queue for exactly this shape of work and enumerated three kinds
for it. This adds the fourth, measured rather than anticipated.

**``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block** on the Postgres
versions this targets, and Alembic wraps every migration in one. ``COMMIT`` first is the
standard escape and is why this migration is one statement long rather than folded into a
larger one.

**The downgrade is a deliberate no-op.** Postgres has no ``ALTER TYPE ... DROP VALUE``, and
an unused enum label costs nothing — nothing references it once the handler is gone. It
also does not survive: ``0004``'s downgrade drops ``job_kind`` outright, so continuing
down the chain removes the label with the type. See :func:`downgrade`.

Revision ID: 0005_run_simulation_job
Revises: 0004_jobs_and_rls
"""

from __future__ import annotations

from alembic import op

revision = "0005_run_simulation_job"
down_revision = "0004_jobs_and_rls"
branch_labels = None
depends_on = None

JOB_KIND = "job_kind"
NEW_VALUE = "run_simulation"


def upgrade() -> None:
    # Alembic opens a transaction per migration; ALTER TYPE ... ADD VALUE cannot run in
    # one. Committing here ends Alembic's transaction, and the statement then runs in its
    # own implicit one.
    op.execute("COMMIT")
    # IF NOT EXISTS so re-running against a database that already has the label is a
    # no-op rather than an error — the same property every other migration here has.
    op.execute(f"ALTER TYPE {JOB_KIND} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    """Deliberately empty.

    Postgres cannot drop an enum label. Doing it properly means creating a replacement
    type, rewriting ``jobs.kind``, and deciding the fate of any row already holding
    ``run_simulation`` — a data-loss decision dressed as a schema one.

    Leaving the label is the better trade and is genuinely harmless: with the handler
    gone nothing can enqueue it, ``ENQUEUEABLE`` no longer lists it, and an unreferenced
    label has no cost. It is also not permanent — ``0004``'s downgrade drops ``job_kind``
    entirely, so anyone rolling back far enough to care is already removing the type.

    Raising here would have been the more self-righteous choice and a worse one: it would
    break ``alembic downgrade base``, which CI runs on every change to prove the migration
    chain reverses.
    """
