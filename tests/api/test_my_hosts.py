"""API tests of "My Hosts" (Pengu fork): operators personalize host names, order and visibility."""

import pytest
from fastapi import status
from pydantic import ValidationError

from app.routers.host import HostPreferenceUpdate
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    delete_admin,
    delete_core,
    get_inbounds,
    unique_name,
)


def test_host_preferences_are_operator_only(access_token):
    """The personalization endpoints must not grant host editing to other roles."""
    assert (
        client.get("/api/hosts/preferences", headers=auth_headers(access_token)).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.put("/api/hosts/preferences", headers=auth_headers(access_token), json=[]).status_code
        == status.HTTP_403_FORBIDDEN
    )


def test_operator_can_save_private_host_preferences(access_token):
    """Operator preferences are account scoped and do not modify shared host records."""
    operator = create_admin(access_token, role_id=3)
    operator_login = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    )
    operator_headers = auth_headers(operator_login.json()["access_token"])
    try:
        baseline = client.get("/api/hosts", headers=auth_headers(access_token)).json()
        preferences_response = client.get("/api/hosts/preferences", headers=operator_headers)
        assert preferences_response.status_code == status.HTTP_200_OK
        preferences = preferences_response.json()
        if preferences:
            invalid_save = client.put(
                "/api/hosts/preferences",
                headers=operator_headers,
                json=[{"id": preferences[0]["id"], "remark": "   ", "priority": preferences[0]["priority"]}]
                + [{"id": host["id"], "remark": None, "priority": host["priority"]} for host in preferences[1:]],
            )
            assert invalid_save.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
            preferences[0]["remark"] = unique_name("private_host_name")
            preferences[0]["is_customized"] = True
            preferences[0]["enabled"] = False
        saved = client.put(
            "/api/hosts/preferences",
            headers=operator_headers,
            json=[
                {
                    "id": host["id"],
                    "remark": host["remark"] if host["is_customized"] else None,
                    "priority": index,
                    "enabled": host.get("enabled", True),
                }
                for index, host in enumerate(preferences)
            ],
        )
        assert saved.status_code == status.HTTP_200_OK
        if preferences:
            saved_hosts = {host["id"]: host for host in saved.json()}
            disabled_host = saved_hosts[preferences[0]["id"]]
            assert disabled_host["enabled"] is False
            locked_save = client.put(
                "/api/hosts/preferences",
                headers=operator_headers,
                json=[
                    {
                        "id": host["id"],
                        "remark": "attempted change while disabled" if host["id"] == disabled_host["id"] else None,
                        "priority": 10000 + index,
                        "enabled": host["id"] != disabled_host["id"],
                    }
                    for index, host in enumerate(saved_hosts.values())
                ],
            )
            assert locked_save.status_code == status.HTTP_200_OK
            locked_host = next(host for host in locked_save.json() if host["id"] == disabled_host["id"])
            assert locked_host["remark"] == disabled_host["remark"]
            assert locked_host["priority"] == disabled_host["priority"]
            globally_disabled = client.post(
                "/api/hosts/bulk/disable",
                headers=auth_headers(access_token),
                json={"ids": [disabled_host["id"]]},
            )
            assert globally_disabled.status_code == status.HTTP_200_OK
            hidden_preferences = client.get("/api/hosts/preferences", headers=operator_headers)
            assert hidden_preferences.status_code == status.HTTP_200_OK
            assert all(host["id"] != disabled_host["id"] for host in hidden_preferences.json())
            globally_enabled = client.post(
                "/api/hosts/bulk/enable",
                headers=auth_headers(access_token),
                json={"ids": [disabled_host["id"]]},
            )
            assert globally_enabled.status_code == status.HTTP_200_OK
            restored_preferences = client.get("/api/hosts/preferences", headers=operator_headers)
            restored_host = next(host for host in restored_preferences.json() if host["id"] == disabled_host["id"])
            assert restored_host["remark"] == disabled_host["remark"]
        assert client.get("/api/hosts", headers=auth_headers(access_token)).json() == baseline
    finally:
        delete_admin(access_token, operator["username"])


def test_operator_order_follows_the_owner_until_changed(access_token):
    """Saving without reordering stores no positions, so the owner's later reordering still applies;
    a reset clears everything, and a stale host list is refused with 409."""
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    operator = create_admin(access_token, role_id=3)
    login = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    )
    operator_headers = auth_headers(login.json()["access_token"])
    host_payload = {
        "remark": unique_name("order_host"),
        "address": ["127.0.0.1"],
        "port": 443,
        "inbound_tag": inbound,
        "priority": 5,
    }
    host_id = None
    try:
        created = client.post("/api/host", headers=auth_headers(access_token), json=host_payload)
        assert created.status_code == status.HTTP_201_CREATED, created.text
        host_id = created.json()["id"]
        preferences = client.get("/api/hosts/preferences", headers=operator_headers).json()
        assert any(item["id"] == host_id for item in preferences)

        def body(remark_for_host):
            return [
                {
                    "id": item["id"],
                    "remark": remark_for_host if item["id"] == host_id else None,
                    "priority": None,
                    "enabled": True,
                }
                for item in preferences
            ]

        saved = client.put("/api/hosts/preferences", headers=operator_headers, json=body("Renamed"))
        assert saved.status_code == status.HTTP_200_OK, saved.text
        mine = next(item for item in saved.json() if item["id"] == host_id)
        assert (mine["remark"], mine["priority"], mine["default_priority"]) == ("Renamed", 5, 5)

        moved = client.put(
            f"/api/host/{host_id}", headers=auth_headers(access_token), json={**host_payload, "priority": 7}
        )
        assert moved.status_code == status.HTTP_200_OK, moved.text
        after = client.get("/api/hosts/preferences", headers=operator_headers).json()
        mine = next(item for item in after if item["id"] == host_id)
        assert (mine["remark"], mine["priority"]) == ("Renamed", 7)

        reset = client.put("/api/hosts/preferences", headers=operator_headers, json=body(None))
        assert reset.status_code == status.HTTP_200_OK, reset.text
        mine = next(item for item in reset.json() if item["id"] == host_id)
        assert (mine["remark"], mine["is_customized"]) == (host_payload["remark"], False)

        stale = client.put(
            "/api/hosts/preferences",
            headers=operator_headers,
            json=[item for item in body(None) if item["id"] != host_id],
        )
        assert stale.status_code == status.HTTP_409_CONFLICT, stale.text
    finally:
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_admin(access_token, operator["username"])
        delete_core(access_token, core["id"])


def test_host_preference_remark_follows_host_format_rules():
    """Custom remarks reach subscription rendering, so they follow the same rules as shared host remarks."""
    for remark in ("{USERNAME.__class__}", "{USERNAME:>1000000}", "{"):
        with pytest.raises(ValidationError):
            HostPreferenceUpdate(id=1, remark=remark, priority=0)
    assert HostPreferenceUpdate(id=1, remark=" 🇩🇪 {USERNAME} ", priority=0).remark == "🇩🇪 {USERNAME}"
