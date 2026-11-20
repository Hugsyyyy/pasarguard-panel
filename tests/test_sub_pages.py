"""Unit tests for app.core.sub_pages ("Subscription pages", Pengu fork)."""

import asyncio
import dataclasses
import hashlib
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, FileSystemLoader

from app.core import sub_pages
from app.models.admin_role import RoleFeatures
from app.routers.sub_pages import with_preview_shim
from app.templates import env
from config import template_settings


@pytest.fixture
def gallery(tmp_path, monkeypatch):
    """A custom templates directory that the page loader reads first, as CUSTOM_TEMPLATES_DIRECTORY sets up."""
    monkeypatch.setattr(template_settings, "custom_templates_directory", str(tmp_path))
    monkeypatch.setattr(env, "loader", ChoiceLoader([FileSystemLoader(str(tmp_path)), env.loader]))
    return tmp_path


def add_page(directory, slug: str, body: str) -> None:
    folder = directory / sub_pages.PAGES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(body, encoding="utf-8")


def test_catalog_entries_are_complete_and_pinned():
    slugs = [page.slug for page in sub_pages.CATALOG]
    assert len(slugs) == len(set(slugs))
    for page in sub_pages.CATALOG:
        assert sub_pages.is_slug(page.slug), page.slug
        assert re.fullmatch(r"[0-9a-f]{64}", page.sha256), page.slug
        assert 0 < page.size <= sub_pages.MAX_PAGE_BYTES, page.slug
        assert page.homepage.startswith("https://github.com/"), page.slug
        # a release asset or a file at a full commit id: never a branch, whose content can change
        assert re.match(
            r"https://github\.com/[^/]+/[^/]+/releases/download/[^/]+/[^/]+$"
            r"|https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/",
            page.download_url,
        ), page.download_url
        assert page.marzban in (None, "path", "url"), page.slug
        for texts in (page.description, page.loads):
            assert texts.get("en") and texts.get("fa"), page.slug


def test_reserved_words_are_not_slugs():
    for word in ("default", "builtin", "other", "preview", "selection", "", "UPPER", "a/b", "..", "x" * 41):
        assert not sub_pages.is_slug(word), word
    assert sub_pages.is_slug("my-page-2")


def test_keys_and_templates_map_both_ways(gallery):
    add_page(gallery, "mine", "<html></html>")
    assert sub_pages.installed_slugs() == ["mine"]
    assert sub_pages.template_for_key("default") is None
    assert sub_pages.template_for_key("mine") == "subscription/pengu-pages/mine/index.html"
    assert sub_pages.key_for_template(None) == "default"
    assert sub_pages.key_for_template("") == "default"
    assert sub_pages.key_for_template("subscription/pengu-pages/mine/index.html") == "mine"
    assert sub_pages.key_for_template("somewhere/else.html") == "other"
    assert sub_pages.key_for_template("subscription/pengu-pages/../x/index.html") == "other"
    for key in ("gone", "other", "preview", "../mine", "pasarguard"):
        with pytest.raises(KeyError):
            sub_pages.template_for_key(key)


def test_builtin_page_is_offered_only_when_the_default_is_another_file(gallery):
    assert sub_pages.default_is_builtin()
    assert [page.key for page in sub_pages.list_pages(include_not_installed=False)] == ["default"]
    with pytest.raises(KeyError):
        sub_pages.template_for_key("builtin")

    # a custom file now shadows the built-in page under the same name
    target = gallery / "subscription" / "index.html"
    target.parent.mkdir(parents=True)
    target.write_text("<html>CUSTOM DEFAULT</html>", encoding="utf-8")
    env.cache.clear()
    assert not sub_pages.default_is_builtin()
    assert [page.key for page in sub_pages.list_pages(include_not_installed=False)] == ["default", "builtin"]
    assert sub_pages.template_for_key("builtin") == sub_pages.BUILTIN_TEMPLATE
    assert "CUSTOM DEFAULT" in env.get_template("subscription/index.html").render()
    assert "CUSTOM DEFAULT" not in env.loader.get_source(env, sub_pages.BUILTIN_TEMPLATE)[0]


