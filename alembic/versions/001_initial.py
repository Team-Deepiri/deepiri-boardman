"""Initial migration

Revision ID: 001_initial
Revises:
Create Date: 2026-04-08

"""

import sqlalchemy as sa

from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issue_task_map",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_repo", sa.String(length=255), nullable=False),
        sa.Column("github_issue_number", sa.Integer(), nullable=False),
        sa.Column("plaky_task_id", sa.String(length=255), nullable=False),
        sa.Column("plaky_task_url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_issue_task_map_github_repo"), "issue_task_map", ["github_repo"])

    op.create_table(
        "sync_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("github_repo", sa.String(length=255), nullable=True),
        sa.Column("github_ref", sa.String(length=100), nullable=True),
        sa.Column("plaky_task_id", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sync_log")
    op.drop_index(op.f("ix_issue_task_map_github_repo"), table_name="issue_task_map")
    op.drop_table("issue_task_map")
