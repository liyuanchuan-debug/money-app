r"""出票单 CLI（选号工具）—— `pick` / `explain` / `simulate` / `verify`。

口径铁律（与 ``services/pick_ticket.py`` 一致，禁止越界表述）：

- 这是一个**出票 / 花费控制 / 留痕复现**工具，**不是**预测工具；本游戏每号等概率，
  赔率 47 时单注期望收益率恒为 ``47 / 49 - 1 = -4.0816%``，与选号方法无关。
- 选号名次与金额分配**全部复用** ``services.lottery`` 的生产引擎，本脚本不重写算法。
- 所有统计只针对「本池已导入的 N 期样本内」，不升格为全量 / 市场结论。
- 写出的票据里 ``claim = "NO_EDGE"``；同 seed + 同数据 + 同设置 ⇒ 逐字节可复现。

子命令：

    pick      生成一张出票单：终端打印 + 落 JSON / TXT / CSV
    explain   打印每个生效设置的来源（默认 / 存储 / 请求覆盖）+ EV 算式
    simulate  按这张票的形状给出诚实的结果分布与期望亏损（默认 208 期）
    verify    用票据里记录的 seed + 数据重算，逐字节核对是否一致

运行（读写真实库时用 backend/.env 的 DATABASE_URL；离线用 --draws-json）：

    cd backend
    .\.venv\Scripts\python.exe scripts\pick_ticket.py pick --budget 50 --pick-count 10
    .\.venv\Scripts\python.exe scripts\pick_ticket.py explain
    .\.venv\Scripts\python.exe scripts\pick_ticket.py simulate --periods 208
    .\.venv\Scripts\python.exe scripts\pick_ticket.py verify --ticket data\tickets\ticket_xxx.json

``backend/data/*`` 已被 .gitignore 忽略，输出默认落在 ``backend/data/tickets/``。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

_ENV_PATH = BACKEND_DIR / ".env"
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)

from services.lottery import (  # noqa: E402
    DEFAULT_SETTINGS,
    MODES,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    TOTAL_AMOUNT_MAX,
    TOTAL_AMOUNT_MIN,
    clamp_settings,
)
from services.pick_ticket import (  # noqa: E402
    SOFT_REASON_LABELS,
    TicketDataError,
    build_ticket,
    explain_effective_settings,
    simulate_ticket,
)

DEFAULT_DRAWS_JSON = ""  # 空 = 用正在运行 / 已配置的库
DEFAULT_OUT_DIR = BACKEND_DIR / "data" / "tickets"


# --------------------------------------------------------------------------- #
# IO 工具
# --------------------------------------------------------------------------- #
def _harden_stdout() -> None:
    """重定向到管道 / 文件时 Windows 用 cp936，个别字符会抛 UnicodeEncodeError。

    刻意**不改编码**（否则 PowerShell 仍按 cp936 解码，反而变成乱码），只把
    ``errors`` 设为 ``replace``：中文照常正确，个别字符退化为 ``?`` 而不会崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except Exception:  # pragma: no cover - 极少数不可重配的流
                pass


def load_draws_from_json(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"开奖数据文件不存在：{file}")
    raw = json.loads(file.read_text(encoding="utf-8"))
    return list(raw)


async def _load_draws_from_store() -> list[dict[str, Any]]:
    from db import close_pool, init_pool
    from repository import get_store, reset_store

    await init_pool()
    try:
        store = await get_store()
        return await store.list_draws()
    finally:
        await close_pool()
        reset_store()


async def _load_settings_from_store() -> dict[str, Any]:
    from db import close_pool, init_pool
    from repository import get_store, reset_store

    await init_pool()
    try:
        store = await get_store()
        return await store.get_settings(None)  # 全局模板（user_id = 0）
    finally:
        await close_pool()
        reset_store()


