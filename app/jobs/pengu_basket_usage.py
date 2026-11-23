"""Pengu fork: charge each node's usage to the traffic basket of that node.

Called from ``record_usages`` with the per-node stats (before upstream merges the
nodes), after upstream has already updated users and ``Admin.used_traffic``. Only
admins in basket mode are charged here; nothing changes for anyone else.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from app.core.manager import core_manager
from app.core.pengu_baskets import node_basket
from app.db import GetDB
from app.db.crud.pengu_baskets import basket_mode_admin_ids, tag_basket_map
from app.db.models import Node, User
from app.operation.pengu_baskets import charge_and_enforce
from app.utils.logger import get_logger

logger = get_logger("pengu-baskets")

_LOOKUP_BATCH = 2000


async def _node_tags(node_ids: list[int]) -> dict[int, list[str]]:
    async with GetDB() as db:
        rows = (await db.execute(select(Node.id, Node.core_config_id).where(Node.id.in_(node_ids)))).all()
    core_of = {node_id: (core_id or 1) for node_id, core_id in rows}
    await core_manager._reload_from_cache()
    cores = await core_manager.get_cores(set(core_of.values()) | {1})
    default = cores.get(1)
    out = {}
    for node_id, core_id in core_of.items():
        core = cores.get(core_id) or default
        out[node_id] = list(core.inbounds) if core is not None else []
    return out


async def basket_usage(api_params: dict, usage_coefficient: dict) -> dict[int, dict[int | None, int]]:
    """admin id -> basket id (None = outside every basket) -> bytes, for admins in basket mode."""
    async with GetDB() as db:
        mode_admins = await basket_mode_admin_ids(db)
        if not mode_admins:
            return {}
        tag_baskets = await tag_basket_map(db)
        uids = {int(p["uid"]) for params in api_params.values() for p in params or []}
        user_admin: dict[int, int] = {}
        uid_list = list(uids)
        for start in range(0, len(uid_list), _LOOKUP_BATCH):
            batch = uid_list[start : start + _LOOKUP_BATCH]
            rows = (
                await db.execute(
                    select(User.id, User.admin_id).where(User.id.in_(batch), User.admin_id.in_(list(mode_admins)))
                )
            ).all()
            user_admin.update({uid: admin_id for uid, admin_id in rows})
    if not user_admin:
        return {}

    node_tags = await _node_tags([node_id for node_id, params in api_params.items() if params])
    usage: dict[int, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
    for node_id, params in api_params.items():
        if not params:
            continue
        basket_id = node_basket(node_tags.get(node_id, []), tag_baskets)
        coeff = usage_coefficient.get(node_id, 1)
        for param in params:
            admin_id = user_admin.get(int(param["uid"]))
            if admin_id is not None:
                usage[admin_id][basket_id] += int(param["value"] * coeff)
    return {admin_id: dict(per_basket) for admin_id, per_basket in usage.items()}


async def charge_baskets(api_params: dict, usage_coefficient: dict) -> None:
    """Never raises: a failure here must not stop upstream's own accounting."""
    try:
        usage = await basket_usage(api_params, usage_coefficient)
        if usage:
            await charge_and_enforce(usage)
    except Exception:
        logger.exception("Failed to charge traffic basket usage")
