"""Unit tests for app.core.host_preferences (operator "My Hosts")."""

from pydantic import BaseModel

from app.core.host_preferences import (
    has_custom_order,
    personalize_hosts,
    preferences_cache_key,
    saved_hosts,
)


class Host(BaseModel):
    id: int | None
    remark: str
    priority: int


HOSTS = [Host(id=1, remark="A", priority=0), Host(id=2, remark="B", priority=1), Host(id=3, remark="C", priority=2)]


def test_no_preferences_returns_the_same_list():
    for prefs in (None, {}, {"hosts": {}}, {"hosts": None}, "garbage", {"hosts": {"1": "garbage"}}):
        assert personalize_hosts(HOSTS, prefs) is HOSTS


def test_rename_and_hide_keep_the_shared_order():
    prefs = {"hosts": {"2": {"remark": "Mine", "priority": None, "enabled": True}, "3": {"enabled": False}}}
    result = personalize_hosts(HOSTS, prefs)
    assert [(h.id, h.remark) for h in result] == [(1, "A"), (2, "Mine")]
    assert not has_custom_order(prefs)
    assert HOSTS[1].remark == "B"  # shared host objects are not modified


def test_custom_order_sorts_by_saved_position():
    prefs = {"hosts": {"1": {"priority": 2}, "2": {"priority": 0}, "3": {"priority": 1}}}
    assert [h.id for h in personalize_hosts(HOSTS, prefs)] == [2, 3, 1]
    assert has_custom_order(prefs)


def test_a_host_without_id_never_breaks_sorting():
    hosts = [Host(id=None, remark="X", priority=1), Host(id=5, remark="Y", priority=1)]
    prefs = {"hosts": {"5": {"priority": 1}}}
    assert [h.remark for h in personalize_hosts(hosts, prefs)] == ["X", "Y"]


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
    assert preferences_cache_key(None) == preferences_cache_key({}) == ()
    assert saved_hosts({"hosts": {1: {"remark": "N"}}}) == {"1": {"remark": "N"}}
