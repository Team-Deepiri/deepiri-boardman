"""GitHub webhook delivery idempotency table

Revision ID: 004_github_webhook_deliveries
Revises: 003_pr_task_links
Create Date: 2026-05-22

"""

import sqlalchemy as sa

from alembic import op

revision = "004_github_webhook_deliveries"
down_revision = "003_pr_task_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_webhook_deliveries",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        op.f("ix_github_webhook_deliveries_event_type"),
        "github_webhook_deliveries",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_github_webhook_deliveries_created_at"),
        "github_webhook_deliveries",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_github_webhook_deliveries_created_at"), table_name="github_webhook_deliveries"
    )
    op.drop_index(
        op.f("ix_github_webhook_deliveries_event_type"), table_name="github_webhook_deliveries"
    )
    op.drop_table("github_webhook_deliveries")
