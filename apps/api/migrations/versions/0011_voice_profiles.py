"""Persist voice personas under the existing per-user/per-call identity."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("voice_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("language_style", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_voice_profiles_owner_id", "voice_profiles", ["owner_id"])


def downgrade():
    op.drop_index("ix_voice_profiles_owner_id", table_name="voice_profiles")
    op.drop_table("voice_profiles")
