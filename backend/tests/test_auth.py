"""认证 / RBAC / 按用户隔离设置 —— 回归测试。

覆盖：

- ``hashlib.scrypt`` 口令哈希与校验（含随机盐、脏数据、常量时间兜底路径）；
- 手机号正则与归一化；
- 自签名会话 Cookie：伪造 / 篡改签名 / 篡改载荷 / 过期 / 换密钥全部拒绝；
- ``SESSION_SECRET`` 缺失时：生产抛错、开发告警兜底；
- 注册 → ``PENDING`` → 不能登录 → 审批 → 可登录；
- 注册不暴露手机号是否已存在（两次响应字节一致，且不覆盖既有口令）；
- 三种角色对每一个受保护接口的**允许 / 拒绝**矩阵（参数化，逐接口 × 逐角色）；
- 未登录时受保护接口返回 401；管理员接口对非管理员返回 403；
- 防管理员锁死：自我降权 / 自我改状态 / 移除最后一个已审批管理员；
- ``settings`` 按用户隔离，新用户继承全局模板，匿名读写落全局模板；
- 对外用户信息永不包含 ``password_hash`` / ``password_salt``。

全部用例强制内存存储（不连真实数据库），并显式控制 ``AUTH_ENFORCED``。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import repository
import services.auth as auth
from repository import MemoryStore, public_user_row
from services.auth import (
    GLOBAL_SETTINGS_USER_ID,
    PHONE_PATTERN,
    ROLE_ADMIN,
    ROLE_USER,
    ROLE_VIP,
    ROLES,
    STATUS_APPROVED,
    STATUS_DISABLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUSES,
    build_session_token,
    decode_session,
    encode_session,
    has_role,
    hash_password,
    is_valid_phone,
    normalize_phone,
    verify_password,
    verify_password_dummy,
)

TEST_SECRET = "unit-test-session-secret"
TEST_PASSWORD = "secret123"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """强制内存存储 + 固定的测试密钥 + 关闭灰度开关，避免污染进程环境。

    ``DATABASE_URL`` 用空串而不是 delenv —— main.py 的 load_dotenv() 会把被删掉的
    变量从 ``backend/.env`` 灌回来，从而让测试连上真实 Supabase。
    详见 ``tests/conftest.py``。
    """
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SESSION_SECRET", TEST_SECRET)
    # 用例默认走旁路；需要强制鉴权的用例再 setenv("AUTH_ENFORCED", "true")
    monkeypatch.setenv("AUTH_ENFORCED", "false")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_SAMESITE", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("JWT_EXPIRE_DAYS", raising=False)
    yield


@pytest.fixture()
def client():
    """启动完整应用（触发 lifespan → 造出全新的 MemoryStore）。"""
    from main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def store(client) -> MemoryStore:
    """与 HTTP 层**同一个**存储实例（client 已确保 lifespan 跑过）。"""
    instance = asyncio.run(repository.get_store())
    assert isinstance(instance, MemoryStore)
    return instance


def seed_user(
    phone: str,
    role: str = ROLE_USER,
    status: str = STATUS_APPROVED,
    password: str = TEST_PASSWORD,
) -> dict:
    """直接落库造一个用户（跳过注册流程），默认「已审批」。"""
    store = asyncio.run(repository.get_store())
    password_hash, password_salt = hash_password(password)
    created = asyncio.run(
        store.create_user(phone, password_hash, password_salt, role=role)
    )
    assert created is not None, "重复播种手机号，测试用例写错了"
    if status != STATUS_PENDING:
        asyncio.run(store.update_user_status(phone, status))
    # create_user 返回的是「刚注册」快照（PENDING），审批后要重新读一次。
    fresh = asyncio.run(store.get_user_by_id(created["id"]))
    assert fresh is not None
    return fresh


def login(client: TestClient, phone: str, password: str = TEST_PASSWORD):
    return client.post("/api/auth/login", json={"phone": phone, "password": password})


def login_ok(client: TestClient, phone: str, password: str = TEST_PASSWORD) -> dict:
    client.cookies.clear()
    response = login(client, phone, password)
    assert response.status_code == 200, response.text
    return response.json()


def register(client: TestClient, phone: str, password: str = TEST_PASSWORD):
    return client.post("/api/auth/register", json={"phone": phone, "password": password})


# --------------------------------------------------------------------------- #
# 1. 口令哈希
# --------------------------------------------------------------------------- #
def test_password_hash_round_trip():
    password_hash, password_salt = hash_password("hunter2hunter2")

    assert verify_password("hunter2hunter2", password_hash, password_salt) is True
    assert verify_password("hunter2hunter3", password_hash, password_salt) is False
    assert verify_password("", password_hash, password_salt) is False


def test_password_hash_uses_random_salt():
    first_hash, first_salt = hash_password("same-password")
    second_hash, second_salt = hash_password("same-password")

    assert first_salt != second_salt  # 每用户独立随机盐
    assert first_hash != second_hash
    assert verify_password("same-password", first_hash, first_salt) is True
    assert verify_password("same-password", second_hash, second_salt) is True


def test_password_hash_rejects_empty_password():
    with pytest.raises(ValueError):
        hash_password("")


@pytest.mark.parametrize(
    "bad_hash, bad_salt",
    [
        ("not-hex", "00ff"),
        ("00ff", "not-hex"),
        ("", ""),
        ("zz", "zz"),
    ],
)
def test_verify_password_survives_garbage(bad_hash: str, bad_salt: str):
    assert verify_password("whatever", bad_hash, bad_salt) is False


def test_verify_password_dummy_always_false():
    assert verify_password_dummy("anything") is False
    assert verify_password_dummy(None) is False


# --------------------------------------------------------------------------- #
# 2. 手机号
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "phone", ["13800138000", "19912345678", "15012345678", "17700000000"]
)
def test_phone_accepts_valid(phone: str):
    assert is_valid_phone(phone) is True


@pytest.mark.parametrize(
    "phone",
    [
        "",
        "12345",
        "12345678901",  # 第二位是 2
        "12800138000",  # 第二位是 2
        "10800138000",  # 第二位是 0
        "1380013800",  # 10 位
        "138001380000",  # 12 位
        "1380013800a",
        "1380013800 ",
        "+8613800138000",
        None,
    ],
)
def test_phone_rejects_invalid(phone):
    assert is_valid_phone(phone) is False


def test_phone_pattern_is_exported_for_frontend_parity():
    assert PHONE_PATTERN == r"^1[3-9]\d{9}$"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" 13800138000 ", "13800138000"),
        ("138-0013-8000", "13800138000"),
        ("+8613800138000", "13800138000"),
        ("8613800138000", "13800138000"),
        ("(138) 0013 8000", "13800138000"),
        (None, ""),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


# --------------------------------------------------------------------------- #
# 3. 会话 Cookie
# --------------------------------------------------------------------------- #
def test_session_round_trip():
    token = build_session_token(7, TEST_SECRET)
    payload = decode_session(token, TEST_SECRET)

    assert payload is not None
    assert payload["uid"] == 7
    assert payload["exp"] > int(datetime.now(timezone.utc).timestamp())


def test_session_rejects_tampered_payload():
    token = build_session_token(7, TEST_SECRET)
    body, _, signature = token.partition(".")
    forged_body = auth._b64e(
        json.dumps({"v": 1, "uid": 999, "iat": 0, "exp": 9999999999}).encode()
    )

    assert decode_session(f"{forged_body}.{signature}", TEST_SECRET) is None


def test_session_rejects_tampered_signature():
    token = build_session_token(7, TEST_SECRET)
    body, _, signature = token.partition(".")
    forged_signature = ("A" if signature[0] != "A" else "B") + signature[1:]

    assert decode_session(f"{body}.{forged_signature}", TEST_SECRET) is None


def test_session_rejects_unsigned_token():
    body = auth._b64e(json.dumps({"v": 1, "uid": 7, "exp": 9999999999}).encode())

    assert decode_session(body, TEST_SECRET) is None  # 只有载荷、没有签名
    assert decode_session(f"{body}.", TEST_SECRET) is None
    assert decode_session(f".{body}", TEST_SECRET) is None


def test_session_rejects_wrong_secret():
    token = build_session_token(7, TEST_SECRET)

    assert decode_session(token, "another-secret") is None


def test_session_rejects_expired_server_side():
    """过期由载荷里的 exp 判定，不信 Cookie 自带的 Max-Age。"""
    past = datetime.now(timezone.utc) - timedelta(days=30)
    token = build_session_token(7, TEST_SECRET, ttl=60, now=past)

    assert decode_session(token, TEST_SECRET) is None
    # 同一枚 cookie，把「服务器时间」拨回过期前 → 仍然有效（证明只信 exp）
    assert decode_session(token, TEST_SECRET, now=past + timedelta(seconds=30)) is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"uid": "7", "exp": 9999999999},  # uid 不是 int
        {"uid": 7, "exp": "9999999999"},  # exp 不是 int
        {"uid": 7},  # 缺 exp
        {"exp": 9999999999},  # 缺 uid
        {"v": 99, "uid": 7, "exp": 9999999999},  # 版本不符
        [1, 2, 3],  # 不是对象
    ],
)
def test_session_rejects_malformed_claims(payload):
    token = encode_session(payload, TEST_SECRET)

    assert decode_session(token, TEST_SECRET) is None


@pytest.mark.parametrize("token", [None, "", "garbage", "a.b", "....", 12345])
def test_session_rejects_garbage(token):
    assert decode_session(token, TEST_SECRET) is None


def test_session_secret_required_in_production(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("RENDER", "true")

    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        auth.session_secret()


def test_session_secret_dev_fallback_warns(monkeypatch, caplog):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(auth, "_missing_secret_warned", False)

    with caplog.at_level("WARNING"):
        secret = auth.session_secret()

    assert secret == auth.DEV_SESSION_SECRET
    assert any("SESSION_SECRET" in record.message for record in caplog.records)


def test_session_ttl_override(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "120")
    assert auth.session_ttl_seconds() == 120

    monkeypatch.setenv("SESSION_TTL_SECONDS", "abc")
    assert auth.session_ttl_seconds() == auth.DEFAULT_SESSION_TTL_SECONDS

    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    monkeypatch.setenv("JWT_EXPIRE_DAYS", "30")
    assert auth.session_ttl_seconds() == 30 * 24 * 3600

    # 秒级配置优先于天数
    monkeypatch.setenv("SESSION_TTL_SECONDS", "99")
    monkeypatch.setenv("JWT_EXPIRE_DAYS", "30")
    assert auth.session_ttl_seconds() == 99

    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    monkeypatch.setenv("JWT_EXPIRE_DAYS", "0")
    assert auth.session_ttl_seconds() == auth.DEFAULT_SESSION_TTL_SECONDS

    monkeypatch.delenv("JWT_EXPIRE_DAYS", raising=False)
    assert auth.session_ttl_seconds() == auth.DEFAULT_SESSION_TTL_SECONDS
    assert auth.DEFAULT_SESSION_EXPIRE_DAYS == 365


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("on", True),
        ("TRUE", True),
        ("1", True),
        ("no", False),
        ("false", False),
        ("0", False),
        ("", True),  # 空串 = 默认开启
    ],
)
def test_auth_enforced_parsing(monkeypatch, raw: str, expected: bool):
    monkeypatch.setenv("AUTH_ENFORCED", raw)
    assert auth.auth_enforced() is expected


def test_auth_enforced_default_on_when_unset(monkeypatch):
    monkeypatch.delenv("AUTH_ENFORCED", raising=False)
    assert auth.auth_enforced() is True


def test_role_hierarchy():
    assert has_role(ROLE_ADMIN, ROLE_VIP) is True
    assert has_role(ROLE_ADMIN, ROLE_USER) is True
    assert has_role(ROLE_VIP, ROLE_USER) is True
    assert has_role(ROLE_VIP, ROLE_ADMIN) is False
    assert has_role(ROLE_USER, ROLE_VIP) is False
    assert has_role(None, ROLE_USER) is False
    assert has_role("GOD", ROLE_USER) is False


# --------------------------------------------------------------------------- #
# 4. Cookie 属性
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, scheme="http", forwarded=None):
        self.url = type("URL", (), {"scheme": scheme})()
        self.headers = {"x-forwarded-proto": forwarded} if forwarded else {}


def test_cookie_secure_detection(monkeypatch):
    assert auth.session_cookie_secure(_FakeRequest(scheme="https")) is True
    assert auth.session_cookie_secure(_FakeRequest(scheme="http")) is False
    assert auth.session_cookie_secure(_FakeRequest(forwarded="https")) is True
    assert (auth.session_cookie_secure(_FakeRequest(forwarded="https, http"))) is True

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    assert auth.session_cookie_secure(_FakeRequest(scheme="http")) is True

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    assert auth.session_cookie_secure(_FakeRequest(scheme="https")) is False


def test_samesite_none_forces_secure(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "none")
    assert auth.session_cookie_samesite() == "none"

    class _Recorder:
        def __init__(self):
            self.kwargs = None

        def set_cookie(self, **kwargs):
            self.kwargs = kwargs

    recorder = _Recorder()
    auth.set_session_cookie(recorder, _FakeRequest(scheme="http"), "token", ttl=60)

    assert recorder.kwargs["secure"] is True
    assert recorder.kwargs["samesite"] == "none"


def test_samesite_defaults_to_lax_and_falls_back(monkeypatch):
    assert auth.session_cookie_samesite() == "lax"

    monkeypatch.setenv("SESSION_COOKIE_SAMESITE", "bogus")
    assert auth.session_cookie_samesite() == "lax"


def test_login_sets_hardened_cookie(client, store):
    seed_user("13800138000")
    response = login(client, "13800138000")

    raw_cookie = "\n".join(response.headers.get_list("set-cookie"))
    assert auth.SESSION_COOKIE_NAME in raw_cookie
    assert "HttpOnly" in raw_cookie
    assert "SameSite=lax" in raw_cookie
    assert "Path=/" in raw_cookie
    # 持久登录：默认 365 天 Max-Age（关浏览器再开仍有效）
    assert f"Max-Age={auth.DEFAULT_SESSION_TTL_SECONDS}" in raw_cookie


def test_login_over_https_sets_secure_cookie(store):
    from main import app

    with TestClient(app, base_url="https://wave-money.test") as https_client:
        seed_user("13800138000")
        response = login(https_client, "13800138000")

    raw_cookie = "\n".join(response.headers.get_list("set-cookie"))
    assert "Secure" in raw_cookie


# --------------------------------------------------------------------------- #
# 5. 注册 / 登录 / me / logout
# --------------------------------------------------------------------------- #
def test_register_creates_pending_user(client, store):
    response = register(client, "13800138000")

    assert response.status_code == 200
    body = response.json()
    # 回执形状固定：不含 status / created 等可推断手机号是否存在的字段
    assert set(body) == {"ok", "message"}
    assert "待审批" in body["message"]

    user = asyncio.run(store.get_user_by_phone("13800138000"))
    assert user["status"] == STATUS_PENDING
    assert user["role"] == ROLE_USER
    assert user["approved_at"] is None


def test_register_normalizes_phone(client, store):
    register(client, "+86 138-0013-8000")

    assert asyncio.run(store.get_user_by_phone("13800138000")) is not None


@pytest.mark.parametrize("phone", ["12345", "23800138000", "", "1380013800"])
def test_register_rejects_bad_phone(client, phone: str):
    response = register(client, phone)

    assert response.status_code == 422
    assert "手机号" in response.json()["detail"]


def test_register_rejects_short_password(client):
    response = client.post(
        "/api/auth/register", json={"phone": "13800138000", "password": "123"}
    )

    assert response.status_code == 422


def test_register_does_not_reveal_existing_phone(client, store):
    """重复注册：回执字节一致、不重复建号、不覆盖既有口令。"""
    first = register(client, "13800138000", "first-password")
    second = register(client, "13800138000", "second-password")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    assert len(asyncio.run(store.list_users())) == 1
    assert login(client, "13800138000", "first-password").status_code == 403  # PENDING
    assert login(client, "13800138000", "second-password").status_code == 401  # 未覆盖


def test_pending_user_cannot_login(client, store):
    register(client, "13800138000")
    response = login(client, "13800138000")

    assert response.status_code == 403
    assert "待管理员审批" in response.json()["detail"]
    assert client.cookies.get(auth.SESSION_COOKIE_NAME) is None


def test_rejected_login_message_is_distinct(client, store):
    seed_user("13800138000", status=STATUS_REJECTED)
    response = login(client, "13800138000")

    assert response.status_code == 403
    assert "未通过" in response.json()["detail"]


def test_disabled_login_message_is_distinct(client, store):
    seed_user("13800138000", status=STATUS_DISABLED)
    response = login(client, "13800138000")

    assert response.status_code == 403
    assert "停用" in response.json()["detail"]


def test_unknown_phone_and_wrong_password_are_indistinguishable(client, store):
    seed_user("13800138000")

    unknown = login(client, "13900139000", "whatever")
    wrong = login(client, "13800138000", "whatever")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_approve_then_login_succeeds(client, store):
    register(client, "13800138000")
    assert login(client, "13800138000").status_code == 403

    asyncio.run(store.update_user_status("13800138000", STATUS_APPROVED))

    body = login_ok(client, "13800138000")
    assert body["user"]["phone"] == "13800138000"
    assert body["user"]["status"] == STATUS_APPROVED
    assert body["user"]["status_label"] == "已通过"
    assert body["user"]["role_label"] == "普通用户"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["phone"] == "13800138000"


def test_login_records_last_login_at(client, store):
    seed_user("13800138000")
    login_ok(client, "13800138000")

    assert asyncio.run(store.get_user_by_phone("13800138000"))["last_login_at"] is not None


def test_me_requires_session_even_when_enforcement_off(client):
    """灰度开关关闭时 /me 也必须如实返回 401（前端据此判断登录态）。"""
    assert client.get("/api/auth/me").status_code == 401

    client.cookies.set(auth.SESSION_COOKIE_NAME, "forged-token")
    assert client.get("/api/auth/me").status_code == 401


def test_me_does_not_leak_password_fields(client, store):
    seed_user("13800138000")
    login_ok(client, "13800138000")

    body = client.get("/api/auth/me").json()

    assert "password_hash" not in body
    assert "password_salt" not in body
    assert set(body) == {
        "id",
        "phone",
        "role",
        "role_label",
        "status",
        "status_label",
        "created_at",
        "approved_at",
        "last_login_at",
        "auth_enforced",
    }
    assert body["auth_enforced"] is False


def test_logout_clears_cookie(client, store):
    seed_user("13800138000")
    login_ok(client, "13800138000")
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    # 幂等：重复登出也返回 200
    assert client.post("/api/auth/logout").status_code == 200


def test_auth_config_endpoint_is_public(client, monkeypatch):
    body = client.get("/api/auth/config").json()
    assert body == {"auth_enforced": False, "roles": list(ROLES), "statuses": list(STATUSES)}

    monkeypatch.setenv("AUTH_ENFORCED", "true")
    assert client.get("/api/auth/config").json()["auth_enforced"] is True


# --------------------------------------------------------------------------- #
# 6. 访问矩阵（逐接口 × 逐角色）
# --------------------------------------------------------------------------- #
# 公开只读（AUTH_ENFORCED 下匿名仍 200）
PUBLIC_READ_ENDPOINTS = [
    ("get", "/api/draws", {}),
    ("get", "/api/draws/latest", {}),
    ("get", "/api/history", {}),
    ("get", "/api/zodiac/table", {}),
    ("get", "/api/zodiac/years", {}),
    ("get", "/api/zodiac/years/2026/numbers", {}),
    ("get", "/api/stats/trend", {}),
    ("get", "/api/stats/frequency", {}),
    ("get", "/api/stats/zodiac-trend", {}),
]

# (HTTP 方法, 路径, 请求参数, 最低角色要求) —— 不含公开只读接口
GATED_ENDPOINTS = [
    # 任意已审批用户
    ("get", "/api/draws/next-period", {}, ROLE_USER),
    ("get", "/api/settings", {}, ROLE_USER),
    ("put", "/api/settings", {"json": {"small_max": 12}}, ROLE_USER),
    # VIP+（个人投注能力；普通 USER 不可见）
    ("get", "/api/stats/pnl", {}, ROLE_VIP),
    ("post", "/api/stats/backtest", {}, ROLE_VIP),
    ("post", "/api/recommend", {"json": {"number": 25}}, ROLE_VIP),
    ("get", "/api/export", {}, ROLE_VIP),
    # ADMIN（录入 / 纠正 / 导入写 / 用户管理）
    ("post", "/api/draws/quick", {"json": {"special_number": 22}}, ROLE_ADMIN),
    ("post", "/api/draws/import", {"json": {"text": ""}}, ROLE_ADMIN),
    ("delete", "/api/draws/1", {}, ROLE_ADMIN),
    ("delete", "/api/draws/999", {}, ROLE_ADMIN),
    ("post", "/api/import", {"json": {"draws": []}}, ROLE_ADMIN),
    ("get", "/api/admin/users", {}, ROLE_ADMIN),
    ("post", "/api/admin/users/13900139000/approve", {}, ROLE_ADMIN),
    ("post", "/api/admin/users/13900139000/reject", {}, ROLE_ADMIN),
    ("post", "/api/admin/users/13900139000/role", {"json": {"role": ROLE_VIP}}, ROLE_ADMIN),
    (
        "post",
        "/api/admin/users/13900139000/status",
        {"json": {"status": STATUS_DISABLED}},
        ROLE_ADMIN,
    ),
]

MATRIX_IDS = [f"{method.upper()} {path} [{minimum}]" for method, path, _, minimum in GATED_ENDPOINTS]
PUBLIC_IDS = [f"{method.upper()} {path}" for method, path, _ in PUBLIC_READ_ENDPOINTS]


@pytest.mark.parametrize("role", [ROLE_USER, ROLE_VIP, ROLE_ADMIN])
@pytest.mark.parametrize("method, path, kwargs, minimum", GATED_ENDPOINTS, ids=MATRIX_IDS)
def test_access_matrix(client, store, monkeypatch, role, method, path, kwargs, minimum):
    """开关打开时：角色够 → 放行（不看业务返回码），角色不够 → 403。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=role)
    seed_user("13900139000", role=ROLE_USER)  # 给 admin 端点准备一个目标
    login_ok(client, "13800138000")

    response = getattr(client, method)(path, **kwargs)

    if has_role(role, minimum):
        assert response.status_code not in (401, 403), response.text
    else:
        assert response.status_code == 403, response.text
        assert "权限不足" in response.json()["detail"]


