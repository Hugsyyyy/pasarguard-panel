from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.core.host_preferences import (
    custom_priority,
    custom_remark,
    is_enabled,
    personalize_hosts,
    saved_hosts,
)
from app.db import AsyncSession, get_db
from app.db.models import Admin, Group, ProxyInbound
from app.models.admin import AdminDetails
from app.models.admin_role import PermissionScope
from app.models.host import (
    BaseHost,
    BulkHostsActionResponse,
    BulkHostSelection,
    CreateHost,
    HostListQuery,
    RemoveHostsResponse,
    validate_format_variables,
)
from app.operation import OperatorType
from app.operation.host import HostOperation
from app.operation.permissions import _get_resource_action, _permission_level
from app.utils import responses

from .authentication import get_current, require_permission
from .dependencies import get_host_list_query

host_operator = HostOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Host"], prefix="/api/host", responses={401: responses._401, 403: responses._403})


class HostPreference(BaseModel):
    """One host as an admin sees it on "My Hosts" (output only: shared values are not limited here)."""

    id: int
    remark: str
    default_remark: str
    priority: int
    default_priority: int
    is_customized: bool = False
    enabled: bool = True


class HostPreferenceUpdate(BaseModel):
    id: int
    remark: str | None = Field(default=None, min_length=1, max_length=256)
    # None keeps the shared order for this host (send None for every host to follow the owner's order)
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


def can_personalize_hosts(admin: AdminDetails) -> bool:
    """Admins who cannot create or edit the shared hosts get their own view of them.

    Decided by permissions, not by the role's name: a renamed or copied operator role keeps the
    feature, and nobody who can edit the shared hosts gets a private view they could save back.
    """
    if admin.id is None or admin.is_owner:
        return False
    return all(
        _permission_level(_get_resource_action(admin, "hosts", action)) == PermissionScope.NONE
        for action in ("create", "update")
    )


async def _personalizing_account(db: AsyncSession, admin: AdminDetails) -> Admin:
    if not can_personalize_hosts(admin):
        raise HTTPException(status_code=403, detail="Only admins who cannot edit hosts can personalize them")
    account = await db.scalar(select(Admin).where(Admin.id == admin.id))
    if account is None:
        raise HTTPException(status_code=403, detail="Admin account required")
    return account


async def _personalizable_hosts(db: AsyncSession, admin: AdminDetails) -> tuple[list[BaseHost], list[BaseHost]]:
    """(all hosts, the ones this admin can personalize): enabled hosts of the groups the role may use."""
    all_hosts = await host_operator.get_hosts(db, HostListQuery())
    hosts = [host for host in all_hosts if not host.is_disabled]
    access = admin.role.access if admin.role else None
    allowed_group_ids = access.allowed_group_ids if access else None
    if allowed_group_ids is not None:
        tags = set(
            await db.scalars(select(ProxyInbound.tag).join(ProxyInbound.groups).where(Group.id.in_(allowed_group_ids)))
        )
        hosts = [host for host in hosts if host.inbound_tag in tags]
    return all_hosts, hosts


