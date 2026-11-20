"""Subscription pages (Pengu fork): every admin picks the page their own users see in a browser.

Upstream already stores a page template per admin (``admins.sub_template``, a template path) that
someone who may edit admins types in by hand. This module turns it into a gallery:

- the panel's default page (``SUBSCRIPTION_PAGE_TEMPLATE``): what an admin without a choice gets;
- PasarGuard's built-in page, when the default is a different file;
- every folder ``subscription/pengu-pages/<slug>/index.html`` inside ``CUSTOM_TEMPLATES_DIRECTORY``:
  pages installed from the catalog below, and the owner's own pages dropped into that folder.

The catalog names well-known community pages. Nothing of them is shipped with the panel: the owner
installs one with a click, the panel downloads the exact file named here from its author's repository
and refuses it unless the SHA-256 matches. A page template is rendered by Jinja with full access and
its scripts run in the users' browsers, so a file is only ever taken at a reviewed, pinned version.

Who may choose is decided by the admin's role (``role_allows_sub_pages``: the role's "Subscription
pages" feature, on by default). The stored choice is used whether or not the role still allows choosing,
so the owner can give an admin a page and lock it. (Upstream's admin form still takes any path from
whoever has ``admins.update``; that is unchanged.) A choice whose file is gone, or that fails while it is
rendered for a user, falls back to the default page instead of failing the subscription.
"""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import shutil
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader, TemplateError

from app.core.host_preferences import _field
from app.templates import env, render_template
from app.utils.logger import get_logger
from config import template_settings

logger = get_logger("sub-pages")

PAGES_DIR = "subscription/pengu-pages"
BUILTIN_PREFIX = "pengu-builtin"
BUILTIN_TEMPLATE = f"{BUILTIN_PREFIX}/subscription/index.html"
_BUILTIN_DIRECTORY = "app/templates"

KEY_DEFAULT = "default"
KEY_BUILTIN = "builtin"
KEY_OTHER = "other"  # a path the owner typed in the admin form that is none of the gallery's pages
_RESERVED_KEYS = frozenset({KEY_DEFAULT, KEY_BUILTIN, KEY_OTHER, "preview", "selection"})

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_PAGE_BYTES = 4 * 1024 * 1024
PREVIEW_TICKET_SECONDS = 3600


@dataclass(frozen=True)
class CatalogPage:
    slug: str
    name: str
    author: str
    homepage: str
    license: str | None  # SPDX id; None when the repository names no license
    version: str  # the release tag or commit the file is pinned to
    download_url: str
    sha256: str
    size: int
    description: dict[str, str]
    # What the page fetches from other sites while a user looks at it. The pinned hash covers the file
    # itself, not these: the owner should know before offering the page.
    loads: dict[str, str]
    # Written for Marzban: its template gets ``user`` in Marzban's shape (see MarzbanUser), with the
    # subscription URL as the page expects it: "url" (complete) or "path" (the page adds the origin).
    marzban: str | None = None