def test_list_shows_not_installed_pages_only_on_request(gallery):
    add_page(gallery, sub_pages.CATALOG[0].slug, "<html></html>")
    add_page(gallery, "mine", "<html></html>")
    (gallery / sub_pages.PAGES_DIR / "Not A Slug").mkdir()
    (gallery / sub_pages.PAGES_DIR / "empty").mkdir()

    for_admins = sub_pages.list_pages(include_not_installed=False)
    assert [(page.key, page.kind) for page in for_admins] == [
        ("default", "default"),
        (sub_pages.CATALOG[0].slug, "catalog"),
        ("mine", "custom"),
    ]
    for_owner = sub_pages.list_pages(include_not_installed=True)
    assert {page.key for page in for_owner if not page.installed} == {page.slug for page in sub_pages.CATALOG[1:]}


def test_missing_or_broken_choice_falls_back_to_the_default(gallery):
    default = template_settings.subscription_page_template
    add_page(gallery, "mine", "<html>{{ user.username }}</html>")
    add_page(gallery, "broken", "<html>{% if %}</html>")
    chosen = sub_pages.page_template("mine")
    assert sub_pages.template_for_admin(SimpleNamespace(sub_template=chosen)) == chosen
    assert sub_pages.template_for_admin(SimpleNamespace(sub_template=sub_pages.page_template("broken"))) == default
    assert sub_pages.template_for_admin(SimpleNamespace(sub_template="no/such/file.html")) == default
    assert sub_pages.template_for_admin(SimpleNamespace(sub_template=None)) == default
    assert sub_pages.template_for_admin(None) == default


def test_role_feature_decides_who_may_choose():
    assert sub_pages.role_allows_sub_pages(None)
    assert sub_pages.role_allows_sub_pages(SimpleNamespace(is_owner=False, features=RoleFeatures()))
    assert sub_pages.role_allows_sub_pages(SimpleNamespace(is_owner=False, features={}))
    assert not sub_pages.role_allows_sub_pages(SimpleNamespace(is_owner=False, features={"can_use_sub_pages": False}))
    off = RoleFeatures(can_use_sub_pages=False)
    assert not sub_pages.role_allows_sub_pages(SimpleNamespace(is_owner=False, features=off))
    assert sub_pages.role_allows_sub_pages(SimpleNamespace(is_owner=True, features=off))


def _marzban_catalog(monkeypatch, slug: str, url_shape: str) -> None:
    page = dataclasses.replace(sub_pages.CATALOG[0], slug=slug, marzban=url_shape)
    monkeypatch.setitem(sub_pages._CATALOG_BY_SLUG, slug, page)


def test_marzban_page_gets_the_user_in_marzbans_shape(gallery, monkeypatch):
    body = (
        "{{ ((user.expire - now().timestamp()) // 86400) | int }}|{{ user.links | length }}|"
        "{{ user.subscription_url }}|{{ user.username }}|{{ user.status.value }}"
    )
    add_page(gallery, "old-style", body)
    add_page(gallery, "new-style", "{{ user.expire.year }}|{{ links | length }}")
    user = SimpleNamespace(
        username="u1",
        status=SimpleNamespace(value="active"),
        expire=datetime.now(UTC) + timedelta(days=10, hours=1),
        on_hold_timeout=None,
    )
    payload = {"user": user, "links": ["a", "b"]}
    url = "https://sub.example.com/sub/TOKEN"

    _marzban_catalog(monkeypatch, "old-style", "path")
    assert sub_pages.render_page(sub_pages.page_template("old-style"), payload, url) == "10|2|/sub/TOKEN|u1|active"
    _marzban_catalog(monkeypatch, "old-style", "url")
    assert sub_pages.render_page(sub_pages.page_template("old-style"), payload, url) == f"10|2|{url}|u1|active"
    # a PasarGuard page gets the payload untouched
    assert sub_pages.render_page(sub_pages.page_template("new-style"), payload, url) == f"{user.expire.year}|2"
    assert payload["user"] is user