@router.get("s/preferences", response_model=list[HostPreference])
async def get_host_preferences(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """The hosts as this admin's users see them ("My Hosts")."""
    account = await _personalizing_account(db, admin)
    _, hosts = await _personalizable_hosts(db, admin)
    saved = saved_hosts(account.host_preferences)
    preferences = []
    for host in hosts:
        entry = saved.get(str(host.id))
        remark = custom_remark(entry)
        priority = custom_priority(entry)
        preferences.append(
            HostPreference(
                id=host.id,
                remark=remark or host.remark,
                default_remark=host.remark,
                priority=host.priority if priority is None else priority,
                default_priority=host.priority,
                is_customized=remark is not None,
                enabled=is_enabled(entry),
            )
        )
    return sorted(preferences, key=lambda item: (item.priority, item.id))


@router.put("s/preferences", response_model=list[HostPreference])
async def save_host_preferences(
    preferences: list[HostPreferenceUpdate],
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(get_current),
):
    """Save this admin's names, order and visibility. Every host of the page must be sent exactly once."""
    account = await _personalizing_account(db, admin)
    if admin.is_limited:
        raise HTTPException(status_code=403, detail="Admin is limited — write actions blocked")
    all_hosts, hosts = await _personalizable_hosts(db, admin)
    submitted_ids = [item.id for item in preferences]
    if len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != {host.id for host in hosts}:
        raise HTTPException(status_code=409, detail="The host list has changed; reload the page and save again")
    saved = saved_hosts(account.host_preferences)
    # Keep what was saved for hosts this page does not show (disabled by the owner, other groups).
    existing_ids = {str(host.id) for host in all_hosts}
    updated = {host_id: entry for host_id, entry in saved.items() if host_id in existing_ids}
    for item in preferences:
        host_id = str(item.id)
        previous = saved.get(host_id)
        if not item.enabled and not is_enabled(previous):
            # A host that stays hidden keeps its saved name and place (the page does not edit them).
            entry = {"remark": custom_remark(previous), "priority": custom_priority(previous), "enabled": False}
        else:
            entry = {"remark": item.remark, "priority": item.priority, "enabled": item.enabled}
        if entry["remark"] is None and entry["priority"] is None and entry["enabled"]:
            updated.pop(host_id, None)  # nothing personal left: follow the shared host
        else:
            updated[host_id] = entry
    account.host_preferences = {"hosts": updated} if updated else None
    await db.commit()
    return await get_host_preferences(db, admin)


@router.get("/{host_id}", response_model=BaseHost)
async def get_host(
    host_id: int, db: AsyncSession = Depends(get_db), _: AdminDetails = Depends(require_permission("hosts", "read"))
):
    """
    get host by **id**
    """
    return await host_operator.get_validated_host(db=db, host_id=host_id)


@router.get("s", response_model=list[BaseHost])
async def get_hosts(
    query=Depends(get_host_list_query),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "read")),
):
    """
    Get proxy hosts.
    """
    hosts = await host_operator.get_hosts(db=db, query=query)
    if can_personalize_hosts(admin):
        account = await db.scalar(select(Admin).where(Admin.id == admin.id))
        if account is not None:
            return personalize_hosts(hosts, account.host_preferences)
    return hosts


@router.post("/", response_model=BaseHost, status_code=status.HTTP_201_CREATED)
async def create_host(
    new_host: CreateHost,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "create")),
):
    """
    create a new host

    **inbound_tag** must be available in one of the configured cores
    """
    return await host_operator.create_host(db, new_host=new_host, admin=admin)


@router.put("/{host_id}", response_model=BaseHost, responses={404: responses._404})
async def modify_host(
    host_id: int,
    modified_host: CreateHost,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """
    modify host by **id**

    **inbound_tag** must be available in one of the configured cores
    """
    return await host_operator.modify_host(db, host_id=host_id, modified_host=modified_host, admin=admin)


@router.delete(
    "/{host_id}",
    responses={404: responses._404, 409: responses._409},
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_host(
    host_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """
    remove host by **id**
    """
    await host_operator.remove_host(db, host_id=host_id, admin=admin)
    return {}


@router.put("s", response_model=list[BaseHost])
async def modify_hosts(
    modified_hosts: list[CreateHost],
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """
    Modify proxy hosts and update the configuration.
    """
    return await host_operator.modify_hosts(db=db, modified_hosts=modified_hosts, admin=admin)


@router.post(
    "s/bulk/delete",
    response_model=RemoveHostsResponse,
    responses={400: responses._400, 403: responses._403, 404: responses._404, 409: responses._409},
)
async def bulk_delete_hosts(
    bulk_hosts: BulkHostSelection,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """Delete selected hosts by ID."""
    return await host_operator.bulk_remove_hosts(db, bulk_hosts, admin)


@router.post(
    "s/bulk/disable",
    response_model=BulkHostsActionResponse,
    responses={400: responses._400, 403: responses._403, 404: responses._404},
)
async def bulk_disable_hosts(
    bulk_hosts: BulkHostSelection,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """Disable selected hosts by ID."""
    return await host_operator.bulk_set_hosts_disabled(db, bulk_hosts, admin, is_disabled=True)


@router.post(
    "s/bulk/enable",
    response_model=BulkHostsActionResponse,
    responses={400: responses._400, 403: responses._403, 404: responses._404},
)
async def bulk_enable_hosts(
    bulk_hosts: BulkHostSelection,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hosts", "update")),
):
    """Enable selected hosts by ID."""
    return await host_operator.bulk_set_hosts_disabled(db, bulk_hosts, admin, is_disabled=False)
