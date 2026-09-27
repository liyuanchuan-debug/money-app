"""创建 / 提升管理员账号（幂等，可重复执行）—— 自助解锁后台用。

这是**唯一**能在没有管理员的情况下拿到后台权限的入口：直接连库建号，
不走 HTTP，因此不受 ``AUTH_ENFORCED`` 影响，也不会因为「没有管理员」而自锁。

行为（幂等）：

| 情况 | 结果 |
|------|------|
| 手机号不存在 | 新建用户，``role=ADMIN``、``status=APPROVED``（必须给密码） |
| 手机号已存在 | 提升为 ``ADMIN`` + ``APPROVED``；**给了密码才改密码**，没给就不动 |

安全约定：

- **绝不打印 / 记录密码**（只打印脱敏手机号与动作）；
- 密码优先从环境变量取，避免留在 shell 历史与进程列表里。

用法：

    # 方式 A（推荐）：密码走环境变量
    $env:ADMIN_PHONE='13800138000'; $env:ADMIN_PASSWORD='换成你的强密码'
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\create_admin.py

    # 方式 B：命令行参数（密码会留在 shell 历史里，注意清除）
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\create_admin.py --phone 13800138000 --password '换成你的强密码'

    # 仅把既有账号提升为管理员（不改密码）
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\create_admin.py --phone 13800138000 --promote-only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# 基于脚本自身位置推导：<repo>/backend/.env（Linux / Render / Windows 通用）
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH if ENV_PATH.exists() else BACKEND_DIR / ".env")

import asyncpg  # noqa: E402

from repository import PostgresStore  # noqa: E402
from services.auth import (  # noqa: E402
    PASSWORD_MIN_LENGTH,
    ROLE_ADMIN,
    STATUS_APPROVED,
    hash_password,
    is_valid_phone,
    mask_phone,
    normalize_phone,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="创建或提升一个 Wave Money 管理员账号（幂等）",
    )
    parser.add_argument(
        "--phone",
        default=os.getenv("ADMIN_PHONE"),
        help="管理员手机号（默认取环境变量 ADMIN_PHONE）",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD"),
        help="密码（默认取环境变量 ADMIN_PASSWORD；新账号必填，已存在账号可省略）",
    )
    parser.add_argument(
        "--promote-only",
        action="store_true",
        help="只提升角色 / 审批状态，绝不修改密码",
    )
    return parser.parse_args(argv)


async def create_admin(phone: str, password: str | None, promote_only: bool) -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    try:
        store = PostgresStore(pool)
        # 顺带保证 users 表存在（幂等；正常流程里 migrate 脚本已经跑过）
        await store.ensure_schema()

        existing = await store.get_user_by_phone(phone)
        actions: list[str] = []

        if existing is None:
            if promote_only:
                raise SystemExit(
                    f"手机号 {mask_phone(phone)} 不存在，--promote-only 无法创建账号；"
                    "请提供密码。"
                )
            if not password:
                raise SystemExit(
                    "新账号必须提供密码：用 --password 或环境变量 ADMIN_PASSWORD 传入。"
                )
            password_hash, password_salt = hash_password(password)
            created = await store.create_user(
                phone, password_hash, password_salt, role=ROLE_ADMIN
            )
            if created is None:  # 并发下被别的进程抢先建号
                actions.append("并发创建冲突：改为提升既有账号")
            else:
                actions.append("新建账号")
                existing = created

        if existing is not None:
            if existing["role"] != ROLE_ADMIN:
                await store.update_user_role(phone, ROLE_ADMIN)
                actions.append(f"角色 {existing['role']} → {ROLE_ADMIN}")
            else:
                actions.append(f"角色已是 {ROLE_ADMIN}（未改动）")

            if existing["status"] != STATUS_APPROVED:
                await store.update_user_status(phone, STATUS_APPROVED)
                actions.append(f"状态 {existing['status']} → {STATUS_APPROVED}")
            else:
                actions.append(f"状态已是 {STATUS_APPROVED}（未改动）")

            if password and not promote_only:
                password_hash, password_salt = hash_password(password)
                await store.update_user_password(phone, password_hash, password_salt)
                actions.append("密码已重置（未回显）")
            else:
                actions.append("密码未改动")

        final = await store.get_user_by_phone(phone)
        admins = await store.count_approved_admins()
        return {
            "phone": phone,
            "actions": actions,
            "final": final,
            "approved_admins": admins,
        }
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    phone = normalize_phone(args.phone)
    if not is_valid_phone(phone):
        raise SystemExit(
            "手机号非法：请提供 11 位中国大陆手机号（--phone 或环境变量 ADMIN_PHONE）"
        )
    if args.password and len(args.password) < PASSWORD_MIN_LENGTH:
        raise SystemExit(f"密码至少 {PASSWORD_MIN_LENGTH} 位")

    result = asyncio.run(create_admin(phone, args.password, args.promote_only))

    print("管理员账号就绪（幂等）：")
    print(f"  手机号        : {mask_phone(result['phone'])}")
    for action in result["actions"]:
        print(f"  - {action}")
    print(f"  角色 / 状态   : {result['final']['role']} / {result['final']['status']}")
    print(f"  已审批管理员数: {result['approved_admins']}")
    print()
    print("下一步：用该手机号 + 密码调用 POST /api/auth/login 登录，")
    print("        （登录接口不受 AUTH_ENFORCED 影响，任何时候都可用）。")


if __name__ == "__main__":
    main()
