"""Pengu fork: traffic basket rules as pure functions (no DB, no I/O).

See ``app/db/pengu_baskets_models.py`` for the data model. Everything that decides
*how much is charged where* and *what is cut* lives here so it can be tested alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Usage of tags that belong to no basket is charged to the shared volume only.
NO_BASKET: None = None


@dataclass(frozen=True)
class Volume:
    """A volume: ``limit`` None means unlimited."""

    limit: int | None
    used: int = 0

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)

    @property
    def has_room(self) -> bool:
        return self.limit is None or self.used < self.limit


def charge(
    usage: Mapping[int | None, int],
    own: Mapping[int, Volume],
    shared: Volume,
) -> tuple[dict[int, int], int]:
    """Split one admin's usage per basket into increments of its own volumes and of the shared volume.

    Each basket's usage fills that basket's own remaining volume first; the rest goes to
    the shared volume (which may then exceed its limit: that is real, already-used traffic).
    ``usage`` keys are basket ids, or ``NO_BASKET`` for tags outside every basket.
    """
    own_increments: dict[int, int] = {}
    shared_increment = 0
    for basket_id, amount in usage.items():
        amount = int(amount)
        if amount <= 0:
            continue
        taken = 0
        volume = own.get(basket_id) if basket_id is not NO_BASKET else None
        if volume is not None:
            already = own_increments.get(basket_id, 0)
            room = amount if volume.limit is None else max(volume.limit - volume.used - already, 0)
            taken = min(amount, room)
            if taken:
                own_increments[basket_id] = already + taken
        shared_increment += amount - taken
    return own_increments, shared_increment


def blocked_baskets(
    basket_ids: Iterable[int],
    own: Mapping[int, Volume],
    shared: Volume,
) -> tuple[set[int], bool]:
    """Return (baskets the admin's users may not use, whether the shared volume is empty).

    A basket is blocked when the admin's own volume in it has no room and the shared
    volume has no room either. An empty shared volume also blocks tags outside every basket.
    """
    shared_empty = not shared.has_room
    if not shared_empty:
        return set(), False
    blocked = set()
    for basket_id in basket_ids:
        volume = own.get(basket_id)
        if volume is None or not volume.has_room:
            blocked.add(basket_id)
    return blocked, True


def node_basket(node_tags: Iterable[str], tag_baskets: Mapping[str, int]) -> int | None:
    """The basket a node's usage belongs to.

    ``tag_baskets`` maps an inbound tag to its basket. Tags outside every basket are
    ignored. A node whose tags fall in exactly one basket belongs to it; a node with
    no basketed tag, or with tags of several baskets (its per-user stats cannot be
    split by tag), is charged to the shared volume (``NO_BASKET``).
    """
    baskets = {tag_baskets[tag] for tag in node_tags if tag in tag_baskets}
    if len(baskets) == 1:
        return next(iter(baskets))
    return NO_BASKET


def ambiguous_nodes(node_tags: Mapping[int, Iterable[str]], tag_baskets: Mapping[str, int]) -> list[int]:
    """Nodes whose tags span several baskets (shown as a warning to the owner)."""
    out = []
    for node_id, tags in node_tags.items():
        if len({tag_baskets[t] for t in tags if t in tag_baskets}) > 1:
            out.append(node_id)
    return sorted(out)


def allowed_tags(
    tags: Iterable[str],
    tag_baskets: Mapping[str, int],
    blocked: set[int],
    shared_blocked: bool,
) -> list[str]:
    """Drop the tags of blocked baskets (and, when the shared volume is empty, tags outside every basket)."""
    out = []
    for tag in tags:
        basket_id = tag_baskets.get(tag)
        if basket_id is None:
            if shared_blocked:
                continue
        elif basket_id in blocked:
            continue
        out.append(tag)
    return out
