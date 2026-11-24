"""Pengu fork: database access for traffic baskets (see app/db/pengu_baskets_models.py)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import bindparam, case, delete, insert, select, update

from app.core.pengu_baskets import Volume, blocked_baskets, charge
from app.db import AsyncSession
from app.db.models import Admin, AdminStatus, Group, ProxyInbound, inbounds_groups_association
from app.db.pengu_baskets_models import (
    AdminBasket,
    AdminBasketBlock,
    AdminSharedBasket,
    TrafficBasket,
    TrafficBasketGroup,
)


def _greatest(a, b):
    """Portable GREATEST (SQLite has none)."""
    return case((a > b, a), else_=b)


# Core tables for set-based updates with many parameter sets (not ORM bulk-by-primary-key).
_OWN = AdminBasket.__table__
_SHARED = AdminSharedBasket.__table__


class BasketError(ValueError):
    """A request the basket rules refuse (shown to the caller as a 400/409)."""


# ---------- basket definitions (owner) ----------


async def list_baskets(db: AsyncSession) -> list[tuple[TrafficBasket, list[int]]]:
    baskets = (
        (await db.execute(select(TrafficBasket).order_by(TrafficBasket.sort_order, TrafficBasket.id))).scalars().all()
    )
    rows = (await db.execute(select(TrafficBasketGroup.basket_id, TrafficBasketGroup.group_id))).all()
    groups: dict[int, list[int]] = {}
    for basket_id, group_id in rows:
        groups.setdefault(basket_id, []).append(group_id)
    return [(b, sorted(groups.get(b.id, []))) for b in baskets]


async def get_basket(db: AsyncSession, basket_id: int) -> TrafficBasket | None:
    return await db.get(TrafficBasket, basket_id)


async def _check_groups(db: AsyncSession, group_ids: Iterable[int], basket_id: int | None) -> list[int]:
    group_ids = sorted({int(g) for g in group_ids})
    if group_ids:
        found = set((await db.execute(select(Group.id).where(Group.id.in_(group_ids)))).scalars().all())
        missing = [g for g in group_ids if g not in found]
        if missing:
            raise BasketError(f"Groups not found: {missing}")
        taken = (
            await db.execute(
                select(TrafficBasketGroup.group_id, TrafficBasketGroup.basket_id).where(
                    TrafficBasketGroup.group_id.in_(group_ids)
                )
            )
        ).all()
        clash = [g for g, b in taken if b != basket_id]
        if clash:
            raise BasketError(f"Groups already in another basket: {sorted(clash)}")
    return group_ids


async def create_basket(db: AsyncSession, name: str, group_ids: Iterable[int], sort_order: int = 0) -> TrafficBasket:
    name = (name or "").strip()
    if not name:
        raise BasketError("Basket name is required")
    group_ids = await _check_groups(db, group_ids, None)
    basket = TrafficBasket(name=name[:64], sort_order=sort_order)
    db.add(basket)
    await db.flush()
    for group_id in group_ids:
        db.add(TrafficBasketGroup(group_id=group_id, basket_id=basket.id))
    await db.commit()
    await db.refresh(basket)
    return basket


async def update_basket(
    db: AsyncSession,
    basket: TrafficBasket,
    name: str | None = None,
    group_ids: Iterable[int] | None = None,
    sort_order: int | None = None,
) -> TrafficBasket:
    if name is not None:
        name = name.strip()
        if not name:
            raise BasketError("Basket name is required")
        basket.name = name[:64]
    if sort_order is not None:
        basket.sort_order = sort_order
    if group_ids is not None:
        group_ids = await _check_groups(db, group_ids, basket.id)
        await db.execute(delete(TrafficBasketGroup).where(TrafficBasketGroup.basket_id == basket.id))
        for group_id in group_ids:
            db.add(TrafficBasketGroup(group_id=group_id, basket_id=basket.id))
    await db.commit()
    await db.refresh(basket)
    return basket


async def delete_basket(db: AsyncSession, basket: TrafficBasket) -> None:
    # Explicit deletes: SQLite runs without foreign key enforcement, so ON DELETE CASCADE never fires there.
    await db.execute(delete(TrafficBasketGroup).where(TrafficBasketGroup.basket_id == basket.id))
    await db.execute(delete(AdminBasket).where(AdminBasket.basket_id == basket.id))
    await db.execute(delete(AdminBasketBlock).where(AdminBasketBlock.basket_id == basket.id))
    await db.delete(basket)
    await db.commit()


async def forget_groups(db: AsyncSession, group_ids: list[int]) -> None:
    """Called when groups are deleted (no commit)."""
    await db.execute(delete(TrafficBasketGroup).where(TrafficBasketGroup.group_id.in_(group_ids)))


async def forget_admins(db: AsyncSession, admin_ids: list[int]) -> None:
    """Called when admins are deleted (no commit)."""
    for model in (AdminBasketBlock, AdminBasket, AdminSharedBasket):
        await db.execute(delete(model).where(model.admin_id.in_(admin_ids)))


async def tag_basket_map(db: AsyncSession) -> dict[str, int]:
    """Inbound tag -> basket id, through the basket's groups."""
    stmt = (
        select(ProxyInbound.tag, TrafficBasketGroup.basket_id)
        .select_from(TrafficBasketGroup)
        .join(inbounds_groups_association, inbounds_groups_association.c.group_id == TrafficBasketGroup.group_id)
        .join(ProxyInbound, ProxyInbound.id == inbounds_groups_association.c.inbound_id)
    )
    return {tag: basket_id for tag, basket_id in (await db.execute(stmt)).all()}


