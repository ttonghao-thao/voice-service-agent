"""Preserve confirmed query location across bounded conversation history."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("slots", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("conversations", "slots")
