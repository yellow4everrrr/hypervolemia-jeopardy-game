"""Broker instrument mapping and broker user id.

Adds what milestone 2's sync pipeline needs beyond the initial schema:

``broker_instrument_map`` translates a broker's own instrument identifier into ours.
Tradovate names a contract by a numeric id that means nothing outside Tradovate and
differs between its demo and live environments — hence the ``(broker, environment,
external_id)`` key. Putting this on ``instruments`` instead would tie a global
reference table to one broker's identifiers.

``broker_connections.external_user_id`` records the broker's user id for the login.
Tradovate's real-time subscription (``user/syncrequest``) takes a user id rather than
an account id, so without it the live feed cannot be opened.

Revision ID: 0002_broker_sync
Revises: 0001_initial_schema
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_broker_sync"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_instrument_map",
        # `broker_kind` already exists from 0001; create_type=False stops SQLAlchemy
        # emitting a second CREATE TYPE, which would fail the migration outright.
        sa.Column(
            "broker",
            postgresql.ENUM("tradovate", "manual", "csv", name="broker_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("environment", sa.String(length=16), server_default="demo", nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("external_symbol", sa.String(length=64), nullable=True),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_broker_instrument_map_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broker_instrument_map")),
        sa.UniqueConstraint(
            "broker", "environment", "external_id", name="uq_broker_instrument_map_identity"
        ),
    )
    op.create_index(
        "ix_broker_instrument_map_instrument",
        "broker_instrument_map",
        ["instrument_id"],
        unique=False,
    )
    op.add_column(
        "broker_connections", sa.Column("external_user_id", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("broker_connections", "external_user_id")
    op.drop_index("ix_broker_instrument_map_instrument", table_name="broker_instrument_map")
    op.drop_table("broker_instrument_map")
