"""Replace the deployment-wide tenant identity with per-call capability access."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("owner_id", sa.String(128)))
    op.add_column("conversations", sa.Column("access_token_hash", sa.String(64)))
    op.execute("UPDATE conversations SET owner_id = user_id")
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("owner_id", nullable=False)
        batch.drop_index("ix_conversations_tenant_id")
        batch.drop_index("ix_conversations_user_id")
        batch.drop_column("tenant_id")
        batch.drop_column("user_id")
        batch.create_index("ix_conversations_owner_id", ["owner_id"])
        batch.create_unique_constraint(
            "uq_conversations_access_token_hash", ["access_token_hash"]
        )
    with op.batch_alter_table("admin_audits") as batch:
        batch.drop_index("ix_admin_audits_tenant_id")
        batch.drop_column("tenant_id")


def downgrade():
    op.add_column("conversations", sa.Column("tenant_id", sa.String(128)))
    op.add_column("conversations", sa.Column("user_id", sa.String(128)))
    op.execute("UPDATE conversations SET tenant_id = 'legacy', user_id = owner_id")
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("tenant_id", nullable=False)
        batch.alter_column("user_id", nullable=False)
        batch.drop_constraint("uq_conversations_access_token_hash", type_="unique")
        batch.drop_index("ix_conversations_owner_id")
        batch.drop_column("access_token_hash")
        batch.drop_column("owner_id")
        batch.create_index("ix_conversations_tenant_id", ["tenant_id"])
        batch.create_index("ix_conversations_user_id", ["user_id"])
    op.add_column(
        "admin_audits",
        sa.Column("tenant_id", sa.String(128), nullable=False, server_default="legacy"),
    )
    op.create_index("ix_admin_audits_tenant_id", "admin_audits", ["tenant_id"])
