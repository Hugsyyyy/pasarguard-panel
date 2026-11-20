"""Pengu fork: access and load fixes for the panel's own Telegram bot and its Mini App login."""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.db.models import AdminStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.notification import client as notification_client
from app.routers import authentication as auth
from app.telegram.handlers.admin import bulk_actions as bulk_h, confirm_action as confirm_h, user as user_h
from app.telegram.middlewares import acl
from app.telegram.utils.filters import HasPermission, IsScopeAll

BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExample"


def _admin(*, status=AdminStatus.active, owner=False, permissions=None) -> AdminDetails:
    return AdminDetails(
        id=7,
        username="reseller",
        status=status,
        role=AdminRoleData(id=3, name="operator", is_owner=owner, permissions=permissions or {}),
    )


ALL_USERS = {"users": {"read": True, "create": True, "update": True, "delete": True}}


def _required(router, kind: str, func) -> list[set[tuple[str, str]]]:
    """The HasPermission filters of every registration of `func` on the router's `kind` observer."""
    found = []
    for handler in getattr(router, kind).handlers:
        if handler.callback is func:
            found.append(
                {
                    (item.callback.resource, item.callback.action)
                    for item in handler.filters
                    if isinstance(item.callback, HasPermission)
                }
            )
    return found


@pytest.mark.parametrize(
    "router, kind, func, permission",
    [
        (user_h.router, "message", user_h.process_username, ("users", "create")),
        (user_h.router, "callback_query", user_h.process_username, ("users", "create")),
        (user_h.router, "message", user_h.modify_expiry_done, ("users", "update")),
        (user_h.router, "message", user_h.modify_data_limit_done, ("users", "update")),
        (user_h.router, "message", user_h.modify_note_done, ("users", "update")),
        (user_h.router, "message", user_h.create_user_from_template_choose, ("users", "create")),
        (user_h.router, "callback_query", user_h.create_user_from_template_choose, ("users", "create")),
        (bulk_h.router, "callback_query", bulk_h.bulk_template_chosen, ("users", "create")),
        (bulk_h.router, "message", bulk_h.bulk_template_count, ("users", "create")),
        (bulk_h.router, "callback_query", bulk_h.bulk_template_strategy, ("users", "create")),
        (bulk_h.router, "message", bulk_h.bulk_template_sequence_username, ("users", "create")),
        (bulk_h.router, "message", bulk_h.bulk_template_start_number, ("users", "create")),
        (confirm_h.router, "callback_query", confirm_h.confirm_action, ("users", "read")),
    ],
)
def test_step_that_reaches_an_operation_asks_for_its_permission(router, kind, func, permission):
    registrations = _required(router, kind, func)
    assert registrations, f"{func.__name__} is not registered on {kind}"
    assert all(permission in filters for filters in registrations)


@pytest.mark.asyncio
async def test_permission_filter_refuses_a_limited_admin_for_writes():
    limited = _admin(status=AdminStatus.limited, permissions=ALL_USERS)
    assert await HasPermission("users", "create")(None, admin=limited) is False
    assert await HasPermission("users", "update")(None, admin=limited) is False
    assert await HasPermission("users", "read")(None, admin=limited) is True
    assert await HasPermission("users", "create")(None, admin=_admin(permissions=ALL_USERS)) is True


@pytest.mark.asyncio
async def test_scope_all_filter_checks_the_permission_and_the_limited_state():
    assert await IsScopeAll("users", "delete")(None, admin=_admin(permissions=ALL_USERS)) is True
    # Scope "all" used to be enough: a limited admin could still delete everyone's expired users.
    limited = _admin(status=AdminStatus.limited, permissions=ALL_USERS)
    assert await IsScopeAll("users", "delete")(None, admin=limited) is False
    assert await IsScopeAll("users", "update")(None, admin=limited) is False
    own_only = _admin(permissions={"users": {"delete": {"scope": 1}}})
    assert await IsScopeAll("users", "delete")(None, admin=own_only) is False
    assert await IsScopeAll("users", "delete")(None, admin=_admin(permissions={})) is False
    assert await IsScopeAll("users", "delete")(None, admin=_admin(status=AdminStatus.limited, owner=True)) is True
    assert await IsScopeAll("users", "delete")(None, admin=None) is False


