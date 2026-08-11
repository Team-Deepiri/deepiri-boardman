"""agent sessions, scan runs, project context

Revision ID: 002_agent_scan
Revises: 001_initial
Create Date: 2026-04-09

"""

import sqlalchemy as sa

from alembic import op

revision = "002_agent_scan"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_repo", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("tasks_proposed", sa.Text(), nullable=True),
        sa.Column("tasks_created", sa.Integer(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scan_runs_github_repo"), "scan_runs", ["github_repo"], unique=False)

    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_active", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index(
        op.f("ix_agent_sessions_session_id"), "agent_sessions", ["session_id"], unique=False
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_pk", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_pk"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_messages_session_pk"), "agent_messages", ["session_pk"], unique=False
    )

    op.create_table(
        "project_contexts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("goals_json", sa.Text(), nullable=True),
        sa.Column("last_scanned", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo"),
    )
    op.create_index(op.f("ix_project_contexts_repo"), "project_contexts", ["repo"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_project_contexts_repo"), table_name="project_contexts")
    op.drop_table("project_contexts")
    op.drop_index(op.f("ix_agent_messages_session_pk"), table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index(op.f("ix_agent_sessions_session_id"), table_name="agent_sessions")
    op.drop_table("agent_sessions")
    op.drop_index(op.f("ix_scan_runs_github_repo"), table_name="scan_runs")
    op.drop_table("scan_runs")
