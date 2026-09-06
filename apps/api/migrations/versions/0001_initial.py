"""Initial conversation, turn, event, evidence and configuration schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("locale", sa.String(20), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("current_turn", sa.String(36)),
        sa.Column("voice_session_id", sa.String(36)),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("tool_config_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("tenant_id", "user_id"):
        op.create_index(f"ix_conversations_{field}", "conversations", [field])

    def fk():
        return sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        )

    def timestamp():
        return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)

    op.create_table(
        "turns",
        sa.Column("id", sa.String(36), primary_key=True),
        fk(),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("answer", sa.JSON()),
        timestamp(),
        sa.UniqueConstraint("conversation_id", "idempotency_key"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        fk(),
        sa.Column("server_seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        timestamp(),
        sa.UniqueConstraint("conversation_id", "server_seq"),
    )
    op.create_table(
        "conversation_records",
        sa.Column("id", sa.String(36), primary_key=True),
        fk(),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(180), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        timestamp(),
        sa.UniqueConstraint("conversation_id", "epoch", "kind", "source_id"),
    )
    op.create_table(
        "tool_configs",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tool_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        fk(),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        timestamp(),
    )
    for table in ("turns", "events", "conversation_records", "tool_runs"):
        op.create_index(f"ix_{table}_conversation_id", table, ["conversation_id"])


def downgrade():
    for table in ("tool_runs", "tool_configs", "conversation_records", "events", "turns", "conversations"):
        op.drop_table(table)
