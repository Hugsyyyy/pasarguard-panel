"""My Hosts API (Pengu fork): operators personalize host names, order and visibility for their own users.

Kept out of upstream's host router so upstream updates merge cleanly. See app/core/host_preferences.py
for the stored format and how it is applied to subscriptions.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.core.host_preferences import (
    custom_remark,
    is_enabled,
    order_hosts,
    saved_hosts,
)
from app.db import AsyncSession, get_db
from app.db.models import Admin, Group, ProxyInbound
from app.models.admin import AdminDetails
from app.models.admin_role import PermissionScope
from app.models.host import HostListQuery, validate_format_variables
from app.operation import OperatorType
from app.operation.host import HostOperation
from app.operation.permissions import _get_resource_action, _resolve_scope
from app.utils import responses

from .authentication import get_current

host_operator = HostOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Host"], responses={401: responses._401, 403: responses._403})


class HostPreference(BaseModel):
    """One host as an admin's users get it (output only: shared values are not limited here)."""

    id: int
    remark: str
    default_remark: str
    priority: int  # position in this admin's list
    default_priority: int  # the shared host's priority
    is_customized: bool = False
    enabled: bool = True


class HostPreferenceUpdate(BaseModel):
    id: int
    remark: str | None = Field(default=None, min_length=1, max_length=256)
    # The host's position in the admin's own order. Send None for every host to follow the shared order.
    priority: int | None = Field(default=None, ge=0, le=1000000)
    enabled: bool = True

    @field_validator("remark")
    @classmethod
    def validate_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Custom host remark cannot be empty")
        return validate_format_variables(value)


class AdminHostPreferences(BaseModel):
    """What an admin changed, for the owner."""

    username: str
    hosts: list[HostPreference]


def _grants(admin: AdminDetails, action: str) -> bool:
    """Same rule as enforce_permission (without the limited-admin part): a truthy value whose scope is not NONE."""
    value = _get_resource_action(admin, "hosts", action)
    return bool(value) and _resolve_scope(value) is not PermissionScope.NONE


def can_personalize_hosts(admin: AdminDetails) -> bool:
    """Admins (not the owner) who can neither create nor edit the shared hosts.

    Decided by the admin's own role, never by an API key's permissions: a key with custom permissions
    is refused outright, so a narrow key (even the owner's) cannot hide hosts for anyone.
    """
    if admin.id is None or admin.is_owner or admin.custom_api_key_id is not None:
        return False
    return not (_grants(admin, "create") or _grants(admin, "update"))


async def _personalizing_account(db: AsyncSession, admin: AdminDetails, write: bool) -> Admin:
    if not can_personalize_hosts(admin):
        raise HTTPException(status_code=403, detail="My Hosts is only for admins who cannot edit the shared hosts")
    if admin.is_limited and (write or (admin.role is not None and admin.role.disabled_when_limited)):
        raise HTTPException(status_code=403, detail="Admin is limited")
    account = await db.scalar(select(Admin).where(Admin.id == admin.id))
    if account is None:
        raise HTTPException(status_code=403, detail="Admin account required")
    return account


async def _personalizable_hosts(db: AsyncSession, admin: AdminDetails) -> tuple[list, list]:
    """(all hosts, the ones this admin can personalize): enabled hosts of the groups the role may use."""
    all_hosts = await host_operator.get_hosts(db, HostListQuery())
    hosts = [host for host in all_hosts if not host.is_disabled]
    access = admin.role.access if admin.role else None
    allowed_group_ids = access.allowed_group_ids if access else None
    if allowed_group_ids is not None:
        query = select(ProxyInbound.tag).join(ProxyInbound.groups).where(Group.id.in_(allowed_group_ids))
        tags = set(await db.scalars(query))
        hosts = [host for host in hosts if host.inbound_tag in tags]
    return all_hosts, hosts


