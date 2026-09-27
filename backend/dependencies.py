"""FastAPI 依赖：会话解析 + 角色门禁。

**``AUTH_ENFORCED`` 是唯一的 API 放行旁路**：默认开启；显式关闭时所有角色依赖
退化为 no-op（读取已登录用户但仍然放行），不存在「依赖内部静默 fail-open」。
前端菜单/路由不读此开关——未登录在 UI 上始终按访客处理。

依赖一览：

| 依赖 | 语义 |
|------|------|
| :func:`require_session` | 必须已登录且已审批；**无视开关**（``/api/auth/me``、``/api/auth/logout`` 用） |
| :func:`require_actor` | 任意**已审批**用户（USER / VIP / ADMIN） |
| :func:`require_vip` | VIP 或 ADMIN |
| :func:`require_admin` | 仅 ADMIN |

关闭开关时若请求带有效会话，仍会返回该用户 —— 这样即便还在灰度期，
``settings`` 等按用户隔离的数据也已经生效。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from repository import get_store
from services.auth import (
    ROLE_ADMIN,
    ROLE_VIP,
    SESSION_COOKIE_NAME,
    STATUS_APPROVED,
    auth_enforced,
    decode_session,
    has_role,
    required_role_label,
    session_secret,
)

# --------------------------------------------------------------------------- #
# 会话解析
# --------------------------------------------------------------------------- #
async def current_user_optional(request: Request) -> dict | None:
    """从会话 Cookie 解析当前用户；无会话 / 签名非法 / 过期 / 未审批 → ``None``。

    角色与状态**每次请求都回数据库读**，因此审批、停用、改角色立即生效。
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    payload = decode_session(token, session_secret())
    if payload is None:
        return None
    user = await (await get_store()).get_user_by_id(int(payload["uid"]))
    if user is None:
        return None
    if user.get("status") != STATUS_APPROVED:
        # 审批被撤销 / 账号被停用 → 会话立即失效（无需等 Cookie 过期）
        return None
    return user


def actor_user_id(user: dict | None) -> int | None:
    """已登录用户 → ``user_id``；匿名 → ``None``（= 全局默认设置）。"""
    if not user:
        return None
    user_id = user.get("id")
    return int(user_id) if user_id is not None else None


async def require_session(request: Request) -> dict:
    """必须已登录（且已审批）。**不受 AUTH_ENFORCED 影响**，供 auth 自身接口使用。"""
    user = await current_user_optional(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="未登录或登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


# --------------------------------------------------------------------------- #
# 角色门禁
# --------------------------------------------------------------------------- #
async def _gate(request: Request, minimum: str | None) -> dict | None:
    """统一门禁：显式旁路关闭 → 直接放行；默认开启 → 必须已登录且角色达标。"""
    if not auth_enforced():
        # 开发旁路：不做任何拦截，但仍尝试解析用户（供按用户隔离的设置使用）
        return await current_user_optional(request)

    user = await current_user_optional(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="未登录或登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Cookie"},
        )
    if minimum is not None and not has_role(user.get("role"), minimum):
        raise HTTPException(
            status_code=403,
            detail=f"权限不足：该功能仅限{required_role_label(minimum)}使用",
        )
    return user


async def require_actor(request: Request) -> dict | None:
    """任意已审批用户（含 ADMIN）。"""
    return await _gate(request, None)


async def require_vip(request: Request) -> dict | None:
    """VIP 或 ADMIN。"""
    return await _gate(request, ROLE_VIP)


async def require_admin(request: Request) -> dict | None:
    """仅 ADMIN。"""
    return await _gate(request, ROLE_ADMIN)


__all__ = [
    "actor_user_id",
    "current_user_optional",
    "require_actor",
    "require_admin",
    "require_session",
    "require_vip",
]
