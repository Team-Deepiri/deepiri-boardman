"""Widen qa_capability_profiles.qa_tier from Integer to Float.

qa_tier is now a weighted blend of demonstrated evidence (e.g. 1.7), not forced to
round to the nearest whole bucket -- see
boardman/github/repo_capability_mining.py's demonstrated_tier_from_repo_stats.
"""

import sqlalchemy as sa

from alembic import op

revision = "015_qa_capability_profiles_float_tier"
down_revision = "014_qa_capability_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("qa_capability_profiles") as batch_op:
        batch_op.alter_column(
            "qa_tier",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("qa_capability_profiles") as batch_op:
        batch_op.alter_column(
            "qa_tier",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
