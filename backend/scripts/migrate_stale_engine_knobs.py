"""收敛 ``settings`` 表里残留的「旧引擎旋钮」—— 幂等迁移，可重复执行。

背景（2026-10-07 引擎改动）
---------------------------
引擎默认旋钮回到**代码默认**：``small_max=10`` / ``normal_max=30`` /
``trend_bias=neutral`` / ``exclude_repeat_zodiac=false``；点阵默认**关闭**
（``lattice_enabled=false``）；并新增 ``wave_alloc=balanced``（非空波动桶均分 + 桶内
最远点优先，避免 10 注挤在同一连续区段）。

全局模板（``user_id = 0``）已由 commit 1184209 + 本次改动收敛到新默认。但
``settings`` 是**按用户隔离**的：``get_settings(user_id)`` 会先用全局模板打底、
再用该用户自己的行**覆盖**。因此只要某个用户的行里还留着旧值，
``POST /api/recommend`` 在**登录态**下就仍旧跑旧引擎 —— 同一个人在 UI 里点「生成财富密码」
看到的还是旧号码（匿名 / 灰度路径读全局模板，所以看起来「后端已经好了」）。

本脚本区分两类残留
------------------
1. **已知默认值漂移**（``lattice_enabled`` / ``wave_alloc``）：旧引擎默认
   ``lattice_enabled=true``，新默认 ``false``；``wave_alloc`` 是新键，缺省即继承全局
   ``balanced``。这类**默认迁移**。
2. **用户自己改过的值**：存量的「线上配置」
   （``small_max=15`` / ``normal_max=20`` / ``trend_bias=mid`` /
   ``exclude_repeat_zodiac=true``，见 ``docs/settings_sensitivity.md`` 与
   ``forward_ledger/production_settings.json``）**默认一律不动**，只在报告里列出；
   必须显式加 ``--reset-legacy-live-config`` 才覆盖（这就是「先问过用户」的那一步）。

写入前会把受影响的行**整行备份**到
``backend/data/settings_backup_engine_knobs_<UTC时间戳>.json``（可回滚）；
``--dry-run`` 只报告不写入。

用法::

    cd backend
    .\\.venv\\Scripts\\python.exe scripts\\migrate_stale_engine_knobs.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts\\migrate_stale_engine_knobs.py
    # 仅某一行：--user-id 9
    # 点名把某个用户对齐代码默认（只动 small_max / normal_max / exclude_repeat_zodiac）：
    .\\.venv\\Scripts\\python.exe scripts\\migrate_stale_engine_knobs.py --align-user-id 9 --dry-run
    .\\.venv\\Scripts\\python.exe scripts\\migrate_stale_engine_knobs.py --align-user-id 9
    # 显式确认后连「线上配置」一起收敛：
    .\\.venv\\Scripts\\python.exe scripts\\migrate_stale_engine_knobs.py --reset-legacy-live-config
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# 显式路径：脚本可能从任意工作目录启动（与同目录其它 migrate_*.py 同口径）
ENV_PATH = Path(r"D:\myproject\wave-money\backend\.env")
load_dotenv(ENV_PATH if ENV_PATH.exists() else BACKEND_DIR / ".env")

import asyncpg  # noqa: E402

from services.auth import GLOBAL_SETTINGS_USER_ID  # noqa: E402
from services.lottery import DEFAULT_SETTINGS  # noqa: E402

BACKUP_DIR = BACKEND_DIR / "data"

# --- 第 1 类：已知默认值漂移（默认迁移）---------------------------------------
DRIFT_DEFAULTS: dict[str, object] = {
    "lattice_enabled": DEFAULT_SETTINGS["lattice_enabled"],  # False
    "wave_alloc": DEFAULT_SETTINGS["wave_alloc"],  # "balanced"
}
# ``wave_alloc`` 是新键：缺失即继承全局模板。只有**显式写了非默认值**才算漂移，
# 且那种写法也可能是用户主动选的（设置页已暴露 drain/balanced），故默认不动。
WAVE_ALLOC_OPT_IN = "wave_alloc"

# --- 第 2 类：存量「线上配置」（默认只报告，不覆盖）--------------------------
LEGACY_LIVE_CONFIG: dict[str, object] = {
    "small_max": 15,
    "normal_max": 20,
    "trend_bias": "mid",
    "exclude_repeat_zodiac": True,
}
LEGACY_RESET: dict[str, object] = {
    "small_max": DEFAULT_SETTINGS["small_max"],  # 10
    "normal_max": DEFAULT_SETTINGS["normal_max"],  # 30
    "trend_bias": DEFAULT_SETTINGS["trend_bias"],  # neutral
    "trend_bias_explicit": False,  # 清掉「手动设置过走势加权」标记
    "exclude_repeat_zodiac": DEFAULT_SETTINGS["exclude_repeat_zodiac"],  # False
}

# --- 第 3 类：点名把某个用户对齐到代码默认（显式 ``--align-user-id N``）--------
# 只动这 3 个旋钮，且只在「值确实不同」时才写；其余字段（mode / pick_count /
# total_amount / amount_unit / odds / trend_bias / lattice_enabled / wave_alloc …）
# 一律不碰。存量「线上配置」签名要求 trend_bias=mid，用户 9 的 trend_bias 已是
# neutral，所以它**不**会被 ``--reset-legacy-live-config`` 命中 —— 这正是需要
# 这个点名开关的原因。
CODE_DEFAULTS_ALIGNMENT: dict[str, object] = {
    "small_max": DEFAULT_SETTINGS["small_max"],  # 10
    "normal_max": DEFAULT_SETTINGS["normal_max"],  # 30
    "exclude_repeat_zodiac": DEFAULT_SETTINGS["exclude_repeat_zodiac"],  # False
}


def _decode(raw: str):
    """settings.value 是 json.dumps 后的文本（bool → ``true``，字符串带引号）。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def _load_rows(conn) -> dict[int, dict[str, object]]:
    rows = await conn.fetch("SELECT user_id, key, value FROM settings ORDER BY user_id, key")
    table: dict[int, dict[str, object]] = {}
    for row in rows:
        table.setdefault(int(row["user_id"]), {})[row["key"]] = _decode(row["value"])
    return table


