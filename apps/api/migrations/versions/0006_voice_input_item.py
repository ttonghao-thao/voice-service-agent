"""Associate a voice input item with its persisted business turn."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("turns", sa.Column("input_item_id", sa.String(128), nullable=True))


def downgrade():
    op.drop_column("turns", "input_item_id")
