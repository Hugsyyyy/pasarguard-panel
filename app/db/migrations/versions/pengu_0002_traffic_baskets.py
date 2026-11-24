"""traffic baskets: per-admin volume for a set of groups (Pengu fork)

Only new tables: admins without baskets keep the upstream behaviour unchanged.

Revision ID: pengu_0002
Revises: pengu_0001
Create Date: 2026-11-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "pengu_0002"
down_revision = "pengu_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pengu_traffic_baskets",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pengu_traffic_basket_groups",
        sa.Column("group_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("basket_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["basket_id"], ["pengu_traffic_baskets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id"),
    )
    op.create_index(
        "ix_pengu_traffic_basket_groups_basket_id", "pengu_traffic_basket_groups", ["basket_id"], unique=False
    )
    op.create_table(
        "pengu_admin_shared_baskets",
        sa.Column("admin_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("data_limit", sa.BigInteger(), server_default="0", nullable=True),
        sa.Column("used_traffic", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("blocked", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("pending_sync", sa.Boolean(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("admin_id"),
    )
    op.create_table(
        "pengu_admin_baskets",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("admin_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("basket_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("data_limit", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("used_traffic", sa.BigInteger(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["basket_id"], ["pengu_traffic_baskets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("admin_id", "basket_id", name="uq_pengu_admin_basket"),
    )
    op.create_index("ix_pengu_admin_baskets_admin_id", "pengu_admin_baskets", ["admin_id"], unique=False)
    op.create_index("ix_pengu_admin_baskets_basket_id", "pengu_admin_baskets", ["basket_id"], unique=False)
    op.create_table(
        "pengu_admin_basket_blocks",
        sa.Column("admin_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("basket_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["basket_id"], ["pengu_traffic_baskets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("admin_id", "basket_id"),
    )


RESTORE_LIMITS = """
UPDATE admins SET data_limit = (
    SELECT CASE WHEN s.data_limit IS NULL THEN NULL ELSE
        admins.used_traffic
        + CASE WHEN s.data_limit > s.used_traffic THEN s.data_limit - s.used_traffic ELSE 0 END
        + COALESCE((
            SELECT SUM(CASE WHEN b.data_limit > b.used_traffic THEN b.data_limit - b.used_traffic ELSE 0 END)
            FROM pengu_admin_baskets b WHERE b.admin_id = s.admin_id
        ), 0)
    END
    FROM pengu_admin_shared_baskets s WHERE s.admin_id = admins.id
)
WHERE id IN (SELECT admin_id FROM pengu_admin_shared_baskets)
"""


def downgrade() -> None:
    # Basket-mode admins have data_limit NULL (unlimited) upstream: give each one back a single
    # limit with the same remaining volume (shared + every basket) before the tables go.
    op.execute(sa.text(RESTORE_LIMITS))
    op.drop_table("pengu_admin_basket_blocks")
    op.drop_index("ix_pengu_admin_baskets_basket_id", table_name="pengu_admin_baskets")
    op.drop_index("ix_pengu_admin_baskets_admin_id", table_name="pengu_admin_baskets")
    op.drop_table("pengu_admin_baskets")
    op.drop_table("pengu_admin_shared_baskets")
    op.drop_index("ix_pengu_traffic_basket_groups_basket_id", table_name="pengu_traffic_basket_groups")
    op.drop_table("pengu_traffic_basket_groups")
    op.drop_table("pengu_traffic_baskets")
