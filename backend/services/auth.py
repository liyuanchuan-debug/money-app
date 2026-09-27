"""认证与授权基础设施 —— 纯标准库实现，零新增依赖。

设计要点（配合 ``dependencies.py`` / ``routers/auth.py`` 阅读）：

- **口令**：``hashlib.scrypt``（每用户随机盐）派生后存储，校验用
  ``hmac.compare_digest`` 常量时间比较。不引入 passlib / bcrypt / argon2。
- **会话**：自签名 HMAC-SHA256 Cookie，格式 ``<payload_b64>.<sig_b64>``，
  载荷只放 ``uid`` / ``iat`` / ``exp``。签名与过期校验都在本模块完成，
  不依赖 itsdangerous / python-jose。
  角色与审批状态**每次请求都回数据库读**，因此「审批通过 / 停用 / 改角色」
  立即生效，不必等 Cookie 过期（代价是每请求多一次主键查询，本量级可忽略）。
- **枚举口径**：角色与状态一律英文常量（项目铁律：落库禁止汉字）；
  汉字只出现在 ``ROLE_LABELS`` / ``STATUS_LABELS``，供接口 ``detail`` 与前端展示。
- **开关**：``AUTH_ENFORCED`` 默认**开启**；显式 ``false`` 才是开发旁路，见 ``auth_enforced()``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 角色 / 状态枚举（唯一来源：其它模块一律 import 这里的常量）
# --------------------------------------------------------------------------- #
ROLE_USER = "USER"
ROLE_VIP = "VIP"
ROLE_ADMIN = "ADMIN"
ROLES: tuple[str, ...] = (ROLE_USER, ROLE_VIP, ROLE_ADMIN)
ROLE_LABELS: dict[str, str] = {
    ROLE_USER: "普通用户",
    ROLE_VIP: "VIP用户",
    ROLE_ADMIN: "管理员",
}

# 角色层级：数值越大权限越高。ADMIN 同时满足 VIP / USER 的要求。
ROLE_RANK: dict[str, int] = {ROLE_USER: 1, ROLE_VIP: 2, ROLE_ADMIN: 3}

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_DISABLED = "DISABLED"
STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_DISABLED,
)
STATUS_LABELS: dict[str, str] = {
    STATUS_PENDING: "待审批",
    STATUS_APPROVED: "已通过",
    STATUS_REJECTED: "已拒绝",
    STATUS_DISABLED: "已停用",
}

# 权限不足时的白话说明（用于 403 detail，不出现内部黑话）
ROLE_REQUIRED_LABELS: dict[str, str] = {
    ROLE_USER: "任意已审批账号",
    ROLE_VIP: "VIP用户或管理员",
    ROLE_ADMIN: "管理员",
}

# --------------------------------------------------------------------------- #
# 按用户隔离的配置
# --------------------------------------------------------------------------- #
# settings 表里 user_id = 0 是**保留的全局模板**（不是真实用户）：
# 新用户没有自己的行 → 读到的就是这份默认值；匿名请求（灰度期）也读它。
GLOBAL_SETTINGS_USER_ID = 0


# --------------------------------------------------------------------------- #
# 手机号
# --------------------------------------------------------------------------- #
# 中国大陆手机号：1 开头，第二位 3-9，共 11 位
PHONE_PATTERN = r"^1[3-9]\d{9}$"
PHONE_RE = re.compile(PHONE_PATTERN)
PHONE_HINT = "手机号格式不正确（应为 11 位中国大陆手机号，如 13800138000）"


def normalize_phone(raw: Any) -> str:
    """归一化手机号：去空白 / 分隔符，剥掉 ``+86`` / ``86`` 国家码。

    只做「宽容输入」的清洗，合法性一律交给 :func:`is_valid_phone` 判定。
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    for char in (" ", "-", "(", ")", "\t"):
        text = text.replace(char, "")
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    return text


def is_valid_phone(phone: Any) -> bool:
    return bool(PHONE_RE.match(str(phone or "")))


def mask_phone(phone: Any) -> str:
    """日志脱敏：``13800138000`` → ``138****8000``（日志里不落完整手机号）。"""
    text = str(phone or "")
    if len(text) < 7:
        return "***"
    return f"{text[:3]}****{text[-4:]}"


# --------------------------------------------------------------------------- #
# 口令哈希（hashlib.scrypt）
# --------------------------------------------------------------------------- #
# 成本参数：N=2**14 / r=8 / p=1 ≈ 16MB、单次约几十毫秒，交互式登录足够。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
# 显式放开 OpenSSL 的 maxmem（默认 32MB），避免大内存构建下直接抛错
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16

PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128


