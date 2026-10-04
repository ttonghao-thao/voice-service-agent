"""Audit voice presentation and applied controls without implying heard audio."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("delivery_attempts", sa.Column("sent_samples", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("delivery_attempts", sa.Column("input_item_id", sa.String(128), nullable=True))
    op.add_column("delivery_attempts", sa.Column("phase", sa.String(32), nullable=True))
    op.add_column("delivery_attempts", sa.Column("reason_code", sa.String(64), nullable=True))
    op.add_column("delivery_attempts", sa.Column("output_suppressed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("delivery_attempts", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("delivery_attempts", sa.Column("playback_finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("delivery_attempts", sa.Column("answer_id", sa.String(36), nullable=True))
    op.add_column("delivery_attempts", sa.Column("validation_status", sa.String(32), nullable=True))
    op.add_column("delivery_attempts", sa.Column("validation_reason", sa.String(64), nullable=True))


def downgrade():
    for name in ("validation_reason", "validation_status", "answer_id", "playback_finished_at",
                 "finished_at", "output_suppressed", "reason_code", "phase", "input_item_id", "sent_samples"):
        op.drop_column("delivery_attempts", name)