CATALOG: tuple[CatalogPage, ...] = (
    CatalogPage(
        slug="pasarguard",
        name="PasarGuard",
        author="PasarGuard",
        homepage="https://github.com/PasarGuard/subscription-template",
        license=None,
        version="v2.2.1",
        download_url="https://github.com/PasarGuard/subscription-template/releases/download/v2.2.1/index.html",
        sha256="ea3546a5dd3eac1ff8adcc1cfa9ae460a2457bade2d937b10f0f3f06859437e8",
        size=1133590,
        description={
            "en": "The official PasarGuard page: usage chart, apps, QR codes, light and dark, four languages.",
            "fa": "قالب رسمی پاسارگارد: نمودار مصرف، برنامه‌ها، کد QR، حالت روشن و تیره، چهار زبان.",
        },
        loads={
            "en": "Self-contained: it loads nothing from other sites.",
            "fa": "مستقل است: چیزی از سایت دیگری بار نمی‌کند.",
        },
    ),
    CatalogPage(
        slug="ourenus",
        name="Ourenus",
        author="MatinDehghanian",
        homepage="https://github.com/MatinDehghanian/Ourenus",
        license="MIT",
        version="v0.9.5.55",
        download_url="https://github.com/MatinDehghanian/Ourenus/releases/download/v0.9.5.55/index.html",
        sha256="6addd672b534370b02c3b83f2a688cf989cccf9a315ab6755cded9358e3108f2",
        size=903890,
        description={
            "en": "Glass-style page with a detailed usage chart, apps for the visitor's device and QR codes.",
            "fa": "قالب شیشه‌ای با نمودار دقیق مصرف، برنامه‌های مناسب دستگاه کاربر و کد QR.",
        },
        loads={
            "en": "Fonts from Google Fonts; its logo and its list of devices are read from the author's GitHub on every visit.",
            "fa": "فونت را از Google Fonts می‌گیرد؛ لوگو و فهرست دستگاه‌ها را در هر بازدید از گیت‌هاب سازنده می‌خواند.",
        },
    ),
    CatalogPage(
        slug="pgclock",
        name="PGClock",
        author="Mrclocks",
        homepage="https://github.com/Mrclocks/PGClock",
        license=None,
        version="2ee77a7",
        download_url=(
            "https://raw.githubusercontent.com/Mrclocks/PGClock/2ee77a78c4b1428d330e276c3fbcc8cdb21c349c/index.html"
        ),
        sha256="b54a4db035379b4228c9426f2d4fd2579bba82118fd5872aabca9b1286fc7e46",
        size=91075,
        description={
            "en": "Persian glass-style page made for PasarGuard: usage chart, warnings, apps and announcements.",
            "fa": "قالب شیشه‌ای فارسی مخصوص پاسارگارد: نمودار مصرف، هشدارها، برنامه‌ها و اعلان‌ها.",
        },
        loads={
            "en": "Its font from jsDelivr and its QR script from cdnjs, both at fixed versions.",
            "fa": "فونت را از jsDelivr و اسکریپت کد QR را از cdnjs می‌گیرد، هر دو با نسخهٔ ثابت.",
        },
    ),
    CatalogPage(
        slug="pgclock-lite",
        name="PGClock Lite",
        author="Mrclocks",
        homepage="https://github.com/Mrclocks/PGClockLite",
        license=None,
        version="0321465",
        download_url=(
            "https://raw.githubusercontent.com/Mrclocks/PGClockLite/03214659c5f50775f6f028d60b1146a3c6aa9404/index.html"
        ),
        sha256="314d73e572551018282958019eab861d6c8c525478531e2ccd9001f88b690bfa",
        size=71390,
        description={
            "en": "The light PGClock: subscription details, links with QR codes and apps, without the chart.",
            "fa": "نسخهٔ سبک PGClock: مشخصات اشتراک، لینک‌ها با کد QR و برنامه‌ها، بدون نمودار.",
        },
        loads={
            "en": "Its font from jsDelivr and its QR script from cdnjs, both at fixed versions.",
            "fa": "فونت را از jsDelivr و اسکریپت کد QR را از cdnjs می‌گیرد، هر دو با نسخهٔ ثابت.",
        },
    ),
    CatalogPage(
        slug="pgclock-plus",
        name="PGClock Plus",
        author="Mrclocks",
        homepage="https://github.com/Mrclocks/PGClockPlus",
        license=None,
        version="81c30c8",
        download_url=(
            "https://raw.githubusercontent.com/Mrclocks/PGClockPlus/81c30c851304b3f5e4cbdfb147fa20c9ea40d6d8/index.html"
        ),
        sha256="ac7cbdf51fbaf139148787b71fb42ec904322906eab3cb4ee53fa44c80db6395",
        size=75851,
        description={
            "en": "PGClock with a different layout: subscription details, links and apps on separate cards.",
            "fa": "PGClock با چیدمانی دیگر: مشخصات اشتراک، لینک‌ها و برنامه‌ها روی کارت‌های جدا.",
        },
        loads={
            "en": "Its font from jsDelivr at a fixed version.",
            "fa": "فونت را با نسخهٔ ثابت از jsDelivr می‌گیرد.",
        },
    ),
    CatalogPage(
        slug="pgclock-pro",
        name="PGClock Pro",
        author="Mrclocks",
        homepage="https://github.com/Mrclocks/PGClockPRO",
        license=None,
        version="0c9ce5a",
        download_url=(
            "https://raw.githubusercontent.com/Mrclocks/PGClockPRO/0c9ce5a988e708cc0011f6fb922e80b63ac054ad/index.html"
        ),
        sha256="50ce64de86c9993edf8b796eed71784d9ae395dcfa95ea175cc76f9f24c212d8",
        size=113784,
        description={
            "en": "The full PGClock: tabs for the subscription, usage chart, links and apps for each device.",
            "fa": "نسخهٔ کامل PGClock: زبانه‌های اشتراک، نمودار مصرف، لینک‌ها و برنامه‌های هر دستگاه.",
        },
        loads={
            "en": "Its font from jsDelivr at a fixed version.",
            "fa": "فونت را با نسخهٔ ثابت از jsDelivr می‌گیرد.",
        },
    ),
    CatalogPage(
        slug="pgclock-fusion",
        name="PGClock Fusion",
        author="Pasham0",
        homepage="https://github.com/Pasham0/PGClockFusion",
        license="MIT",
        version="6bd04a3",
        download_url=(
            "https://raw.githubusercontent.com/Pasham0/PGClockFusion/6bd04a341537ab65524ef14c84e0d49f5c6c8a35/index.html"
        ),
        sha256="e3c6615d8b0d192a65ff013908a8c5add768f0acf5cbd8e20ceb68f740da73c7",
        size=108318,
        description={
            "en": "Three tabs with a live clock, a usage chart and the links with country flags.",
            "fa": "سه زبانه با ساعت زنده، نمودار مصرف و لینک‌ها همراه پرچم کشورها.",
        },
        loads={
            "en": "Its font and the country flags from jsDelivr, both at fixed versions.",
            "fa": "فونت و پرچم کشورها را با نسخهٔ ثابت از jsDelivr می‌گیرد.",
        },
    ),
    CatalogPage(
        slug="neon",
        name="Neon",
        author="7Berlin",
        homepage="https://github.com/7Berlin/pasarguard-neon-template",
        license=None,
        version="717386e",
        download_url=(
            "https://raw.githubusercontent.com/7Berlin/pasarguard-neon-template/717386e08ae3f3e58ef839565a0e884c0615573e/index.html"
        ),
        sha256="4630f659186e1a59ebb8815739d9cb738d6f255e31a733a74fe542f3bb7174de",
        size=216026,
        description={
            "en": "A neon-style page made for PasarGuard: usage, links with QR codes and apps.",
            "fa": "قالب نئونی مخصوص پاسارگارد: مصرف، لینک‌ها با کد QR و برنامه‌ها.",
        },
        loads={
            "en": "Self-contained: it loads nothing from other sites.",
            "fa": "مستقل است: چیزی از سایت دیگری بار نمی‌کند.",
        },
    ),
    CatalogPage(
        slug="marzban-sub",
        name="Marzban Sub",
        author="x0sina",
        homepage="https://github.com/x0sina/marzban-sub",
        license=None,
        version="sub2.1",
        download_url="https://github.com/x0sina/marzban-sub/releases/download/sub2.1/index.html",
        sha256="29fc547919850a2cd17e01b498cad08222e2a81e027a3bef27af87a688fdde7c",
        size=486811,
        description={
            "en": "The best-known Marzban page: usage, expiry, links with QR codes and app guides.",
            "fa": "شناخته‌شده‌ترین قالب مرزبان: مصرف، انقضا، لینک‌ها با کد QR و راهنمای برنامه‌ها.",
        },
        loads={
            "en": (
                "Written for Marzban in 2023. Scripts, styles, icons and fonts come from several other sites, "
                "some without a fixed version: they can change or stop working, and the page needs them."
            ),
            "fa": (
                "سال ۲۰۲۳ برای مرزبان نوشته شده است. اسکریپت، استایل، آیکون و فونت را از چند سایت دیگر می‌گیرد، "
                "بعضی بدون نسخهٔ ثابت: ممکن است عوض شوند یا از کار بیفتند، و صفحه بدون آن‌ها کار نمی‌کند."
            ),
        },
        marzban="path",
    ),
    CatalogPage(
        slug="ashouri-01",
        name="Marzban Templates 01",
        author="MuhammadAshouri",
        homepage="https://github.com/MuhammadAshouri/marzban-templates",
        license="GPL-3.0",
        version="d67d0d4",
        download_url=(
            "https://raw.githubusercontent.com/MuhammadAshouri/marzban-templates/"
            "d67d0d4cc72a2cd312293f5a10127b8278b4cbf6/template-01/index.html"
        ),
        sha256="4b54b278a564656e836d8a3d0399af97a52aaf5d0cdb2cf8ae352da90e967b2f",
        size=47610,
        description={
            "en": "A clean bilingual Marzban page with usage, expiry, QR codes and app downloads.",
            "fa": "قالب ساده و دوزبانهٔ مرزبان با مصرف، انقضا، کد QR و دانلود برنامه‌ها.",
        },
        loads={
            "en": (
                "Written for Marzban. Scripts and styles come from other sites; its style sheet, QR script and "
                "logo are read from the author's repository without a fixed version."
            ),
            "fa": (
                "برای مرزبان نوشته شده است. اسکریپت و استایل را از سایت‌های دیگر می‌گیرد؛ استایل اصلی، اسکریپت "
                "کد QR و لوگو را بدون نسخهٔ ثابت از مخزن سازنده می‌خواند."
            ),
        },
        marzban="url",
    ),
)

