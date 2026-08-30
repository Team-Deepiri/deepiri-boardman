"""Add agent_sessions.byok_provider / byok_key_encrypted / byok_key_expires_at.

Bring-your-own-key: a chat session can temporarily use its own LLM provider API key
instead of the shared default. The raw key is never stored — only Fernet ciphertext.
See boardman/security/byok.py.
"""

import sqlalchemy as sa

from alembic import op

revision = "010_agent_session_byok"
down_revision = "009_pr_link_commits_at_last_review"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == name for c in inspector.get_columns("agent_sessions"))


def upgrade() -> None:
    if not _has_column("byok_provider"):
        op.add_column("agent_sessions", sa.Column("byok_provider", sa.String(32), nullable=True))
    if not _has_column("byok_key_encrypted"):
        op.add_column("agent_sessions", sa.Column("byok_key_encrypted", sa.Text(), nullable=True))
    if not _has_column("byok_key_expires_at"):
        op.add_column(
            "agent_sessions", sa.Column("byok_key_expires_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    for col in ("byok_key_expires_at", "byok_key_encrypted", "byok_provider"):
        if _has_column(col):
            op.drop_column("agent_sessions", col)
