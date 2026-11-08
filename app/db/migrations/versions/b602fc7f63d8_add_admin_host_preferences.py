"""add per-admin host display preferences

Revision ID: b602fc7f63d8
Revises: 1a2b3c4d5e6f
Create Date: 2026-11-08 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b602fc7f63d8"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "host_preferences",
                sa.JSON().with_variant(postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.drop_column("host_preferences")