@pytest.mark.asyncio
async def test_inline_search_answer_is_personal(monkeypatch, fake_user, fake_inline_query):
    monkeypatch.setattr(user_h, "Texts", SimpleNamespace(status_emoji=lambda s: "", user_short_detail=lambda u: ""))
    monkeypatch.setattr(user_h.user_operations, "get_users", AsyncMock(return_value=SimpleNamespace(users=[fake_user])))
    inline = type(fake_inline_query)(query="")

    await user_h.search_user(inline, admin=_admin(permissions=ALL_USERS), db=object())

    # Without is_personal Telegram hands this list to any account that sends the same query.
    assert inline.answer.await_args.kwargs["is_personal"] is True


def _init_data(auth_date: datetime, user_id: int = 42) -> str:
    fields = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAExample",
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def mini_app(monkeypatch):
    settings = SimpleNamespace(mini_app_login=True, enable=True, token=BOT_TOKEN)
    monkeypatch.setattr(auth, "telegram_settings", AsyncMock(return_value=settings))
    lookups = SimpleNamespace(
        find=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(auth, "find_admins_by_telegram_id", lookups.find)
    monkeypatch.setattr(auth, "get_admin_by_telegram_id", lookups.get)
    return lookups


@pytest.mark.asyncio
async def test_mini_app_login_accepts_fresh_init_data(mini_app):
    assert await auth.validate_mini_app_admin(object(), _init_data(datetime.now(UTC) - timedelta(minutes=5))) is None
    assert mini_app.get.await_args.args[1] == 42


@pytest.mark.asyncio
async def test_mini_app_login_refuses_old_init_data(mini_app):
    with pytest.raises(HTTPException) as refused:
        await auth.validate_mini_app_admin(object(), _init_data(datetime.now(UTC) - timedelta(hours=25)))
    assert refused.value.status_code == 403
    mini_app.find.assert_not_awaited()
    mini_app.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_mini_app_login_still_refuses_a_bad_signature(mini_app):
    forged = _init_data(datetime.now(UTC)).replace("Test", "Else")
    with pytest.raises(HTTPException) as refused:
        await auth.validate_mini_app_admin(object(), forged)
    assert refused.value.status_code == 403
    mini_app.get.assert_not_awaited()


def test_mini_app_freshness_rule():
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    assert auth.mini_app_init_data_is_fresh(now - timedelta(hours=23, minutes=59), now)
    assert not auth.mini_app_init_data_is_fresh(now - timedelta(hours=24, seconds=1), now)
    assert auth.mini_app_init_data_is_fresh((now - timedelta(hours=1)).replace(tzinfo=None), now)
    assert not auth.mini_app_init_data_is_fresh(None, now)


@pytest.mark.asyncio
async def test_acl_does_not_load_users_or_usage_logs(monkeypatch):
    @asynccontextmanager
    async def fake_db():
        yield object()

    db_admin = SimpleNamespace(status=AdminStatus.active)
    get_admin = AsyncMock(return_value=db_admin)

    def build(*args, **kwargs):
        return ("details", args, kwargs)

    monkeypatch.setattr(acl, "GetDB", fake_db)
    monkeypatch.setattr(acl, "telegram_settings", AsyncMock(return_value=SimpleNamespace(for_admins_only=True)))
    monkeypatch.setattr(acl, "get_admin_by_telegram_id", get_admin)
    monkeypatch.setattr(acl, "build_admin_details", build)
    event = SimpleNamespace(
        message=SimpleNamespace(from_user=SimpleNamespace(id=5)), callback_query=None, inline_query=None
    )
    data = {}
    handler = AsyncMock(return_value="handled")

    assert await acl.ACLMiddleware()(handler, event, data) == "handled"

    assert get_admin.await_args.args[1] == 5
    assert get_admin.await_args.kwargs == {"load_users": False, "load_usage_logs": False}
    # Asking for the loaded metrics would lazy-load every user of the admin on each update.
    assert data["admin"] == ("details", (db_admin,), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings, queued",
    [
        (SimpleNamespace(notify_telegram=False, telegram_api_token=None), False),
        (SimpleNamespace(notify_telegram=False, telegram_api_token="1:a"), False),
        (SimpleNamespace(notify_telegram=True, telegram_api_token=None), False),
        (SimpleNamespace(notify_telegram=True, telegram_api_token="1:a"), True),
    ],
)
async def test_telegram_notification_is_queued_only_when_it_can_be_sent(monkeypatch, settings, queued):
    enqueue = AsyncMock()
    monkeypatch.setattr(notification_client, "notification_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(notification_client, "enqueue_telegram", enqueue)

    await notification_client.send_telegram_message("text", chat_id=99)
    await notification_client.send_telegram_message("text", chat_id=None)

    assert enqueue.await_count == (1 if queued else 0)