def _is_legacy_live(row: dict[str, object]) -> bool:
    """该行是否整套命中存量「线上配置」签名。"""
    return all(row.get(key) == value for key, value in LEGACY_LIVE_CONFIG.items())


def _plan(
    table: dict[int, dict[str, object]],
    *,
    user_id: int | None,
    reset_legacy: bool,
    include_wave_alloc: bool,
    align_user_id: int | None = None,
) -> list[tuple[int, str, object, object, str]]:
    """算出 (user_id, key, before, after, reason) 迁移计划（纯函数，便于 --dry-run）。"""
    plan: list[tuple[int, str, object, object, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(uid: int, key: str, before: object, after: object, reason: str) -> None:
        if before == after or (uid, key) in seen:
            return
        seen.add((uid, key))
        plan.append((uid, key, before, after, reason))

    for uid in sorted(table):
        if uid == GLOBAL_SETTINGS_USER_ID:
            continue  # 全局模板是参照系，不迁移它自己
        if user_id is not None and uid != int(user_id):
            continue
        row = table[uid]

        # 0) 点名对齐代码默认（--align-user-id）：只动 CODE_DEFAULTS_ALIGNMENT 的 3 个键
        if align_user_id is not None and uid == int(align_user_id):
            for key, after in CODE_DEFAULTS_ALIGNMENT.items():
                if key in row:
                    add(uid, key, row[key], after, "CODE_DEFAULTS_ALIGN")

        # 1) lattice_enabled：旧默认 true → 新默认 false
        if "lattice_enabled" in row and row["lattice_enabled"] != DRIFT_DEFAULTS["lattice_enabled"]:
            add(uid, "lattice_enabled", row["lattice_enabled"],
                DRIFT_DEFAULTS["lattice_enabled"], "KNOWN_DEFAULT_DRIFT")

        # 2) wave_alloc：只有显式非默认值才需要考虑（缺省=继承全局，无需动作）
        if include_wave_alloc and WAVE_ALLOC_OPT_IN in row:
            if row[WAVE_ALLOC_OPT_IN] != DRIFT_DEFAULTS[WAVE_ALLOC_OPT_IN]:
                add(uid, WAVE_ALLOC_OPT_IN, row[WAVE_ALLOC_OPT_IN],
                    DRIFT_DEFAULTS[WAVE_ALLOC_OPT_IN], "EXPLICIT_NON_DEFAULT")

        # 3) 存量线上配置：默认只报告（这里不进计划），显式确认才进计划
        if reset_legacy and _is_legacy_live(row):
            for key, after in LEGACY_RESET.items():
                add(uid, key, row.get(key), after, "LEGACY_LIVE_CONFIG_RESET")
    return plan


async def _apply(conn, plan) -> None:
    async with conn.transaction():
        for uid, key, _before, after, _reason in plan:
            await conn.execute(
                "UPDATE settings SET value = $3 WHERE user_id = $1 AND key = $2",
                int(uid),
                key,
                json.dumps(after),
            )


async def migrate(
    *,
    dry_run: bool,
    user_id: int | None,
    reset_legacy: bool,
    include_wave_alloc: bool,
    align_user_id: int | None = None,
) -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        before_table = await _load_rows(conn)
        plan = _plan(
            before_table,
            user_id=user_id,
            reset_legacy=reset_legacy,
            include_wave_alloc=include_wave_alloc,
            align_user_id=align_user_id,
        )

        # 只报告、不覆盖的「用户自定义」清单：存量线上配置里那些小/常阈值等
        custom_report: list[tuple[int, dict[str, object]]] = []
        for uid in sorted(before_table):
            if uid == GLOBAL_SETTINGS_USER_ID:
                continue
            if user_id is not None and uid != int(user_id):
                continue
            if _is_legacy_live(before_table[uid]):
                custom_report.append((uid, {
                    key: before_table[uid].get(key) for key in LEGACY_LIVE_CONFIG
                }))

        backup_path = None
        if plan and not dry_run:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = BACKUP_DIR / f"settings_backup_engine_knobs_{stamp}.json"
            touched = sorted({int(uid) for uid, *_ in plan})
            backup_path.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "reason": "migrate_stale_engine_knobs.py",
                        "rows_before": {
                            str(uid): before_table[uid] for uid in touched
                        },
                        "plan": [
                            {"user_id": uid, "key": key, "before": before,
                             "after": after, "reason": reason}
                            for uid, key, before, after, reason in plan
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            await _apply(conn, plan)

        after_table = await _load_rows(conn)
        return {
            "rows_before": before_table,
            "plan": plan,
            "custom_report": custom_report,
            "backup_path": str(backup_path) if backup_path else None,
            "after_table": after_table,
        }
    finally:
        await conn.close()


def _fmt(value: object) -> str:
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument("--user-id", type=int, default=None, help="只处理某个 user_id")
    parser.add_argument(
        "--reset-legacy-live-config",
        action="store_true",
        help=(
            "显式确认：连存量「线上配置」（small_max=15 / normal_max=20 / "
            "trend_bias=mid / exclude_repeat_zodiac=true）一起收敛成代码默认。"
            "不加这个开关时这些值一律保留。"
        ),
    )
    parser.add_argument(
        "--include-wave-alloc",
        action="store_true",
        help="把显式写成非默认值的 wave_alloc 也收敛回 balanced",
    )
    parser.add_argument(
        "--align-user-id",
        type=int,
        default=None,
        metavar="N",
        help=(
            "点名把某个用户对齐到代码默认：只改 small_max→10 / normal_max→30 / "
            "exclude_repeat_zodiac→false 这三个键，其余字段（mode / pick_count / "
            "trend_bias / lattice_enabled / wave_alloc 等）一律不动。"
            "不给 --user-id 时作用域即该用户；幂等，可重复执行。"
        ),
    )
    args = parser.parse_args()

    if args.align_user_id is not None:
        if int(args.align_user_id) == GLOBAL_SETTINGS_USER_ID:
            raise SystemExit("--align-user-id 不允许指向全局模板 user_id=0")
        if args.user_id is not None and int(args.user_id) != int(args.align_user_id):
            raise SystemExit(
                "--align-user-id 与 --user-id 指向不同用户；请只保留一个作用域"
            )
        # 点名对齐时把作用域收敛到该用户，避免顺手迁移到别人
        args.user_id = int(args.align_user_id)

    result = asyncio.run(
        migrate(
            dry_run=args.dry_run,
            user_id=args.user_id,
            reset_legacy=args.reset_legacy_live_config,
            include_wave_alloc=args.include_wave_alloc,
            align_user_id=args.align_user_id,
        )
    )

    print("settings 表（迁移前）按 user_id：")
    for uid in sorted(result["rows_before"]):
        tag = "（全局模板，参照系）" if uid == GLOBAL_SETTINGS_USER_ID else ""
        print(f"  user_id={uid}: {len(result['rows_before'][uid])} 个键 {tag}")

    print("\n待迁移（已知默认值漂移 / 显式确认项）：")
    if not result["plan"]:
        print("  （无 —— 已经是新默认，幂等：本次不改任何数据）")
    for uid, key, before, after, reason in result["plan"]:
        print(f"  user_id={uid} {key}: {_fmt(before)} → {_fmt(after)}  [{reason}]")

    print("\n保留不动的「用户自定义 / 存量线上配置」（默认不覆盖）：")
    if not result["custom_report"]:
        print("  （无）")
    for uid, values in result["custom_report"]:
        print(f"  user_id={uid}: {_fmt(values)}")
    if result["custom_report"] and not args.reset_legacy_live_config:
        print(
            "  提示：这些是用户自己改过的值，本脚本**没有**覆盖。"
            "若确认要一并收敛成代码默认，请显式加 --reset-legacy-live-config 再跑一次。"
        )

    if args.dry_run:
        print("\n--dry-run：未写库。")
    elif result["backup_path"]:
        print(f"\n已写库。迁移前整行备份：{result['backup_path']}")
    else:
        print("\n无需写库（无漂移）。")

    print("\n生效配置复核（全局模板 + 各行覆盖后的关键旋钮）：")


async def _verify():
    url = os.getenv("DATABASE_URL")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        table = await _load_rows(conn)
        base = dict(table.get(GLOBAL_SETTINGS_USER_ID, {}))
        for uid in sorted(table):
            if uid == GLOBAL_SETTINGS_USER_ID:
                continue
            row = base.copy()
            row.update(table[uid])
            keys = ("small_max", "normal_max", "trend_bias", "exclude_repeat_zodiac",
                    "lattice_enabled", "wave_alloc", "pick_count", "mode")
            print(f"  user_id={uid}: " + ", ".join(f"{k}={_fmt(row.get(k))}" for k in keys))
    finally:
        await conn.close()


if __name__ == "__main__":
    main()
    asyncio.run(_verify())
