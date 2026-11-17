"""Per-admin host display preferences ("My Hosts").

An admin who cannot edit the shared hosts (an operator) may rename, reorder and hide hosts for
their own users only. The shared host records are never modified.

Stored in ``admins.host_preferences``::

    {"hosts": {"<host id>": {"remark": str | None, "priority": int | None, "enabled": bool}}}

- ``remark`` None keeps the shared name.
- ``priority`` None keeps the shared order, so when the owner later reorders hosts those users
  follow. Priorities are only stored once the admin really reorders.
- ``enabled`` False hides the host from that admin's users.

Malformed entries are ignored rather than trusted: a subscription must never fail because of them.
"""


def saved_hosts(host_preferences: dict | None) -> dict[str, dict]:
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


def has_custom_order(host_preferences: dict | None) -> bool:
    return any(custom_priority(entry) is not None for entry in saved_hosts(host_preferences).values())


def personalize_hosts[H](hosts: list[H], host_preferences: dict | None) -> list[H]:
    """Apply saved names, order and visibility to hosts (anything with id, remark and priority).

    Without saved preferences the list comes back unchanged. The list is re-sorted only when the
    admin chose an order, so the shared order (and the owner's random order) still apply otherwise.
    """
    saved = saved_hosts(host_preferences)
    if not saved:
        return hosts
    personalized = []
    for host in hosts:
        host_id = getattr(host, "id", None)
        entry = saved.get(str(host_id)) if host_id is not None else None
        if entry is None:
            personalized.append(host)
            continue
        if not is_enabled(entry):
            continue
        update = {}
        remark = custom_remark(entry)
        if remark is not None:
            update["remark"] = remark
        priority = custom_priority(entry)
        if priority is not None:
            update["priority"] = priority
        personalized.append(host.model_copy(update=update) if update else host)
    if has_custom_order(host_preferences):
        personalized.sort(key=lambda host: (host.priority, host.id if host.id is not None else -1))
    return personalized


def preferences_cache_key(host_preferences: dict | None) -> tuple:
    """A hashable summary of the preferences for the subscription config cache key."""
    return tuple(
        sorted(
            (host_id, custom_remark(entry), custom_priority(entry), is_enabled(entry))
            for host_id, entry in saved_hosts(host_preferences).items()
        )
    )
