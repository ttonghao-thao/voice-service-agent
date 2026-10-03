"""Persist optional external-model execution and direct retrieval evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "turns", sa.Column("execution_mode", sa.String(16), nullable=False, server_default="external")
    )
    op.add_column("turns", sa.Column("knowledge_result", sa.JSON(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM turns WHERE execution_mode = 'direct' LIMIT 1")).first():
        raise RuntimeError(
            "Cannot downgrade Q07 with direct turns; restore a backup or deploy a compatible fix"
        )
    op.drop_column("turns", "knowledge_result")
    op.drop_column("turns", "execution_mode")
