"""API and DB tests of traffic baskets (Pengu fork): separate volume per set of groups plus a shared volume."""

import asyncio

from fastapi import status
from sqlalchemy import select

from app.db.crud import pengu_baskets as crud
from app.db.models import Admin
from app.node.user import core_users
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    delete_admin,
    delete_core,
    delete_group,
    delete_user,
    get_inbounds,
    unique_name,
)

GB = 1024**3


def _run(coro):
    return asyncio.run(coro)


def _login(admin: dict) -> dict:
    token = client.post(
        "/api/admin/token",
        data={"username": admin["username"], "password": admin["password"], "grant_type": "password"},
    ).json()["access_token"]
    return auth_headers(token)


def _operator(access_token: str, data_limit: int) -> dict:
    operator = create_admin(access_token, role_id=3)
    r = client.put(
        f"/api/admin/{operator['username']}", headers=auth_headers(access_token), json={"data_limit": data_limit}
    )
    assert r.status_code == status.HTTP_200_OK, r.text
    return operator


async def _upstream_limit(username: str):
    async with TestSession() as db:
        return await db.scalar(select(Admin.data_limit).where(Admin.username == username))


async def _admin_id(username: str) -> int:
    async with TestSession() as db:
        return await db.scalar(select(Admin.id).where(Admin.username == username))


async def _charge(admin_id: int, per_basket: dict) -> set[int]:
    async with TestSession() as db:
        return await crud.charge_usage(db, {admin_id: per_basket})


async def _payload_tags(user_id: int) -> list[str]:
    async with TestSession() as db:
        users = await core_users(db, user_ids=[user_id])
    return sorted(users[0].inbounds) if users else []


class _World:
    """Two groups on disjoint inbounds (one stands for Xray, one for WireGuard) and their baskets."""

    def __init__(self, access_token: str):
        self.headers = auth_headers(access_token)
        self.core = create_core(access_token)
        tags = get_inbounds(access_token)
        assert len(tags) >= 2, "the test core must expose at least two inbounds"
        self.xray_tag, self.wg_tag = tags[0], tags[1]
        self.xray_group = create_group(access_token, name=unique_name("bk_xray"), inbound_tags=[self.xray_tag])
        self.wg_group = create_group(access_token, name=unique_name("bk_wg"), inbound_tags=[self.wg_tag])
        self.baskets = []

    def make_baskets(self):
        for name, group in (("Xray", self.xray_group), ("WireGuard", self.wg_group)):
            r = client.post(
                "/api/pengu/baskets",
                headers=self.headers,
                json={"name": unique_name(name), "group_ids": [group["id"]]},
            )
            assert r.status_code == status.HTTP_201_CREATED, r.text
        body = client.get("/api/pengu/baskets", headers=self.headers).json()
        by_group = {b["group_ids"][0]: b for b in body["baskets"] if b["group_ids"]}
        self.xray = by_group[self.xray_group["id"]]
        self.wg = by_group[self.wg_group["id"]]
        self.baskets = [self.xray, self.wg]

    def cleanup(self, access_token: str):
        for basket in self.baskets:
            client.delete(f"/api/pengu/baskets/{basket['id']}", headers=self.headers)
        delete_group(access_token, self.xray_group["id"])
        delete_group(access_token, self.wg_group["id"])
        delete_core(access_token, self.core["id"])