def resolve_inputs(args: argparse.Namespace) -> dict[str, Any]:
    """解析数据与设置来源。

    返回 ``{"draws", "base", "stored", "source"}``：``base`` 是参与合并的基线设置，
    ``stored`` 只在基线真的来自存储设置时才非空（供 explain 标注来源）。
    """
    if args.draws_json:
        draws = load_draws_from_json(args.draws_json)
        source = f"离线快照 {args.draws_json}"
    else:
        draws = asyncio.run(_load_draws_from_store())
        source = "已配置数据库（store.list_draws）"

    stored: dict[str, Any] | None = None
    if args.settings_json:
        base = json.loads(Path(args.settings_json).read_text(encoding="utf-8"))
        source += f" + 设置文件 {args.settings_json}"
    elif args.draws_json:
        # 离线快照下不连库：退化为仓库默认设置（可用 --settings-json 覆盖）
        base = dict(DEFAULT_SETTINGS)
        source += " + 仓库默认设置"
    else:
        base = asyncio.run(_load_settings_from_store())
        stored = dict(base)
        source += " + 存储设置（全局模板）"
    return {"draws": draws, "base": base, "stored": stored, "source": source}


def ticket_options(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "repeat_number", None) is not None:
        overrides["include_repeat_number"] = args.repeat_number
    if getattr(args, "exclude_repeat_zodiac", None) is not None:
        overrides["exclude_repeat_zodiac"] = args.exclude_repeat_zodiac
    if getattr(args, "stale", None) is not None:
        # 「冷号」开关：开 = 用默认冷号权重，关 = 权重 1.0（不降权，等价旧行为）
        overrides["stale_weight"] = (
            DEFAULT_SETTINGS["stale_weight"] if args.stale else 1.0
        )
    if getattr(args, "lattice", None) is not None:
        overrides["lattice_enabled"] = args.lattice
    return overrides


def build_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    inputs = resolve_inputs(args)
    settings = clamp_settings({**inputs["base"], **ticket_options(args)})
    overrides = {
        "total_amount": args.budget,
        "pick_count": args.pick_count,
        "mode": args.mode,
    }
    try:
        ticket = build_ticket(
            inputs["draws"],
            settings=settings,
            budget=overrides["total_amount"],
            pick_count=overrides["pick_count"],
            seed=args.seed,
            mode=overrides["mode"],
        )
    except TicketDataError as exc:
        raise SystemExit(f"无法出票：{exc}") from exc
    return ticket, inputs["source"]


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def _out_paths(args: argparse.Namespace, ticket: dict[str, Any]) -> dict[str, Path]:
    raw = Path(args.out) if args.out else DEFAULT_OUT_DIR
    if raw.suffix.lower() == ".json":
        raw.parent.mkdir(parents=True, exist_ok=True)
        return {
            "json": raw,
            "text": raw.with_suffix(".txt"),
            "csv": raw.with_suffix(".csv"),
        }
    raw.mkdir(parents=True, exist_ok=True)
    period = ticket.get("data", {}).get("target_period") or "next"
    stem = f"ticket_{period}_{ticket['ticket_id']}"
    return {
        "json": raw / f"{stem}.json",
        "text": raw / f"{stem}.txt",
        "csv": raw / f"{stem}.csv",
    }


