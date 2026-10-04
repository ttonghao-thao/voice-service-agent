"""Persist sourced conversation conditions and per-task context snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    # Old slots have no source; preserve them but do not fabricate provenance.
    op.add_column("conversations", sa.Column("context_state", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("turns", sa.Column("task_context", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("turns", "task_context")
    op.drop_column("conversations", "context_state")
