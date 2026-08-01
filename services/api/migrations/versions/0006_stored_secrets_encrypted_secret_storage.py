"""Encrypted broker credentials at rest.

Closes a gap nothing had exercised: ``POST /broker/connections`` calls
``SecretStore.put``, and the store selected in staging and production —
``EnvironmentSecretStore`` — raises ``NotImplementedError`` on write. Linking a broker
therefore returned a 500 in every deployed environment, while every test passed against
the in-memory store used locally.

The table holds a Fernet token per reference and nothing else. Encryption happens in
``app.infrastructure.secrets.encrypted`` before a value reaches this table, and the key
lives only in the environment — so a stolen dump, a leaked backup or a read-only replica
yields ciphertext rather than broker logins.

**No ``user_id`` column and no row-level security**, unlike every other tenant table. The
secret is written before its ``broker_connections`` row exists, because credentials are
verified against the broker first and only a working login earns a connection — so there
is no tenant to scope to at insert time. The reference is ``<broker>/<uuid7>``, minted
server-side, and every read reaches it through a ``credential_ref`` on the RLS-protected
connections table. The model docstring records the residual risk this leaves.

Revision ID: 0006_stored_secrets
Revises: 0005_run_simulation_job
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_stored_secrets"
down_revision: str | None = "0005_run_simulation_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stored_secrets",
        # The reference is the key. A surrogate id would let two rows claim the same
        # reference and leave which one wins to insertion order.
        sa.Column("reference", sa.String(length=200), nullable=False),
        # LargeBinary, not Text: a Fernet token is bytes, and a column a reader might
        # mistake for readable is a column someone eventually logs.
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
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
        sa.PrimaryKeyConstraint("reference", name=op.f("pk_stored_secrets")),
    )


def downgrade() -> None:
    op.drop_table("stored_secrets")
