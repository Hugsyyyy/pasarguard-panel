"""API tests of "My Hosts" (Pengu fork): operators personalize host names, order and visibility."""

import asyncio
import base64
import json
import urllib.parse

import pytest
from fastapi import status
from pydantic import ValidationError
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import hash_password
from app.routers.host_preferences import HostPreferenceUpdate
from app.subscription import config_cache
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    delete_admin,
    delete_core,
    delete_group,
    delete_user,
    get_inbounds,
    strong_password,
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


def _operator(access_token):
    operator = create_admin(access_token, role_id=3)
    login = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    )
    return operator, auth_headers(login.json()["access_token"])


def _create_host(access_token, inbound, priority):
    payload = {
        "remark": unique_name("my_hosts"),
        "address": ["127.0.0.1"],
        "port": 443,
        "inbound_tag": inbound,
        "priority": priority,
    }
    response = client.post("/api/host", headers=auth_headers(access_token), json=payload)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return payload, response.json()["id"]


def _shared_order(rows):
    return [row["id"] for row in sorted(rows, key=lambda row: (row["default_priority"], row["id"]))]


def _body(rows, remarks=None, hidden=(), positions=None):
    return [
        {
            "id": row["id"],
            "remark": (remarks or {}).get(row["id"]),
            "priority": None if positions is None else positions.index(row["id"]),
            "enabled": row["id"] not in hidden,
        }
        for row in rows
    ]


def test_operator_order_follows_the_owner_until_changed(access_token):
    """Saving without reordering stores no positions, so the owner's later reordering still applies;
    a name equal to the shared one is not a customization, and a stale host list is refused with 409."""
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    operator, operator_headers = _operator(access_token)
    host_id = None
    try:
        payload, host_id = _create_host(access_token, inbound, priority=5)
        rows = client.get("/api/hosts/preferences", headers=operator_headers).json()
        assert any(row["id"] == host_id for row in rows)
        assert [row["id"] for row in rows] == _shared_order(rows)

        saved = client.put("/api/hosts/preferences", headers=operator_headers, json=_body(rows, {host_id: "Renamed"}))
        assert saved.status_code == status.HTTP_200_OK, saved.text
        mine = next(row for row in saved.json() if row["id"] == host_id)
        assert (mine["remark"], mine["is_customized"], mine["default_priority"]) == ("Renamed", True, 5)
        assert [row["id"] for row in saved.json()] == _shared_order(saved.json())

        moved = client.put(f"/api/host/{host_id}", headers=auth_headers(access_token), json={**payload, "priority": 7})
        assert moved.status_code == status.HTTP_200_OK, moved.text
        after = client.get("/api/hosts/preferences", headers=operator_headers).json()
        mine = next(row for row in after if row["id"] == host_id)
        assert (mine["remark"], mine["default_priority"]) == ("Renamed", 7)
        assert [row["id"] for row in after] == _shared_order(after)

        same = client.put(
            "/api/hosts/preferences", headers=operator_headers, json=_body(after, {host_id: f" {payload['remark']} "})
        )
        assert same.status_code == status.HTTP_200_OK, same.text
        mine = next(row for row in same.json() if row["id"] == host_id)
        assert (mine["remark"], mine["is_customized"]) == (payload["remark"], False)

        stale = client.put(
            "/api/hosts/preferences", headers=operator_headers, json=[b for b in _body(after) if b["id"] != host_id]
        )
        assert stale.status_code == status.HTTP_409_CONFLICT, stale.text
    finally:
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_admin(access_token, operator["username"])
        delete_core(access_token, core["id"])


