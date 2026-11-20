"""Subscription pages API (Pengu fork): the gallery, each admin's own choice, and live previews.

Kept in its own module so upstream updates merge cleanly. The rules are in app/core/sub_pages.py.

A preview is the real page template rendered with a made-up user, served under a URL that carries a
short-lived ticket (an iframe cannot send the admin's token). Page scripts come from third parties, so
a preview is always sandboxed: it runs in an origin of its own and cannot read the dashboard's session.
"""

import math
import re
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.core import sub_pages
from app.db import AsyncSession, get_db
from app.db.models import Admin, DataLimitResetStrategy, UserStatus
from app.models.admin import AdminDetails
from app.models.stats import Period
from app.models.user import UsersResponseWithInbounds
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.settings import subscription_settings
from app.subscription.share import apply_custom_format_variables, get_effective_custom_variables, setup_format_variables
from app.utils import responses
from app.utils.jwt import get_secret_key
from config import template_settings

from .authentication import get_current

subscription_operator = SubscriptionOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Subscription pages"])
_protected = {401: responses._401, 403: responses._403}

PREVIEW_PREFIX = "/api/sub-pages/preview"


class SubPageInfo(BaseModel):
    key: str
    kind: str  # default | builtin | catalog | custom
    name: str
    installed: bool
    author: str | None = None
    homepage: str | None = None
    license: str | None = None
    version: str | None = None
    description: dict[str, str] = Field(default_factory=dict)
    # Owner only: what the page fetches from other sites, the path to type into an admin's form, and how
    # many admins' users get this page
    loads: dict[str, str] = Field(default_factory=dict)
    template: str | None = None
    admins: int | None = None


class SubPagesResponse(BaseModel):
    can_manage: bool  # the owner: may install and remove pages
    install_blocker: str | None = None  # why no page can be installed (owner only)
    selected: str
    selected_template: str | None = None  # the stored path when it is none of the gallery's pages
    selected_missing: bool = False  # the stored page cannot be loaded: users get the default page
    preview_base: str
    pages: list[SubPageInfo]


class SubPageSelection(BaseModel):
    key: str = Field(min_length=1, max_length=64)


def can_use_sub_pages(admin: AdminDetails) -> bool:
    """Admins whose role has "Subscription pages" on (the default), and the owner. Decided by the
    admin's own role, never by an API key's permissions: a key with custom permissions is refused."""
    if admin.id is None or admin.custom_api_key_id is not None:
        return False
    return admin.is_owner or sub_pages.role_allows_sub_pages(admin.role)


async def _account(db: AsyncSession, admin: AdminDetails, write: bool) -> Admin:
    if not can_use_sub_pages(admin):
        if admin.id is None:
            raise HTTPException(status_code=403, detail="Admin account required")
        if admin.custom_api_key_id is not None:
            raise HTTPException(status_code=403, detail="Subscription pages are not available to custom API keys")
        raise HTTPException(status_code=403, detail="Subscription pages are switched off for your role")
    if admin.is_limited and (write or (admin.role is not None and admin.role.disabled_when_limited)):
        raise HTTPException(status_code=403, detail="Admin is limited")
    account = await db.scalar(select(Admin).where(Admin.id == admin.id))
    if account is None:
        raise HTTPException(status_code=403, detail="Admin account required")
    return account


async def _owner_account(db: AsyncSession, admin: AdminDetails) -> Admin:
    if not admin.is_owner:
        raise HTTPException(status_code=403, detail="Only the owner can install or remove subscription pages")
    return await _account(db, admin, write=True)


async def _gallery(db: AsyncSession, admin: AdminDetails, account: Admin) -> SubPagesResponse:
    is_owner = admin.is_owner
    usage: dict[str, int] = {}
    if is_owner:
        rows = await db.execute(
            select(Admin.sub_template, func.count()).where(Admin.sub_template.is_not(None)).group_by(Admin.sub_template)
        )
        usage = {template: count for template, count in rows.all() if template}

    pages = []
    for page in sub_pages.list_pages(include_not_installed=is_owner):
        info = SubPageInfo(key=page.key, kind=page.kind, name=page.name, installed=page.installed)
        if page.catalog is not None:
            info.author = page.catalog.author
            info.homepage = page.catalog.homepage
            info.license = page.catalog.license
            info.version = page.catalog.version
            info.description = dict(page.catalog.description)
        if is_owner:
            if page.catalog is not None:
                info.loads = dict(page.catalog.loads)
            info.template = page.template or template_settings.subscription_page_template
            if page.template is not None:
                info.admins = usage.get(page.template, 0)
        pages.append(info)

    stored = account.sub_template or None
    selected = sub_pages.key_for_template(stored)
    ticket = sub_pages.make_preview_ticket(await get_secret_key())
    return SubPagesResponse(
        can_manage=is_owner,
        install_blocker=sub_pages.install_blocker() if is_owner else None,
        selected=selected,
        selected_template=stored if selected == sub_pages.KEY_OTHER else None,
        selected_missing=bool(stored) and not sub_pages.usable(stored),
        preview_base=f"{PREVIEW_PREFIX}/{ticket}",
        pages=pages,
    )