async def group_basket_map(db: AsyncSession) -> dict[int, int]:
    rows = (await db.execute(select(TrafficBasketGroup.group_id, TrafficBasketGroup.basket_id))).all()
    return {g: b for g, b in rows}


# ---------- an admin's volumes ----------


async def basket_mode_admin_ids(db: AsyncSession, admin_ids: Iterable[int] | None = None) -> set[int]:
    stmt = select(AdminSharedBasket.admin_id)
    if admin_ids is not None:
        admin_ids = list(admin_ids)
        if not admin_ids:
            return set()
        stmt = stmt.where(AdminSharedBasket.admin_id.in_(admin_ids))
    return set((await db.execute(stmt)).scalars().all())


async def get_shared(db: AsyncSession, admin_id: int) -> AdminSharedBasket | None:
    # populate_existing: a row changed by a bulk UPDATE in this session must never be read stale
    return await db.scalar(
        select(AdminSharedBasket)
        .where(AdminSharedBasket.admin_id == admin_id)
        .execution_options(populate_existing=True)
    )


async def admin_volumes(db: AsyncSession, admin_id: int) -> tuple[AdminSharedBasket | None, dict[int, AdminBasket]]:
    shared = await get_shared(db, admin_id)
    own = (
        (
            await db.execute(
                select(AdminBasket).where(AdminBasket.admin_id == admin_id).execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return shared, {row.basket_id: row for row in own}


async def blocked_state(db: AsyncSession, admin_id: int) -> tuple[set[int], bool]:
    shared = await get_shared(db, admin_id)
    if not isinstance(shared, AdminSharedBasket):  # not in basket mode (also a stub session in unit tests)
        return set(), False
    blocked = set(
        (await db.execute(select(AdminBasketBlock.basket_id).where(AdminBasketBlock.admin_id == admin_id)))
        .scalars()
        .all()
    )
    return blocked, bool(shared.blocked)


# ---------- locked state (usage job and volume changes serialize on the shared row) ----------


class _State:
    __slots__ = ("admin_id", "blocks", "own", "shared", "shared_blocked")

    def __init__(self, admin_id: int, shared: Volume, shared_blocked: bool):
        self.admin_id = admin_id
        self.shared = shared
        self.shared_blocked = shared_blocked
        self.own: dict[int, Volume] = {}
        self.blocks: set[int] = set()


async def _locked_states(db: AsyncSession, admin_ids: Iterable[int]) -> dict[int, _State]:
    """Lock the shared rows of these admins (in id order, so two writers never deadlock) and read
    everything the basket rules need with plain column selects (no stale ORM objects)."""
    ids = sorted({int(a) for a in admin_ids})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                AdminSharedBasket.admin_id,
                AdminSharedBasket.data_limit,
                AdminSharedBasket.used_traffic,
                AdminSharedBasket.blocked,
            )
            .where(AdminSharedBasket.admin_id.in_(ids))
            .order_by(AdminSharedBasket.admin_id)
            .with_for_update()
        )
    ).all()
    states = {aid: _State(aid, Volume(limit, int(used or 0)), bool(blocked)) for aid, limit, used, blocked in rows}
    if not states:
        return {}
    for aid, basket_id, limit, used in (
        await db.execute(
            select(AdminBasket.admin_id, AdminBasket.basket_id, AdminBasket.data_limit, AdminBasket.used_traffic).where(
                AdminBasket.admin_id.in_(list(states))
            )
        )
    ).all():
        states[aid].own[basket_id] = Volume(int(limit or 0), int(used or 0))
    for aid, basket_id in (
        await db.execute(
            select(AdminBasketBlock.admin_id, AdminBasketBlock.basket_id).where(
                AdminBasketBlock.admin_id.in_(list(states))
            )
        )
    ).all():
        states[aid].blocks.add(basket_id)
    return states