_CATALOG_BY_SLUG = {page.slug: page for page in CATALOG}


@dataclass(frozen=True)
class Page:
    """One entry of the gallery."""

    key: str
    kind: str  # default | builtin | catalog | custom
    name: str
    installed: bool
    template: str | None  # the value stored in admins.sub_template; None = the panel's default
    catalog: CatalogPage | None = None


class InstallError(Exception):
    """A page could not be installed; the message is safe to show to the owner."""


def _extend_loader() -> None:
    """Make PasarGuard's own page reachable by name even when a custom file shadows it."""
    loader = env.loader
    if isinstance(loader, ChoiceLoader) and getattr(loader, "_pengu_builtin", False):
        return
    extended = ChoiceLoader([loader, PrefixLoader({BUILTIN_PREFIX: FileSystemLoader(_BUILTIN_DIRECTORY)})])
    extended._pengu_builtin = True
    env.loader = extended


_extend_loader()


def catalog_page(slug: str) -> CatalogPage | None:
    return _CATALOG_BY_SLUG.get(slug)


def custom_directory() -> Path | None:
    directory = template_settings.custom_templates_directory
    return Path(directory) if directory else None


def pages_directory() -> Path | None:
    directory = custom_directory()
    return directory / PAGES_DIR if directory is not None else None


