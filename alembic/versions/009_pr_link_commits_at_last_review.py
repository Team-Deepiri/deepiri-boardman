"""Add pr_task_links.commits_at_last_review.

Tracks the PR's cumulative commit count at the moment of the last QA review verdict,
so a later push can be compared against it to tell "one follow-up commit" (Revisions
In Progress) from "developer kept pushing past the review without asking QA back"
(Needs QA Again).
"""

import sqlalchemy as sa

from alembic import op

revision = "009_pr_link_commits_at_last_review"
down_revision = "008_agent_session_task_draft_json"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == name for c in inspector.get_columns("pr_task_links"))


def upgrade() -> None:
    if not _has_column("commits_at_last_review"):
        op.add_column(
            "pr_task_links", sa.Column("commits_at_last_review", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    if _has_column("commits_at_last_review"):
        op.drop_column("pr_task_links", "commits_at_last_review")
