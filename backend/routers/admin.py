"""管理后台 API（``/api/admin``）：用户审批 / 角色管理。

安全设计（**防管理员把自己锁死**，三条规则同时生效）：

1. 管理员不能降低**自己**的角色（``role`` 改成非 ADMIN → 409）；
2. 管理员不能把**自己**的状态改成非「已通过」（→ 409）；
3. 任何操作都不能让系统里「已审批的管理员」数量归零（→ 409），
   这条不依赖调用者是谁，是纯粹的数据完整性护栏（灰度期同样生效）。

状态口径：``approve`` 允许从任意状态回到 ``APPROVED``（包括把误停用的账号救回来），
因此它是幂等的；``reject`` 允许从任意状态转到 ``REJECTED``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from dependencies import require_admin
from models.auth import RoleIn, UserOut, UserStatusIn, public_user
from repository import Store, get_store
from services.auth import (
    ROLE_ADMIN,
    STATUSES,
    STATUS_APPROVED,
    STATUS_REJECTED,
    is_valid_status,
    mask_phone,
    normalize_phone,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _load_target(store: Store, phone_raw: str) -> dict:
    phone = normalize_phone(phone_raw)
    user = await store.get_user_by_phone(phone)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


async def _guard_admin_lockout(
    store: Store,
    actor: dict | None,
    target: dict,
    *,
    new_role: str | None = None,
    new_status: str | None = None,
) -> None:
    """防锁死护栏。``actor`` 为 ``None`` 表示灰度期（AUTH_ENFORCED 关闭）匿名调用，
    此时跳过「自己」相关规则，但「至少保留一个已审批管理员」照常生效。"""
    is_self = bool(actor) and str(actor.get("phone")) == str(target.get("phone"))

    if is_self and new_role is not None and new_role != ROLE_ADMIN:
        raise HTTPException(
            status_code=409,
            detail="不能降低自己的角色：管理员给自己降权会立刻失去后台权限",
        )
    if is_self and new_status is not None and new_status != STATUS_APPROVED:
        raise HTTPException(
            status_code=409,
            detail="不能修改自己的审批状态：把自己改成非「已通过」会立刻被锁在系统外",
        )

    removes_approved_admin = (
        target.get("role") == ROLE_ADMIN
        and target.get("status") == STATUS_APPROVED
        and (
            (new_role is not None and new_role != ROLE_ADMIN)
            or (new_status is not None and new_status != STATUS_APPROVED)
        )
    )
    if removes_approved_admin and await store.count_approved_admins() <= 1:
        raise HTTPException(
            status_code=409,
            detail="系统必须保留至少一个「已通过 + 管理员」账号，否则没人能再登录后台",
        )


async def _set_status(actor: dict | None, phone_raw: str, status: str) -> dict:
    store = await get_store()
    target = await _load_target(store, phone_raw)
    await _guard_admin_lockout(store, actor, target, new_status=status)
    updated = await store.update_user_status(target["phone"], status)
    logger.info(
        "[ADMIN] 审批状态变更：%s → %s", mask_phone(target["phone"]), status
    )
    return public_user(updated or target)


@router.get("/users", response_model=list[UserOut])
async def list_users(
    status: str | None = Query(
        default=None,
        description=f"按审批状态过滤：{' / '.join(STATUSES)}；留空返回全部",
    ),
    _admin: dict | None = Depends(require_admin),
) -> list[dict]:
    """用户列表（最新注册在前）。"""
    if status is not None and not is_valid_status(status):
        raise HTTPException(
            status_code=422, detail=f"状态必须是 {' / '.join(STATUSES)} 之一"
        )
    store = await get_store()
    return [public_user(user) for user in await store.list_users(status)]


@router.post("/users/{phone}/approve", response_model=UserOut)
async def approve_user(
    phone: str = Path(..., description="手机号"),
    admin: dict | None = Depends(require_admin),
) -> dict:
    """审批通过（任意状态 → ``APPROVED``，幂等）。"""
    return await _set_status(admin, phone, STATUS_APPROVED)


@router.post("/users/{phone}/reject", response_model=UserOut)
async def reject_user(
    phone: str = Path(..., description="手机号"),
    admin: dict | None = Depends(require_admin),
) -> dict:
    """拒绝注册申请（任意状态 → ``REJECTED``，幂等）。"""
    return await _set_status(admin, phone, STATUS_REJECTED)


@router.post("/users/{phone}/status", response_model=UserOut)
async def set_user_status(
    payload: UserStatusIn,
    phone: str = Path(..., description="手机号"),
    admin: dict | None = Depends(require_admin),
) -> dict:
    """通用状态变更：``PENDING`` / ``APPROVED`` / ``REJECTED`` / ``DISABLED``。

    停用 / 重新启用走这里（没有单独的 ``/disable`` 端点，避免接口面膨胀）。
    """
    return await _set_status(admin, phone, payload.status)


@router.post("/users/{phone}/role", response_model=UserOut)
async def set_user_role(
    payload: RoleIn,
    phone: str = Path(..., description="手机号"),
    admin: dict | None = Depends(require_admin),
) -> dict:
    """变更角色（``USER`` / ``VIP`` / ``ADMIN``），受防锁死护栏约束。"""
    store = await get_store()
    target = await _load_target(store, phone)
    await _guard_admin_lockout(store, admin, target, new_role=payload.role)
    updated = await store.update_user_role(target["phone"], payload.role)
    logger.info("[ADMIN] 角色变更：%s → %s", mask_phone(target["phone"]), payload.role)
    return public_user(updated or target)
