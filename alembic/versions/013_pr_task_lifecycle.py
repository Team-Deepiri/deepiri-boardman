"""Add pr_task_lifecycle table.

Tracks which Plaky tasks the PR pipeline created (no existing task matched) vs matched
(linked to a pre-existing task), so a background sweep can delete the "created" ones
after their cleanup TTL and archive the "matched" ones once their PR is done.
"""

import sqlalchemy as sa

from alembic import op

revision = "013_pr_task_lifecycle"
down_revision = "012_pr_task_links_qa_plaky_id"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("pr_task_lifecycle"):
        return
    op.create_table(
        "pr_task_lifecycle",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_repo", sa.String(255), nullable=False),
        sa.Column("github_pr_number", sa.Integer(), nullable=False),
        sa.Column("plaky_task_id", sa.String(255), nullable=False),
        sa.Column("plaky_board_id", sa.String(255), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("cleanup_due_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "github_repo",
            "github_pr_number",
            "plaky_task_id",
            name="uq_pr_task_lifecycle_repo_pr_task",
        ),
    )
    op.create_index("ix_pr_task_lifecycle_github_repo", "pr_task_lifecycle", ["github_repo"])
    op.create_index(
        "ix_pr_task_lifecycle_github_pr_number", "pr_task_lifecycle", ["github_pr_number"]
    )
    op.create_index("ix_pr_task_lifecycle_plaky_task_id", "pr_task_lifecycle", ["plaky_task_id"])


def downgrade() -> None:
    if _has_table("pr_task_lifecycle"):
        op.drop_table("pr_task_lifecycle")
