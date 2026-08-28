"""Record the GitHub-side timestamp of a link withdrawal.

Reviving a withdrawn link asks "was this delivery built before the close?", and
`withdrawn_at` cannot answer it: it records when this process handled the close, on this
host's clock, while the delivery's `updated_at` comes from GitHub's. A queued job or a
poller catch-up puts those hours apart, and a genuine reopen in between was refused.
"""

import sqlalchemy as sa

from alembic import op

revision = "007_pr_link_withdrawn_github_at"
down_revision = "006_issue_mapping_integrity"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == name for c in inspector.get_columns("pr_task_links"))


def upgrade() -> None:
    # Guarded, because `init_db`'s SQLite shim adds this column at startup so an instance
    # running from an older file keeps working. An app that starts before
    # `alembic upgrade head` -- the exact sequence this migration exists for -- would
    # otherwise make the upgrade fail with "duplicate column name".
    if not _has_column("withdrawn_github_at"):
        op.add_column(
            "pr_task_links",
            sa.Column("withdrawn_github_at", sa.String(length=40), nullable=True),
        )


def downgrade() -> None:
    if _has_column("withdrawn_github_at"):
        op.drop_column("pr_task_links", "withdrawn_github_at")