def hash_password(password: str, *, salt: bytes | None = None) -> tuple[str, str]:
    """返回 ``(password_hash_hex, password_salt_hex)``。

    ``salt`` 只供测试注入固定值；生产路径一律 ``secrets.token_bytes``。
    """
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    salt_bytes = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    return digest.hex(), salt_bytes.hex()


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    """常量时间校验口令。任何异常（格式非法 / 参数越界）都返回 ``False``，不抛错。"""
    try:
        salt = bytes.fromhex(str(password_salt))
        expected = bytes.fromhex(str(password_hash))
    except (TypeError, ValueError):
        return False
    try:
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=SCRYPT_DKLEN,
        )
    except (ValueError, MemoryError):  # 极端参数 / 内存不足
        return False
    return hmac.compare_digest(digest, expected)


# 手机号不存在时也要烧掉同等算力，避免「响应快 = 手机号不存在」的时间侧信道。
# 固定盐只用于这条一次性消耗路径，不保护任何真实口令。
_DUMMY_SALT = b"wave-money-dummy"
_DUMMY_HASH, _ = hash_password("not-a-real-password", salt=_DUMMY_SALT)


def verify_password_dummy(password: object) -> bool:
    """未知手机号分支的算力消耗；恒返回 ``False``。"""
    verify_password(str(password or ""), _DUMMY_HASH, _DUMMY_SALT.hex())
    return False


# --------------------------------------------------------------------------- #
# 会话 Cookie（自签名 HMAC-SHA256）
# --------------------------------------------------------------------------- #
SESSION_SECRET_ENV = "SESSION_SECRET"
SESSION_COOKIE_NAME = "wm_session"
SESSION_VERSION = 1
# 默认 365 天：登录后只要不清站点数据 / 不点退出，会话一直有效（体感上「永不过期」）。
# 可通过 JWT_EXPIRE_DAYS 或 SESSION_TTL_SECONDS 覆盖，见 session_ttl_seconds()。
DEFAULT_SESSION_EXPIRE_DAYS = 365
DEFAULT_SESSION_TTL_SECONDS = DEFAULT_SESSION_EXPIRE_DAYS * 24 * 3600
# 仅供本地开发：生产环境缺失 SESSION_SECRET 会直接抛错（见 session_secret）
DEV_SESSION_SECRET = "wave-money-insecure-dev-session-secret"
_TRUTHY = {"1", "true", "yes", "on", "y"}

_same_site_default = "lax"
_missing_secret_warned = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def is_production() -> bool:
    """生产环境判定：``APP_ENV``/``ENVIRONMENT`` 为 prod，或跑在 Render 上。"""
    env = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if env in {"prod", "production"}:
        return True
    return _truthy(os.getenv("RENDER"))


