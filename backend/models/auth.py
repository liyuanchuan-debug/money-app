"""认证 / 用户管理的请求与响应模型。

落库字段一律英文枚举（见 ``services.auth`` 的常量），
汉字只出现在 ``*_label`` 这类**展示**字段里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator

from services.auth import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    ROLES,
    STATUSES,
    STATUS_LABELS,
    ROLE_LABELS,
)


class RegisterIn(BaseModel):
    phone: str = Field(..., description="中国大陆手机号，如 13800138000")
    password: str = Field(
        ...,
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"密码，{PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} 位",
    )


class LoginIn(BaseModel):
    phone: str = Field(..., description="中国大陆手机号")
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class RoleIn(BaseModel):
    role: str = Field(..., description="目标角色：USER / VIP / ADMIN")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"角色必须是 {' / '.join(ROLES)} 之一")
        return value


class UserStatusIn(BaseModel):
    status: str = Field(..., description="目标状态：PENDING / APPROVED / REJECTED / DISABLED")

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in STATUSES:
            raise ValueError(f"状态必须是 {' / '.join(STATUSES)} 之一")
        return value


class UserOut(BaseModel):
    """对外暴露的用户信息 —— **永不包含** ``password_hash`` / ``password_salt``。"""

    id: int
    phone: str
    role: str
    role_label: str
    status: str
    status_label: str
    created_at: datetime | None = None
    approved_at: datetime | None = None
    last_login_at: datetime | None = None


class MeOut(UserOut):
    """``GET /api/auth/me`` —— 在 UserOut 之上附上灰度开关，便于前端决定是否上锁。"""

    auth_enforced: bool


class RegisterOut(BaseModel):
    """注册回执 —— **刻意不含 status / created**，避免暴露手机号是否已注册。"""

    ok: bool = True
    message: str


class LoginOut(BaseModel):
    ok: bool = True
    user: UserOut


class AuthConfigOut(BaseModel):
    auth_enforced: bool
    roles: list[str]
    statuses: list[str]


def public_user(user: Mapping[str, Any]) -> dict[str, Any]:
    """用户行 → 对外字段（白名单映射，杜绝口令字段泄漏）。"""
    role = str(user.get("role") or "")
    status = str(user.get("status") or "")
    return {
        "id": user.get("id"),
        "phone": user.get("phone"),
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "created_at": user.get("created_at"),
        "approved_at": user.get("approved_at"),
        "last_login_at": user.get("last_login_at"),
    }
