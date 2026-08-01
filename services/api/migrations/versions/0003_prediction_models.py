"""Stored predictive models and the evidence that gates them.

Milestone 11 trains two heads — win probability and expected R — over a trader's own
history. ``prediction_models`` holds one row per trained head, carrying the fitted
coefficients alongside the walk-forward evidence that decided whether the model may be
served at all.

Three columns exist specifically to stop this table becoming a source of unfounded
numbers:

``is_deployable`` is stored rather than derived on read. The gate depends on a session
block bootstrap and a calibration simulation costing seconds; recomputing it per request
would put a statistical decision in the serving path, where it would be cached and then
skipped.

``refusal`` holds the plain-language reason a model is not served, and is populated on
exactly the rows where ``is_deployable`` is false. A refused model is kept, not
discarded — the trader is owed the reason, and the next training run needs something to
be compared against.

``skill_low`` / ``skill_high`` carry the bootstrap interval. A positive skill score is
not evidence of skill, and storing the point estimate alone would let a later reader
draw exactly the conclusion the interval exists to prevent.

Revision ID: 0003_prediction_models
Revises: 0002_broker_sync
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_prediction_models"
down_revision: str | None = "0002_broker_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prediction_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("head", sa.String(length=32), nullable=False),
        sa.Column(
            "is_deployable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("refusal", sa.Text(), nullable=True),
        sa.Column("skill", sa.Numeric(18, 8), nullable=True),
        sa.Column("skill_low", sa.Numeric(18, 8), nullable=True),
        sa.Column("skill_high", sa.Numeric(18, 8), nullable=True),
        sa.Column("trades", sa.Integer(), server_default="0", nullable=False),
        sa.Column("folds", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "out_of_sample_predictions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("model_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # A deployable model may not carry a refusal, and a refused one may not be
        # served. Enforced in the database because this is the invariant the whole
        # milestone rests on, and application code is not the only thing that writes
        # rows — a backfill or a manual fix would bypass it.
        sa.CheckConstraint(
            "(is_deployable AND refusal IS NULL) OR (NOT is_deployable AND refusal IS NOT NULL)",
            name=op.f("ck_prediction_models_refusal_matches_deployability"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_prediction_models_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_prediction_models_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prediction_models")),
    )
    op.create_index(
        op.f("ix_prediction_models_user_id"), "prediction_models", ["user_id"], unique=False
    )
    op.create_index(
        "ix_prediction_models_user_head", "prediction_models", ["user_id", "head"], unique=False
    )
    # Serving reads the newest model for a head, so the index carries created_at.
    op.create_index(
        "ix_prediction_models_user_head_created",
        "prediction_models",
        ["user_id", "head", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_prediction_models_user_head_created", table_name="prediction_models")
    op.drop_index("ix_prediction_models_user_head", table_name="prediction_models")
    op.drop_index(op.f("ix_prediction_models_user_id"), table_name="prediction_models")
    op.drop_table("prediction_models")
