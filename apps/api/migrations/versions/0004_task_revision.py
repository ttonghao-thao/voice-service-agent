"""Separate business request revision and result delivery state from audio epochs."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("request_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("turns", sa.Column("request_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("turns", sa.Column("parent_task_id", sa.String(36)))
    op.add_column("turns", sa.Column("native_call_id", sa.String(128)))
    op.add_column("turns", sa.Column("cancellation_reason", sa.String(64)))
    op.add_column(
        "turns",
        sa.Column("delivery_status", sa.String(32), nullable=False, server_default="pending_validation"),
    )
    op.add_column(
        "turns", sa.Column("output_suppressed", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index("ix_turns_conversation_revision", "turns", ["conversation_id", "request_revision"])


def downgrade():
    op.drop_index("ix_turns_conversation_revision", table_name="turns")
    for column in (
        "delivery_status",
        "output_suppressed",
        "cancellation_reason",
        "native_call_id",
        "parent_task_id",
        "request_revision",
    ):
        op.drop_column("turns", column)
    op.drop_column("conversations", "request_revision")
