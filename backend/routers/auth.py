"""认证 API（``/api/auth``）：注册 / 登录 / 当前用户 / 登出。

**注册**：手机号 + 密码 → 落 ``PENDING``（待审批）。为避免手机号枚举，
无论该号是否已注册，返回体**完全一致**（不含 ``status`` / ``created`` 等可推断字段），
已存在时也不会覆盖任何字段。代价是「已注册用户重复注册」拿不到明确提示——
这是刻意的取舍，见 README「认证与权限」。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dependencies import require_session
from models.auth import (
    AuthConfigOut,
    LoginIn,
    LoginOut,
    MeOut,
    RegisterIn,
    RegisterOut,
    public_user,
)
from repository import get_store
from services.auth import (
    PHONE_HINT,
    ROLES,
    STATUSES,
    STATUS_APPROVED,
    STATUS_DISABLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    auth_enforced,
    build_session_token,
    clear_session_cookie,
    hash_password,
    is_valid_phone,
    mask_phone,
    normalize_phone,
    session_secret,
    set_session_cookie,
    verify_password,
    verify_password_dummy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 注册回执：对「新号」与「已存在的号」用同一句话，堵住枚举
REGISTER_MESSAGE = (
    "注册申请已提交。若该手机号尚未注册，账号会处于「待审批」状态，"
    "请等待管理员审批通过后再登录；若该手机号已注册过，请直接登录或联系管理员。"
)

# 登录失败统一文案：不区分「手机号不存在」与「密码错误」
LOGIN_FAILED_MESSAGE = "手机号或密码错误"

# 审批状态 → 登录被拒文案（仅在口令校验通过后才返回，避免成为状态探针）
STATUS_LOGIN_MESSAGES: dict[str, str] = {
    STATUS_PENDING: "账号待管理员审批，暂时无法登录",
    STATUS_REJECTED: "注册申请未通过，请联系管理员",
    STATUS_DISABLED: "账号已被停用，请联系管理员",
}


@router.get("/config", response_model=AuthConfigOut)
async def get_auth_config() -> dict:
    """公开的认证配置：前端据此决定是否显示登录墙（不含任何敏感信息）。"""
    return {
        "auth_enforced": auth_enforced(),
        "roles": list(ROLES),
        "statuses": list(STATUSES),
    }


@router.post("/register", response_model=RegisterOut)
async def register(payload: RegisterIn) -> dict:
    """提交注册申请 → 落 ``PENDING``。

    - 手机号格式非法 → 422（格式规则本就公开，不构成枚举面）；
    - 手机号已存在 → **不覆盖**、**不回显**，返回与成功完全相同的回执。
    """
    phone = normalize_phone(payload.phone)
    if not is_valid_phone(phone):
        raise HTTPException(status_code=422, detail=PHONE_HINT)

    password_hash, password_salt = hash_password(payload.password)
    store = await get_store()
    created = await store.create_user(phone, password_hash, password_salt)

    if created is None:
        logger.info(
            "[AUTH] 注册请求命中已存在的手机号 %s：未重复创建、未覆盖（对外不回显）",
            mask_phone(phone),
        )
    else:
        logger.info("[AUTH] 新注册待审批：%s", mask_phone(phone))
    return {"ok": True, "message": REGISTER_MESSAGE}


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, request: Request, response: Response) -> dict:
    """手机号 + 密码登录，成功后下发签名会话 Cookie。

    顺序刻意如此：**先校验口令、再看审批状态**。
    - 手机号不存在 / 密码错误 → 同一个 401 文案，且未知手机号也照样烧一次 scrypt
      算力，避免用响应时间区分两者；
    - 只有口令正确的人才会看到「待审批 / 已拒绝 / 已停用」的差异文案，
      因此状态差异不构成匿名枚举面。
    """
    phone = normalize_phone(payload.phone)
    store = await get_store()
    credentials = (
        await store.get_user_credentials(phone) if is_valid_phone(phone) else None
    )

    if credentials is None:
        verify_password_dummy(payload.password)  # 抹平时间差
        raise HTTPException(status_code=401, detail=LOGIN_FAILED_MESSAGE)

    if not verify_password(
        payload.password, credentials["password_hash"], credentials["password_salt"]
    ):
        raise HTTPException(status_code=401, detail=LOGIN_FAILED_MESSAGE)

    status = str(credentials.get("status") or "")
    if status != STATUS_APPROVED:
        message = STATUS_LOGIN_MESSAGES.get(status, "账号状态异常，请联系管理员")
        logger.info("[AUTH] 登录被拒（%s）：%s", status, mask_phone(phone))
        raise HTTPException(status_code=403, detail=message)

    token = build_session_token(int(credentials["id"]), session_secret())
    set_session_cookie(response, request, token)
    await store.touch_user_login(phone)
    logger.info("[AUTH] 登录成功：%s", mask_phone(phone))

    user = await store.get_user_by_phone(phone) or credentials
    return {"ok": True, "user": public_user(user)}


@router.get("/me", response_model=MeOut)
async def me(user: dict = Depends(require_session)) -> dict:
    """当前登录用户；无有效会话 → 401。

    该依赖**不受 AUTH_ENFORCED 影响**：无论灰度开关如何，``/api/auth/me``
    都必须如实反映登录态，前端才能用它判断是否展示登录页。
    """
    return {**public_user(user), "auth_enforced": auth_enforced()}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """清掉会话 Cookie（幂等：未登录调用也返回 200）。"""
    clear_session_cookie(response, request)
    return {"ok": True}
