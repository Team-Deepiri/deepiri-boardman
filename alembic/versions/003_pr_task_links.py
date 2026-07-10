"""PR ↔ Plaky task registry for multi-PR merge gating

Revision ID: 003_pr_task_links
Revises: 002_agent_scan
Create Date: 2026-04-16

"""

import sqlalchemy as sa

from alembic import op

revision = "003_pr_task_links"
down_revision = "002_agent_scan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pr_task_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_repo", sa.String(length=255), nullable=False),
        sa.Column("github_pr_number", sa.Integer(), nullable=False),
        sa.Column("github_issue_number", sa.Integer(), nullable=False),
        sa.Column("plaky_task_id", sa.String(length=255), nullable=False),
        sa.Column("link_source", sa.String(length=32), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "github_repo",
            "github_pr_number",
            "github_issue_number",
            name="uq_pr_task_links_repo_pr_issue",
        ),
    )
    op.create_index(
        op.f("ix_pr_task_links_github_repo"), "pr_task_links", ["github_repo"], unique=False
    )
    op.create_index(
        op.f("ix_pr_task_links_github_pr_number"),
        "pr_task_links",
        ["github_pr_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pr_task_links_plaky_task_id"), "pr_task_links", ["plaky_task_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pr_task_links_plaky_task_id"), table_name="pr_task_links")
    op.drop_index(op.f("ix_pr_task_links_github_pr_number"), table_name="pr_task_links")
    op.drop_index(op.f("ix_pr_task_links_github_repo"), table_name="pr_task_links")
    op.drop_table("pr_task_links")
