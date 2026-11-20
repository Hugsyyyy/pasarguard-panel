"""API tests of "Subscription pages" (Pengu fork): every admin chooses the page their own users see."""

import asyncio
import dataclasses
import hashlib

import pytest
from fastapi import status
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import select

from app.core import sub_pages
from app.db.models import Admin, AdminStatus
from app.models.admin import hash_password
from app.templates import env
from config import template_settings
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

BROWSER = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    """A custom templates directory that the page loader reads first, as CUSTOM_TEMPLATES_DIRECTORY sets up."""
    monkeypatch.setattr(template_settings, "custom_templates_directory", str(tmp_path))
    monkeypatch.setattr(env, "loader", ChoiceLoader([FileSystemLoader(str(tmp_path)), env.loader]))
    return tmp_path


def _add_page(directory, slug: str, body: str) -> None:
    folder = directory / sub_pages.PAGES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(body, encoding="utf-8")


def _login(account: dict) -> dict[str, str]:
    response = client.post(
        "/api/admin/token",
        data={"username": account["username"], "password": account["password"], "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return auth_headers(response.json()["access_token"])


def _operator(access_token, role_id: int = 3):
    operator = create_admin(access_token, role_id=role_id)
    return operator, _login(operator)


@pytest.fixture
def owner():
    """A real owner account: the env admin of the tests has no row to keep a choice in."""
    account = {"username": unique_name("sub_pages_owner"), "password": strong_password("SubPagesOwner")}

    async def _create() -> None:
        async with TestSession() as session:
            hashed = await hash_password(account["password"])
            session.add(Admin(username=account["username"], hashed_password=hashed, role_id=1))
            await session.commit()

    async def _delete() -> None:
        async with TestSession() as session:
            row = (await session.execute(select(Admin).where(Admin.username == account["username"]))).scalar_one()
            await session.delete(row)
            await session.commit()

    asyncio.run(_create())
    try:
        yield {**account, "headers": _login(account)}
    finally:
        asyncio.run(_delete())


@pytest.fixture
def served_user(access_token):
    """(operator, operator headers, a user of that operator) and the things a user needs to exist."""
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    group = create_group(access_token, inbound_tags=[inbound])
    operator, headers = _operator(access_token)
    user = create_user(access_token, group_ids=[group["id"]])
    moved = client.put(
        f"/api/user/{user['username']}/set_owner",
        params={"admin_username": operator["username"]},
        headers=auth_headers(access_token),
    )
    assert moved.status_code == status.HTTP_200_OK, moved.text
    try:
        yield operator, headers, user
    finally:
        delete_user(access_token, user["username"])
        delete_admin(access_token, operator["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _page_of(user: dict) -> str:
    response = client.get(user["subscription_url"], headers=BROWSER)
    assert response.status_code == status.HTTP_200_OK, response.text[:300]
    return response.text


def test_an_admin_sees_only_what_can_be_chosen(access_token, gallery):
    operator, headers = _operator(access_token)
    _add_page(gallery, "mine", "<html></html>")
    try:
        response = client.get("/api/sub-pages", headers=headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["can_manage"] is False and body["install_blocker"] is None
        assert body["selected"] == "default" and body["selected_missing"] is False
        assert body["preview_base"].startswith("/api/sub-pages/preview/")
        assert [(page["key"], page["kind"], page["installed"]) for page in body["pages"]] == [
            ("default", "default", True),
            ("mine", "custom", True),
        ]
        # the owner's details stay with the owner
        assert all(
            page["template"] is None and page["admins"] is None and page["loads"] == {} for page in body["pages"]
        )
        for method, url in (("post", "/api/sub-pages/pasarguard/install"), ("delete", "/api/sub-pages/pasarguard")):
            refused = getattr(client, method)(url, headers=headers)
            assert refused.status_code == status.HTTP_403_FORBIDDEN, (url, refused.text)
    finally:
        delete_admin(access_token, operator["username"])


def test_requests_without_a_login_are_refused():
    assert client.get("/api/sub-pages").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.put("/api/sub-pages/selection", json={"key": "default"}).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.post("/api/sub-pages/pasarguard/install").status_code == status.HTTP_401_UNAUTHORIZED
    assert client.delete("/api/sub-pages/pasarguard").status_code == status.HTTP_401_UNAUTHORIZED


def test_chosen_page_reaches_only_that_admins_users(access_token, gallery, served_user):
    _, headers, user = served_user
    _add_page(gallery, "mine", "<html><head></head><body>MINE {{ user.username }} {{ links | length }}</body></html>")
    other_user = create_user(access_token, group_ids=user["group_ids"])
    try:
        assert "MINE" not in _page_of(user)

        chosen = client.put("/api/sub-pages/selection", headers=headers, json={"key": "mine"})
        assert chosen.status_code == status.HTTP_200_OK, chosen.text
        assert chosen.json()["selected"] == "mine"
        assert f"MINE {user['username']}" in _page_of(user)
        assert "MINE" not in _page_of(other_user)
        # apps still get their configs: only the browser page changed
        assert client.get(f"{user['subscription_url']}/links").status_code == status.HTTP_200_OK

        for key in ("nope", "other", "preview", "pasarguard", "../mine"):
            refused = client.put("/api/sub-pages/selection", headers=headers, json={"key": key})
            assert refused.status_code == status.HTTP_404_NOT_FOUND, (key, refused.text)
        assert client.get("/api/sub-pages", headers=headers).json()["selected"] == "mine"

        back = client.put("/api/sub-pages/selection", headers=headers, json={"key": "default"})
        assert back.status_code == status.HTTP_200_OK and back.json()["selected"] == "default"
        assert "MINE" not in _page_of(user)
    finally:
        delete_user(access_token, other_user["username"])


def test_a_page_whose_file_is_gone_falls_back_to_the_default(gallery, served_user):
    _, headers, user = served_user
    _add_page(gallery, "mine", "<html>MINE</html>")
    assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "mine"}).status_code == 200
    assert "MINE" in _page_of(user)

    (gallery / sub_pages.PAGES_DIR / "mine" / "index.html").unlink()
    assert "MINE" not in _page_of(user)
    body = client.get("/api/sub-pages", headers=headers).json()
    assert body["selected"] == "mine" and body["selected_missing"] is True
    assert [page["key"] for page in body["pages"]] == ["default"]


def test_a_path_typed_by_the_owner_is_shown_as_another_page(access_token, gallery, served_user):
    operator, headers, user = served_user
    target = gallery / "special" / "page.html"
    target.parent.mkdir()
    target.write_text("<html>SPECIAL</html>", encoding="utf-8")
    typed = client.put(
        f"/api/admin/{operator['username']}",
        headers=auth_headers(access_token),
        json={"sub_template": "special/page.html"},
    )
    assert typed.status_code == status.HTTP_200_OK, typed.text
    assert "SPECIAL" in _page_of(user)
    body = client.get("/api/sub-pages", headers=headers).json()
    assert body["selected"] == "other" and body["selected_template"] == "special/page.html"
    assert body["selected_missing"] is False


def test_role_feature_switches_choosing_off_and_keeps_the_page(access_token, gallery):
    owner_headers = auth_headers(access_token)
    core = create_core(access_token)
    inbound = get_inbounds(access_token)[0]
    operator_role = client.get("/api/admin-role/3", headers=owner_headers).json()
    response = client.post(
        "/api/admin-role",
        headers=owner_headers,
        json={"name": unique_name("sub_pages_role"), "permissions": operator_role["permissions"]},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    role = response.json()
    assert role["features"]["can_use_sub_pages"] is True

    def switch(on: bool) -> None:
        features = {**role["features"], "can_use_sub_pages": on}
        changed = client.put(f"/api/admin-role/{role['id']}", headers=owner_headers, json={"features": features})
        assert changed.status_code == status.HTTP_200_OK, changed.text
        assert changed.json()["features"]["can_use_sub_pages"] is on

    operator, headers = _operator(access_token, role_id=role["id"])
    group = create_group(access_token, inbound_tags=[inbound])
    user = create_user(access_token, group_ids=[group["id"]])
    _add_page(gallery, "mine", "<html>MINE</html>")
    try:
        moved = client.put(
            f"/api/user/{user['username']}/set_owner",
            params={"admin_username": operator["username"]},
            headers=owner_headers,
        )
        assert moved.status_code == status.HTTP_200_OK, moved.text
        assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "mine"}).status_code == 200

        switch(False)
        refused = client.get("/api/sub-pages", headers=headers)
        assert refused.status_code == status.HTTP_403_FORBIDDEN
        assert refused.json()["detail"] == "Subscription pages are switched off for your role"
        changed = client.put("/api/sub-pages/selection", headers=headers, json={"key": "default"})
        assert changed.status_code == status.HTTP_403_FORBIDDEN
        # the page the admin has stays in effect: the owner can give a page and lock it
        assert "MINE" in _page_of(user)

        switch(True)
        assert client.get("/api/sub-pages", headers=headers).json()["selected"] == "mine"
    finally:
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_admin(access_token, operator["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=owner_headers)
        delete_core(access_token, core["id"])


def test_api_keys_with_their_own_permissions_cannot_use_sub_pages(owner):
    response = client.post(
        "/api/api_key",
        headers=owner["headers"],
        json={
            "name": unique_name("sub_pages_key"),
            "inherit_permissions": False,
            "permissions": {"users": {"read": {"scope": 2}}},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    key = response.json()
    headers = {"X-Api-Key": key["api_key"]}
    try:
        assert client.get("/api/sub-pages", headers=headers).status_code == status.HTTP_403_FORBIDDEN
        refused = client.put("/api/sub-pages/selection", headers=headers, json={"key": "default"})
        assert refused.status_code == status.HTTP_403_FORBIDDEN
        assert (
            client.post("/api/sub-pages/pasarguard/install", headers=headers).status_code == status.HTTP_403_FORBIDDEN
        )
        assert client.delete("/api/sub-pages/pasarguard", headers=headers).status_code == status.HTTP_403_FORBIDDEN
    finally:
        client.delete(f"/api/api_key/{key['id']}", headers=owner["headers"])


def test_owner_needs_a_templates_directory_to_install(owner, monkeypatch):
    monkeypatch.setattr(template_settings, "custom_templates_directory", None)
    body = client.get("/api/sub-pages", headers=owner["headers"]).json()
    assert body["can_manage"] is True and body["install_blocker"] == "no_custom_directory"
    catalog = [page for page in body["pages"] if page["kind"] == "catalog"]
    assert [page["key"] for page in catalog] == [page.slug for page in sub_pages.CATALOG]
    assert all(page["installed"] is False and page["loads"].get("en") and page["homepage"] for page in catalog)
    refused = client.post(f"/api/sub-pages/{sub_pages.CATALOG[0].slug}/install", headers=owner["headers"])
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text


def _fake_catalog_page(monkeypatch, data: bytes, **changes) -> sub_pages.CatalogPage:
    page = dataclasses.replace(
        sub_pages.CATALOG[0], slug="fake-page", sha256=hashlib.sha256(data).hexdigest(), size=len(data), **changes
    )
    monkeypatch.setattr(sub_pages, "CATALOG", (page,))
    monkeypatch.setattr(sub_pages, "_CATALOG_BY_SLUG", {page.slug: page})
    return page


def test_owner_installs_and_removes_a_catalog_page(owner, gallery, monkeypatch, served_user):
    _, headers, user = served_user
    good = b"<html><head></head><body>INSTALLED {{ user.username }}</body></html>"
    _fake_catalog_page(monkeypatch, good)
    served = {"data": good + b"tampered"}

    async def download(_page):
        return served["data"]

    monkeypatch.setattr(sub_pages, "download", download)

    refused = client.post("/api/sub-pages/fake-page/install", headers=owner["headers"])
    assert refused.status_code == status.HTTP_502_BAD_GATEWAY, refused.text
    assert "not the reviewed version" in refused.json()["detail"]
    assert sub_pages.installed_slugs() == []
    assert client.post("/api/sub-pages/unknown-page/install", headers=owner["headers"]).status_code == 404

    served["data"] = good
    installed = client.post("/api/sub-pages/fake-page/install", headers=owner["headers"])
    assert installed.status_code == status.HTTP_200_OK, installed.text
    page = next(page for page in installed.json()["pages"] if page["key"] == "fake-page")
    assert page["installed"] is True and page["admins"] == 0
    assert page["template"] == "subscription/pengu-pages/fake-page/index.html"

    # now every admin can choose it
    assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "fake-page"}).status_code == 200
    assert f"INSTALLED {user['username']}" in _page_of(user)
    listed = client.get("/api/sub-pages", headers=owner["headers"]).json()
    assert next(page for page in listed["pages"] if page["key"] == "fake-page")["admins"] == 1

    removed = client.delete("/api/sub-pages/fake-page", headers=owner["headers"])
    assert removed.status_code == status.HTTP_200_OK, removed.text
    assert next(page for page in removed.json()["pages"] if page["key"] == "fake-page")["installed"] is False
    # the admin who had chosen it is back on the default page, in the database too
    assert client.get("/api/sub-pages", headers=headers).json()["selected"] == "default"
    assert "INSTALLED" not in _page_of(user)
    assert client.delete("/api/sub-pages/fake-page", headers=owner["headers"]).status_code == 404


def test_owner_cannot_remove_a_page_added_by_hand(owner, gallery):
    _add_page(gallery, "mine", "<html>MINE</html>")
    refused = client.delete("/api/sub-pages/mine", headers=owner["headers"])
    assert refused.status_code == status.HTTP_400_BAD_REQUEST, refused.text
    assert sub_pages.installed_slugs() == ["mine"]


def test_preview_is_a_sandboxed_sample(owner):
    base = client.get("/api/sub-pages", headers=owner["headers"]).json()["preview_base"]

    # no login on these requests: the ticket in the URL is what lets them in
    page = client.get(f"{base}/default", headers=BROWSER)
    assert page.status_code == status.HTTP_200_OK, page.text[:300]
    assert "demo-user" in page.text and "pengu-preview" in page.text
    policy = page.headers["content-security-policy"]
    assert policy.startswith("sandbox ") and "allow-scripts" in policy and "allow-same-origin" not in policy
    assert page.headers["cache-control"] == "no-store"
    assert client.get(f"{base}/default/", headers=BROWSER).status_code == status.HTTP_200_OK

    info = client.get(f"{base}/default/info")
    assert info.status_code == status.HTTP_200_OK and info.json()["username"] == "demo-user"
    assert "proxy_settings" not in info.json() and info.headers["access-control-allow-origin"] == "*"
    assert isinstance(client.get(f"{base}/default/apps").json(), list)
    usage = client.get(f"{base}/default/usage", params={"period": "day"}).json()
    assert usage["period"] == "day" and len(usage["stats"]["-1"]) >= 1
    assert client.get(f"{base}/default/links").text.startswith("vless://")
    raw = client.get(f"{base}/default/raw").json()
    assert raw["body"]["user"]["username"] == "demo-user" and len(raw["body"]["links"]) == 4

    assert client.get(f"{base}/not-installed", headers=BROWSER).status_code == status.HTTP_404_NOT_FOUND
    assert client.get(f"{base}/default/clash").status_code == status.HTTP_404_NOT_FOUND
    for bad in ("/api/sub-pages/preview/0.x/default", f"{base}x/default", "/api/sub-pages/preview/zz/default/info"):
        assert client.get(bad, headers=BROWSER).status_code == status.HTTP_403_FORBIDDEN, bad


def test_preview_of_a_marzban_page(owner, gallery, monkeypatch):
    body = b"<html><head></head>{{ ((user.expire - now().timestamp()) // 86400) | int }}|{{ user.links | length }}|{{ user.subscription_url }}</html>"
    _fake_catalog_page(monkeypatch, body, marzban="path")

    async def download(_page):
        return body

    monkeypatch.setattr(sub_pages, "download", download)
    installed = client.post("/api/sub-pages/fake-page/install", headers=owner["headers"])
    assert installed.status_code == status.HTTP_200_OK, installed.text
    base = installed.json()["preview_base"]
    page = client.get(f"{base}/fake-page", headers=BROWSER)
    assert page.status_code == status.HTTP_200_OK, page.text[:300]
    assert f"23|4|{base}/fake-page</html>" in page.text


def _set_admin_status(username: str, value: AdminStatus) -> None:
    async def _set() -> None:
        async with TestSession() as session:
            row = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one()
            row.status = value
            await session.commit()

    asyncio.run(_set())


def test_limited_admin_cannot_change_the_page(gallery, served_user):
    operator, headers, _ = served_user
    _add_page(gallery, "mine", "<html>MINE</html>")
    assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "mine"}).status_code == 200
    _set_admin_status(operator["username"], AdminStatus.limited)
    try:
        refused = client.put("/api/sub-pages/selection", headers=headers, json={"key": "default"})
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
    finally:
        _set_admin_status(operator["username"], AdminStatus.active)
    assert client.get("/api/sub-pages", headers=headers).json()["selected"] == "mine"


def test_page_that_fails_for_a_user_gives_that_user_the_default_page(owner, gallery, served_user):
    _, headers, user = served_user
    # compiles, and renders for the preview's user (who has an expiry), but not for a user without one
    _add_page(gallery, "trips", "<html><head></head>TRIPS {{ (user.expire - now()).days }}</html>")
    assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "trips"}).status_code == 200

    page = _page_of(user)
    assert "TRIPS" not in page and user["username"] in page

    base = client.get("/api/sub-pages", headers=owner["headers"]).json()["preview_base"]
    assert "TRIPS 23" in client.get(f"{base}/trips", headers=BROWSER).text
    _add_page(gallery, "broken", "<html>{{ user.nothing.here }}</html>")
    failed = client.get(f"{base}/broken", headers=BROWSER)
    assert failed.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert failed.json()["detail"] == "This page could not be rendered (UndefinedError)"