def test_only_owner_defines_baskets_and_a_group_sits_in_one_basket(access_token):
    world = _World(access_token)
    operator = create_admin(access_token, role_id=3)
    try:
        r = client.post(
            "/api/pengu/baskets", headers=_login(operator), json={"name": "x", "group_ids": [world.wg_group["id"]]}
        )
        assert r.status_code == status.HTTP_403_FORBIDDEN
        world.make_baskets()
        clash = client.post(
            "/api/pengu/baskets", headers=world.headers, json={"name": "dup", "group_ids": [world.wg_group["id"]]}
        )
        assert clash.status_code == status.HTTP_409_CONFLICT
        missing = client.post("/api/pengu/baskets", headers=world.headers, json={"name": "m", "group_ids": [999999]})
        assert missing.status_code == status.HTTP_409_CONFLICT
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_enabling_keeps_the_old_volume_as_shared_and_lifts_the_upstream_limit(access_token):
    world = _World(access_token)
    operator = _operator(access_token, 10 * GB)
    try:
        world.make_baskets()
        before = client.get(f"/api/admin/by-username/{operator['username']}/baskets", headers=world.headers).json()
        assert before["basket_mode"] is False
        r = client.post(f"/api/admin/by-username/{operator['username']}/baskets/enable", headers=world.headers)
        assert r.status_code == status.HTTP_200_OK, r.text
        body = r.json()
        assert body["basket_mode"] is True
        assert body["shared"]["data_limit"] == 10 * GB
        assert {b["basket_id"] for b in body["baskets"]} >= {world.xray["id"], world.wg["id"]}
        assert all(b["data_limit"] == 0 for b in body["baskets"])
        # idempotent
        again = client.post(f"/api/admin/by-username/{operator['username']}/baskets/enable", headers=world.headers)
        assert again.json()["shared"]["data_limit"] == 10 * GB

        assert _run(_upstream_limit(operator["username"])) is None  # upstream never limits a basket-mode admin

        overview = client.get("/api/pengu/baskets/admins", headers=world.headers)
        assert overview.status_code == status.HTTP_200_OK, overview.text
        listed = [a for a in overview.json()["admins"] if a["username"] == operator["username"]]
        assert listed and listed[0]["shared"]["data_limit"] == 10 * GB
        assert {b["basket_id"] for b in listed[0]["baskets"]} >= {world.xray["id"], world.wg["id"]}
        assert client.get("/api/pengu/baskets/admins", headers=_login(operator)).status_code == 403

        mine = client.get("/api/pengu/baskets/me", headers=_login(operator))
        assert mine.status_code == status.HTTP_200_OK
        assert mine.json()["shared"]["remaining"] == 10 * GB

        # the upstream admin API now sets the shared volume
        modify = client.put(f"/api/admin/{operator['username']}", headers=world.headers, json={"data_limit": 20 * GB})
        assert modify.status_code == status.HTTP_200_OK, modify.text
        assert modify.json()["data_limit"] is None
        shared = client.get(f"/api/admin/by-username/{operator['username']}/baskets", headers=world.headers).json()
        assert shared["shared"]["data_limit"] == 20 * GB
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_operator_cannot_change_own_volumes(access_token):
    world = _World(access_token)
    operator = _operator(access_token, GB)
    try:
        world.make_baskets()
        client.post(f"/api/admin/by-username/{operator['username']}/baskets/enable", headers=world.headers)
        r = client.post(
            f"/api/admin/by-username/{operator['username']}/baskets/{world.wg['id']}/add",
            headers=_login(operator),
            json={"bytes": 100 * GB},
        )
        assert r.status_code == status.HTTP_403_FORBIDDEN
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_used_up_basket_cuts_only_that_service_and_a_purchase_restores_it(access_token):
    world = _World(access_token)
    operator = _operator(access_token, 1 * GB)
    headers = world.headers
    user = None
    try:
        world.make_baskets()
        name = operator["username"]
        client.post(f"/api/admin/by-username/{name}/baskets/enable", headers=headers)
        r = client.put(
            f"/api/admin/by-username/{name}/baskets/{world.wg['id']}", headers=headers, json={"data_limit": 2 * GB}
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        r = client.put(
            f"/api/admin/by-username/{name}/baskets/{world.xray['id']}", headers=headers, json={"data_limit": 5 * GB}
        )
        assert r.status_code == status.HTTP_200_OK, r.text

        op_headers = _login(operator)
        created = client.post(
            "/api/user",
            headers=op_headers,
            json={
                "username": unique_name("bk_user"),
                "proxy_settings": {},
                "group_ids": [world.xray_group["id"], world.wg_group["id"]],
                "data_limit": 0,
                "status": "active",
            },
        )
        assert created.status_code == status.HTTP_201_CREATED, created.text
        user = created.json()
        assert _run(_payload_tags(user["id"])) == sorted([world.xray_tag, world.wg_tag])

        admin_id = _run(_admin_id(name))
        # 2 GB of WireGuard fill its own basket; 1.5 GB more spill into the 1 GB shared volume and empty it
        changed = _run(_charge(admin_id, {world.wg["id"]: 2 * GB}))
        assert changed == set()
        changed = _run(_charge(admin_id, {world.wg["id"]: GB + GB // 2}))
        assert changed == {admin_id}
        state = client.get(f"/api/admin/by-username/{name}/baskets", headers=headers).json()
        by_id = {b["basket_id"]: b for b in state["baskets"]}
        assert by_id[world.wg["id"]]["used_traffic"] == 2 * GB
        assert by_id[world.wg["id"]]["blocked"] is True
        assert by_id[world.xray["id"]]["blocked"] is False  # Xray still has its own 5 GB
        assert state["shared"]["used_traffic"] == GB + GB // 2
        assert state["shared"]["blocked"] is True

        # only WireGuard is cut for this admin's users
        assert _run(_payload_tags(user["id"])) == [world.xray_tag]

        # new users into the used-up basket are refused; the other basket still works
        refused = client.post(
            "/api/user",
            headers=op_headers,
            json={
                "username": unique_name("bk_new"),
                "proxy_settings": {},
                "group_ids": [world.wg_group["id"]],
                "status": "active",
            },
        )
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        allowed = client.post(
            "/api/user",
            headers=op_headers,
            json={
                "username": unique_name("bk_ok"),
                "proxy_settings": {},
                "group_ids": [world.xray_group["id"]],
                "status": "active",
            },
        )
        assert allowed.status_code == status.HTTP_201_CREATED, allowed.text
        delete_user(access_token, allowed.json()["username"])
        # editing the existing user (it already has the WireGuard group) still works
        edit = client.put(
            f"/api/user/{user['username']}",
            headers=op_headers,
            json={"note": "still editable", "group_ids": [world.xray_group["id"], world.wg_group["id"]]},
        )
        assert edit.status_code == status.HTTP_200_OK, edit.text

        # a purchase restores WireGuard
        r = client.post(
            f"/api/admin/by-username/{name}/baskets/{world.wg['id']}/add", headers=headers, json={"bytes": 3 * GB}
        )
        assert r.status_code == status.HTTP_200_OK, r.text
        by_id = {b["basket_id"]: b for b in r.json()["baskets"]}
        assert by_id[world.wg["id"]]["data_limit"] == 5 * GB
        assert by_id[world.wg["id"]]["blocked"] is False
        assert _run(_payload_tags(user["id"])) == sorted([world.xray_tag, world.wg_tag])

        # taking volume back never goes below what was used
        r = client.post(
            f"/api/admin/by-username/{name}/baskets/{world.wg['id']}/add", headers=headers, json={"bytes": -100 * GB}
        )
        by_id = {b["basket_id"]: b for b in r.json()["baskets"]}
        assert by_id[world.wg["id"]]["data_limit"] == by_id[world.wg["id"]]["used_traffic"] == 2 * GB
        assert by_id[world.wg["id"]]["blocked"] is True

        # the owner's usage reset starts every basket again
        reset = client.post(f"/api/admin/{name}/reset", headers=headers)
        assert reset.status_code == status.HTTP_200_OK, reset.text
        state = client.get(f"/api/admin/by-username/{name}/baskets", headers=headers).json()
        assert state["shared"]["used_traffic"] == 0
        assert all(b["used_traffic"] == 0 for b in state["baskets"])
        assert not any(b["blocked"] for b in state["baskets"])
        assert _run(_payload_tags(user["id"])) == sorted([world.xray_tag, world.wg_tag])
    finally:
        if user:
            delete_user(access_token, user["username"])
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_admins_without_baskets_are_untouched(access_token):
    world = _World(access_token)
    operator = _operator(access_token, GB)
    try:
        world.make_baskets()
        admin_id = _run(_admin_id(operator["username"]))
        assert _run(_charge(admin_id, {world.wg["id"]: 50 * GB})) == set()
        state = client.get(f"/api/admin/by-username/{operator['username']}/baskets", headers=world.headers).json()
        assert state["basket_mode"] is False
        r = client.post(
            f"/api/admin/by-username/{operator['username']}/baskets/{world.wg['id']}/add",
            headers=world.headers,
            json={"bytes": GB},
        )
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_shared_volume_add_and_set_and_leaving_basket_mode(access_token):
    """Review finding: shared/add answered 500 after committing (stale row). Also leaving basket mode."""
    world = _World(access_token)
    operator = _operator(access_token, 4 * GB)
    try:
        world.make_baskets()
        name = operator["username"]
        base = f"/api/admin/by-username/{name}/baskets"
        client.post(f"{base}/enable", headers=world.headers)
        admin_id = _run(_admin_id(name))
        _run(_charge(admin_id, {None: 5 * GB}))  # more than the shared 4 GB: everything outside baskets is cut
        state = client.get(base, headers=world.headers).json()
        assert state["shared"]["blocked"] is True

        r = client.post(f"{base}/shared/add", headers=world.headers, json={"bytes": 3 * GB})
        assert r.status_code == status.HTTP_200_OK, r.text
        assert r.json()["shared"]["data_limit"] == 7 * GB
        assert r.json()["shared"]["blocked"] is False
        r = client.put(f"{base}/shared", headers=world.headers, json={"data_limit": None})
        assert r.status_code == status.HTTP_200_OK, r.text
        assert r.json()["shared"]["data_limit"] is None
        r = client.put(f"{base}/shared", headers=world.headers, json={"data_limit": 6 * GB})
        assert r.json()["shared"]["data_limit"] == 6 * GB

        client.put(f"{base}/{world.wg['id']}", headers=world.headers, json={"data_limit": 2 * GB})
        # leaving basket mode: one limit with the same remaining volume (1 GB shared + 2 GB WireGuard)
        r = client.delete(base, headers=world.headers)
        assert r.status_code == status.HTTP_200_OK, r.text
        assert r.json()["basket_mode"] is False
        limit = _run(_upstream_limit(name))
        used = client.get("/api/admins", params={"usernames": [name]}, headers=world.headers).json()["admins"][0][
            "used_traffic"
        ]
        assert limit == used + 3 * GB
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_admin_cannot_lift_own_shared_volume_through_the_admin_api(access_token):
    """Review finding: data_limit 0 (= unlimited) slipped past the upstream self-check in basket mode."""
    world = _World(access_token)
    role = client.get("/api/admin-roles", headers=world.headers)
    operator = _operator(access_token, GB)
    try:
        world.make_baskets()
        client.post(f"/api/admin/by-username/{operator['username']}/baskets/enable", headers=world.headers)
        headers = _login(operator)
        for value in (0, 100 * GB):
            r = client.put(f"/api/admin/{operator['username']}", headers=headers, json={"data_limit": value})
            assert r.status_code in (status.HTTP_403_FORBIDDEN,), (value, r.status_code, r.text)
        state = client.get(f"/api/admin/by-username/{operator['username']}/baskets", headers=world.headers).json()
        assert state["shared"]["data_limit"] == GB
        assert role.status_code in (200, 403)
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_bulk_usage_reset_also_resets_baskets(access_token):
    world = _World(access_token)
    operator = _operator(access_token, GB)
    try:
        world.make_baskets()
        name = operator["username"]
        client.post(f"/api/admin/by-username/{name}/baskets/enable", headers=world.headers)
        admin_id = _run(_admin_id(name))
        _run(_charge(admin_id, {world.wg["id"]: 2 * GB}))
        r = client.post("/api/admins/bulk/reset", headers=world.headers, json={"ids": [admin_id]})
        assert r.status_code == status.HTTP_200_OK, r.text
        state = client.get(f"/api/admin/by-username/{name}/baskets", headers=world.headers).json()
        assert state["shared"]["used_traffic"] == 0
        assert state["shared"]["blocked"] is False
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_usage_of_a_node_with_several_baskets_is_never_free(access_token):
    """Review finding: such usage went only to the shared volume, so with shared empty it was unlimited."""
    world = _World(access_token)
    operator = _operator(access_token, GB)
    try:
        world.make_baskets()
        name = operator["username"]
        base = f"/api/admin/by-username/{name}/baskets"
        client.post(f"{base}/enable", headers=world.headers)
        client.put(f"{base}/{world.xray['id']}", headers=world.headers, json={"data_limit": 2 * GB})
        client.put(f"{base}/{world.wg['id']}", headers=world.headers, json={"data_limit": 2 * GB})
        admin_id = _run(_admin_id(name))
        key = tuple(sorted([world.xray["id"], world.wg["id"]]))
        _run(_charge(admin_id, {key: 6 * GB}))
        state = client.get(base, headers=world.headers).json()
        by_id = {b["basket_id"]: b for b in state["baskets"]}
        assert by_id[world.xray["id"]]["used_traffic"] == 2 * GB
        assert by_id[world.wg["id"]]["used_traffic"] == 2 * GB
        assert state["shared"]["used_traffic"] == 2 * GB
        assert by_id[world.xray["id"]]["blocked"] and by_id[world.wg["id"]]["blocked"]
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)


def test_reseller_does_not_see_groups_of_a_used_up_basket(access_token):
    """A reseller without any volume for a basket (own and shared both 0) does not get its groups in the
    group lists; buying volume brings them back. The owner always sees every group."""
    world = _World(access_token)
    operator = _operator(access_token, 4 * GB)
    try:
        world.make_baskets()
        name = operator["username"]
        base = f"/api/admin/by-username/{name}/baskets"
        client.post(f"{base}/enable", headers=world.headers)
        client.put(f"{base}/{world.xray['id']}", headers=world.headers, json={"data_limit": 2 * GB})
        r = client.put(f"{base}/shared", headers=world.headers, json={"data_limit": 0})  # «Xray only»
        assert r.status_code == status.HTTP_200_OK, r.text
        assert r.json()["shared"]["data_limit"] == 0  # a real zero, not unlimited

        reseller = _login(operator)

        def simple_ids(headers):
            r = client.get("/api/groups/simple", params={"all": True}, headers=headers)
            assert r.status_code == status.HTTP_200_OK, r.text
            body = r.json()
            ids = {g["id"] for g in body["groups"]}
            assert body["total"] >= len(ids)
            return ids

        seen = simple_ids(reseller)
        assert world.xray_group["id"] in seen
        assert world.wg_group["id"] not in seen
        both = simple_ids(world.headers)
        assert {world.xray_group["id"], world.wg_group["id"]} <= both

        full = client.get("/api/groups", headers=reseller)
        if full.status_code == status.HTTP_200_OK:  # only when the role may read full groups
            assert world.wg_group["id"] not in {g["id"] for g in full.json()["groups"]}

        # buying WireGuard volume shows the group again
        client.put(f"{base}/{world.wg['id']}", headers=world.headers, json={"data_limit": GB})
        assert world.wg_group["id"] in simple_ids(reseller)
    finally:
        delete_admin(access_token, operator["username"])
        world.cleanup(access_token)
