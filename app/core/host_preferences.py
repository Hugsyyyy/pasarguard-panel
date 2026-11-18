"""Per-admin host display preferences ("My Hosts", Pengu fork).

An admin who cannot edit the shared hosts (an operator) may rename, reorder and hide hosts for
their own users only. The shared host records are never modified.

Stored in ``admins.host_preferences``::

    {"hosts": {"<host id>": {"remark": str | None, "priority": int | None, "enabled": bool}}}

- ``remark`` None keeps the shared name.
- ``priority`` is the host's position in the admin's own order, stored only once the admin really
  reorders. Without positions the users follow the shared order (and the owner's random order).
- ``enabled`` False hides the host from that admin's users.

Who may use it is decided by the admin's role (``role_allows_my_hosts``): the role's "My Hosts" feature
is on (the default) and the role can neither create nor edit hosts. The saved preferences of an admin whose
role does not allow it are kept but not applied, so switching the feature back on restores them.

Malformed entries are ignored rather than trusted: a subscription must never fail because of them.
No app imports, so the subscription code can use this module freely.
"""

from sqlalchemy.exc import SQLAlchemyError


def saved_hosts(host_preferences) -> dict[str, dict]:
    """The per-host entries, keyed by host id as a string; empty when nothing usable is saved."""
    if not isinstance(host_preferences, dict):
        return {}
    hosts = host_preferences.get("hosts")
    if not isinstance(hosts, dict):
        return {}
    return {str(host_id): entry for host_id, entry in hosts.items() if isinstance(entry, dict)}


def custom_priority(entry: dict | None) -> int | None:
    value = (entry or {}).get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def custom_remark(entry: dict | None) -> str | None:
    value = (entry or {}).get("remark")
    return value if isinstance(value, str) and value.strip() else None


def is_enabled(entry: dict | None) -> bool:
    return (entry or {}).get("enabled", True) is not False


def has_custom_order(host_preferences) -> bool:
    """True when a host the users actually get has a saved position (a hidden host's old position
    must not switch the owner's random order off)."""
    return any(
        custom_priority(entry) is not None and is_enabled(entry) for entry in saved_hosts(host_preferences).values()
    )


def _host_id(host):
    return getattr(host, "id", None)


def _shared_key(host):
    host_id = _host_id(host)
    return (getattr(host, "priority", 0) or 0, host_id if host_id is not None else -1)


def order_hosts[H](hosts: list[H], host_preferences) -> list[H]:
    """The hosts in the admin's order.

    Hosts with a saved position come first, sorted by it. Hosts without one (added after the admin
    saved, or never on the admin's page) follow at the end, in the shared order. Without any saved
    position the list comes back unchanged.
    """
    saved = saved_hosts(host_preferences)
    if not any(custom_priority(entry) is not None for entry in saved.values()):
        return list(hosts)
    keys: dict[int, tuple] = {}
    for rank, index in enumerate(sorted(range(len(hosts)), key=lambda i: _shared_key(hosts[i]))):
        host_id = _host_id(hosts[index])
        position = custom_priority(saved.get(str(host_id))) if host_id is not None else None
        keys[index] = (0, position, rank) if position is not None else (1, 0, rank)
    return [hosts[i] for i in sorted(range(len(hosts)), key=lambda i: keys[i])]


def personalize_hosts[H](hosts: list[H], host_preferences) -> list[H]:
    """Apply saved names, visibility and order to hosts (anything with id, remark and priority).

    Without saved preferences the very same list comes back. Re-sorted only when a visible host has
    a saved position, so the shared order (and the owner's random order) still apply otherwise.
    """
    saved = saved_hosts(host_preferences)
    if not saved:
        return hosts
    personalized = []
    for host in hosts:
        host_id = _host_id(host)
        entry = saved.get(str(host_id)) if host_id is not None else None
        if entry is None:
            personalized.append(host)
            continue
        if not is_enabled(entry):
            continue
        remark = custom_remark(entry)
        personalized.append(host.model_copy(update={"remark": remark}) if remark is not None else host)
    if has_custom_order(host_preferences):
        return order_hosts(personalized, host_preferences)
    return personalized


def preferences_cache_key(host_preferences) -> tuple:
    """A hashable summary of the preferences for the subscription config cache key."""
    return tuple(
        sorted(
            (host_id, custom_remark(entry), custom_priority(entry), is_enabled(entry))
            for host_id, entry in saved_hosts(host_preferences).items()
        )
    )


def _field(source, name: str):
    """Read a key from a JSON dict (ORM role) or an attribute / dict-like model (API role)."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    getter = getattr(source, "get", None)
    if callable(getter):
        return getter(name)
    return getattr(source, name, None)


def _grants(value) -> bool:
    """Same rule as the permission checks: a truthy value whose scope (if any) is not NONE (0)."""
    if not value:
        return False
    if isinstance(value, dict) and value.get("scope") is not None:
        try:
            return int(value["scope"]) != 0
        except TypeError, ValueError:
            return False
    return True


def role_allows_my_hosts(role) -> bool:
    """Whether admins of this role may use My Hosts (and have their saved preferences applied).

    Not the owner role, the role's "My Hosts" feature not switched off, and neither ``hosts.create`` nor
    ``hosts.update`` granted. Works with the ORM role (JSON dicts) and with the API model. A missing role
    keeps the old behaviour (allowed).
    """
    if role is None:
        return True
    if getattr(role, "is_owner", False):
        return False
    if _field(getattr(role, "features", None), "can_use_my_hosts") is False:
        return False
    hosts = _field(getattr(role, "permissions", None), "hosts")
    return not (_grants(_field(hosts, "create")) or _grants(_field(hosts, "update")))


async def drop_disallowed_preferences(db_user, user) -> None:
    """Forget the admin's preferences on ``user`` (the response model) when their role does not allow
    My Hosts. Nothing is changed in the database. The role is loaded only for admins with preferences
    (the subscription endpoints have it loaded already); a detached admin keeps the old behaviour."""
    admin = getattr(db_user, "admin", None)
    admin_model = getattr(user, "admin", None)
    if admin is None or admin_model is None or not getattr(admin_model, "host_preferences", None):
        return
    try:
        role = await admin.awaitable_attrs.role
    except SQLAlchemyError:
        return
    if not role_allows_my_hosts(role):
        admin_model.host_preferences = None
