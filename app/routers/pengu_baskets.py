"""Traffic baskets API (Pengu fork). See app/db/pengu_baskets_models.py for the model.

- Owner: define baskets (sets of groups).
- Owner, or admins allowed to update admins: read and change an admin's volumes.
- Every admin: read its own volumes.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.pengu_baskets import Volume, ambiguous_nodes
from app.db import AsyncSession, get_db
from app.db.crud import pengu_baskets as crud
from app.db.models import Admin, Node
from app.models.admin import AdminDetails
from app.operation import pengu_baskets as ops
from app.operation.permissions import PermissionDenied, enforce_permission
from app.utils import responses

from .authentication import get_current

router = APIRouter(tags=["Traffic baskets"], responses={401: responses._401, 403: responses._403})


class BasketBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    group_ids: list[int] = Field(default_factory=list)
    sort_order: int = Field(default=0, ge=0, le=1000000)


class BasketUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    group_ids: list[int] | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000000)


class BasketOut(BaseModel):
    id: int
    name: str
    sort_order: int
    group_ids: list[int]


class BasketsOut(BaseModel):
    baskets: list[BasketOut]
    # Nodes whose core serves tags of several baskets: their usage goes to the shared volume.
    ambiguous_node_ids: list[int] = Field(default_factory=list)


class VolumeOut(BaseModel):
    data_limit: int | None  # None = unlimited
    used_traffic: int
    remaining: int | None
    blocked: bool


class AdminBasketOut(VolumeOut):
    basket_id: int
    name: str
    group_ids: list[int]


class AdminBasketsOut(BaseModel):
    username: str
    basket_mode: bool
    shared: VolumeOut | None = None
    baskets: list[AdminBasketOut] = Field(default_factory=list)


class LimitBody(BaseModel):
    data_limit: int | None = Field(default=None, ge=0)  # bytes; None = unlimited (shared only)


class AddBody(BaseModel):
    bytes: int  # may be negative to take volume back


def _refuse(detail: str, code: int = 403):
    raise HTTPException(status_code=code, detail=detail)


def _owner_only(admin: AdminDetails) -> None:
    if not admin.is_owner:
        _refuse("Only the owner can define traffic baskets")


def _may(admin: AdminDetails, action: str) -> None:
    if admin.is_owner:
        return
    try:
        enforce_permission(admin, "admins", action)
    except PermissionDenied as exc:
        _refuse(str(exc))


async def _target(db: AsyncSession, username: str) -> Admin:
    target = await db.scalar(select(Admin).where(Admin.username == username))
    if target is None:
        _refuse("Admin not found", 404)
    return target


async def _admin_baskets(db: AsyncSession, target: Admin) -> AdminBasketsOut:
    shared, own = await crud.admin_volumes(db, target.id)
    if shared is None:
        return AdminBasketsOut(username=target.username, basket_mode=False)
    blocked, shared_blocked = await crud.blocked_state(db, target.id)
    sv = Volume(shared.data_limit, shared.used_traffic)
    out = AdminBasketsOut(
        username=target.username,
        basket_mode=True,
        shared=VolumeOut(
            data_limit=shared.data_limit,
            used_traffic=shared.used_traffic,
            remaining=sv.remaining,
            blocked=shared_blocked,
        ),
    )
    for basket, group_ids in await crud.list_baskets(db):
        row = own.get(basket.id)
        limit, used = (row.data_limit, row.used_traffic) if row else (0, 0)
        out.baskets.append(
            AdminBasketOut(
                basket_id=basket.id,
                name=basket.name,
                group_ids=group_ids,
                data_limit=limit,
                used_traffic=used,
                remaining=Volume(limit, used).remaining,
                blocked=basket.id in blocked,
            )
        )
    return out


async def _ambiguous(db: AsyncSession) -> list[int]:
    from app.jobs.pengu_basket_usage import _node_tags

    node_ids = list((await db.execute(select(Node.id))).scalars().all())
    if not node_ids:
        return []
    return ambiguous_nodes(await _node_tags(node_ids), await crud.tag_basket_map(db))


async def _baskets_out(db: AsyncSession) -> BasketsOut:
    items = [
        BasketOut(id=b.id, name=b.name, sort_order=b.sort_order, group_ids=g) for b, g in await crud.list_baskets(db)
    ]
    try:
        ambiguous = await _ambiguous(db)
    except Exception:
        ambiguous = []
    return BasketsOut(baskets=items, ambiguous_node_ids=ambiguous)


# ---------- basket definitions (owner) ----------


@router.get("/api/pengu/baskets", response_model=BasketsOut)
async def list_baskets(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    _may(admin, "read")
    return await _baskets_out(db)


async def _after_definition_change(db: AsyncSession) -> None:
    await ops.apply_blocks_for_all(db)
    await ops.resync_all_basket_admins(db)


@router.post("/api/pengu/baskets", response_model=BasketsOut, status_code=status.HTTP_201_CREATED)
async def create_basket(
    body: BasketBody, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    _owner_only(admin)
    try:
        await crud.create_basket(db, body.name, body.group_ids, body.sort_order)
    except crud.BasketError as exc:
        _refuse(str(exc), 409)
    await _after_definition_change(db)
    return await _baskets_out(db)


@router.put("/api/pengu/baskets/{basket_id}", response_model=BasketsOut)
async def update_basket(
    basket_id: int, body: BasketUpdate, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    _owner_only(admin)
    basket = await crud.get_basket(db, basket_id)
    if basket is None:
        _refuse("Basket not found", 404)
    try:
        await crud.update_basket(db, basket, body.name, body.group_ids, body.sort_order)
    except crud.BasketError as exc:
        _refuse(str(exc), 409)
    await _after_definition_change(db)
    return await _baskets_out(db)


@router.delete("/api/pengu/baskets/{basket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_basket(basket_id: int, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Admins' volumes in this basket are deleted with it; its groups fall back to the shared volume."""
    _owner_only(admin)
    basket = await crud.get_basket(db, basket_id)
    if basket is None:
        _refuse("Basket not found", 404)
    await crud.delete_basket(db, basket)
    await _after_definition_change(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- an admin's volumes ----------


@router.get("/api/pengu/baskets/me", response_model=AdminBasketsOut)
async def my_baskets(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """The current admin's own volumes (basket_mode false = upstream data limit applies)."""
    if admin.id is None:
        _refuse("Admin account required")
    return await _admin_baskets(db, await _target(db, admin.username))


@router.get("/api/admin/by-username/{username}/baskets", response_model=AdminBasketsOut)
async def get_admin_baskets(
    username: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    _may(admin, "read")
    return await _admin_baskets(db, await _target(db, username))


async def _writable_target(db: AsyncSession, admin: AdminDetails, username: str, need_mode: bool = True) -> Admin:
    _may(admin, "update")
    target = await _target(db, username)
    if target.id == admin.id and not admin.is_owner:
        _refuse("You're not allowed to change your own data limit.")
    if target.role_id == 1 and not admin.is_owner:
        _refuse("Owner cannot be modified via this endpoint.")
    if need_mode and await crud.get_shared(db, target.id) is None:
        _refuse("This admin is not in traffic basket mode; enable it first", 409)
    return target


@router.post("/api/admin/by-username/{username}/baskets/enable", response_model=AdminBasketsOut)
async def enable_baskets(username: str, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Put an admin in basket mode: its current data limit and usage become the shared volume."""
    target = await _writable_target(db, admin, username, need_mode=False)
    await crud.enable_basket_mode(db, target)
    await ops.resync_admin_users(db, target.id)
    return await _admin_baskets(db, target)


@router.put("/api/admin/by-username/{username}/baskets/shared", response_model=AdminBasketsOut)
async def set_shared(
    username: str, body: LimitBody, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    target = await _writable_target(db, admin, username)
    await crud.set_shared_limit(db, target.id, body.data_limit)
    await ops.apply_blocks(db, target.id)
    return await _admin_baskets(db, target)


@router.post("/api/admin/by-username/{username}/baskets/shared/add", response_model=AdminBasketsOut)
async def add_shared(
    username: str, body: AddBody, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)
):
    target = await _writable_target(db, admin, username)
    await crud.add_shared_volume(db, target.id, body.bytes)
    await ops.apply_blocks(db, target.id)
    return await _admin_baskets(db, target)


async def _basket_or_404(db: AsyncSession, basket_id: int):
    basket = await crud.get_basket(db, basket_id)
    if basket is None:
        _refuse("Basket not found", 404)
    return basket


@router.put("/api/admin/by-username/{username}/baskets/{basket_id}", response_model=AdminBasketsOut)
async def set_basket(
    username: str,
    basket_id: int,
    body: LimitBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(get_current),
):
    target = await _writable_target(db, admin, username)
    await _basket_or_404(db, basket_id)
    try:
        await crud.set_basket_limit(db, target.id, basket_id, body.data_limit or 0)
    except crud.BasketError as exc:
        _refuse(str(exc), 400)
    await ops.apply_blocks(db, target.id)
    return await _admin_baskets(db, target)


@router.post("/api/admin/by-username/{username}/baskets/{basket_id}/add", response_model=AdminBasketsOut)
async def add_basket(
    username: str,
    basket_id: int,
    body: AddBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(get_current),
):
    """Add volume atomically (a purchase). A negative amount takes volume back, never below what was used."""
    target = await _writable_target(db, admin, username)
    await _basket_or_404(db, basket_id)
    await crud.add_basket_volume(db, target.id, basket_id, body.bytes)
    await ops.apply_blocks(db, target.id)
    return await _admin_baskets(db, target)
