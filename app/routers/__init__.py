from fastapi import APIRouter

from . import (
    admin,
    admin_role,
    api_key,
    client_template,
    core,
    group,
    home,
    host,
    hwid,
    node,
    passkey,
    settings,
    setup,
    subscription,
    system,
    user,
    user_template,
)

api_router = APIRouter()

routers = [
    home.router,
    admin.router,
    api_key.router,
    admin_role.router,
    setup.router,
    system.router,
    settings.router,
    group.router,
    core.router,
    client_template.router,
    host.router,
    node.router,
    user.router,
    subscription.router,
    user_template.router,
    hwid.router,
    passkey.router,
]

for router in routers:
    api_router.include_router(router)

# Pengu fork: My Hosts (kept in its own module so upstream changes to the list merge cleanly)
from . import host_preferences

api_router.include_router(host_preferences.router)

# Pengu fork: Subscription pages (each admin's own page, the gallery and its previews)
from . import sub_pages

api_router.include_router(sub_pages.router)

__all__ = ["api_router"]