def _preferences(hosts: list, host_preferences) -> list[HostPreference]:
    saved = saved_hosts(host_preferences)
    rows = []
    # Start from the shared order (priority, then id), the order the dashboard treats as "not customized".
    shared_order = sorted(hosts, key=lambda host: (host.priority or 0, host.id))
    for position, host in enumerate(order_hosts(shared_order, host_preferences)):
        entry = saved.get(str(host.id))
        remark = custom_remark(entry)
        rows.append(
            HostPreference(
                id=host.id,
                remark=remark or host.remark,
                default_remark=host.remark,
                priority=position,
                default_priority=host.priority,
                is_customized=remark is not None,
                enabled=is_enabled(entry),
            )
        )
    return rows


@router.get("/api/hosts/preferences", response_model=list[HostPreference])
async def get_host_preferences(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """The hosts as this admin's users get them ("My Hosts"), in their order."""
    account = await _personalizing_account(db, admin, write=False)
    _, hosts = await _personalizable_hosts(db, admin)
    return _preferences(hosts, account.host_preferences)


@router.put("/api/hosts/preferences", response_model=list[HostPreference])
async def save_host_preferences(
    preferences: list[HostPreferenceUpdate],
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(get_current),
):
    """Save this admin's names, order and visibility. Every host of the page must be sent exactly once."""
    account = await _personalizing_account(db, admin, write=True)
    all_hosts, hosts = await _personalizable_hosts(db, admin)
    submitted_ids = [item.id for item in preferences]
    if len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != {host.id for host in hosts}:
        raise HTTPException(status_code=409, detail="The host list has changed; reload the page and save again")
    shared = {host.id: host for host in hosts}
    saved = saved_hosts(account.host_preferences)
    # Hosts this page does not show (disabled by the owner, other groups) keep their name and visibility,
    # never a position: positions only mean something within the list the admin saw.
    existing_ids = {str(host.id) for host in all_hosts}
    updated = {}
    for host_id, entry in saved.items():
        if host_id in existing_ids and (custom_remark(entry) is not None or not is_enabled(entry)):
            updated[host_id] = {"remark": custom_remark(entry), "priority": None, "enabled": is_enabled(entry)}
    for item in preferences:
        host_id = str(item.id)
        remark = item.remark
        if not item.enabled and not is_enabled(saved.get(host_id)):
            remark = custom_remark(saved.get(host_id))  # a hidden host's name cannot be edited on the page
        if remark is not None and remark == (shared[item.id].remark or "").strip():
            remark = None  # the shared name: follow it
        entry = {"remark": remark, "priority": item.priority, "enabled": item.enabled}
        if entry["remark"] is None and entry["priority"] is None and entry["enabled"]:
            updated.pop(host_id, None)
        else:
            updated[host_id] = entry
    account.host_preferences = {"hosts": updated} if updated else None
    await db.commit()
    return _preferences(hosts, account.host_preferences)


@router.delete("/api/hosts/preferences", status_code=status.HTTP_204_NO_CONTENT)
async def reset_host_preferences(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Forget everything this admin personalized, including hosts the page no longer shows."""
    account = await _personalizing_account(db, admin, write=True)
    account.host_preferences = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _owner_target(db: AsyncSession, admin: AdminDetails, username: str) -> Admin:
    if not admin.is_owner:
        raise HTTPException(status_code=403, detail="Only the owner can see other admins' host preferences")
    target = await db.scalar(select(Admin).where(Admin.username == username))
    if target is None:
        raise HTTPException(status_code=404, detail="Admin not found")
    return target


@router.get("/api/admin/by-username/{username}/host-preferences", response_model=AdminHostPreferences)
async def get_admin_host_preferences(
    username: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    """Owner: the hosts an admin renamed, hid or moved (also after a role change took My Hosts away)."""
    target = await _owner_target(db, admin, username)
    saved = saved_hosts(target.host_preferences)
    all_hosts = await host_operator.get_hosts(db, HostListQuery())
    changed = [host for host in all_hosts if str(host.id) in saved]
    return AdminHostPreferences(username=target.username, hosts=_preferences(changed, target.host_preferences))


@router.delete("/api/admin/by-username/{username}/host-preferences", status_code=status.HTTP_204_NO_CONTENT)
async def reset_admin_host_preferences(
    username: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    """Owner: reset an admin's My Hosts; their users get the shared hosts again."""
    target = await _owner_target(db, admin, username)
    target.host_preferences = None
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
