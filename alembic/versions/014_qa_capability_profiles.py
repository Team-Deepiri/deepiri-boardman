"""Add qa_capability_profiles table.

Own commit-history-mined QA cold-start signal (scripts/mine_qa_repo_capability.py):
DB-backed rather than a JSON file in the working tree, so a mining run never produces
a file that could churn in the git repo -- durable on the VM, queryable, one row per
GitHub login.
"""

import sqlalchemy as sa

from alembic import op

revision = "014_qa_capability_profiles"
down_revision = "013_pr_task_lifecycle"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("qa_capability_profiles"):
        return
    op.create_table(
        "qa_capability_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_login", sa.String(255), nullable=False),
        sa.Column("qa_tier", sa.Integer(), nullable=True),
        sa.Column("repos_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repos_mined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("github_login", name="uq_qa_capability_profiles_github_login"),
    )
    op.create_index(
        "ix_qa_capability_profiles_github_login", "qa_capability_profiles", ["github_login"]
    )


def downgrade() -> None:
    if _has_table("qa_capability_profiles"):
        op.drop_table("qa_capability_profiles")
