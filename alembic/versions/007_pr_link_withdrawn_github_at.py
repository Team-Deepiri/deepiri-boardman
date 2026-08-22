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


def upgrade() -> None:
    op.add_column(
        "pr_task_links",
        sa.Column("withdrawn_github_at", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pr_task_links", "withdrawn_github_at")
