"""Background job queue, and row-level security on every tenant table.

Two changes that belong together.

``jobs`` is the work queue. It is a table rather than a Redis list because a job's
completion and the rows it produces must commit atomically; with an external broker they
cannot, and the worker either loses work on rollback or runs it twice. Claiming with
``SELECT ... FOR UPDATE SKIP LOCKED`` inside the same transaction as the work removes the
second system entirely.

Row-level security arrives in the same migration because the worker is *why* it is
needed. Every path until now took its ``user_id`` from an authenticated principal, so a
missing filter was conspicuous. A background handler has no principal and nobody watching
it, and a plausible-looking ``select(Trade).where(Trade.id == ...)`` written there would
cross tenants silently. The policies make Postgres enforce the boundary underneath
whatever the application does.

**FORCE is not optional.** Postgres exempts a table's owner from its own policies unless
``FORCE ROW LEVEL SECURITY`` is set. Since most deployments connect as the role that ran
the migrations, policies without ``FORCE`` would be present, correct, and completely
inert — the worst possible outcome, because every check short of an actual cross-tenant
query would report success.

**The policy compares against a transaction-scoped GUC.** ``current_setting('app.user_id',
true)`` returns NULL when unset, and ``user_id = NULL`` is NULL rather than true, so an
unbound connection sees nothing. That is the correct failure direction: a path that
forgets to bind a tenant returns empty results rather than everything.

Revision ID: 0004_jobs_and_rls
Revises: 0003_prediction_models
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_jobs_and_rls"
down_revision: str | None = "0003_prediction_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept in step with ``app.infrastructure.db.tenancy.TENANT_TABLES``; a test asserts the
#: two agree, so a table added to the schema and forgotten in one of them fails the build
#: rather than shipping unprotected.
TENANT_TABLES = (
    "accounts",
    "account_balance_snapshots",
    "ai_analyses",
    "ai_recommendations",
    "broker_connections",
    "daily_journals",
    "detected_patterns",
    "equity_curve_points",
    "executions",
    "jobs",
    "market_conditions",
    "notes",
    "orders",
    "performance_metrics",
    "positions",
    "prediction_models",
    "replay_metadata",
    "reports",
    "risk_metrics",
    "rule_evaluations",
    "screenshots",
    "setups",
    "strategies",
    "strategy_rules",
    "sync_runs",
    "tags",
    "trades",
    "trading_sessions",
)

JOB_KIND = "job_kind"
JOB_STATE = "job_state"


def upgrade() -> None:
    job_kind = postgresql.ENUM(
        "sync_broker",
        "compute_analytics",
        "detect_patterns",
        "train_models",
        "generate_reports",
        "measure_excursions",
        name=JOB_KIND,
        create_type=False,
    )
    job_state = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        "dead",
        "cancelled",
        name=JOB_STATE,
        create_type=False,
    )
    job_kind.create(op.get_bind(), checkfirst=True)
    job_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", job_kind, nullable=False),
        sa.Column("state", job_state, server_default="pending", nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_by", sa.String(length=120), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_jobs_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_jobs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_user_id"), "jobs", ["user_id"], unique=False)
    op.create_index("ix_jobs_user_kind", "jobs", ["user_id", "kind"], unique=False)
    # The claim query's index. Partial, because finished jobs are the overwhelming
    # majority of the table within a week and none of them is ever claimable.
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["state", "run_after"],
        unique=False,
        postgresql_where=sa.text("state IN ('pending', 'running')"),
    )
    # Idempotency enforced by the database. An application-level check-then-insert is
    # wrong under concurrency: two requests racing both pass the check.
    op.create_index(
        "uq_jobs_idempotency",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Without FORCE the owner bypasses the policy, and most deployments connect as
        # the owner. The policies would be present, correct, and inert.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (user_id::text = current_setting('app.user_id', true))
            WITH CHECK (user_id::text = current_setting('app.user_id', true))
            """
        )

    # The application role. This is the load-bearing half of the change, and it was added
    # only after measuring: with the policies above in place and FORCE set, a connection
    # as the migration role still saw *every* tenant's rows, because Postgres exempts
    # superusers unconditionally and table owners unless forced. Policies alone are
    # decoration; a role that is neither owner nor superuser is what makes them bite.
    #
    # Verified: as `ledgerline_app`, an unbound connection sees zero rows and a bound one
    # sees exactly its own tenant's.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerline_app') THEN
                CREATE ROLE ledgerline_app NOLOGIN NOBYPASSRLS;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO ledgerline_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO ledgerline_app"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledgerline_app")
    # Future tables too, or the next migration silently creates something the app cannot
    # read and the failure surfaces as a permission error in production.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledgerline_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO ledgerline_app"
    )

    # Migrations, backfills and the schema-drift check must see everything. A dedicated
    # bypass role is the honest way to grant that: it is auditable, and it is explicitly
    # not the role the application connects as.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerline_admin') THEN
                CREATE ROLE ledgerline_admin NOLOGIN BYPASSRLS;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("uq_jobs_idempotency", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_index("ix_jobs_user_kind", table_name="jobs")
    op.drop_index(op.f("ix_jobs_user_id"), table_name="jobs")
    op.drop_table("jobs")

    op.execute(f"DROP TYPE IF EXISTS {JOB_STATE}")
    op.execute(f"DROP TYPE IF EXISTS {JOB_KIND}")