@pytest.mark.parametrize(
    "method, path, kwargs, minimum", GATED_ENDPOINTS, ids=MATRIX_IDS
)
def test_access_matrix_anonymous_is_401(client, store, monkeypatch, method, path, kwargs, minimum):
    """开关打开 + 未登录 → 受保护接口 401（而不是 403）。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    client.cookies.clear()

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401, response.text
    assert "未登录" in response.json()["detail"]


@pytest.mark.parametrize("method, path, kwargs", PUBLIC_READ_ENDPOINTS, ids=PUBLIC_IDS)
def test_public_read_endpoints_allow_anonymous(client, monkeypatch, method, path, kwargs):
    """开关打开 + 无 cookie → 公开只读接口仍 200。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    client.cookies.clear()

    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 200, response.text


def test_public_endpoints_stay_public(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    client.cookies.clear()

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/config").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/draws").status_code == 200
    assert client.get("/api/stats/frequency").status_code == 200
    assert client.get("/api/zodiac/table").status_code == 200


def test_enforcement_off_is_a_pure_noop(client, store):
    """灰度开关关闭：所有受保护接口无需登录即可访问（含管理员接口）。"""
    assert client.get("/api/draws").status_code == 200
    assert client.get("/api/stats/frequency").status_code == 200
    assert client.post("/api/recommend", json={"number": 25}).status_code == 200
    assert client.get("/api/admin/users").status_code == 200
    assert client.get("/api/export").status_code == 200
    assert client.post("/api/draws/quick", json={"special_number": 22}).status_code == 200


