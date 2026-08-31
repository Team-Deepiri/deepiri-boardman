"""Add background_jobs, agent_rate_limit_buckets, open_pr_tracks, repo_tier_cache.

These four tables existed only in boardman/database/models.py, never in any prior
migration -- every environment that worked did so because init_db()'s create_all
silently created them alongside whatever alembic had actually migrated. Restricting
create_all to SQLite only (see boardman/database/session.py) surfaced the gap: the
first real Postgres deploy failed with UndefinedTableError on background_jobs because
alembic's own chain never created it.

Idempotent (checks for existing tables first) since a SQLite install or an already-
running Postgres instance whose create_all ran before this migration existed may
already have some or all of these.
"""

import sqlalchemy as sa

from alembic import op

revision = "011_background_jobs_and_caches"
down_revision = "010_agent_session_byok"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "background_jobs" not in existing:
        op.create_table(
            "background_jobs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("kind", sa.String(128), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("success", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_background_jobs_kind", "background_jobs", ["kind"])
        op.create_index("ix_background_jobs_status", "background_jobs", ["status"])
        op.create_index("ix_background_jobs_created_at", "background_jobs", ["created_at"])

    if "agent_rate_limit_buckets" not in existing:
        op.create_table(
            "agent_rate_limit_buckets",
            sa.Column("bucket_key", sa.String(768), primary_key=True),
            sa.Column("water", sa.Float(), nullable=False),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if "open_pr_tracks" not in existing:
        op.create_table(
            "open_pr_tracks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("repo_full_name", sa.String(255), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("plaky_item_id", sa.String(255), nullable=False),
            sa.Column("pr_url", sa.String(512), nullable=True),
            sa.Column("pr_title", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_open_pr_tracks_repo_full_name", "open_pr_tracks", ["repo_full_name"]
        )

    if "repo_tier_cache" not in existing:
        op.create_table(
            "repo_tier_cache",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("repo_full_name", sa.String(255), nullable=False, unique=True),
            sa.Column("tier", sa.Integer(), nullable=False),
            sa.Column("classified_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_repo_tier_cache_repo_full_name", "repo_tier_cache", ["repo_full_name"]
        )


def downgrade() -> None:
    for table in ("repo_tier_cache", "open_pr_tracks", "agent_rate_limit_buckets", "background_jobs"):
        if table in _existing_tables():
            op.drop_table(table)
