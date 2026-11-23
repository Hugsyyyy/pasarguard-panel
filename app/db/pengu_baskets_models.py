"""Pengu fork: traffic baskets (per-admin volume for a set of groups).

The owner defines baskets as sets of groups (e.g. Xray = groups 1, 4; WireGuard =
groups 2, 3). An admin is in *basket mode* when it has an ``AdminSharedBasket``
row; it then holds a volume per basket plus a shared volume that covers every
service. Its upstream ``Admin.data_limit`` is kept unlimited (None) so upstream's
whole-admin limiting never fires; ``Admin.used_traffic`` still counts all usage.
Admins without that row behave exactly as upstream.

Usage of a node is charged to the basket its inbound tags belong to: first the
admin's own volume in that basket, then the shared volume. A basket is *blocked*
for an admin when both are empty; the admin's users then lose that basket's
inbound tags on the nodes (see ``app/core/pengu_baskets.py``). Tags that belong
to no basket are covered by the shared volume only.

Kept in a separate module so upstream merges never touch these tables.
"""

from datetime import UTC, datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class TrafficBasket(Base):
    __tablename__ = "pengu_traffic_baskets"
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC), init=False)


class TrafficBasketGroup(Base):
    """A group belongs to at most one basket (group_id is the primary key)."""

    __tablename__ = "pengu_traffic_basket_groups"
    group_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    basket_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("pengu_traffic_baskets.id", ondelete="CASCADE"), index=True
    )


class AdminSharedBasket(Base):
    """Marks an admin as being in basket mode and holds its shared volume.

    ``data_limit`` None means unlimited, 0 means no shared volume. ``used_traffic`` may
    exceed ``data_limit`` (usage that arrived after everything ran out). ``blocked`` is
    True while the shared volume is empty: tags outside every basket are then cut.
    """

    __tablename__ = "pengu_admin_shared_baskets"
    admin_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True
    )
    data_limit: Mapped[int | None] = mapped_column(BigInteger, default=0, server_default="0", nullable=True)
    used_traffic: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    blocked: Mapped[bool] = mapped_column(default=False, server_default="0")


class AdminBasket(Base):
    """An admin's own volume in one basket. ``used_traffic`` never exceeds ``data_limit``:
    whatever does not fit is charged to the shared volume."""

    __tablename__ = "pengu_admin_baskets"
    __table_args__ = (UniqueConstraint("admin_id", "basket_id", name="uq_pengu_admin_basket"),)
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("admins.id", ondelete="CASCADE"), index=True
    )
    basket_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("pengu_traffic_baskets.id", ondelete="CASCADE"), index=True
    )
    data_limit: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    used_traffic: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class AdminBasketBlock(Base):
    """A basket the admin's users currently may not use (own volume and shared basket empty).

    Kept as rows so the node payload query can exclude the basket's tags with one join.
    """

    __tablename__ = "pengu_admin_basket_blocks"
    admin_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True
    )
    basket_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("pengu_traffic_baskets.id", ondelete="CASCADE"), primary_key=True
    )