def test_pending_and_unapproved_users_never_pass_the_gate(client, store, monkeypatch):
    """非 APPROVED 状态拿不到会话；公开只读仍可用，受保护接口仍 401。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN, status=STATUS_REJECTED)

    assert login(client, "13800138000").status_code == 403
    assert client.get("/api/draws").status_code == 200
    assert client.get("/api/settings").status_code == 401


def test_disabling_user_invalidates_live_session(client, store, monkeypatch):
    """停用立即生效：不必等 Cookie 过期。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")
    assert client.get("/api/auth/me").status_code == 200

    asyncio.run(store.update_user_status("13800138000", STATUS_DISABLED))

    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/draws").status_code == 200  # 公开只读仍可
    assert client.get("/api/settings").status_code == 401


def test_role_change_takes_effect_immediately(client, store, monkeypatch):
    """同一个会话，角色被降级后立刻失去对应权限。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")
    assert client.post("/api/recommend", json={"number": 25}).status_code == 200

    asyncio.run(store.update_user_role("13800138000", ROLE_USER))

    assert client.post("/api/recommend", json={"number": 25}).status_code == 403
    assert client.get("/api/stats/pnl").status_code == 403
    assert client.get("/api/settings").status_code == 200  # USER 仍可用
    assert client.get("/api/stats/frequency").status_code == 200  # 公开只读


def test_admin_can_reach_vip_endpoints(client, store, monkeypatch):
    """ADMIN 是 VIP 的超集。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    assert client.get("/api/stats/pnl").status_code == 200
    assert client.post("/api/stats/backtest", json={}).status_code == 200
    assert client.post("/api/recommend", json={"number": 25}).status_code == 200


