"""Allow only one active agent run per conversation.

Revision ID: 20261112_0002
Revises: 20261112_0001
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_0002"
down_revision = "20261112_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_agent_runs_running_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_running_conversation", table_name="agent_runs")
