from PasarGuardNodeBridge import create_proxy, create_user
from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import async_object_session

from app.db import AsyncSession
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Group,
    ProxyInbound,
    User,
    UserStatus,
    inbounds_groups_association,
    users_groups_association,
)
from app.models.protocol import ProxyProtocol

_ALL_PROXY_PROTOCOLS = frozenset(ProxyProtocol)


def _inbounds_from_loaded_groups(user: User) -> list[str] | None:
    loaded_groups = user.__dict__.get("groups")
    if loaded_groups is None:
        return None

    tags: set[str] = set()
    for group in loaded_groups:
        if group.is_disabled:
            continue

        loaded_inbounds = group.__dict__.get("inbounds")
        if loaded_inbounds is None:
            return None

        for inbound in loaded_inbounds:
            tags.add(inbound.tag)

    return list(tags)


async def serialize_user(user: User, allowed_protocols: frozenset[ProxyProtocol] | None = None) -> ProtoUser:
    user_settings = user.proxy_settings
    inbounds = None
    status = user.__dict__.get("status")
    if status is None:
        status = await user.awaitable_attrs.status

    if status in (UserStatus.active, UserStatus.on_hold):
        inbounds = _inbounds_from_loaded_groups(user)
        if inbounds is None:
            inbounds = await user.inbounds()
        if inbounds:
            inbounds = (await _pengu_basket_filtered(user, [(user, inbounds)]))[0]

    return _serialize_user_for_node(user.id, user_settings, inbounds, allowed_protocols)


async def _pengu_basket_filtered(any_user: User, items: list[tuple[User, list[str]]]) -> list[list[str]]:
    """Pengu fork: drop the tags of traffic baskets the user's admin has used up (app/core/pengu_baskets.py)."""
    session = async_object_session(any_user)
    if session is None or not items:
        return [tags for _, tags in items]
    from app.db.crud.pengu_baskets import basket_tag_filter

    apply = await basket_tag_filter(session, {user.__dict__.get("admin_id") for user, _ in items})
    if apply is None:
        return [tags for _, tags in items]
    return [apply(user.__dict__.get("admin_id"), tags) for user, tags in items]


async def node_payload_signature(user: User) -> tuple[str, bytes, tuple[str, ...]]:
    """What a node would receive for this user, in a form that can be compared before and after a change.

    Uses the same serialization as delivery (credentials, inbounds derived from
    enabled groups, and status), so equal signatures mean no node needs an update.
    """
    proto = await serialize_user(user)
    return proto.email, proto.proxies.SerializeToString(deterministic=True), tuple(sorted(proto.inbounds))


