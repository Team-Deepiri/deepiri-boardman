"""Prevent duplicate issue -> Plaky mappings during concurrent webhook delivery."""

from alembic import op

revision = "006_issue_mapping_integrity"
down_revision = "005_project_context_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_issue_task_map_repo_issue",
        "issue_task_map",
        ["github_repo", "github_issue_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_issue_task_map_repo_issue", table_name="issue_task_map")
