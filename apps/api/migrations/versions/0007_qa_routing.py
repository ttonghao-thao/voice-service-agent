"""Snapshot QA policy, knowledge execution and native delivery state."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    for name, default, length in (("qa_execution_mode", "legacy", 20),
                                   ("answer_policy", "knowledge_required", 32),
                                   ("qa_toolset_version", "legacy", 32)):
        op.add_column("conversations", sa.Column(name, sa.String(length), nullable=False, server_default=default))
    for name, length in (("selected_tool", 80), ("effective_executor", 32),
                         ("escalation_reason", 64), ("execution_phase", 32), ("toolset_version", 32)):
        op.add_column("turns", sa.Column(name, sa.String(length), nullable=True))
    op.add_column("turns", sa.Column("evidence", sa.JSON(), nullable=True))
    op.create_table("utterances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("input_item_id", sa.String(128), nullable=False),
        sa.Column("user_text", sa.Text()), sa.Column("turn_id", sa.String(36)),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("answer", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "epoch", "input_item_id"))
    op.create_index("ix_utterances_conversation_id", "utterances", ["conversation_id"])
    op.create_table("delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("request_revision", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.String(36)), sa.Column("native_call_id", sa.String(128), nullable=False),
        sa.Column("response_id", sa.String(128)), sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("played_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "epoch", "native_call_id", "kind"))
    op.create_index("ix_delivery_attempts_conversation_id", "delivery_attempts", ["conversation_id"])


def downgrade():
    op.drop_table("delivery_attempts")
    op.drop_table("utterances")
    for name in ("evidence", "toolset_version", "execution_phase", "escalation_reason", "effective_executor", "selected_tool"):
        op.drop_column("turns", name)
    for name in ("qa_toolset_version", "answer_policy", "qa_execution_mode"):
        op.drop_column("conversations", name)