def test_user_cannot_reach_vip_personal_endpoints(client, store, monkeypatch):
    """普通 USER 登录后仍不可用 recommend / pnl（铁律）。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_USER)
    login_ok(client, "13800138000")

    assert client.post("/api/recommend", json={"number": 25}).status_code == 403
    assert client.get("/api/stats/pnl").status_code == 403
    assert client.post("/api/stats/backtest", json={}).status_code == 403


def test_tampered_cookie_is_rejected_end_to_end(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    login_ok(client, "13800138000")

    token = client.cookies.get(auth.SESSION_COOKIE_NAME)
    body, _, signature = token.partition(".")
    client.cookies.set(auth.SESSION_COOKIE_NAME, f"{body}.{'A' * len(signature)}")

    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/draws").status_code == 200  # 公开只读不依赖会话


def test_cookie_signed_with_other_secret_is_rejected(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    user = asyncio.run(store.get_user_by_phone("13800138000"))
    client.cookies.set(
        auth.SESSION_COOKIE_NAME, build_session_token(user["id"], "attacker-secret")
    )

    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/draws").status_code == 200


# --------------------------------------------------------------------------- #
# 7. 管理接口
# --------------------------------------------------------------------------- #
def test_admin_list_users_and_status_filter(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", status=STATUS_PENDING)
    seed_user("13700137000", status=STATUS_REJECTED)
    login_ok(client, "13800138000")

    everything = client.get("/api/admin/users").json()
    assert len(everything) == 3
    assert all("password_hash" not in user for user in everything)

    pending = client.get("/api/admin/users", params={"status": STATUS_PENDING}).json()
    assert [user["phone"] for user in pending] == ["13900139000"]


def test_admin_list_rejects_unknown_status_filter(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    response = client.get("/api/admin/users", params={"status": "BOGUS"})

    assert response.status_code == 422
    assert "状态必须是" in response.json()["detail"]


def test_approve_reject_and_role_endpoints(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", status=STATUS_PENDING)
    login_ok(client, "13800138000")

    approved = client.post("/api/admin/users/13900139000/approve").json()
    assert approved["status"] == STATUS_APPROVED
    assert approved["status_label"] == "已通过"
    assert approved["approved_at"] is not None

    promoted = client.post(
        "/api/admin/users/13900139000/role", json={"role": ROLE_VIP}
    ).json()
    assert promoted["role"] == ROLE_VIP
    assert promoted["role_label"] == "VIP用户"

    rejected = client.post("/api/admin/users/13900139000/reject").json()
    assert rejected["status"] == STATUS_REJECTED
    assert rejected["approved_at"] is None  # 离开 APPROVED 时清空

    disabled = client.post(
        "/api/admin/users/13900139000/status", json={"status": STATUS_DISABLED}
    ).json()
    assert disabled["status"] == STATUS_DISABLED


def test_admin_role_endpoint_validates_enum(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000")
    login_ok(client, "13800138000")

    role_response = client.post(
        "/api/admin/users/13900139000/role", json={"role": "SUPERUSER"}
    )
    status_response = client.post(
        "/api/admin/users/13900139000/status", json={"status": "SLEEPING"}
    )

    assert role_response.status_code == 422
    assert status_response.status_code == 422


def test_admin_endpoints_404_for_unknown_phone(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    response = client.post("/api/admin/users/13600136000/approve")

    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_admin_cannot_demote_themselves(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", role=ROLE_ADMIN)  # 有第二个人兜底
    login_ok(client, "13800138000")

    response = client.post(
        "/api/admin/users/13800138000/role", json={"role": ROLE_VIP}
    )

    assert response.status_code == 409
    assert "自己的角色" in response.json()["detail"]
    # 未被改动
    assert asyncio.run(store.count_approved_admins()) == 2


def test_admin_cannot_disable_or_reject_themselves(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    for status in (STATUS_DISABLED, STATUS_REJECTED, STATUS_PENDING):
        response = client.post(
            "/api/admin/users/13800138000/status", json={"status": status}
        )
        assert response.status_code == 409, status
        assert "自己的审批状态" in response.json()["detail"]

    assert client.get("/api/auth/me").status_code == 200  # 会话仍然有效


def test_last_approved_admin_cannot_be_removed(client, store, monkeypatch):
    """另一个管理员来操作也不行：系统必须保留至少一个已审批管理员。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    assert asyncio.run(store.count_approved_admins()) == 1

    demote = client.post("/api/admin/users/13800138000/role", json={"role": ROLE_USER})
    disable = client.post(
        "/api/admin/users/13800138000/status", json={"status": STATUS_DISABLED}
    )

    assert demote.status_code == 409
    assert disable.status_code == 409