def test_custom_order_resets_and_owner_view(access_token):
    """A custom order, "reset order" with a hidden host, the full reset, and the owner's view and reset."""
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    operator, operator_headers = _operator(access_token)
    created = []
    try:
        for priority in (5, 6):
            created.append(_create_host(access_token, inbound, priority)[1])
        rows = client.get("/api/hosts/preferences", headers=operator_headers).json()
        reversed_ids = [row["id"] for row in rows][::-1]
        hidden = created[0]

        custom = client.put(
            "/api/hosts/preferences",
            headers=operator_headers,
            json=_body(rows, {created[1]: "Second"}, hidden={hidden}, positions=reversed_ids),
        )
        assert custom.status_code == status.HTTP_200_OK, custom.text
        assert [row["id"] for row in custom.json()] == reversed_ids
        assert next(row for row in custom.json() if row["id"] == hidden)["enabled"] is False

        owner_view = client.get(
            f"/api/admin/by-username/{operator['username']}/host-preferences", headers=auth_headers(access_token)
        )
        assert owner_view.status_code == status.HTTP_200_OK, owner_view.text
        changed = {row["id"]: row for row in owner_view.json()["hosts"]}
        assert changed[created[1]]["remark"] == "Second" and changed[hidden]["enabled"] is False
        forbidden = client.get(
            f"/api/admin/by-username/{operator['username']}/host-preferences", headers=operator_headers
        )
        assert forbidden.status_code == status.HTTP_403_FORBIDDEN

        # "reset order" while a host stays hidden: back to the shared order, nothing left of the positions
        reset_order = client.put(
            "/api/hosts/preferences",
            headers=operator_headers,
            json=_body(custom.json(), {created[1]: "Second"}, hidden={hidden}),
        )
        assert reset_order.status_code == status.HTTP_200_OK, reset_order.text
        assert [row["id"] for row in reset_order.json()] == _shared_order(reset_order.json())
        assert next(row for row in reset_order.json() if row["id"] == hidden)["enabled"] is False

        # reset all (also what the page no longer shows)
        assert client.delete("/api/hosts/preferences", headers=operator_headers).status_code == 204
        cleared = client.get("/api/hosts/preferences", headers=operator_headers).json()
        assert not any(row["is_customized"] or not row["enabled"] for row in cleared)
        assert [row["id"] for row in cleared] == _shared_order(cleared)

        # the owner can reset an admin too
        client.put("/api/hosts/preferences", headers=operator_headers, json=_body(cleared, hidden={hidden}))
        owner_reset = client.delete(
            f"/api/admin/by-username/{operator['username']}/host-preferences", headers=auth_headers(access_token)
        )
        assert owner_reset.status_code == status.HTTP_204_NO_CONTENT
        assert all(row["enabled"] for row in client.get("/api/hosts/preferences", headers=operator_headers).json())
    finally:
        for host_id in created:
            client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_admin(access_token, operator["username"])
        delete_core(access_token, core["id"])


def _create_owner_admin() -> dict:
    """A real owner account (env admins cannot create API keys), as upstream's API key tests do."""
    username = unique_name("my_hosts_owner")
    password = strong_password("MyHostsOwner")

    async def _create() -> None:
        async with TestSession() as session:
            session.add(Admin(username=username, hashed_password=await hash_password(password), role_id=1))
            await session.commit()

    asyncio.run(_create())
    return {"username": username, "password": password}


def _delete_admin_row(username: str) -> None:
    """Owners cannot be deleted through the API; remove the test owner (and its keys) directly."""

    async def _delete() -> None:
        async with TestSession() as session:
            admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if admin is not None:
                await session.delete(admin)
                await session.commit()

    asyncio.run(_delete())


def test_api_keys_with_their_own_permissions_cannot_use_my_hosts(access_token):
    """A narrow key (here the owner's) must never be able to hide hosts for anyone."""
    owner = _create_owner_admin()
    login = client.post(
        "/api/admin/token",
        data={"username": owner["username"], "password": owner["password"], "grant_type": "password"},
    )
    owner_headers = auth_headers(login.json()["access_token"])
    try:
        response = client.post(
            "/api/api_key",
            headers=owner_headers,
            json={
                "name": unique_name("my_hosts_key"),
                "inherit_permissions": False,
                "permissions": {"users": {"read": {"scope": 2}}},
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        key = response.json()
        headers = {"X-Api-Key": key["api_key"]}
        assert client.get("/api/hosts/preferences", headers=headers).status_code == status.HTTP_403_FORBIDDEN
        assert client.put("/api/hosts/preferences", headers=headers, json=[]).status_code == status.HTTP_403_FORBIDDEN
        assert client.delete("/api/hosts/preferences", headers=headers).status_code == status.HTTP_403_FORBIDDEN
        # the owner's own login is refused too: My Hosts is for admins who cannot edit hosts
        assert client.get("/api/hosts/preferences", headers=owner_headers).status_code == status.HTTP_403_FORBIDDEN
        client.delete(f"/api/api_key/{key['id']}", headers=owner_headers)
    finally:
        _delete_admin_row(owner["username"])


def test_host_preference_remark_follows_host_format_rules():
    """Custom remarks reach subscription rendering, so they follow the same rules as shared host remarks."""
    for remark in ("{USERNAME.__class__}", "{USERNAME:>1000000}", "{"):
        with pytest.raises(ValidationError):
            HostPreferenceUpdate(id=1, remark=remark, priority=0)
    assert HostPreferenceUpdate(id=1, remark=" 🇩🇪 {USERNAME} ", priority=0).remark == "🇩🇪 {USERNAME}"


def _link_remarks(subscription_url: str) -> list[str]:
    """The server names a user's links subscription carries."""
    config_cache.clear_sub_config_cache()
    response = client.get(f"{subscription_url}/links")
    assert response.status_code == status.HTTP_200_OK, response.text
    remarks = []
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("vmess://"):
            raw = line[len("vmess://") :]
            remarks.append(json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4))).get("ps", ""))
        elif "://" in line:
            remarks.append(urllib.parse.unquote(urllib.parse.urlsplit(line).fragment))
    return remarks


