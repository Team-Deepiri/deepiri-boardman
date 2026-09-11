"""Add pr_review_nudges table.

Escalation state for the stale-PR @mention sweep (boardman/services/pr_review_nudges.py):
whose turn it currently is (QA or developer), since when, and how far the escalation
schedule has already progressed.
"""

import sqlalchemy as sa

from alembic import op

revision = "016_pr_review_nudges"
down_revision = "015_qa_capability_profiles_float_tier"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("pr_review_nudges"):
        return
    op.create_table(
        "pr_review_nudges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_repo", sa.String(255), nullable=False),
        sa.Column("github_pr_number", sa.Integer(), nullable=False),
        sa.Column("developer_login", sa.String(255), nullable=False),
        sa.Column("primary_qa_login", sa.String(255), nullable=True),
        sa.Column("extra_qa_json", sa.Text(), nullable=True),
        sa.Column("waiting_on", sa.String(16), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("nudge_stage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_nudge_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("github_repo", "github_pr_number", name="uq_pr_review_nudges_repo_pr"),
    )
    op.create_index("ix_pr_review_nudges_github_repo", "pr_review_nudges", ["github_repo"])
    op.create_index(
        "ix_pr_review_nudges_github_pr_number", "pr_review_nudges", ["github_pr_number"]
    )


def downgrade() -> None:
    if _has_table("pr_review_nudges"):
        op.drop_table("pr_review_nudges")