def session_secret() -> str:
    """签名密钥。生产环境缺失 → 直接报错（fail loudly）；开发环境 → 告警后兜底。"""
    global _missing_secret_warned
    secret = (os.getenv(SESSION_SECRET_ENV) or "").strip()
    if secret:
        return secret
    if is_production():
        raise RuntimeError(
            f"{SESSION_SECRET_ENV} 未配置：生产环境必须设置会话签名密钥，"
            '生成方式 python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if not _missing_secret_warned:
        _missing_secret_warned = True
        logger.warning(
            "[AUTH] 未配置 %s，正在使用不安全的开发兜底密钥；"
            "生产环境必须配置 %s，否则登录接口会直接报错。",
            SESSION_SECRET_ENV,
            SESSION_SECRET_ENV,
        )
    return DEV_SESSION_SECRET


def auth_enforced() -> bool:
    """权限校验总开关，默认 **开启**（生产安全默认）。

    - 未设置 / 空串 → **开启**（访客调用受保护接口会 401/403）；
    - 显式 ``false`` / ``0`` / ``off`` / ``no`` → 关闭（开发旁路：角色依赖放行）；
    - ``/api/auth/*`` 始终可用，不受本开关影响。

    前端菜单与路由**不依赖**此开关：未登录在 UI 上始终按访客处理。
    """
    raw = os.getenv("AUTH_ENFORCED")
    if raw is None or not str(raw).strip():
        return True
    return _truthy(raw)


def session_ttl_seconds() -> int:
    """会话有效期（秒）：写入 Cookie ``Max-Age`` 与载荷 ``exp``。

    优先级：
    1. ``SESSION_TTL_SECONDS``（秒，精细覆盖）；
    2. ``JWT_EXPIRE_DAYS``（天，产品向配置名；本项目会话是 HMAC Cookie，非 JWT，语义等同）；
    3. 默认 ``DEFAULT_SESSION_EXPIRE_DAYS``（365 天）。
    """
    raw_seconds = (os.getenv("SESSION_TTL_SECONDS") or "").strip()
    if raw_seconds:
        try:
            value = int(raw_seconds)
        except ValueError:
            logger.warning("[AUTH] SESSION_TTL_SECONDS=%r 非法，回退默认值", raw_seconds)
            return DEFAULT_SESSION_TTL_SECONDS
        if value <= 0:
            logger.warning("[AUTH] SESSION_TTL_SECONDS=%r 非法，回退默认值", raw_seconds)
            return DEFAULT_SESSION_TTL_SECONDS
        return value

    raw_days = (os.getenv("JWT_EXPIRE_DAYS") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            logger.warning("[AUTH] JWT_EXPIRE_DAYS=%r 非法，回退默认值", raw_days)
            return DEFAULT_SESSION_TTL_SECONDS
        if days <= 0:
            logger.warning("[AUTH] JWT_EXPIRE_DAYS=%r 非法，回退默认值", raw_days)
            return DEFAULT_SESSION_TTL_SECONDS
        return days * 24 * 3600

    return DEFAULT_SESSION_TTL_SECONDS


def session_cookie_samesite() -> str:
    """默认 ``lax``（需求口径）。

    注意：前后端跨站部署（如 ``*.vercel.app`` ↔ ``*.onrender.com``）时
    ``lax`` 的 Cookie 不会随跨站 fetch 发送，必须显式改成 ``none``。
    """
    raw = (os.getenv("SESSION_COOKIE_SAMESITE") or _same_site_default).strip().lower()
    if raw not in {"lax", "strict", "none"}:
        logger.warning("[AUTH] SESSION_COOKIE_SAMESITE=%r 非法，回退 lax", raw)
        return _same_site_default
    return raw


def session_cookie_secure(request: Any = None) -> bool:
    """Secure 标志：显式配置优先，否则按请求是否 HTTPS 判定。"""
    override = (os.getenv("SESSION_COOKIE_SECURE") or "").strip()
    if override:
        return _truthy(override)
    return _request_is_https(request)


def _request_is_https(request: Any) -> bool:
    if request is None:
        return False
    try:
        if getattr(request.url, "scheme", "") == "https":
            return True
    except AttributeError:  # pragma: no cover - 防御性
        pass
    try:
        header = (request.headers.get("x-forwarded-proto") or "")
    except AttributeError:  # pragma: no cover - 防御性
        return False
    return header.split(",")[0].strip().lower() == "https"


def set_session_cookie(response: Any, request: Any, token: str, ttl: int | None = None) -> None:
    """集中设置会话 Cookie 属性（HttpOnly + SameSite + 可选 Secure + 过期）。

    只写 ``Max-Age``：持久登录靠它；不要把相对秒数塞进 ``expires``
    （不同 Starlette / http.cookies 版本对 int expires 语义不一致）。
    """
    ttl = session_ttl_seconds() if ttl is None else ttl
    same_site = session_cookie_samesite()
    # SameSite=None 必须同时 Secure，否则浏览器直接丢弃
    secure = session_cookie_secure(request) or same_site == "none"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        path="/",
        httponly=True,
        samesite=same_site,
        secure=secure,
    )


def clear_session_cookie(response: Any, request: Any = None) -> None:
    same_site = session_cookie_samesite()
    secure = session_cookie_secure(request) or same_site == "none"
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite=same_site,
        secure=secure,
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(body: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def encode_session(payload: dict[str, Any], secret: str) -> str:
    """把载荷 JSON 编码成 ``<body>.<sig>``。签名覆盖整个 body。"""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(body, secret)}"


def build_session_token(
    user_id: int,
    secret: str,
    *,
    ttl: int | None = None,
    now: datetime | None = None,
) -> str:
    issued = now if now is not None else datetime.now(timezone.utc)
    issued_ts = int(issued.timestamp())
    payload = {
        "v": SESSION_VERSION,
        "uid": int(user_id),
        "iat": issued_ts,
        "exp": issued_ts + (session_ttl_seconds() if ttl is None else int(ttl)),
    }
    return encode_session(payload, secret)


def decode_session(token: str | None, secret: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """校验签名与**服务端过期时间**；任何异常都返回 ``None``（不抛错）。

    不依赖 Cookie 自带的 Max-Age：载荷里的 ``exp`` 才是权威。
    """
    if not token or not isinstance(token, str):
        return None
    body, _, signature = token.partition(".")
    if not body or not signature:
        return None
    try:
        expected = _sign(body, secret)
    except (UnicodeEncodeError, ValueError):
        return None
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != SESSION_VERSION:
        return None
    user_id = payload.get("uid")
    expires_at = payload.get("exp")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    current = (now if now is not None else datetime.now(timezone.utc)).timestamp()
    if expires_at <= int(current):
        return None
    return payload


# --------------------------------------------------------------------------- #
# 角色判定
# --------------------------------------------------------------------------- #
def has_role(role: str | None, minimum: str) -> bool:
    """层级判定：``ADMIN`` 同时满足 ``VIP`` / ``USER`` 要求。"""
    return ROLE_RANK.get(str(role or ""), 0) >= ROLE_RANK.get(minimum, 99)


def required_role_label(minimum: str | None) -> str:
    if minimum is None:
        return ROLE_REQUIRED_LABELS[ROLE_USER]
    return ROLE_REQUIRED_LABELS.get(minimum, "权限不足")


def is_valid_role(role: Any) -> bool:
    return str(role or "") in ROLES


def is_valid_status(status: Any) -> bool:
    return str(status or "") in STATUSES