def test_role_feature_switches_my_hosts_off_and_back_on(access_token):
    """The role's "My Hosts" feature: off refuses the API and stops applying the saved changes to the
    users' subscriptions (the owner still sees them); on again brings them back unchanged."""
    owner_headers = auth_headers(access_token)
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    operator_role = client.get("/api/admin-role/3", headers=owner_headers).json()
    response = client.post(
        "/api/admin-role",
        headers=owner_headers,
        json={"name": unique_name("my_hosts_role"), "permissions": operator_role["permissions"]},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    role = response.json()
    assert role["features"]["can_use_my_hosts"] is True

    def switch(on: bool) -> None:
        features = {**role["features"], "can_use_my_hosts": on}
        changed = client.put(f"/api/admin-role/{role['id']}", headers=owner_headers, json={"features": features})
        assert changed.status_code == status.HTTP_200_OK, changed.text
        assert changed.json()["features"]["can_use_my_hosts"] is on

    operator = create_admin(access_token, role_id=role["id"])
    login = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    )
    operator_headers = auth_headers(login.json()["access_token"])
    group = create_group(access_token, inbound_tags=[inbound])
    user = create_user(access_token, group_ids=[group["id"]])
    host_payload, host_id = _create_host(access_token, inbound, 7)
    renamed = unique_name("renamed_by_operator")
    try:
        moved = client.put(
            f"/api/user/{user['username']}/set_owner",
            params={"admin_username": operator["username"]},
            headers=owner_headers,
        )
        assert moved.status_code == status.HTTP_200_OK, moved.text
        rows = client.get("/api/hosts/preferences", headers=operator_headers).json()
        saved = client.put("/api/hosts/preferences", headers=operator_headers, json=_body(rows, {host_id: renamed}))
        assert saved.status_code == status.HTTP_200_OK, saved.text
        remarks = _link_remarks(user["subscription_url"])
        assert renamed in remarks and host_payload["remark"] not in remarks

        switch(False)
        for method in ("get", "put", "delete"):
            kwargs = {"json": []} if method == "put" else {}
            refused = getattr(client, method)("/api/hosts/preferences", headers=operator_headers, **kwargs)
            assert refused.status_code == status.HTTP_403_FORBIDDEN, (method, refused.text)
            assert refused.json()["detail"] == "My Hosts is switched off for your role"
        remarks = _link_remarks(user["subscription_url"])
        assert host_payload["remark"] in remarks and renamed not in remarks
        owner_view = client.get(
            f"/api/admin/by-username/{operator['username']}/host-preferences", headers=owner_headers
        )
        assert owner_view.status_code == status.HTTP_200_OK, owner_view.text
        assert any(row["remark"] == renamed for row in owner_view.json()["hosts"])

        switch(True)
        assert client.get("/api/hosts/preferences", headers=operator_headers).status_code == status.HTTP_200_OK
        remarks = _link_remarks(user["subscription_url"])
        assert renamed in remarks and host_payload["remark"] not in remarks
    finally:
        delete_user(access_token, user["username"])
        client.delete(f"/api/host/{host_id}", headers=owner_headers)
        delete_group(access_token, group["id"])
        delete_admin(access_token, operator["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=owner_headers)
        delete_core(access_token, core["id"])
