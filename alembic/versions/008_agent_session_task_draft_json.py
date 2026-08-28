"""Add agent_sessions.task_draft_json.

The only one of the four columns `init_db`'s SQLite shim adds at startup that never got
its own migration. `AgentSession.task_draft_json` (boardman/database/models.py) has relied
on the shim alone since it was added; this brings it in line with the other three.
"""

import sqlalchemy as sa

from alembic import op

revision = "008_agent_session_task_draft_json"
down_revision = "007_pr_link_withdrawn_github_at"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == name for c in inspector.get_columns("agent_sessions"))


def upgrade() -> None:
    # Guarded, same as 007: the SQLite shim in init_db adds this column at startup, so an
    # instance that starts before `alembic upgrade head` runs must not hit "duplicate
    # column name" here.
    if not _has_column("task_draft_json"):
        op.add_column("agent_sessions", sa.Column("task_draft_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("task_draft_json"):
        op.drop_column("agent_sessions", "task_draft_json")