def test_marzban_user_without_expiry():
    user = sub_pages.MarzbanUser(SimpleNamespace(expire=None, on_hold_timeout=None, data_limit=5), None, "")
    assert user.expire is None and user.on_hold_timeout is None
    assert user.links == [] and user.data_limit == 5


def test_preview_ticket_expires_and_cannot_be_forged():
    ticket = sub_pages.make_preview_ticket("secret", now=1000)
    assert sub_pages.preview_ticket_valid("secret", ticket, now=1000 + sub_pages.PREVIEW_TICKET_SECONDS - 1)
    assert not sub_pages.preview_ticket_valid("secret", ticket, now=1000 + sub_pages.PREVIEW_TICKET_SECONDS + 1)
    assert not sub_pages.preview_ticket_valid("other-secret", ticket, now=1000)
    expires, signature = ticket.split(".")
    later = f"{int(expires, 16) + 86400:x}.{signature}"
    assert not sub_pages.preview_ticket_valid("secret", later, now=1000)
    for junk in ("", ".", "zz.zz", "0.", expires, f"{expires}.", "x" * 500):
        assert not sub_pages.preview_ticket_valid("secret", junk, now=1000)


def test_preview_shim_goes_first_without_moving_the_doctype():
    with_head = with_preview_shim("<!DOCTYPE html><html><head><script>first()</script></head></html>")
    assert with_head.startswith("<!DOCTYPE html><html><head><script>(function")
    assert with_head.index("pengu-preview") < with_head.index("first()")
    no_head = with_preview_shim("<!doctype html>\n<body>x</body>")
    assert no_head.startswith("<!doctype html><script>")
    assert with_preview_shim("plain").startswith("<script>") and with_preview_shim("plain").endswith("plain")


def _fake_catalog_page(monkeypatch, data: bytes) -> sub_pages.CatalogPage:
    page = dataclasses.replace(
        sub_pages.CATALOG[0], slug="fake-page", sha256=hashlib.sha256(data).hexdigest(), size=len(data), marzban=None
    )
    monkeypatch.setitem(sub_pages._CATALOG_BY_SLUG, page.slug, page)
    return page


def test_install_accepts_only_the_pinned_file(gallery, monkeypatch):
    good = b"<html><head></head><body>{{ user.username }}</body></html>"
    page = _fake_catalog_page(monkeypatch, good)

    async def tampered(_page):
        return good + b"<script>evil()</script>"

    monkeypatch.setattr(sub_pages, "download", tampered)
    with pytest.raises(sub_pages.InstallError, match="not the reviewed version"):
        asyncio.run(sub_pages.install(page))
    assert sub_pages.installed_slugs() == []

    async def genuine(_page):
        return good

    monkeypatch.setattr(sub_pages, "download", genuine)
    asyncio.run(sub_pages.install(page))
    assert sub_pages.installed_slugs() == ["fake-page"]
    assert (gallery / sub_pages.PAGES_DIR / "fake-page" / "index.html").read_bytes() == good
    assert sub_pages.uninstall("fake-page") is True
    assert sub_pages.uninstall("fake-page") is False
    assert sub_pages.installed_slugs() == []


def test_install_refuses_a_page_the_panel_cannot_load(gallery, monkeypatch):
    broken = b"<html>{% if %}</html>"
    page = _fake_catalog_page(monkeypatch, broken)

    async def download(_page):
        return broken

    monkeypatch.setattr(sub_pages, "download", download)
    with pytest.raises(sub_pages.InstallError, match="cannot load"):
        asyncio.run(sub_pages.install(page))
    assert sub_pages.installed_slugs() == []


def test_install_needs_a_custom_templates_directory(monkeypatch):
    monkeypatch.setattr(template_settings, "custom_templates_directory", None)
    assert sub_pages.install_blocker() == "no_custom_directory"
    assert sub_pages.installed_slugs() == []
    with pytest.raises(sub_pages.InstallError, match="CUSTOM_TEMPLATES_DIRECTORY"):
        asyncio.run(sub_pages.install(sub_pages.CATALOG[0]))
    assert sub_pages.uninstall(sub_pages.CATALOG[0].slug) is False
