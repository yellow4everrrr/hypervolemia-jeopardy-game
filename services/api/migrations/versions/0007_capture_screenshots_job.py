"""Add ``capture_screenshots`` to the job kind enum.

The ``screenshots`` table has claimed since milestone 1 that "capture is automatic at six
moments per trade". Nothing implemented it: the table, the ``ScreenshotKind`` enum and the
object store all existed, and no code ever wrote a row. This is the job kind that makes
the word *automatic* true.

Same constraint as ``0005``: ``ALTER TYPE ... ADD VALUE`` cannot run inside the
transaction Alembic wraps a migration in, hence the ``COMMIT``. Same reasoning on the
downgrade, too — Postgres cannot drop an enum label, an unreferenced one costs nothing,
and ``0004`` drops the type outright further down the chain.

Revision ID: 0007_capture_screenshots
Revises: 0006_stored_secrets
"""

from __future__ import annotations

from alembic import op

revision = "0007_capture_screenshots"
down_revision = "0006_stored_secrets"
branch_labels = None
depends_on = None

JOB_KIND = "job_kind"
NEW_VALUE = "capture_screenshots"


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute(f"ALTER TYPE {JOB_KIND} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    """Deliberately empty; see 0005 for the reasoning in full."""
