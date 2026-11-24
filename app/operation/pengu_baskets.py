"""Pengu fork: traffic basket operations that also reach the nodes.

Whenever an admin's blocked baskets change, ``pending_sync`` is raised in the same
transaction and the admin's users are re-sent to the nodes; the node payload drops the
tags of blocked baskets (app/node/user.py). The flag is cleared only after the re-send,
and the usage job retries every admin still flagged, so a failed delivery is not lost.
"""

from __future__ import annotations

import asyncio

from app.db import AsyncSession, GetDB
from app.db.crud import pengu_baskets as crud
from app.db.crud.user import get_admin_users_for_node_sync
from app.node.sync import sync_users
from app.utils.logger import get_logger

logger = get_logger("pengu-baskets")


SHARED_NAME = "shared"

# Admins whose re-send is running in this process (the usage job must not start a second one).
_resyncing: set[int] = set()
_tasks: set[asyncio.Task] = set()


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
    """Re-send the admin's users to the nodes, then clear its ``pending_sync`` flag."""
    users = await get_admin_users_for_node_sync(db, admin_id)
    if users:
        await sync_users(users)
    await crud.clear_pending_sync(db, admin_id)
    return len(users)


async def resync_admin_users_plain(db: AsyncSession, admin_id: int) -> int:
    """Re-send the admin's users (no basket flag to clear, e.g. after leaving basket mode)."""
    users = await get_admin_users_for_node_sync(db, admin_id)
    if users:
        await sync_users(users)
    return len(users)


async def resync_after_owner_change(db: AsyncSession, users: list, admin_ids: set) -> None:
    """Users moved between admins: re-send them when the old or new owner has a used-up basket."""
    if users and await crud.blocked_tags_by_admin(db, [a for a in admin_ids if a is not None]):
        await sync_users(users)


async def is_basket_mode(db: AsyncSession, admin_id: int) -> bool:
    return await crud.get_shared(db, admin_id) is not None


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


async def _resync_in_background(admin_ids: list[int]) -> None:
    for admin_id in admin_ids:
        try:
            async with GetDB() as db:
                count = await resync_admin_users(db, admin_id)
            logger.info("Admin %s: basket blocks changed, %s users re-sent to nodes", admin_id, count)
        except Exception:
            logger.exception("Admin %s: re-sending users after a basket change failed; will retry", admin_id)
        finally:
            _resyncing.discard(admin_id)


def _schedule_resync(admin_ids) -> None:
    todo = sorted(a for a in set(admin_ids) if a not in _resyncing)
    if not todo:
        return
    _resyncing.update(todo)
    task = asyncio.create_task(_resync_in_background(todo))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def charge_and_enforce(usage) -> None:
    """Called by the usage job with admin id -> basket key -> bytes.

    Charging is one short locked transaction; re-sending users to the nodes runs outside the
    job, for admins whose blocks just changed and for any admin still flagged from before.
    """
    async with GetDB() as db:
        changed = await crud.charge_usage(db, usage) if usage else set()
        pending = await crud.pending_sync_admin_ids(db)
    _schedule_resync(set(changed) | set(pending))