async def _store_blocks(db: AsyncSession, states: Iterable[_State], basket_ids: list[int]) -> set[int]:
    """Write each admin's new blocks (rows are held locked by the caller). Returns admins that changed."""
    changed: set[int] = set()
    for state in states:
        new_blocked, new_shared = blocked_baskets(basket_ids, state.own, state.shared)
        if new_blocked == state.blocks and new_shared == state.shared_blocked:
            continue
        await db.execute(delete(AdminBasketBlock).where(AdminBasketBlock.admin_id == state.admin_id))
        for basket_id in sorted(new_blocked):
            await db.execute(insert(AdminBasketBlock).values(admin_id=state.admin_id, basket_id=basket_id))
        await db.execute(
            update(AdminSharedBasket)
            .where(AdminSharedBasket.admin_id == state.admin_id)
            .values(blocked=new_shared, pending_sync=True)
            .execution_options(synchronize_session=False)
        )
        changed.add(state.admin_id)
    return changed


async def recompute_blocks(db: AsyncSession, admin_id: int, commit: bool = True) -> bool:
    """Store which baskets the admin's users may not use. Returns True when that changed.

    A change also raises ``pending_sync`` until the admin's users have been re-sent to the nodes.
    """
    states = await _locked_states(db, [admin_id])
    changed = False
    if states:
        basket_ids = list((await db.execute(select(TrafficBasket.id))).scalars().all())
        changed = bool(await _store_blocks(db, states.values(), basket_ids))
    if commit:
        await db.commit()
    else:
        await db.flush()
    return changed


async def pending_sync_admin_ids(db: AsyncSession) -> list[int]:
    return list(
        (await db.execute(select(AdminSharedBasket.admin_id).where(AdminSharedBasket.pending_sync.is_(True))))
        .scalars()
        .all()
    )


