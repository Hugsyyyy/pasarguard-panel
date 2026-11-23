"""Pengu fork: pure traffic-basket rules (app/core/pengu_baskets.py)."""

from app.core.pengu_baskets import (
    NO_BASKET,
    Volume,
    allowed_tags,
    ambiguous_nodes,
    blocked_baskets,
    charge,
    node_basket,
)

GB = 1024**3
XRAY, WG = 1, 2


def test_volume_remaining_and_room():
    assert Volume(10, 3).remaining == 7
    assert Volume(10, 12).remaining == 0
    assert Volume(None, 99).remaining is None
    assert Volume(None, 99).has_room
    assert Volume(10, 9).has_room
    assert not Volume(10, 10).has_room
    assert not Volume(0, 0).has_room


def test_charge_own_basket_first_then_shared():
    own = {WG: Volume(10 * GB, 8 * GB)}
    own_inc, shared_inc = charge({WG: 5 * GB}, own, Volume(100 * GB, 0))
    assert own_inc == {WG: 2 * GB}
    assert shared_inc == 3 * GB


def test_charge_basket_without_own_volume_goes_to_shared():
    own_inc, shared_inc = charge({XRAY: 4 * GB, NO_BASKET: 1 * GB}, {WG: Volume(10 * GB, 0)}, Volume(0, 0))
    assert own_inc == {}
    assert shared_inc == 5 * GB


def test_charge_keeps_every_byte():
    own = {XRAY: Volume(3, 0), WG: Volume(5, 4)}
    usage = {XRAY: 10, WG: 10, NO_BASKET: 7}
    own_inc, shared_inc = charge(usage, own, Volume(None, 0))
    assert sum(own_inc.values()) + shared_inc == 27
    assert own_inc == {XRAY: 3, WG: 1}


def test_charge_ignores_zero_and_negative():
    assert charge({XRAY: 0, WG: -5}, {XRAY: Volume(10, 0)}, Volume(0, 0)) == ({}, 0)


def test_blocked_only_when_own_and_shared_are_empty():
    shared_empty = Volume(5, 5)
    blocked, shared_blocked = blocked_baskets([XRAY, WG], {XRAY: Volume(10, 2), WG: Volume(10, 10)}, shared_empty)
    assert blocked == {WG}
    assert shared_blocked


def test_nothing_blocked_while_shared_has_room():
    blocked, shared_blocked = blocked_baskets([XRAY, WG], {}, Volume(10, 9))
    assert blocked == set()
    assert not shared_blocked


def test_unlimited_shared_never_blocks():
    assert blocked_baskets([XRAY, WG], {WG: Volume(0, 0)}, Volume(None, 10**15)) == (set(), False)


def test_basket_never_bought_is_blocked_once_shared_is_empty():
    blocked, _ = blocked_baskets([XRAY, WG], {XRAY: Volume(10, 0)}, Volume(0, 0))
    assert blocked == {WG}


def test_node_basket():
    tag_baskets = {"germany-pf": XRAY, "Netherland": XRAY, "turkey-wg-1": WG}
    assert node_basket(["germany-pf", "Albania"], tag_baskets) == XRAY
    assert node_basket(["turkey-wg-1"], tag_baskets) == WG
    assert node_basket(["unknown"], tag_baskets) is NO_BASKET
    assert node_basket([], tag_baskets) is NO_BASKET
    # tags of two baskets on one node: stats cannot be split, so the shared volume pays
    assert node_basket(["Netherland", "turkey-wg-1"], tag_baskets) is NO_BASKET
    assert ambiguous_nodes({1: ["Netherland", "turkey-wg-1"], 2: ["Netherland"]}, tag_baskets) == [1]


def test_allowed_tags():
    tag_baskets = {"x1": XRAY, "x2": XRAY, "w1": WG}
    tags = ["x1", "x2", "w1", "loose"]
    assert allowed_tags(tags, tag_baskets, set(), False) == tags
    assert allowed_tags(tags, tag_baskets, {WG}, False) == ["x1", "x2", "loose"]
    assert allowed_tags(tags, tag_baskets, {WG}, True) == ["x1", "x2"]
    assert allowed_tags(tags, tag_baskets, {XRAY, WG}, True) == []