def test_second_admin_can_be_demoted(client, store, monkeypatch):
    """有两个管理员时，降级「另一个」是允许的（只要不归零）。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", role=ROLE_ADMIN)
    login_ok(client, "13800138000")

    response = client.post("/api/admin/users/13900139000/role", json={"role": ROLE_USER})

    assert response.status_code == 200
    assert asyncio.run(store.count_approved_admins()) == 1


def test_approve_is_idempotent(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", status=STATUS_PENDING)
    login_ok(client, "13800138000")

    first = client.post("/api/admin/users/13900139000/approve").json()
    second = client.post("/api/admin/users/13900139000/approve").json()

    assert first["status"] == second["status"] == STATUS_APPROVED
    # 幂等：重复审批不会把 approved_at 清空或改坏（时间戳本身允许刷新）
    assert first["approved_at"] is not None
    assert second["approved_at"] is not None


# --------------------------------------------------------------------------- #
# 8. 按用户隔离的 settings
# --------------------------------------------------------------------------- #
def test_settings_are_isolated_between_users(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    seed_user("13900139000")
    seed_user("13600136000", role=ROLE_VIP)

    login_ok(client, "13800138000")
    assert client.put("/api/settings", json={"small_max": 7}).json()["small_max"] == 7

    login_ok(client, "13900139000")
    assert client.get("/api/settings").json()["small_max"] == 10  # 全局默认
    assert client.put("/api/settings", json={"small_max": 3}).json()["small_max"] == 3

    login_ok(client, "13800138000")
    assert client.get("/api/settings").json()["small_max"] == 7  # 未被别人覆盖

    login_ok(client, "13600136000")
    assert client.get("/api/settings").json()["small_max"] == 10

    # 全局模板始终未被污染
    assert asyncio.run(store.get_settings())["small_max"] == 10
    assert asyncio.run(store.get_settings(GLOBAL_SETTINGS_USER_ID))["small_max"] == 10


def test_new_user_inherits_global_default_template(client, store):
    seeded = seed_user("13800138000")
    assert seeded["status"] == STATUS_APPROVED

    defaults = asyncio.run(store.get_settings(seeded["id"]))
    for key, value in repository.DEFAULT_SETTINGS.items():
        assert defaults[key] == value


def test_settings_partial_update_keeps_other_fields(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    login_ok(client, "13800138000")

    client.put("/api/settings", json={"small_max": 5})
    updated = client.put("/api/settings", json={"total_amount": 20}).json()

    assert updated["small_max"] == 5  # 上一次改的没丢
    assert updated["total_amount"] == 20  # 预算真值
    # 派生：20/5=4 单位 ÷ 6 注 = 0 单位 → 0（向下对齐；不足以每注 1 单位）
    assert updated["bet_unit"] == 0
    assert updated["normal_max"] == 30  # 其余来自全局模板


def test_legacy_bet_unit_write_is_accepted_but_ignored(client, store, monkeypatch):
    """旧字段 bet_unit 仍被接收（不 422），但已降级为派生值，写不进去。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    login_ok(client, "13800138000")

    response = client.put("/api/settings", json={"bet_unit": 20})
    assert response.status_code == 200
    body = response.json()
    assert body["total_amount"] == 50  # 预算未被 bet_unit 篡改
    # 派生：50/5=10 单位 ÷ 6 注 = 1 单位 → 5 元（向下对齐到 amount_unit）
    assert body["bet_unit"] == 5


