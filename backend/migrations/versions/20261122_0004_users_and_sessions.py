"""Add accounts, JWT sessions and conversation ownership."""

import sqlalchemy as sa
from alembic import op


revision = "20261122_0004"
down_revision = "20261112_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.add_column("conversations", sa.Column("owner_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_conversations_owner_id", "conversations", "users", ["owner_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_conversations_owner_updated", "conversations", ["owner_id", "updated_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_conversations_owner_updated", table_name="conversations")
    op.drop_constraint("fk_conversations_owner_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "owner_id")
    op.drop_table("auth_sessions")
    op.drop_table("users")