def write_ticket_files(ticket: dict[str, Any], paths: dict[str, Path]) -> None:
    paths["json"].write_text(
        json.dumps(ticket, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    paths["text"].write_text(ticket["ticket_text"] + "\n", encoding="utf-8")
    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "number",
                "amount",
                "role",
                "role_label",
                "wave_type",
                "wave_label",
                "zodiac",
                "zodiac_label",
                "soft_reasons",
                "in_lattice_band",
            ]
        )
        for pick in ticket["picks"]:
            writer.writerow(
                [
                    f"{int(pick['number']):02d}",
                    int(pick["amount"]),
                    pick.get("role"),
                    pick.get("role_label"),
                    pick.get("wave_type"),
                    pick.get("wave_label"),
                    pick.get("zodiac"),
                    pick.get("zodiac_label"),
                    "|".join(pick.get("soft_reasons") or []),
                    bool(pick.get("in_lattice_band")),
                ]
            )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_pick(args: argparse.Namespace) -> int:
    ticket, source = build_from_args(args)
    print(f"数据来源：{source}")
    print(ticket["ticket_text"])
    print("")
    print(
        f"selection={ticket['selection']}  claim={ticket['claim']}  "
        f"ticket_id={ticket['ticket_id']}"
    )
    if args.out != "-":
        paths = _out_paths(args, ticket)
        write_ticket_files(ticket, paths)
        print("已写出：")
        for kind, path in paths.items():
            print(f"  {kind:<4} {path}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    inputs = resolve_inputs(args)
    toggles = ticket_options(args)
    overrides = {
        **toggles,
        **{
            key: value
            for key, value in {
                "total_amount": args.budget,
                "pick_count": args.pick_count,
                "mode": args.mode,
            }.items()
            if value is not None
        },
    }
    settings = clamp_settings({**inputs["base"], **toggles})
    if args.budget is not None:
        settings["total_amount"] = args.budget
    if args.pick_count is not None:
        settings["pick_count"] = args.pick_count
    if args.mode:
        settings["mode"] = args.mode
    settings = clamp_settings(settings)

    report = explain_effective_settings(
        settings,
        stored=inputs["stored"],
        overrides=overrides,
    )
    print(f"数据来源：{inputs['source']}")
    print(f"样本：本池已导入 {len(inputs['draws'])} 期")
    print("")
    print("生效设置来源（default / stored_settings / request_override）：")
    for row in report["fields"]:
        flag = "*" if row["changed_from_default"] else " "
        detail = ""
        if row["source"] == "stored_settings":
            detail = f" stored={row['stored_value']!r}"
        elif row["source"] == "request_override":
            detail = f" override={row['override_value']!r}"
        print(
            f" {flag} {row['key']:<26} {row['source']:<18} "
            f"effective={row['effective_value']!r}{detail}"
        )
    print("")
    print(f"EV 算式：{report['ev_per_100_arithmetic']}")
    budget = int(settings["total_amount"])
    odds = float(settings["odds"])
    from services.analytics import NUMBERS

    loss = budget * (1.0 - odds / len(NUMBERS))
    print(
        f"按本预算 {budget} 元、赔率 {odds:g}：期望回报 "
        f"{budget * odds / len(NUMBERS):.2f} 元，期望亏损 {loss:.2f} 元"
    )
    print("（期望值只由赔率决定；换选号方法 / 换权重 / 换注数都不改变它。）")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    if args.ticket:
        ticket = json.loads(Path(args.ticket).read_text(encoding="utf-8"))
        source = f"票据文件 {args.ticket}"
    else:
        ticket, source = build_from_args(args)
    result = simulate_ticket(ticket, periods=args.periods)
    print(f"形状来源：{source}")
    print(
        f"期数 {result['periods']} · {result['orders']} 注 · 每期成本 "
        f"{result['stake_total']} 元 · 赔率 {result['odds']:g}"
    )
    print(
        f"单期命中概率 {result['hit_rate_per_period']:.4%}（= 注数 / 49）"
    )
    print(
        f"{result['periods']} 期内至少命中一次 {result['p_at_least_one_hit_period']:.4%}"
        f" · 一次都没中 {result['p_no_hit_in_periods']:.4e}"
    )
    print(
        f"命中期数期望 {result['hit_periods_mean']:.2f} ± "
        f"{result['hit_periods_sd']:.2f}（二项分布）"
    )
    print("")
    print(f"累计投注期望 {result['expected_staked']:.2f} 元")
    print(f"期望盈亏 {result['expected_profit']:+.2f} 元（即期望亏损 {result['expected_loss']:.2f} 元）")
    print(
        f"盈亏波动 sd {result['profit_sd']:.2f} 元；"
        f"p05 {result['profit_p05']:+.2f} 元 / p95 {result['profit_p95']:+.2f} 元"
    )
    if result["p_profit_positive_normal_approx"] is not None:
        print(
            "整段盈利概率（正态近似）"
            f" {result['p_profit_positive_normal_approx']:.4%}"
        )
    print("")
    print(result["note"])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.ticket)
    if not path.exists():
        raise SystemExit(f"票据文件不存在：{path}")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    inputs = resolve_inputs(args)
    draws = inputs["draws"]
    source = inputs["source"]
    settings = dict(recorded.get("settings") or {})
    try:
        rebuilt = build_ticket(
            draws,
            settings=settings,
            seed=(recorded.get("seed") or {}).get("value"),
        )
    except TicketDataError as exc:
        raise SystemExit(f"无法复算：{exc}") from exc

    data = recorded.get("data") or {}
    print(f"数据来源：{source}")
    print(
        f"票据记录：{data.get('draws_used')} 期 · 最新 {data.get('latest_draw_date')} "
        f"#{data.get('latest_number')} · 目标第 {data.get('target_period')} 期"
    )
    print(f"复算数据：{rebuilt['data']['draws_used']} 期 · 最新 "
          f"{rebuilt['data']['latest_draw_date']} #{rebuilt['data']['latest_number']}")

    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    same_data = (
        data.get("draws_used") == rebuilt["data"]["draws_used"]
        and data.get("latest_number") == rebuilt["data"]["latest_number"]
        and data.get("latest_draw_date") == rebuilt["data"]["latest_draw_date"]
    )
    identical = _dump(recorded) == _dump(rebuilt)
    print("")
    print(f"数据一致：{'是' if same_data else '否'}")
    print(f"逐字节一致：{'是' if identical else '否'}")
    print(f"记录 ticket_id：{recorded.get('ticket_id')}")
    print(f"复算 ticket_id：{rebuilt['ticket_id']}")
    if not identical:
        print("")
        print("差异字段：")
        for key in sorted(set(recorded) | set(rebuilt)):
            if _dump({key: recorded.get(key)}) != _dump({key: rebuilt.get(key)}):
                print(f"  - {key}")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--draws-json",
        default=DEFAULT_DRAWS_JSON,
        help="离线开奖快照（留空 = 读已配置的数据库）",
    )
    parser.add_argument(
        "--settings-json", default="", help="设置覆盖 JSON（与存储设置合并）"
    )


