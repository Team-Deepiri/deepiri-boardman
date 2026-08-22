"""store compact progressive repository context snapshots

Revision ID: 005_project_context_snapshot
Revises: 004_github_webhook_deliveries
Create Date: 2026-08-18
"""

import sqlalchemy as sa

from alembic import op

revision = "005_project_context_snapshot"
down_revision = "004_github_webhook_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_contexts", sa.Column("context_json", sa.Text(), nullable=True))
    op.add_column(
        "project_contexts",
        sa.Column("context_source_revision", sa.String(length=255), nullable=True),
    )
    op.add_column("project_contexts", sa.Column("context_fetched_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_contexts", "context_fetched_at")
    op.drop_column("project_contexts", "context_source_revision")
    op.drop_column("project_contexts", "context_json")