def page_template(slug: str) -> str:
    return f"{PAGES_DIR}/{slug}/index.html"


def is_slug(value: str) -> bool:
    return bool(_SLUG.fullmatch(value)) and value not in _RESERVED_KEYS


def installed_slugs() -> list[str]:
    directory = pages_directory()
    if directory is None:
        return []
    try:
        names = sorted(entry.name for entry in directory.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [name for name in names if is_slug(name) and (directory / name / "index.html").is_file()]


def install_blocker() -> str | None:
    """Why no page can be installed here, as a short code for the dashboard; None when it can."""
    directory = custom_directory()
    if directory is None:
        return "no_custom_directory"
    probe = directory
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        return "not_writable"
    return None


def _source_file(template: str) -> str | None:
    try:
        filename = env.loader.get_source(env, template)[1]
    except TemplateError, OSError:
        return None
    return os.path.realpath(filename) if filename else None


def default_is_builtin() -> bool:
    """Whether the panel's default page is PasarGuard's built-in file (then it is not offered twice)."""
    default = _source_file(template_settings.subscription_page_template)
    return default is not None and default == _source_file(BUILTIN_TEMPLATE)


def usable(template: str) -> bool:
    """The template exists and compiles. Compiled templates are cached, so this is cheap per request."""
    try:
        env.get_template(template)
    except TemplateError, OSError:
        return False
    return True


def template_for_admin(admin) -> str:
    """The page template for the users of ``admin``: their saved choice while its file is usable,
    otherwise the panel's default. Never raises: a subscription must not fail over a missing page."""
    chosen = getattr(admin, "sub_template", None) if admin is not None else None
    if chosen and isinstance(chosen, str) and usable(chosen):
        return chosen
    return template_settings.subscription_page_template


def list_pages(include_not_installed: bool) -> list[Page]:
    pages = [Page(key=KEY_DEFAULT, kind="default", name="Panel default", installed=True, template=None)]
    if not default_is_builtin():
        pages.append(
            Page(key=KEY_BUILTIN, kind="builtin", name="PasarGuard built-in", installed=True, template=BUILTIN_TEMPLATE)
        )
    installed = installed_slugs()
    for page in CATALOG:
        if page.slug in installed:
            pages.append(
                Page(
                    key=page.slug,
                    kind="catalog",
                    name=page.name,
                    installed=True,
                    template=page_template(page.slug),
                    catalog=page,
                )
            )
        elif include_not_installed:
            pages.append(
                Page(
                    key=page.slug,
                    kind="catalog",
                    name=page.name,
                    installed=False,
                    template=page_template(page.slug),
                    catalog=page,
                )
            )
    for slug in installed:
        if slug not in _CATALOG_BY_SLUG:
            pages.append(Page(key=slug, kind="custom", name=slug, installed=True, template=page_template(slug)))
    return pages


def key_for_template(template: str | None) -> str:
    """The gallery key of a stored ``admins.sub_template`` value."""
    if not template:
        return KEY_DEFAULT
    if template == BUILTIN_TEMPLATE:
        return KEY_BUILTIN
    prefix, suffix = f"{PAGES_DIR}/", "/index.html"
    if template.startswith(prefix) and template.endswith(suffix):
        slug = template[len(prefix) : -len(suffix)]
        if is_slug(slug):
            return slug
    return KEY_OTHER


def template_for_key(key: str) -> str | None:
    """The value to store for a gallery key (None = the default page). KeyError when the key is not a
    page that can be chosen right now."""
    if key == KEY_DEFAULT:
        return None
    if key == KEY_BUILTIN:
        if default_is_builtin():
            raise KeyError(key)
        return BUILTIN_TEMPLATE
    if is_slug(key) and key in installed_slugs():
        return page_template(key)
    raise KeyError(key)


def role_allows_sub_pages(role) -> bool:
    """Whether admins of this role may choose their own subscription page. On unless the role's
    "Subscription pages" feature is switched off; the owner role always may. Works with the ORM role
    (JSON dicts) and with the API model."""
    if role is None:
        return True
    if getattr(role, "is_owner", False):
        return True
    return _field(getattr(role, "features", None), "can_use_sub_pages") is not False


# --- rendering -------------------------------------------------------------------------------------


class MarzbanUser:
    """``user`` as a page written for Marzban expects it. PasarGuard changed three things such pages
    rely on: ``expire`` became a datetime (Marzban: a Unix timestamp), and the links and the
    subscription URL moved out of the user object. Everything else is read from the real user."""

    def __init__(self, user, links, subscription_url: str):
        self._user = user
        self.links = list(links or [])
        self.subscription_url = subscription_url or ""

    def __getattr__(self, name):
        return getattr(self._user, name)

    @staticmethod
    def _timestamp(value):
        if isinstance(value, datetime):
            return int(value.timestamp())
        return value

    @property
    def expire(self):
        return self._timestamp(self._user.expire)

    @property
    def on_hold_timeout(self):
        return self._timestamp(self._user.on_hold_timeout)


def _catalog_page_of(template: str) -> CatalogPage | None:
    key = key_for_template(template)
    return _CATALOG_BY_SLUG.get(key)


_marzban_environment = None


def _marzban_env():
    """Marzban renders its pages without autoescaping, and its pages count on that: one of them puts the
    links into a script, where an escaped ``&`` would stay escaped and break every link. Same loader,
    filters and globals as the panel's own environment; rebuilt when that one's loader is replaced."""
    global _marzban_environment
    if _marzban_environment is None or _marzban_environment.loader is not env.loader:
        _marzban_environment = env.overlay(autoescape=False)
    return _marzban_environment


def _render(template: str, payload: dict, subscription_url: str) -> str:
    page = _catalog_page_of(template)
    if page is None or not page.marzban:
        return render_template(template, payload)
    if page.marzban == "path":
        parts = urlsplit(subscription_url)
        subscription_url = parts.path + (f"?{parts.query}" if parts.query else "")
    user = MarzbanUser(payload.get("user"), payload.get("links"), subscription_url)
    return _marzban_env().get_template(template).render({**payload, "user": user})


def render_page(template: str, payload: dict, subscription_url: str = "", fallback: bool = True) -> str:
    """Render a subscription page. ``payload`` is the dict PasarGuard gives its own page.

    A chosen page that fails for this user (a page tried only with ordinary users can still trip over
    one without an expiry, for example) is replaced by the panel's default page: a user must get a
    subscription page, not an error. ``fallback=False`` (previews) lets the error through instead."""
    default = template_settings.subscription_page_template
    if not fallback or template == default:
        return _render(template, payload, subscription_url)
    try:
        return _render(template, payload, subscription_url)
    except Exception:
        logger.exception("subscription page %s failed to render; the default page was used", template)
        return render_template(default, payload)


# --- install and remove ----------------------------------------------------------------------------


async def download(page: CatalogPage) -> bytes:
    """Fetch the page's pinned file. Proxy settings of the environment are honoured."""
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
            session.get(page.download_url, allow_redirects=True) as response,
        ):
            if response.status != 200:
                raise InstallError(f"The download answered HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_PAGE_BYTES:
                    raise InstallError("The downloaded file is larger than expected")
            return bytes(data)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise InstallError(f"Could not download the page from its repository: {exc or type(exc).__name__}") from exc


def _write_page(slug: str, data: bytes) -> None:
    directory = pages_directory()
    if directory is None:
        raise InstallError("CUSTOM_TEMPLATES_DIRECTORY is not set")
    target = directory / slug
    try:
        target.mkdir(parents=True, exist_ok=True)
        # a name of its own: two workers installing the same page must not share a half-written file
        temporary = target / f".index.{secrets.token_hex(8)}.tmp"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target / "index.html")
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise InstallError(f"Could not write the page into the templates directory: {exc.strerror or exc}") from exc