def _add_ticket_shape(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--budget", type=int, default=None, help="本次预算（元）")
    parser.add_argument(
        "--pick-count",
        type=int,
        default=None,
        dest="pick_count",
        help=f"注数 {PICK_COUNT_MIN}-{PICK_COUNT_MAX}",
    )
    parser.add_argument("--mode", choices=MODES, default=None, help="筹码模式")
    parser.add_argument(
        "--seed", default=None, help="种子（不传 = 生产引擎既定名次；传了 = 换一批）"
    )
    parser.add_argument(
        "--repeat-number",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="上期特码（重号）是否留在候选池（不避开、只降权）",
    )
    parser.add_argument(
        "--exclude-repeat-zodiac",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否避开最新一期同肖整组",
    )
    parser.add_argument(
        "--stale",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="冷号软降权开关（关 = 权重 1.0，不降权）",
    )
    parser.add_argument(
        "--lattice",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="预测波动线号码点阵开关（带内优先取号）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pick_ticket",
        description="出票单（选号工具）：花费控制 / 号码卫生 / 覆盖透明 / 留痕复现",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pick = sub.add_parser("pick", help="生成一张出票单")
    _add_common(pick)
    _add_ticket_shape(pick)
    pick.add_argument(
        "--out",
        default="",
        help="输出路径：目录（默认 backend/data/tickets）或 xxx.json；'-' = 不落盘",
    )
    pick.set_defaults(func=cmd_pick)

    explain = sub.add_parser("explain", help="打印生效设置来源 + EV 算式")
    _add_common(explain)
    _add_ticket_shape(explain)
    explain.set_defaults(func=cmd_explain)

    simulate = sub.add_parser("simulate", help="按票据形状给出结果分布与期望亏损")
    _add_common(simulate)
    _add_ticket_shape(simulate)
    simulate.add_argument("--periods", type=int, default=208, help="模拟期数（默认 208）")
    simulate.add_argument("--ticket", default="", help="直接读一张已落盘的票据 JSON")
    simulate.set_defaults(func=cmd_simulate)

    verify = sub.add_parser("verify", help="用 seed + 数据复算票据，逐字节核对")
    _add_common(verify)
    verify.add_argument("--ticket", required=True, help="票据 JSON 路径")
    verify.set_defaults(func=cmd_verify)
    return parser


def main() -> None:
    _harden_stdout()
    parser = build_parser()
    args = parser.parse_args()
    if args.command in ("pick", "explain", "simulate"):
        # 这几个子命令共用形状参数，统一做一次范围校验，避免把非法值带进引擎
        if args.budget is not None and not (
            TOTAL_AMOUNT_MIN <= args.budget <= TOTAL_AMOUNT_MAX
        ):
            raise SystemExit(
                f"--budget 必须在 {TOTAL_AMOUNT_MIN}..{TOTAL_AMOUNT_MAX} 之间"
            )
        if args.pick_count is not None and not (
            PICK_COUNT_MIN <= args.pick_count <= PICK_COUNT_MAX
        ):
            raise SystemExit(
                f"--pick-count 必须在 {PICK_COUNT_MIN}..{PICK_COUNT_MAX} 之间"
            )
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
