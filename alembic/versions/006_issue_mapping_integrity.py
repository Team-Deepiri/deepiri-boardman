"""Prevent duplicate issue -> Plaky mappings during concurrent webhook delivery."""

import sqlalchemy as sa

from alembic import op

revision = "006_issue_mapping_integrity"
down_revision = "005_project_context_snapshot"
branch_labels = None
depends_on = None

_INDEX = "uq_issue_task_map_repo_issue"


def _has_index() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(i["name"] == _INDEX for i in inspector.get_indexes("issue_task_map"))


def upgrade() -> None:
    if _has_index():
        return

    # Deduplicate FIRST. This index exists because concurrent deliveries could write two
    # mappings for one issue, so any database old enough to need it is the one most likely
    # to have them -- and creating the index on top of duplicates aborts the upgrade, which
    # leaves the chain stamped here and 007 permanently unapplied. The row kept is the
    # newest, which is the mapping the handlers have been using.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM issue_task_map WHERE id NOT IN ("
            "  SELECT MAX(id) FROM issue_task_map GROUP BY github_repo, github_issue_number"
            ")"
        )
    )
    op.create_index(
        _INDEX,
        "issue_task_map",
        ["github_repo", "github_issue_number"],
        unique=True,
    )


def downgrade() -> None:
    if _has_index():
        op.drop_index(_INDEX, table_name="issue_task_map")