def test_anonymous_settings_use_and_write_global_template(client, store):
    """灰度期（匿名）读写落全局模板，行为与改造前一致。"""
    assert client.get("/api/settings").json()["bet_unit"] == 5
    assert client.get("/api/settings").json()["total_amount"] == 50

    client.put("/api/settings", json={"total_amount": 60})

    assert client.get("/api/settings").json()["total_amount"] == 60
    assert client.get("/api/settings").json()["bet_unit"] == 10  # 60/5=12 单位 ÷ 6 → 10 元
    assert asyncio.run(store.get_settings())["total_amount"] == 60


def test_trend_uses_callers_own_thresholds(client, store, monkeypatch):
    """同一份数据，不同用户因为自己的阈值不同看到不同的波动分布。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000")
    seed_user("13900139000", role=ROLE_VIP)
    asyncio.run(
        store.import_draws(
            [
                {"draw_date": "2026-06-01", "period": 1, "special_number": 1},
                {"draw_date": "2026-06-02", "period": 2, "special_number": 20},
            ]
        )
    )

    login_ok(client, "13800138000")
    assert client.put("/api/settings", json={"small_max": 5, "normal_max": 6}).status_code == 200
    narrow = client.get("/api/stats/trend").json()

    login_ok(client, "13900139000")
    wide = client.get("/api/stats/trend").json()

    assert narrow["settings"]["small_max"] == 5
    assert wide["settings"]["small_max"] == 10
    assert narrow["series"][-1]["wave_type"] == "big"
    assert wide["series"][-1]["wave_type"] == "normal"


def test_backtest_uses_callers_own_settings(client, store, monkeypatch):
    """回测默认取「当前登录用户」自己的配置：改过配置与没改过的看到不同结果。"""
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_VIP)
    seed_user("13900139000", role=ROLE_VIP)
    asyncio.run(
        store.import_draws(
            [
                {"draw_date": "2026-06-01", "period": 1, "special_number": 1},
                {"draw_date": "2026-06-02", "period": 2, "special_number": 20},
                {"draw_date": "2026-06-03", "period": 3, "special_number": 3},
                {"draw_date": "2026-06-04", "period": 4, "special_number": 30},
            ]
        )
    )

    login_ok(client, "13800138000")
    assert (
        client.put(
            "/api/settings", json={"mode": "single", "normal_max": 25}
        ).status_code
        == 200
    )
    mine = client.post("/api/stats/backtest", json={}).json()

    login_ok(client, "13900139000")
    theirs = client.post("/api/stats/backtest", json={}).json()

    # 改过配置的用户：单挑恒 1 注，阈值用自己的 25
    assert mine["settings"]["mode"] == "single"
    assert mine["settings"]["effective_pick_count"] == 1
    assert mine["settings"]["normal_max"] == 25
    assert mine["parameter_sources"]["mode"] == "saved_settings"

    # 没改过的用户：继承全局模板（默认 even / normal_max=30 / 6 注）
    assert theirs["settings"]["mode"] == "even"
    assert theirs["settings"]["effective_pick_count"] == 6
    assert theirs["settings"]["normal_max"] == 30
    assert theirs["parameter_sources"]["normal_max"] == "saved_settings"


def test_export_carries_callers_settings(client, store, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    seed_user("13800138000", role=ROLE_VIP)
    login_ok(client, "13800138000")
    client.put("/api/settings", json={"total_amount": 42})

    exported = client.get("/api/export").json()

    assert exported["settings"]["total_amount"] == 42
    # 只读派生字段与 GET /api/settings 同形（老调用方仍能读到 bet_unit）
    assert exported["settings"]["bet_unit"] == 5  # 42/5=8 单位 ÷ 6 → 5 元
    assert asyncio.run(store.get_settings())["total_amount"] == 50  # 全局未被改


# --------------------------------------------------------------------------- #
# 9. 存储层语义（两种实现必须一致）
# --------------------------------------------------------------------------- #
def test_create_user_returns_none_on_duplicate(client, store):
    assert asyncio.run(store.create_user("13800138000", "h", "s")) is not None
    assert asyncio.run(store.create_user("13800138000", "h2", "s2")) is None
    assert len(asyncio.run(store.list_users())) == 1


def test_store_user_lifecycle(client, store):
    created = asyncio.run(store.create_user("13800138000", "hash", "salt"))
    assert created["role"] == ROLE_USER
    assert created["status"] == STATUS_PENDING
    assert created["approved_at"] is None

    approved = asyncio.run(store.update_user_status("13800138000", STATUS_APPROVED))
    assert approved["approved_at"] is not None

    rejected = asyncio.run(store.update_user_status("13800138000", STATUS_REJECTED))
    assert rejected["approved_at"] is None  # 离开 APPROVED 清空

    assert asyncio.run(store.update_user_role("13800138000", ROLE_ADMIN))["role"] == ROLE_ADMIN
    assert asyncio.run(store.update_user_password("13800138000", "new", "newsalt"))["phone"] == "13800138000"
    assert asyncio.run(store.update_user_status("13600136000", STATUS_APPROVED)) is None
    assert asyncio.run(store.update_user_role("13600136000", ROLE_ADMIN)) is None
    assert asyncio.run(store.update_user_password("13600136000", "h", "s")) is None

    credentials = asyncio.run(store.get_user_credentials("13800138000"))
    assert credentials["password_hash"] == "new"
    assert credentials["password_salt"] == "newsalt"
    assert asyncio.run(store.get_user_credentials("13600136000")) is None


def test_store_count_approved_admins(client, store):
    assert asyncio.run(store.count_approved_admins()) == 0

    seed_user("13800138000", role=ROLE_ADMIN)
    seed_user("13900139000", role=ROLE_ADMIN, status=STATUS_DISABLED)
    seed_user("13700137000", role=ROLE_ADMIN, status=STATUS_PENDING)
    seed_user("13600136000", role=ROLE_VIP)

    assert asyncio.run(store.count_approved_admins()) == 1


def test_touch_user_login(client, store):
    seed_user("13800138000")
    asyncio.run(store.touch_user_login("13800138000"))
    asyncio.run(store.touch_user_login("13600136000"))  # 不存在也不报错

    assert asyncio.run(store.get_user_by_phone("13800138000"))["last_login_at"] is not None


def test_public_user_row_is_a_whitelist():
    row = {
        "id": 1,
        "phone": "13800138000",
        "password_hash": "SECRET-HASH",
        "password_salt": "SECRET-SALT",
        "role": ROLE_USER,
        "status": STATUS_APPROVED,
        "created_at": None,
        "approved_at": None,
        "last_login_at": None,
    }

    assert public_user_row(row) == {
        "id": 1,
        "phone": "13800138000",
        "role": ROLE_USER,
        "status": STATUS_APPROVED,
        "created_at": None,
        "approved_at": None,
        "last_login_at": None,
    }


def test_get_settings_is_backward_compatible(client, store):
    """老调用点不传 user_id，必须仍然读写全局模板。"""
    asyncio.run(store.update_settings({"normal_max": 25, "big_min": 999}))

    settings = asyncio.run(store.get_settings())
    assert settings["normal_max"] == 25
    assert "big_min" not in settings  # 派生字段不落库
    assert asyncio.run(store.get_settings(GLOBAL_SETTINGS_USER_ID)) == settings


def test_user_settings_bucket_is_independent_of_global(client, store):
    asyncio.run(store.update_settings({"small_max": 4}, user_id=1))

    assert asyncio.run(store.get_settings(1))["small_max"] == 4
    assert asyncio.run(store.get_settings(2))["small_max"] == 10
    assert asyncio.run(store.get_settings())["small_max"] == 10


# --------------------------------------------------------------------------- #
# 10. 迁移脚本 / DDL 约束（不需要真实数据库）
# --------------------------------------------------------------------------- #
def test_tests_never_touch_a_real_database(client, store):
    """守卫：测试必须永远跑在 MemoryStore 上，绝不允许连到真实 Supabase。"""
    assert isinstance(store, MemoryStore)
    assert client.get("/api/export").json()["storage"] == "memory"


def test_schema_declares_users_table_and_per_user_settings():
    ddl = "\n".join(repository.SCHEMA_STATEMENTS)

    assert "CREATE TABLE IF NOT EXISTS users" in ddl
    assert "uq_users_phone UNIQUE (phone)" in ddl
    for role in ROLES:
        assert f"'{role}'" in ddl  # CHECK 约束由常量拼出，不手写
    for status in STATUSES:
        assert f"'{status}'" in ddl
    assert "PRIMARY KEY (user_id, key)" in ddl
    assert "ADD COLUMN IF NOT EXISTS user_id" in ddl


def test_schema_ddl_has_no_chinese_enum_values():
    """铁律：落库的枚举值禁止汉字（DDL 里只能是英文/数字）。"""
    ddl = "\n".join(repository.SCHEMA_STATEMENTS)
    for statement in ddl.split("\n"):
        stripped = statement.strip()
        if stripped.startswith("--") or not stripped:
            continue
        # 去掉注释后再查汉字
        code = stripped.split("--")[0]
        assert not any("\u4e00" <= char <= "\u9fff" for char in code), code


def test_settings_seed_uses_global_template_sentinel():
    assert GLOBAL_SETTINGS_USER_ID == 0
    assert GLOBAL_SETTINGS_USER_ID not in {1, 2, 3}  # 不是任何真实用户 id（BIGSERIAL 从 1 开始）


def test_auth_enum_constants_are_english():
    for value in (*ROLES, *STATUSES):
        assert value.isascii()
        assert value == value.upper()


def test_register_message_is_chinese_and_actionable(client):
    from routers.auth import REGISTER_MESSAGE

    body = register(client, "13800138000").json()

    assert body["message"] == REGISTER_MESSAGE
    assert "手机号" in body["message"]
    assert body["ok"] is True
