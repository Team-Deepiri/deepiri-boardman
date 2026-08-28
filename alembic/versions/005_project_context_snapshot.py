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


def _missing(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return not any(c["name"] == name for c in inspector.get_columns("project_contexts"))


def upgrade() -> None:
    # Guarded, because `init_db`'s SQLite shim adds these at startup so an instance running
    # from an older file keeps working. An app that starts before `alembic upgrade head`
    # otherwise makes this fail with "duplicate column name" -- and then 006 and 007 never
    # apply, which is how a database ends up stamped at 004 with 005's columns present.
    if _missing("context_json"):
        op.add_column("project_contexts", sa.Column("context_json", sa.Text(), nullable=True))
    if _missing("context_source_revision"):
        op.add_column(
            "project_contexts",
            sa.Column("context_source_revision", sa.String(length=255), nullable=True),
        )
    if _missing("context_fetched_at"):
        op.add_column(
            "project_contexts", sa.Column("context_fetched_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("project_contexts", "context_fetched_at")
    op.drop_column("project_contexts", "context_source_revision")
    op.drop_column("project_contexts", "context_json")
