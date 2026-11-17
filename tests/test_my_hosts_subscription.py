"""Subscription tests of "My Hosts" (Pengu fork)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import UserStatus
from app.models.subscription import TCPTransportConfig, TLSConfig
from app.subscription import config_cache
from app.subscription.share import process_inbounds_and_tags


def test_sub_config_cache_key_includes_operator_host_preferences():
    user = SimpleNamespace(
        id=7,
        status=UserStatus.active,
        inbounds=["tag"],
        admin=SimpleNamespace(host_preferences={"hosts": {"1": {"remark": "My host", "priority": 0}}}),
    )
    first_key = config_cache.make_sub_config_key(user, "links", False, False)
    user.admin.host_preferences["hosts"]["1"]["remark"] = "Updated name"
    second_key = config_cache.make_sub_config_key(user, "links", False, False)
    user.admin.host_preferences["hosts"]["1"]["enabled"] = False
    third_key = config_cache.make_sub_config_key(user, "links", False, False)

    assert first_key != second_key
    assert second_key != third_key


@pytest.mark.asyncio
async def test_process_inbounds_uses_operator_host_names_and_order(monkeypatch: pytest.MonkeyPatch):
    from app.models.subscription import SubscriptionInboundData

    hosts = [
        SubscriptionInboundData(
            id=1,
            remark="Shared A",
            inbound_tag="in1",
            protocol="vless",
            address="a.example",
            port=443,
            network="tcp",
            tls_config=TLSConfig(),
            transport_config=TCPTransportConfig(),
            priority=0,
        ),
        SubscriptionInboundData(
            id=2,
            remark="Shared B",
            inbound_tag="in1",
            protocol="vless",
            address="b.example",
            port=443,
            network="tcp",
            tls_config=TLSConfig(),
            transport_config=TCPTransportConfig(),
            priority=1,
        ),
    ]
    monkeypatch.setattr(
        "app.subscription.share.host_manager.get_hosts", AsyncMock(return_value={1: hosts[0], 2: hosts[1]})
    )
    monkeypatch.setattr("app.subscription.share.filter_hosts", AsyncMock(side_effect=lambda values, _status: values))
    monkeypatch.setattr("app.subscription.share.process_host", AsyncMock(side_effect=lambda host, *_args: (host, {})))

    class CaptureConfig:
        def __init__(self):
            self.items = []

        def add(self, **kwargs):
            self.items.append((kwargs["remark"], kwargs["inbound"].id))

        def render(self):
            return "rendered"

    config = CaptureConfig()
    user = SimpleNamespace(
        id=15,
        status=UserStatus.active,
        inbounds=["in1"],
        proxy_settings=SimpleNamespace(dict=dict),
        admin=SimpleNamespace(
            host_preferences={
                "hosts": {
                    "1": {"remark": "Private A", "priority": 1},
                    "2": {"remark": None, "priority": 0, "enabled": False},
                }
            }
        ),
    )

    result = await process_inbounds_and_tags(
        user,
        {},
        config,
        {},
        randomize_order=True,
    )

    assert result == "rendered"
    assert config.items == [("Private A", 1)]


@pytest.mark.asyncio
async def test_renaming_hosts_keeps_the_random_order(monkeypatch: pytest.MonkeyPatch):
    """Only a chosen order replaces the owner's random order; a rename or a hidden host does not."""
    from app.models.subscription import SubscriptionInboundData

    hosts = [
        SubscriptionInboundData(
            id=host_id,
            remark=f"Shared {host_id}",
            inbound_tag="in1",
            protocol="vless",
            address=f"{host_id}.example",
            port=443,
            network="tcp",
            tls_config=TLSConfig(),
            transport_config=TCPTransportConfig(),
            priority=host_id,
        )
        for host_id in (1, 2, 3)
    ]
    monkeypatch.setattr(
        "app.subscription.share.host_manager.get_hosts", AsyncMock(return_value={h.id: h for h in hosts})
    )
    monkeypatch.setattr("app.subscription.share.filter_hosts", AsyncMock(side_effect=lambda values, _status: values))
    monkeypatch.setattr("app.subscription.share.process_host", AsyncMock(side_effect=lambda host, *_args: (host, {})))
    shuffled: list[list[int]] = []

    def fake_shuffle(items):
        shuffled.append([item.id for item in items])
        items.reverse()

    monkeypatch.setattr("app.subscription.share.random.shuffle", fake_shuffle)

    class CaptureConfig:
        def __init__(self):
            self.items = []

        def add(self, **kwargs):
            self.items.append((kwargs["remark"], kwargs["inbound"].id))

        def render(self):
            return "rendered"

    def user_with(preferences):
        return SimpleNamespace(
            id=16,
            status=UserStatus.active,
            inbounds=["in1"],
            proxy_settings=SimpleNamespace(dict=dict),
            admin=SimpleNamespace(host_preferences=preferences),
        )

    renamed = CaptureConfig()
    await process_inbounds_and_tags(
        user_with({"hosts": {"1": {"remark": "Mine", "priority": None, "enabled": True}, "3": {"enabled": False}}}),
        {},
        renamed,
        {},
        randomize_order=True,
    )
    assert shuffled == [[1, 2]]
    assert renamed.items == [("Shared 2", 2), ("Mine", 1)]

    ordered = CaptureConfig()
    await process_inbounds_and_tags(
        user_with({"hosts": {"1": {"priority": 2}, "2": {"priority": 1}, "3": {"priority": 0}}}),
        {},
        ordered,
        {},
        randomize_order=True,
    )
    assert shuffled == [[1, 2]]  # not shuffled again
    assert ordered.items == [("Shared 3", 3), ("Shared 2", 2), ("Shared 1", 1)]