def test_marzban_page_for_a_real_user(owner, gallery, monkeypatch, served_user):
    _, headers, user = served_user
    body = (
        b"<html><head></head><script>const days = {{ ((user.expire - now().timestamp()) // 86400) | int "
        b'if user.expire else -1 }}; const subLinks = "{{ user.links }}"</script>'
        b"URL={{ user.subscription_url }} NAME={{ user.username }} STATUS={{ user.status.value }}</html>"
    )
    _fake_catalog_page(monkeypatch, body, marzban="path")

    async def download(_page):
        return body

    monkeypatch.setattr(sub_pages, "download", download)
    assert client.post("/api/sub-pages/fake-page/install", headers=owner["headers"]).status_code == 200
    assert client.put("/api/sub-pages/selection", headers=headers, json={"key": "fake-page"}).status_code == 200

    page = _page_of(user)
    assert "const days = -1;" in page  # this user has no expiry
    assert f"NAME={user['username']} STATUS=active" in page
    assert "URL=/" in page and "URL=http" not in page
    assert "&#39;" not in page and "&amp;" not in page


def test_ticket_with_odd_characters_is_refused_not_an_error(owner):
    client.get("/api/sub-pages", headers=owner["headers"])
    for ticket in ("ffffffffff.%C3%A9", "ffffffffff.%E2%80%8F", "%C3%A9.%C3%A9"):
        response = client.get(f"/api/sub-pages/preview/{ticket}/default", headers=BROWSER)
        assert response.status_code == status.HTTP_403_FORBIDDEN, (ticket, response.status_code)