async def install(page: CatalogPage) -> None:
    """Download the catalog page and put it into the gallery folder. Only the exact file the catalog
    names is accepted."""
    blocker = install_blocker()
    if blocker == "no_custom_directory":
        raise InstallError("CUSTOM_TEMPLATES_DIRECTORY is not set")
    if blocker is not None:
        raise InstallError("The templates directory is not writable")
    data = await download(page)
    if len(data) != page.size or hashlib.sha256(data).hexdigest() != page.sha256:
        raise InstallError("The downloaded file is not the reviewed version of this page; nothing was installed")
    await asyncio.to_thread(_write_page, page.slug, data)
    if not usable(page_template(page.slug)):
        await asyncio.to_thread(uninstall, page.slug)
        raise InstallError("The page was downloaded but the panel cannot load it")


def uninstall(slug: str) -> bool:
    """Remove an installed catalog page. False when it was not there or could not be removed."""
    directory = pages_directory()
    if directory is None or not is_slug(slug):
        return False
    target = directory / slug
    try:
        # the page file first: once it is gone the page is uninstalled, whatever is left of the folder
        (target / "index.html").unlink()
    except OSError:
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


# --- preview tickets -------------------------------------------------------------------------------


def _ticket_signature(secret: str, expires: int) -> str:
    digest = hmac.new(secret.encode(), f"pengu-sub-page-preview:{expires}".encode(), hashlib.sha256).digest()
    return urlsafe_b64encode(digest[:18]).decode()


def make_preview_ticket(secret: str, now: float | None = None) -> str:
    """A short-lived ticket that goes into the preview URL: an iframe cannot send the admin's token,
    and a page's own requests (``./info``, ``./apps``) then carry it without knowing about it."""
    expires = int((time.time() if now is None else now) + PREVIEW_TICKET_SECONDS)
    return f"{expires:x}.{_ticket_signature(secret, expires)}"


def preview_ticket_valid(secret: str, ticket: str, now: float | None = None) -> bool:
    head, _, signature = ticket.partition(".")
    try:
        expires = int(head, 16)
    except ValueError:
        return False
    if expires < (time.time() if now is None else now):
        return False
    # as bytes: compare_digest refuses text that is not ASCII, and the ticket comes from the URL
    return hmac.compare_digest(signature.encode(), _ticket_signature(secret, expires).encode())