async def clear_pending_sync(db: AsyncSession, admin_id: int) -> None:
    await db.execute(
        update(AdminSharedBasket)
        .where(AdminSharedBasket.admin_id == admin_id)
        .values(pending_sync=False)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# ---------- volume changes ----------


async def enable_basket_mode(db: AsyncSession, admin: Admin) -> AdminSharedBasket:
    """Put an admin in basket mode: its current volume becomes the shared volume.

    The upstream limit is lifted (data_limit None) so upstream never limits the whole
    admin; the basket rules decide what is cut from now on. Idempotent.
    """
    shared = await get_shared(db, admin.id)
    if shared is not None:
        return shared
    if admin.data_limit is None or admin.data_limit <= 0:
        limit, used = None, 0  # unlimited stays unlimited
    else:
        limit, used = int(admin.data_limit), int(admin.used_traffic or 0)
    shared = AdminSharedBasket(admin_id=admin.id, data_limit=limit, used_traffic=used)
    db.add(shared)
    admin.data_limit = None
    if admin.status == AdminStatus.limited:
        admin.status = AdminStatus.active
    await db.flush()
    await recompute_blocks(db, admin.id, commit=False)
    await db.commit()
    return await get_shared(db, admin.id)


async def disable_basket_mode(db: AsyncSession, admin: Admin) -> None:
    """Back to one volume: the upstream limit gets the same remaining volume (shared + every basket)."""
    states = await _locked_states(db, [admin.id])
    state = states.get(admin.id)
    if state is None:
        return
    if state.shared.limit is None:
        admin.data_limit = None
    else:
        remaining = state.shared.remaining + sum(v.remaining or 0 for v in state.own.values())
        admin.data_limit = int(admin.used_traffic or 0) + int(remaining)
    await forget_admins(db, [admin.id])
    await db.commit()


async def set_shared_limit(db: AsyncSession, admin_id: int, data_limit: int | None) -> None:
    if data_limit is not None and data_limit < 0:
        raise BasketError("data_limit cannot be negative")
    await db.execute(
        update(AdminSharedBasket)
        .where(AdminSharedBasket.admin_id == admin_id)
        .values(data_limit=data_limit)
        .execution_options(synchronize_session=False)
    )


async def add_shared_volume(db: AsyncSession, admin_id: int, delta: int) -> None:
    """Add (or with a negative delta remove) shared volume atomically. Unlimited stays unlimited."""
    await db.execute(
        update(AdminSharedBasket)
        .where(AdminSharedBasket.admin_id == admin_id, AdminSharedBasket.data_limit.is_not(None))
        .values(data_limit=_greatest(AdminSharedBasket.data_limit + int(delta), 0))
        .execution_options(synchronize_session=False)
    )


async def _ensure_own(db: AsyncSession, admin_id: int, basket_id: int) -> None:
    exists = await db.scalar(
        select(AdminBasket.id).where(AdminBasket.admin_id == admin_id, AdminBasket.basket_id == basket_id)
    )
    if exists is None:
        await db.execute(
            insert(AdminBasket).values(admin_id=admin_id, basket_id=basket_id, data_limit=0, used_traffic=0)
        )


async def set_basket_limit(db: AsyncSession, admin_id: int, basket_id: int, data_limit: int) -> None:
    if data_limit < 0:
        raise BasketError("data_limit cannot be negative")
    await _ensure_own(db, admin_id, basket_id)
    await db.execute(
        update(AdminBasket)
        .where(AdminBasket.admin_id == admin_id, AdminBasket.basket_id == basket_id)
        .values(data_limit=int(data_limit))
        .execution_options(synchronize_session=False)
    )


async def add_basket_volume(db: AsyncSession, admin_id: int, basket_id: int, delta: int) -> None:
    """Add (or remove) volume in one basket atomically; never below what was already used."""
    await _ensure_own(db, admin_id, basket_id)
    await db.execute(
        update(AdminBasket)
        .where(AdminBasket.admin_id == admin_id, AdminBasket.basket_id == basket_id)
        .values(data_limit=_greatest(AdminBasket.data_limit + int(delta), AdminBasket.used_traffic))
        .execution_options(synchronize_session=False)
    )


async def reset_volumes_usage(db: AsyncSession, admin_id: int) -> None:
    """The owner's «reset usage»: every basket and the shared volume start again from zero."""
    await db.execute(
        update(AdminBasket)
        .where(AdminBasket.admin_id == admin_id)
        .values(used_traffic=0)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(AdminSharedBasket)
        .where(AdminSharedBasket.admin_id == admin_id)
        .values(used_traffic=0)
        .execution_options(synchronize_session=False)
    )


# ---------- usage ----------


async def charge_usage(db: AsyncSession, usage: Mapping[int, Mapping]) -> set[int]:
    """Charge per-admin, per-basket usage of basket-mode admins. Returns admins whose blocks changed.

    ``usage``: admin id -> key -> bytes, already multiplied by the node usage coefficient. A key
    is a basket id, ``None`` (tags outside every basket) or a tuple of basket ids (a node serving
    several baskets: their own volumes are drained in order, then the shared one). Admins not in
    basket mode are ignored; upstream accounting on ``Admin.used_traffic`` is untouched for all.

    One short transaction: the shared rows are locked (id order), all rows are read at once,
    the increments are computed in Python and written back as set-based updates.
    """
    states = await _locked_states(db, usage.keys())
    if not states:
        await db.commit()
        return set()
    own_updates: list[dict] = []
    shared_updates: list[dict] = []
    for admin_id, state in states.items():
        own_inc, shared_inc = charge(usage[admin_id], state.own, state.shared)
        for basket_id, amount in own_inc.items():
            own_updates.append({"a": admin_id, "b": basket_id, "v": amount})
            volume = state.own[basket_id]
            state.own[basket_id] = Volume(volume.limit, volume.used + amount)
        if shared_inc:
            shared_updates.append({"a": admin_id, "v": shared_inc})
            state.shared = Volume(state.shared.limit, state.shared.used + shared_inc)
    if own_updates:
        await db.execute(
            update(_OWN)
            .where(_OWN.c.admin_id == bindparam("a"), _OWN.c.basket_id == bindparam("b"))
            .values(used_traffic=_OWN.c.used_traffic + bindparam("v")),
            own_updates,
        )
    if shared_updates:
        await db.execute(
            update(_SHARED)
            .where(_SHARED.c.admin_id == bindparam("a"))
            .values(used_traffic=_SHARED.c.used_traffic + bindparam("v")),
            shared_updates,
        )
    basket_ids = list((await db.execute(select(TrafficBasket.id))).scalars().all())
    changed = await _store_blocks(db, states.values(), basket_ids)
    await db.commit()
    return changed


# ---------- node payload ----------


async def basket_tag_filter(db: AsyncSession, admin_ids: Iterable[int | None]):
    """A function (admin_id, tags) -> allowed tags for these admins, or None when nobody is blocked.

    Costs one small query when no admin has a block, so every node payload can call it.
    """
    from app.core.pengu_baskets import allowed_tags

    states = await blocked_tags_by_admin(db, [a for a in admin_ids if a is not None])
    if not states:
        return None
    tag_baskets = await tag_basket_map(db)

    def apply(admin_id: int | None, tags: list[str]) -> list[str]:
        state = states.get(admin_id) if admin_id is not None else None
        if state is None:
            return tags
        return allowed_tags(tags, tag_baskets, state[0], state[1])

    return apply


async def blocked_tags_by_admin(db: AsyncSession, admin_ids: Iterable[int]) -> dict[int, tuple[set[int], bool]]:
    """For the given admins in basket mode with any block: admin id -> (blocked baskets, shared empty)."""
    admin_ids = list({int(a) for a in admin_ids if a is not None})
    if not admin_ids:
        return {}
    shared_rows = (
        await db.execute(
            select(AdminSharedBasket.admin_id, AdminSharedBasket.blocked).where(
                AdminSharedBasket.admin_id.in_(admin_ids)
            )
        )
    ).all()
    if not shared_rows:
        return {}
    out: dict[int, tuple[set[int], bool]] = {aid: (set(), bool(b)) for aid, b in shared_rows}
    blocks = (
        await db.execute(
            select(AdminBasketBlock.admin_id, AdminBasketBlock.basket_id).where(
                AdminBasketBlock.admin_id.in_(list(out))
            )
        )
    ).all()
    for admin_id, basket_id in blocks:
        out[admin_id][0].add(basket_id)
    return {aid: state for aid, state in out.items() if state[0] or state[1]}