@router.get("/api/sub-pages", response_model=SubPagesResponse, responses=_protected)
async def get_sub_pages(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """The subscription pages this admin can choose from, and the one their users get now."""
    account = await _account(db, admin, write=False)
    return await _gallery(db, admin, account)


@router.put("/api/sub-pages/selection", response_model=SubPagesResponse, responses=_protected)
async def select_sub_page(
    selection: SubPageSelection, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    """Choose the page this admin's users see. ``default`` goes back to the panel's default page."""
    account = await _account(db, admin, write=True)
    try:
        template = sub_pages.template_for_key(selection.key)
    except KeyError:
        raise HTTPException(status_code=404, detail="This subscription page is not available")
    account.sub_template = template
    await db.commit()
    return await _gallery(db, admin, account)


@router.post("/api/sub-pages/{key}/install", response_model=SubPagesResponse, responses=_protected)
async def install_sub_page(key: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Owner: download a catalog page (the pinned, reviewed file) and add it to the gallery."""
    account = await _owner_account(db, admin)
    page = sub_pages.catalog_page(key)
    if page is None:
        raise HTTPException(status_code=404, detail="No such page in the catalog")
    if sub_pages.install_blocker() is not None:
        raise HTTPException(
            status_code=409,
            detail="Pages are installed into CUSTOM_TEMPLATES_DIRECTORY: set it in .env to a writable folder",
        )
    try:
        await sub_pages.install(page)
    except sub_pages.InstallError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return await _gallery(db, admin, account)


@router.delete("/api/sub-pages/{key}", response_model=SubPagesResponse, responses=_protected)
async def remove_sub_page(key: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Owner: remove an installed catalog page. Admins who had chosen it go back to the default page."""
    account = await _owner_account(db, admin)
    if sub_pages.catalog_page(key) is None:
        if key in sub_pages.installed_slugs():
            raise HTTPException(
                status_code=400, detail="This page was added by hand: remove its folder from the templates directory"
            )
        raise HTTPException(status_code=404, detail="No such page in the catalog")
    if not sub_pages.uninstall(key):
        raise HTTPException(status_code=404, detail="This page is not installed")
    await db.execute(update(Admin).where(Admin.sub_template == sub_pages.page_template(key)).values(sub_template=None))
    await db.commit()
    await db.refresh(account)
    return await _gallery(db, admin, account)


# --- preview ---------------------------------------------------------------------------------------

_GIB = 1024**3

# Scripts only, in an origin of its own: no access to the dashboard's storage or cookies, whether the
# preview is shown in the dashboard's iframe or opened directly.
_SANDBOX = "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads"
_PREVIEW_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # the sandboxed page's own requests come from origin "null"
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
}

# A sandboxed page has no localStorage and no cookies: reading them throws, and a page that remembers a
# theme or language would stop there. It gets stand-ins. They are kept in window.name, which survives a
# reload: some pages save the language and reload themselves, and would do so forever with a storage
# that starts empty each time.
_STORAGE_SHIM_SOURCE = """
(function () {
  var prefix = 'pengu-preview:', state = {};
  try { if (window.name.indexOf(prefix) === 0) { state = JSON.parse(window.name.slice(prefix.length)) || {}; } }
  catch (e) { state = {}; }
  function save() { try { window.name = prefix + JSON.stringify(state); } catch (e) {} }
  function has(map, key) { return Object.prototype.hasOwnProperty.call(map, key); }
  function storage(name) {
    var map = state[name] || (state[name] = {});
    var api = {
      getItem: function (key) { return has(map, key) ? map[key] : null; },
      setItem: function (key, value) { map[key] = String(value); save(); },
      removeItem: function (key) { delete map[key]; save(); },
      clear: function () { Object.keys(map).forEach(function (key) { delete map[key]; }); save(); },
      key: function (index) { return Object.keys(map)[index] || null; }
    };
    return new Proxy(api, {
      get: function (target, key) {
        if (key === 'length') { return Object.keys(map).length; }
        if (has(target, key)) { return target[key]; }
        return typeof key === 'string' && has(map, key) ? map[key] : undefined;
      },
      set: function (target, key, value) { map[key] = String(value); save(); return true; },
      deleteProperty: function (target, key) { delete map[key]; save(); return true; }
    });
  }
  ['localStorage', 'sessionStorage'].forEach(function (name) {
    try { window[name].getItem('pengu-preview'); }
    catch (e) {
      try { Object.defineProperty(window, name, { value: storage(name), configurable: true }); } catch (e2) {}
    }
  });
  try { void document.cookie; }
  catch (e) {
    try {
      var cookies = state.cookie || (state.cookie = {});
      Object.defineProperty(document, 'cookie', {
        configurable: true,
        get: function () {
          return Object.keys(cookies).map(function (key) { return key + '=' + cookies[key]; }).join('; ');
        },
        set: function (value) {
          var pair = String(value).split(';')[0], at = pair.indexOf('=');
          if (at > 0) { cookies[pair.slice(0, at).trim()] = pair.slice(at + 1).trim(); save(); }
        }
      });
    } catch (e2) {}
  }
})();
"""
_STORAGE_SHIM = (
    "<script>" + " ".join(line.strip() for line in _STORAGE_SHIM_SOURCE.splitlines() if line.strip()) + "</script>"
)
_HEAD_TAG = re.compile(r"<head[^>]*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"\s*<!doctype[^>]*>", re.IGNORECASE)


def with_preview_shim(html: str) -> str:
    """Put the storage shim before the page's own scripts without pushing the doctype down."""
    head = _HEAD_TAG.search(html)
    if head is not None:
        return html[: head.end()] + _STORAGE_SHIM + html[head.end() :]
    doctype = _DOCTYPE.match(html)
    if doctype is not None:
        return html[: doctype.end()] + _STORAGE_SHIM + html[doctype.end() :]
    return _STORAGE_SHIM + html


def _sample_user(now: datetime) -> UsersResponseWithInbounds:
    return UsersResponseWithInbounds(
        id=0,
        username="demo-user",
        status=UserStatus.active,
        used_traffic=int(18.4 * _GIB),
        lifetime_used_traffic=int(126.7 * _GIB),
        data_limit=50 * _GIB,
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        expire=now + timedelta(days=23, hours=5),
        created_at=now - timedelta(days=37),
        edit_at=now - timedelta(days=7),
        online_at=now - timedelta(minutes=3),
    )


def _sample_links() -> list[str]:
    user_id = "3f2b8c1e-5d47-4a9b-8e21-6c0d9a7f4b10"
    shadowsocks = b64encode(b"chacha20-ietf-poly1305:demo-password").decode().rstrip("=")
    germany = (
        f"vless://{user_id}@de.example.com:443?security=tls&sni=de.example.com&type=ws"
        "&host=de.example.com&path=%2Fws&fp=chrome"
    )
    netherlands = (
        f"vless://{user_id}@nl.example.com:443?security=reality&sni=www.example.com&fp=chrome"
        "&pbk=Zm9yLXByZXZpZXctb25seS1ub3QtYS1yZWFsLWtleS0wMDAw&sid=6ba85179e30d4fc2&type=tcp"
        "&flow=xtls-rprx-vision"
    )
    turkey = "trojan://demo-password@tr.example.com:2053?security=tls&sni=tr.example.com&type=grpc&serviceName=grpc"
    links = [
        (germany, "🇩🇪 Germany"),
        (netherlands, "🇳🇱 Netherlands"),
        (turkey, "🇹🇷 Turkey"),
        (f"ss://{shadowsocks}@fi.example.com:8388", "🇫🇮 Finland"),
    ]
    return [f"{link}#{quote(name)}" for link, name in links]


async def _preview_template(ticket: str, key: str) -> str:
    if not sub_pages.preview_ticket_valid(await get_secret_key(), ticket):
        raise HTTPException(status_code=403, detail="This preview link has expired: reload the page")
    if key == sub_pages.KEY_DEFAULT:
        return template_settings.subscription_page_template
    try:
        template = sub_pages.template_for_key(key)
    except KeyError:
        raise HTTPException(status_code=404, detail="This subscription page is not installed")
    return template


async def _sample(request: Request, ticket: str, key: str) -> tuple[dict, str]:
    """(what the page template gets, the preview's own URL standing in for the subscription URL)."""
    sub_settings = await subscription_settings()
    user = _sample_user(datetime.now(UTC))
    url = f"{str(request.base_url).rstrip('/')}{PREVIEW_PREFIX}/{ticket}/{key}"
    links = _sample_links() if sub_settings.allow_browser_config else []

    format_variables = setup_format_variables(user, sub_settings.custom_variables)
    format_variables.update({"url": url})
    title = SubscriptionOperation._format_profile_title(user, format_variables, sub_settings)
    format_variables.update({"PROFILE_TITLE": title})
    apply_custom_format_variables(format_variables, get_effective_custom_variables(user, sub_settings.custom_variables))
    announce = SubscriptionOperation._format_announce(sub_settings, format_variables)
    try:
        payload = subscription_operator._build_subscription_body_payload(
            user, links, announce, sub_settings, format_variables, False
        )
    except KeyError, ValueError, IndexError:
        # An app's import URL the sample cannot fill in: show the page without the apps
        payload = {"user": user, "links": links, "announce": announce, "announce_url": "", "apps": []}
    return payload, url


def _json(content) -> JSONResponse:
    return JSONResponse(content=content, headers=_PREVIEW_HEADERS)


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}", include_in_schema=False)
@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/", include_in_schema=False)
async def preview_sub_page(request: Request, ticket: str, key: str):
    """The page rendered with a made-up user; without ``text/html`` in Accept, that user's links."""
    template = await _preview_template(ticket, key)
    payload, url = await _sample(request, ticket, key)
    if "text/html" not in request.headers.get("Accept", ""):
        encoded = b64encode("\n".join(_sample_links()).encode()).decode()
        return PlainTextResponse(encoded, headers=_PREVIEW_HEADERS)
    try:
        html = sub_pages.render_page(template, payload, subscription_url=url)
    except Exception as exc:
        # Shown to the admin who is looking at the gallery: say why instead of a bare 500
        raise HTTPException(status_code=500, detail=f"This page could not be rendered: {type(exc).__name__}: {exc}")
    return HTMLResponse(with_preview_shim(html), headers={**_PREVIEW_HEADERS, "Content-Security-Policy": _SANDBOX})


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/info", include_in_schema=False)
async def preview_sub_page_info(request: Request, ticket: str, key: str):
    await _preview_template(ticket, key)
    payload, _ = await _sample(request, ticket, key)
    return _json(payload["user"].model_dump(mode="json"))


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/apps", include_in_schema=False)
async def preview_sub_page_apps(request: Request, ticket: str, key: str):
    await _preview_template(ticket, key)
    payload, _ = await _sample(request, ticket, key)
    return _json([app.model_dump(mode="json") for app in payload["apps"]])


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/raw", include_in_schema=False)
async def preview_sub_page_raw(request: Request, ticket: str, key: str):
    await _preview_template(ticket, key)
    payload, _ = await _sample(request, ticket, key)
    body = {
        "user": payload["user"].model_dump(mode="json"),
        "links": payload["links"],
        "announce": payload["announce"],
        "announce_url": payload["announce_url"],
        "apps": [app.model_dump(mode="json") for app in payload["apps"]],
    }
    return _json({"body": body, "headers": {}})


_PERIOD_STEP = {
    Period.minute: timedelta(minutes=10),
    Period.hour: timedelta(hours=1),
    Period.day: timedelta(days=1),
    Period.month: timedelta(days=30),
}


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/usage", include_in_schema=False)
async def preview_sub_page_usage(
    ticket: str,
    key: str,
    period: Period = Period.hour,
    start: datetime | None = None,
    end: datetime | None = None,
):
    """A made-up usage history with a daily rhythm, in the shape of the real usage endpoint."""
    await _preview_template(ticket, key)
    now = datetime.now(UTC)
    end_at = min(_aware(end, now), now)
    start_at = max(_aware(start, end_at - timedelta(days=1)), now - timedelta(days=37))
    step = _PERIOD_STEP[period]
    scale = step.total_seconds() / 3600 * 0.11 * _GIB  # about 2.6 GB a day
    stats = []
    moment = start_at.replace(minute=0, second=0, microsecond=0)
    while moment <= end_at and len(stats) < 400:
        wave = 0.45 + 0.4 * math.sin(moment.timestamp() / 86400 * 2 * math.pi) + 0.15 * math.sin(len(stats) * 1.7)
        stats.append({"period_start": moment.isoformat(), "total_traffic": int(scale * max(wave, 0.05))})
        moment += step
    return _json(
        {"period": period.value, "start": start_at.isoformat(), "end": end_at.isoformat(), "stats": {"-1": stats}}
    )


@router.get(PREVIEW_PREFIX + "/{ticket}/{key}/{client_type}", include_in_schema=False)
async def preview_sub_page_links(ticket: str, key: str, client_type: str):
    await _preview_template(ticket, key)
    text = "\n".join(_sample_links())
    if client_type == "links":
        return PlainTextResponse(text, headers=_PREVIEW_HEADERS)
    if client_type == "links_base64":
        return PlainTextResponse(b64encode(text.encode()).decode(), headers=_PREVIEW_HEADERS)
    return Response(status_code=404, headers=_PREVIEW_HEADERS)
