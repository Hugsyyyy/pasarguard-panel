"""Pengu fork: traffic basket operations that also reach the nodes.

Whenever an admin's blocked baskets change, that admin's users are re-sent to the
nodes; the node payload drops the tags of blocked baskets (app/node/user.py).
"""

from __future__ import annotations

from app.db import AsyncSession, GetDB
from app.db.crud import pengu_baskets as crud
from app.db.crud.user import get_admin_users_for_node_sync
from app.node.sync import sync_users
from app.utils.logger import get_logger

logger = get_logger("pengu-baskets")


SHARED_NAME = "shared"


async def used_up_basket_for_groups(db: AsyncSession, admin_id: int, group_ids: list[int]) -> str | None:
    """Name of a used-up basket among these groups for this admin, or None when all can be used."""
    if not group_ids:
        return None
    blocked, shared_blocked = await crud.blocked_state(db, admin_id)
    if not blocked and not shared_blocked:
        return None
    group_baskets = await crud.group_basket_map(db)
    for group_id in group_ids:
        basket_id = group_baskets.get(group_id)
        if basket_id is None:
            if shared_blocked:
                return SHARED_NAME
        elif basket_id in blocked:
            basket = await crud.get_basket(db, basket_id)
            return basket.name if basket else str(basket_id)
    return None


async def resync_admin_users(db: AsyncSession, admin_id: int) -> int:
    users = await get_admin_users_for_node_sync(db, admin_id)
    if users:
        await sync_users(users)
    return len(users)


async def redirect_shared_limit(db: AsyncSession, db_admin, modified_admin) -> bool:
    """For an admin in basket mode, a data limit sent through the upstream admin API sets its shared
    basket instead (0 = unlimited, as upstream). Returns whether the admin is in basket mode."""
    shared = await crud.get_shared(db, db_admin.id)
    if shared is None:
        return False
    if modified_admin.data_limit is not None:
        value = int(modified_admin.data_limit)
        shared.data_limit = value if value > 0 else None
        modified_admin.data_limit = None
    return True


async def reset_basket_usage(db: AsyncSession, admin_id: int) -> None:
    if await crud.get_shared(db, admin_id) is None:
        return
    await crud.reset_volumes_usage(db, admin_id)
    await db.commit()
    await apply_blocks(db, admin_id)


async def apply_blocks(db: AsyncSession, admin_id: int) -> bool:
    """Recompute one admin's blocks after a volume change; re-send its users when they changed."""
    await db.commit()
    changed = await crud.recompute_blocks(db, admin_id)
    if changed:
        count = await resync_admin_users(db, admin_id)
        logger.info("Admin %s: basket blocks changed, %s users re-sent to nodes", admin_id, count)
    return changed


async def apply_blocks_for_all(db: AsyncSession) -> int:
    """After a basket definition changed (groups moved): every basket-mode admin is re-checked."""
    changed = 0
    for admin_id in sorted(await crud.basket_mode_admin_ids(db)):
        if await apply_blocks(db, admin_id):
            changed += 1
    return changed


async def resync_all_basket_admins(db: AsyncSession) -> None:
    """Group membership of a basket changed: the tags a block removes changed too."""
    for admin_id in await crud.blocked_tags_by_admin(db, await crud.basket_mode_admin_ids(db)):
        await resync_admin_users(db, admin_id)


async def charge_and_enforce(usage) -> None:
    """Called by the usage job with admin id -> basket id -> bytes."""
    if not usage:
        return
    async with GetDB() as db:
        changed = await crud.charge_usage(db, usage)
        for admin_id in sorted(changed):
            count = await resync_admin_users(db, admin_id)
            logger.info("Admin %s: basket blocks changed by usage, %s users re-sent to nodes", admin_id, count)
