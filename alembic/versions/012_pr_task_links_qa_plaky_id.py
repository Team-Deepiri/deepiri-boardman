"""Add pr_task_links.qa_plaky_id.

Same shape of gap as 011: this column exists in boardman/database/models.py but no
migration ever added it. Found live -- POST /api/v1/reconcile/{owner}/{repo} failed
on every PR with UndefinedColumnError the moment a Postgres deploy actually queried
it (SQLite's create_all had always been filling this in silently on every other
environment).
"""

import sqlalchemy as sa

from alembic import op

revision = "012_pr_task_links_qa_plaky_id"
down_revision = "011_background_jobs_and_caches"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == name for c in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("pr_task_links", "qa_plaky_id"):
        op.add_column("pr_task_links", sa.Column("qa_plaky_id", sa.String(255), nullable=True))


def downgrade() -> None:
    if _has_column("pr_task_links", "qa_plaky_id"):
        op.drop_column("pr_task_links", "qa_plaky_id")