def _serialize_user_for_node(
    id: int,
    user_settings: dict,
    inbounds: list[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
) -> ProtoUser:
    """Build a node user with the numeric ID as its email and supplied inbounds.

    Include credentials only for ``allowed_protocols``; both ``None`` and an
    empty set select all protocols. MASQUE uses the stored ``password`` as its
    node credential. Bridge serialization errors propagate to the caller.
    """
    allowed_protocols = allowed_protocols or _ALL_PROXY_PROTOCOLS

    proxy_kwargs = {}
    if ProxyProtocol.vmess in allowed_protocols:
        proxy_kwargs["vmess_id"] = user_settings.get("vmess", {}).get("id")
    if ProxyProtocol.vless in allowed_protocols:
        proxy_kwargs["vless_id"] = user_settings.get("vless", {}).get("id")
    if ProxyProtocol.trojan in allowed_protocols:
        proxy_kwargs["trojan_password"] = user_settings.get("trojan", {}).get("password")
    if ProxyProtocol.shadowsocks in allowed_protocols:
        shadowsocks_settings = user_settings.get("shadowsocks", {})
        proxy_kwargs["shadowsocks_password"] = shadowsocks_settings.get("password")
        proxy_kwargs["shadowsocks_method"] = shadowsocks_settings.get("method")
    if ProxyProtocol.wireguard in allowed_protocols:
        wireguard_settings = user_settings.get("wireguard", {})
        proxy_kwargs["wireguard_public_key"] = wireguard_settings.get("public_key")
        proxy_kwargs["wireguard_peer_ips"] = wireguard_settings.get("peer_ips") or []
    if ProxyProtocol.hysteria in allowed_protocols:
        proxy_kwargs["hysteria_auth"] = user_settings.get("hysteria", {}).get("auth")
    if ProxyProtocol.masque in allowed_protocols:
        proxy_kwargs["masque_pass"] = user_settings.get("masque", {}).get("password")

    return create_user(
        str(id),
        create_proxy(**proxy_kwargs),
        inbounds,
    )


async def core_users(
    db: AsyncSession,
    inbound_tags: list[str] | set[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
    user_ids: list[int] | set[int] | None = None,
):
    """Serialize users with enabled, matching inbounds, optionally restricted to ``user_ids``."""
    if user_ids is not None and not user_ids:
        return []
    dialect = db.bind.dialect.name
    inbound_tags = list(dict.fromkeys(inbound_tags or []))

    # Use dialect-specific aggregation and grouping
    if dialect == "postgresql":
        inbound_agg = func.string_agg(ProxyInbound.tag.distinct(), ",").label("inbound_tags")
    else:
        # MySQL and SQLite use group_concat
        inbound_agg = func.group_concat(ProxyInbound.tag.distinct()).label("inbound_tags")

    stmt = (
        select(
            User.id,
            User.proxy_settings,
            inbound_agg,
            User.admin_id,  # Pengu fork: traffic basket filter below
        )
        # Users without a matching enabled inbound are omitted from the payload below.
        # Inner joins let the database prune unrelated memberships before aggregation.
        .join(users_groups_association, User.id == users_groups_association.c.user_id)
        .join(
            Group,
            and_(
                users_groups_association.c.groups_id == Group.id,
                Group.is_disabled.is_(False),
            ),
        )
        .join(inbounds_groups_association, Group.id == inbounds_groups_association.c.group_id)
        .join(
            ProxyInbound,
            and_(
                inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                ProxyInbound.tag.in_(inbound_tags) if inbound_tags else True,
            ),
        )
        # Exclude users whose admin role blocks user sync for the admin's current status.
        .outerjoin(Admin, Admin.id == User.admin_id)
        .outerjoin(AdminRole, AdminRole.id == Admin.role_id)
        .where(User.status.in_([UserStatus.active, UserStatus.on_hold]))
        .where(
            or_(
                Admin.id.is_(None),
                Admin.status.not_in([AdminStatus.limited, AdminStatus.disabled]),
                and_(Admin.status == AdminStatus.limited, AdminRole.disconnect_users_when_limited.is_not(True)),
                and_(Admin.status == AdminStatus.disabled, AdminRole.disconnect_users_when_disabled.is_not(True)),
            )
        )
        .group_by(User.id)
    )
    if user_ids is not None:
        stmt = stmt.where(User.id.in_(list(user_ids)))

    results = (await db.execute(stmt)).all()
    bridge_users: list = []

    # Pengu fork: drop the tags of traffic baskets the user's admin has used up.
    from app.db.crud.pengu_baskets import basket_tag_filter

    basket_filter = await basket_tag_filter(db, {row.admin_id for row in results})

    for row in results:
        inbound_tags = row.inbound_tags.split(",") if row.inbound_tags else []
        if inbound_tags and basket_filter is not None:
            inbound_tags = basket_filter(row.admin_id, inbound_tags)
        if inbound_tags:
            bridge_users.append(
                _serialize_user_for_node(
                    row.id,
                    row.proxy_settings,
                    inbound_tags,
                    allowed_protocols,
                )
            )
    return bridge_users


async def removal_users(db: AsyncSession, user_ids: list[int] | set[int]) -> list[ProtoUser]:
    """Removal payloads (no inbounds) with the stored credentials of the given users that still exist.

    Nodes may need the credentials to find what to remove: node v0.5.4's
    WireGuard backend ignores a removal without a public key and peer IPs.
    """
    if not user_ids:
        return []
    stmt = select(User.id, User.proxy_settings).where(User.id.in_(list(user_ids)))
    return [_serialize_user_for_node(row.id, row.proxy_settings or {}) for row in (await db.execute(stmt)).all()]


async def serialize_users_for_node(
    users: list[User],
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
) -> list[ProtoUser]:
    """Serialize users for node dispatch."""
    bridge_users: list = []
    prepared: list[tuple[User, list[str]]] = []

    for user in users:
        inbounds_list = []
        if user.status in [UserStatus.active, UserStatus.on_hold]:
            loaded_inbounds = _inbounds_from_loaded_groups(user)
            if loaded_inbounds is None:
                inbounds_list = await user.inbounds()
            else:
                inbounds_list = loaded_inbounds
        prepared.append((user, inbounds_list))

    filtered = await _pengu_basket_filtered(users[0], prepared) if users else []
    for (user, _), inbounds_list in zip(prepared, filtered):
        bridge_users.append(_serialize_user_for_node(user.id, user.proxy_settings, inbounds_list, allowed_protocols))

    return bridge_users
