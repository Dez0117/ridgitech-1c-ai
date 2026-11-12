"""Add a queued state for external workers.

Revision ID: 20261112_0003
Revises: 20261112_0002
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa


revision = "20261112_0003"
down_revision = "20261112_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_agent_runs_running_conversation", table_name="agent_runs")
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('queued', 'running', 'completed', 'needs_clarification', 'failed')",
    )
    op.create_index(
        "uq_agent_runs_running_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_running_conversation", table_name="agent_runs")
    op.execute("UPDATE agent_runs SET status = 'running' WHERE status = 'queued'")
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('running', 'completed', 'needs_clarification', 'failed')",
    )
    op.create_index(
        "uq_agent_runs_running_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )
