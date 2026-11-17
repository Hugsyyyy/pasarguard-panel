"""Unit tests for app.core.host_preferences (operator "My Hosts", Pengu fork)."""

from pydantic import BaseModel

from app.core.host_preferences import (
    has_custom_order,
    order_hosts,
    personalize_hosts,
    preferences_cache_key,
    saved_hosts,
)


class Host(BaseModel):
    id: int | None
    remark: str
    priority: int


A, B, C, D = (Host(id=i, remark=r, priority=p) for i, r, p in ((1, "A", 0), (2, "B", 1), (3, "C", 2), (4, "D", 3)))
HOSTS = [A, B, C]


def ids(hosts):
    return [h.id for h in hosts]


def test_no_preferences_returns_the_same_list():
    for prefs in (None, {}, {"hosts": {}}, {"hosts": None}, "garbage", [1], {"hosts": {"1": "garbage"}}):
        assert personalize_hosts(HOSTS, prefs) is HOSTS


def test_rename_and_hide_keep_the_shared_order():
    prefs = {"hosts": {"2": {"remark": "Mine", "priority": None, "enabled": True}, "3": {"enabled": False}}}
    result = personalize_hosts(HOSTS, prefs)
    assert [(h.id, h.remark) for h in result] == [(1, "A"), (2, "Mine")]
    assert not has_custom_order(prefs)
    assert B.remark == "B"  # shared host objects are not modified


def test_custom_order_sorts_by_saved_position():
    prefs = {"hosts": {"1": {"priority": 2}, "2": {"priority": 0}, "3": {"priority": 1}}}
    assert ids(personalize_hosts(HOSTS, prefs)) == [2, 3, 1]
    assert has_custom_order(prefs)


def test_a_hidden_hosts_old_position_is_not_a_custom_order():
    prefs = {"hosts": {"2": {"priority": 0, "enabled": False}}}
    assert not has_custom_order(prefs)
    assert ids(personalize_hosts(HOSTS, prefs)) == [1, 3]


def test_new_hosts_follow_a_custom_order_in_the_shared_order():
    # the operator saved C, A, B; the owner then adds hosts anywhere in the shared order
    prefs = {"hosts": {"3": {"priority": 0}, "1": {"priority": 1}, "2": {"priority": 2}}}
    assert ids(personalize_hosts([A, B, C, D], prefs)) == [3, 1, 2, 4]
    top = Host(id=5, remark="E", priority=-1)
    middle = Host(id=6, remark="F", priority=0)
    assert ids(personalize_hosts([top, A, middle, B, C, D], prefs)) == [3, 1, 2, 5, 6, 4]


def test_order_hosts_without_positions_keeps_the_given_order():
    assert ids(order_hosts([C, A, B], {"hosts": {"1": {"remark": "x"}}})) == [3, 1, 2]


def test_a_host_without_id_never_breaks_sorting():
    hosts = [Host(id=None, remark="X", priority=1), Host(id=5, remark="Y", priority=1)]
    prefs = {"hosts": {"5": {"priority": 1}}}
    assert [h.remark for h in personalize_hosts(hosts, prefs)] == ["Y", "X"]  # placed first, unplaced after


def test_malformed_entries_are_ignored():
    prefs = {"hosts": {"1": {"remark": "  ", "priority": "high", "enabled": "no"}, "2": {"priority": True}}}
    result = personalize_hosts(HOSTS, prefs)
    assert [(h.id, h.remark, h.priority) for h in result] == [(1, "A", 0), (2, "B", 1), (3, "C", 2)]
    assert not has_custom_order(prefs)


def test_cache_key_changes_with_every_visible_setting():
    base = {"hosts": {"1": {"remark": "N", "priority": None, "enabled": True}}}
    keys = {
        preferences_cache_key(base),
        preferences_cache_key({"hosts": {"1": {"remark": "M", "priority": None, "enabled": True}}}),
        preferences_cache_key({"hosts": {"1": {"remark": "N", "priority": 3, "enabled": True}}}),
        preferences_cache_key({"hosts": {"1": {"remark": "N", "priority": None, "enabled": False}}}),
    }
    assert len(keys) == 4
    assert preferences_cache_key(None) == preferences_cache_key({}) == preferences_cache_key("x") == ()
    assert saved_hosts({"hosts": {1: {"remark": "N"}}}) == {"1": {"remark": "N"}}
