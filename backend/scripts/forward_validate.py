r"""前瞻（期外）验证 CLI —— 冻结下一批未开奖期的预测，开奖后再诚实计分。

与 ``scripts/max_fit_analysis.py`` 的关系：那个脚本做的是**回看 / 样本内**分析
（永远可以事后挑选口径），本脚本做的是**前瞻**验证 —— 预测在开奖前冻结，
开奖后只允许读。这是唯一无法被「后见之明」污染的验证。

子命令：

    freeze   冻结接下来 N 个尚未开奖的期（线上引擎 + 均匀对照，可选仅拟合演示行）
    score    对所有「已开奖」的冻结期计分（未开奖的一律 pending，绝不提前计分）
    status   进度：冻结 / 待开奖 / 已计分各几期 + 有效独立样本 + 当前判定 + 完整性
    verify   哈希链 + 数据摘要完整性自检

安全铁律（由 ``services.forward_ledger`` 执行）：

- 目标期已开奖 → **硬报错**（禁止事后补冻，那等于编造预测）；
- 目标期已有记录 → **硬报错**（账本 append-only，禁止覆盖或追加第二条）；
- 冻结记录里的 ``available_length`` = ``period_index`` = 策略实际看到的前缀长度，
  目标期从不出现在输入里（无前视）；
- **一个开奖周期只冻结一期**：同一份已开奖前缀只能产出一个预测（线上引擎是
  确定性的），所以一次冻结多期只是把**同一注重复登记**。这类行会被写死
  ``independent=false`` / ``duplicate_of_period``，计分时排除出有效独立样本
  （``effective_independent_rows``），并打印醒目警告 —— 它们不增加统计功效。

规范工作流（一次开奖只冻结一期）：
    cd backend
    .\.venv\Scripts\python.exe scripts\forward_validate.py freeze --next 1   # ① 开奖前冻结下一期
    .\.venv\Scripts\python.exe scripts\forward_validate.py verify            # ② 立刻校验并提交账本
    #  等待该期开奖、把新开奖写进数据文件 → ③ 计分 → ④ 再冻结下一期
    .\.venv\Scripts\python.exe scripts\forward_validate.py score
    .\.venv\Scripts\python.exe scripts\forward_validate.py status

输出：终端可读表格 + 机器可读 JSON（默认写 ``backend/data/forward_validate_<子命令>.json``，
``backend/data/*`` 被 .gitignore 忽略，因此不会弄脏工作区）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import forward_ledger as FL  # noqa: E402


# --------------------------------------------------------------------------- #
# 输出小工具
# --------------------------------------------------------------------------- #
def _num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.{digits}f}%"


def _p(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_out(command: str) -> str:
    return str(BACKEND_ROOT / "data" / f"forward_validate_{command}.json")


# --------------------------------------------------------------------------- #
# 配置来源
# --------------------------------------------------------------------------- #
def _settings_from_store() -> dict[str, Any]:
    """从线上库读当前生效配置（``Store.get_settings(None)`` = 全局模板）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 依赖缺失时明确报错，不静默
        raise FL.LedgerError("--from-store 需要 python-dotenv（requirements.txt 已含）")
    load_dotenv(str(BACKEND_ROOT / ".env"), override=False)
    try:
        import db  # noqa: PLC0415
        import repository  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise FL.LedgerError(f"--from-store 无法导入后端存储层：{exc}") from exc
    async def _run() -> dict[str, Any]:
        await db.init_pool()
        try:
            store = await repository.get_store()
            return dict(await store.get_settings(None))
        finally:
            await db.close_pool()

    return asyncio.run(_run())


def _resolve_settings(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if getattr(args, "from_store", False):
        return _settings_from_store(), "store(global_template)"
    if getattr(args, "settings_json", None):
        path = Path(args.settings_json)
        payload = json.loads(path.read_text(encoding="utf-8"))
        settings = payload.get("settings", payload)
        return dict(settings), f"settings_json:{path}"
    return FL.load_settings_snapshot(args.settings_snapshot), f"snapshot:{args.settings_snapshot}"


def _counts_by_strategy(score: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        sid: {
            "frozen_rows": block["frozen_rows"],
            "scored_rows": block["scored_rows"],
            "effective_independent_rows": block["effective_independent_rows"],
            "pending": block["pending"],
            "excluded_duplicates": len(block["excluded_duplicates"]),
        }
        for sid, block in score["strategies"].items()
    }


# --------------------------------------------------------------------------- #
# freeze
# --------------------------------------------------------------------------- #
def cmd_freeze(args: argparse.Namespace) -> dict[str, Any]:
    ledger = FL.load_ledger(args.ledger)
    draws = FL.load_draws(args.draws_json)
    settings, source = _resolve_settings(args)
    frozen_periods = [int(record["period"]) for record in ledger.get("records") or []]
    periods = (
        [int(value) for value in args.period]
        if args.period
        else FL.next_undrawn_periods(draws, int(args.next), skip_periods=frozen_periods)
    )
    updated = FL.freeze_periods(
        ledger,
        draws,
        periods,
        settings=settings,
        pick_count=args.pick_count,
        include_fit_demo=not args.no_fit_demo,
    )
    FL.dump_ledger(updated, args.ledger)

    frozen = [record for record in updated["records"] if int(record["period"]) in set(periods)]
    latest_drawn = max(int(draw["period"]) for draw in draws)
    print(
        f"已冻结 {len(frozen)} 期：{sorted(int(r['period']) for r in frozen)}"
        f"（当前最新已开奖第 {latest_drawn} 期；配置来源 {source}）"
    )
    frozen_entries = [
        entry for record in frozen for entry in record["strategies"]
    ]
    new_independent = sum(1 for entry in frozen_entries if entry.get("independent", True))
    print(
        f"本次新增冻结条目 {len(frozen_entries)} 条：独立预测 {new_independent} 条 / "
        f"重复登记 {len(frozen_entries) - new_independent} 条"
        "（只有独立预测会增加有效样本量）"
    )
    skipped = [
        period
        for period in range(latest_drawn + 1, max(periods) + 1)
        if period in set(frozen_periods)
    ]
    if skipped:
        print(
            f"已跳过账本里已冻结的期号 {skipped}（--next 只取尚未冻结的期；"
            "要显式指定请用 --period，append-only 会拒绝重复期）"
        )
    # 开奖数据没更新就冻结 = 策略看不到任何新信息 → 只会重复上一注
    new_periods = {int(record["period"]) for record in frozen}
    first_new_index = next(
        index
        for index, record in enumerate(updated["records"])
        if int(record["period"]) in new_periods
    )
    if first_new_index > 0:
        previous = updated["records"][first_new_index - 1]
        if int(frozen[0]["available_last_period"]) == int(previous["available_last_period"]):
            print(
                "\n提示：本次冻结的可用前缀与上一条记录完全相同（都截止第 "
                f"{previous['available_last_period']} 期）—— 开奖数据没有更新，策略看不到任何"
                "新信息，本次冻结不会产生新的预测。请先把新开奖写进数据文件，再冻结下一期。"
            )
    for record in frozen:
        print(
            f"\n第 {record['period']} 期  冻结于 {record['frozen_at']}"
            f"  可用前缀=第 {record['available_first_period']}…{record['available_last_period']} 期"
            f"（{record['available_length']} 期 = period_index {record['period_index']}）"
        )
        print(f"  data_digest  {record['data_digest'][:16]}…   record_hash {record['record_hash'][:16]}…")
        for entry in record["strategies"]:
            picks = " ".join(f"{int(number):02d}" for number in entry["picks"])
            independent = bool(entry.get("independent", True))
            print(
                f"  ┌ {entry['strategy']:<18s} [{entry['fit_role']}] "
                f"k={entry['pick_count']} 预算={entry['budget']}元 实投={entry['staked_total']}元 赔率={entry['odds']:g}"
            )
            print(f"  └ 选号 {picks}")
            print(f"      逐注金额 {entry['amounts']}")
            print(
                f"      prediction_digest {str(entry.get('prediction_digest', ''))[:16]}…  "
                f"independent={str(independent).lower()}"
                + (
                    ""
                    if independent
                    else f"  duplicate_of_period={entry.get('duplicate_of_period')}"
                )
            )
            if entry.get("fit_role") == FL.FIT_ROLE_FIT_ONLY:
                print("      ! 仅拟合演示（FIT_ONLY），不是预测，禁止进入线上推荐路径")

    duplicates = [
        (int(record["period"]), str(entry["strategy"]), entry.get("duplicate_of_period"))
        for record in frozen
        for entry in record["strategies"]
        if not entry.get("independent", True)
    ]
    warning: str | None = None
    if duplicates:
        warning = (
            "一次性冻结多期产生了「同一策略下的重复预测」：这些行不计入有效独立样本，"
            "不增加统计功效。同一份已开奖前缀只能产出一个预测，一次冻结多期只是把同一注"
            "重复登记。规范做法是每个开奖周期只冻结 1 期：freeze --next 1 → 等待开奖 → "
            "再冻结下一期。"
        )
        print("\n" + "!" * 96)
        print("! 警告：" + warning)
        for period, sid, first in duplicates:
            print(
                f"!   {sid:<20s} 第 {period} 期 = 第 {first} 期的同一注"
                "（picks / amounts / settings 逐字相同）"
            )
        print("!   这些行已写死 independent=false，score 会按 DUPLICATE_PREDICTION 排除。")
        print("!" * 96)

    artifact = {
        "command": "freeze",
        "frozen_periods": sorted(int(r["period"]) for r in frozen),
        "latest_drawn_period": latest_drawn,
        "settings_source": source,
        "settings": settings,
        "ledger": str(args.ledger),
        "duplicate_predictions": [
            {"period": period, "strategy": sid, "duplicate_of_period": first}
            for period, sid, first in duplicates
        ],
        "warning": warning,
        "records": frozen,
    }
    _write_json(Path(args.out), artifact)
    print(f"\n记录数：{len(updated['records'])}；账本：{args.ledger}；JSON 产物：{args.out}")
    print("提示：冻结后请立刻提交账本文件（git add backend/forward_ledger/ledger.json），"
          "提交时间就是「预测早于开奖」的旁证。")
    return artifact


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def _print_score(score: dict[str, Any]) -> None:
    print(f"口径：{score['scope']}；已计分期 {score['scored_periods']}；待开奖期 {score['pending_periods']}")
    print(
        f"样本量口径：冻结条目 {score['frozen_rows_total']} 条（独立预测 "
        f"{score['independent_rows_frozen']} 条 / 重复登记 {score['duplicate_rows_frozen']} 条）；"
        f"有效独立样本（已计分口径）{score['effective_independent_rows_total']} 条"
    )
    header = (
        f"{'策略':<22s}{'冻结':>6s}{'已计分':>7s}{'有效独立':>9s}{'待开奖':>7s}"
        f"{'命中率':>9s}{'基线':>9s}{'平均排名':>9s}{'log-loss':>10s}{'Brier':>9s}"
        f"{'投入':>8s}{'回收':>9s}{'净盈亏':>9s}{'p(>随机)':>10s}"
    )
    print(header)
    print("-" * len(header))
    for sid, block in score["strategies"].items():
        print(
            f"{sid:<22s}{block['frozen_rows']:>6d}{block['scored_rows']:>7d}"
            f"{block['effective_independent_rows']:>9d}{block['pending']:>7d}"
            f"{_pct(block['hit_rate']):>9s}{_pct(block['baseline_hit_rate']):>9s}"
            f"{_num(block['mean_rank'], 2):>9s}{_num(block['log_loss']):>10s}"
            f"{_num(block['brier']):>9s}{_num(block['total_spend'], 1):>8s}"
            f"{_num(block['total_return'], 1):>9s}{_num(block['net_pnl'], 1):>9s}"
            f"{_p(block['binomial_p_greater']):>10s}"
        )
    print(
        "\n均匀参考：命中率 = k/49、平均排名 25.0、log-loss ln49≈3.8918、"
        "Brier (1/49)(1-1/49)≈0.0200"
    )
    print(
        "列口径：命中率 / 基线 / 平均排名 / log-loss / Brier / p(>随机) 只按「有效独立」"
        "（prediction_digest 去重后）计算；投入 / 回收 / 净盈亏 是全部已计分行的真实收支。"
    )
    for sid, block in score["strategies"].items():
        excluded = block["excluded_duplicates"]
        if excluded:
            print(
                f"\n[{sid}] 已排除 {len(excluded)} 条重复预测：理由 DUPLICATE_PREDICTION"
                f" —— 不计入有效独立样本（已计分 {block['scored_rows']} 条中可用 "
                f"{block['effective_independent_rows']} 条）"
            )
            for item in excluded:
                print(
                    f"  - 第 {item['period']} 期 与第 {item['duplicate_of_period']} 期逐字相同"
                    f"（{'命中' if item['hit'] else '未命中'}，该行不参与统计）"
                )
    for sid, block in score["strategies"].items():
        verdict = block.get("verdict") or {}
        power = block.get("power") or {}
        print(f"\n[{sid}] 判定：{verdict.get('kind', 'n/a')} —— {verdict.get('label', verdict.get('text', 'n/a'))}")
        if verdict.get("text"):
            print(f"  {verdict['text']}")
        for note in block.get("notes") or []:
            print(f"  [样本量] {note}")
        if power.get("minimum_detectable_delta") is not None:
            print(
                f"  最小可检测 delta = {_pct(power['minimum_detectable_delta'])}（有效独立样本 "
                f"{power['evaluated']} 条，alpha={power['alpha']}，功效={power['power']}）；"
                f"要检出 +5% 绝对优势约需 {power['required_draws'].get('+5%')} 期、"
                f"+2% 约需 {power['required_draws'].get('+2%')} 期"
            )
        else:
            print(
                f"  数据不足（evidence_status={block.get('evidence_status')}）："
                "尚无足够的有效独立样本，无法估计最小可检测 delta / 所需期数。"
            )


def cmd_score(args: argparse.Namespace) -> dict[str, Any]:
    ledger = FL.load_ledger(args.ledger)
    draws = FL.load_draws(args.draws_json)
    score = FL.score_ledger(ledger, draws, alpha=args.alpha, power=args.power)
    _print_score(score)
    artifact = {
        "command": "score",
        "ledger": str(args.ledger),
        "draws_json": str(args.draws_json),
        "alpha": args.alpha,
        "power": args.power,
        **score,
    }
    _write_json(Path(args.out), artifact)
    print(f"\nJSON 产物：{args.out}")
    return artifact


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    ledger = FL.load_ledger(args.ledger)
    draws = FL.load_draws(args.draws_json)
    payload = FL.status_payload(ledger, draws)
    records = ledger.get("records") or []

    print(f"账本：{args.ledger}")
    print(
        f"冻结 {len(records)} 期 | 已计分 {len(payload['scored_periods'])} 期 "
        f"{payload['scored_periods']} | 待开奖 {len(payload['pending_periods'])} 期 "
        f"{payload['pending_periods']}"
    )
    print(
        f"有效独立样本：已计分口径 {payload['effective_independent_rows_total']} 条"
        f"（冻结口径 共 {payload['frozen_rows_total']} 条 = 独立预测 "
        f"{payload['independent_rows_frozen']} 条 + 重复登记 {payload['duplicate_rows_frozen']} 条）"
    )
    print(
        "  按策略："
        + "；".join(
            f"{sid} 冻结 {block['frozen_rows']} / 已计分 {block['scored_rows']}"
            f" / 有效独立 {block['effective_independent_rows']}"
            for sid, block in payload["strategies"].items()
        )
    )
    if records:
        frozen_at = {int(r["period"]): r["frozen_at"] for r in records}
        for period in sorted(frozen_at):
            print(f"  第 {period} 期：冻结于 {frozen_at[period]}")
    print(f"完整性：{'OK' if payload['integrity_ok'] else 'FAILED'}（链根 {payload['chain_root'][:16]}…）")
    if payload["integrity_problems"]:
        for problem in payload["integrity_problems"]:
            print(f"  [FAIL] {problem}")
    _print_score(payload)
    artifact = {
        "command": "status",
        "ledger": str(args.ledger),
        "counts_by_strategy": _counts_by_strategy(payload),
        **{key: value for key, value in payload.items() if key != "strategies"},
        "strategies": {
            sid: {key: value for key, value in block.items() if key != "rows"}
            for sid, block in payload["strategies"].items()
        },
    }
    _write_json(Path(args.out), artifact)
    print(f"\nJSON 产物：{args.out}")
    return artifact


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    ledger = FL.load_ledger(args.ledger)
    draws = FL.load_draws(args.draws_json)
    result = FL.verify_ledger(ledger, draws)
    print(f"账本：{args.ledger}")
    print(
        f"记录 {result['records']} 条；链根（最后一条 record_hash）：{result['chain_root']}"
    )
    print(f"创世哈希：{result['genesis_hash']}")
    print(f"结果：{'OK —— 哈希链与数据摘要全部一致' if result['ok'] else 'FAILED'}")
    for detail in result["details"]:
        mark = "[OK]  " if detail["ok"] else "[FAIL]"
        print(f"  {mark} 第 {detail['period']} 期  record_hash {str(detail['record_hash'])[:16]}…")
        for problem in detail["problems"]:
            print(f"      - {problem}")
    artifact = {
        "command": "verify",
        "ledger": str(args.ledger),
        "draws_json": str(args.draws_json),
        **result,
    }
    _write_json(Path(args.out), artifact)
    print(f"JSON 产物：{args.out}")
    if not result["ok"]:
        raise SystemExit(2)
    return artifact


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", default=str(FL.DEFAULT_LEDGER_PATH), help="账本 JSON 路径")
    parser.add_argument(
        "--draws-json",
        default=str(BACKEND_ROOT / "data" / "draws_70_279.json"),
        help="开奖数据 JSON（backend/data/* 被 .gitignore 忽略）",
    )
    parser.add_argument("--out", default=None, help="JSON 产物路径（默认 backend/data/forward_validate_<子命令>.json）")


def _configure_stdout() -> None:
    """Windows 控制台常是 GBK：把不可编码字符降级为 ``?``，绝不因打印而崩。"""
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - 老解释器 / 非标准流
        pass


def main(argv: Sequence[str] | None = None) -> None:
    _configure_stdout()
    parser = argparse.ArgumentParser(
        description="前瞻（期外）验证：开奖前冻结预测，开奖后诚实计分"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="冻结接下来 N 个尚未开奖的期")
    freeze.add_argument("--next", type=int, default=1, help="冻结接下来 N 个尚未开奖、且尚未冻结的期（默认 1）")
    freeze.add_argument("--period", type=int, action="append", help="显式指定期号（可重复；须晚于最新已开奖期）")
    freeze.add_argument("--settings-snapshot", default=str(FL.DEFAULT_SETTINGS_SNAPSHOT_PATH))
    freeze.add_argument("--settings-json", default=None, help="自定义配置 JSON（含 settings 键或直接就是配置对象）")
    freeze.add_argument("--from-store", action="store_true", help="从线上库读当前生效配置（Store.get_settings(None)）")
    freeze.add_argument("--pick-count", type=int, default=None, help="覆盖注数（默认取配置 pick_count）")
    freeze.add_argument("--no-fit-demo", action="store_true", help="不冻结 max_fit 的 FIT_ONLY 演示行")
    _add_common(freeze)

    score = sub.add_parser("score", help="对所有已开奖的冻结期计分")
    score.add_argument("--alpha", type=float, default=FL.DEFAULT_ALPHA)
    score.add_argument("--power", type=float, default=FL.DEFAULT_POWER)
    _add_common(score)

    status = sub.add_parser("status", help="进度 + 当前判定 + 完整性")
    _add_common(status)

    verify = sub.add_parser("verify", help="哈希链 + 数据摘要完整性自检")
    _add_common(verify)

    args = parser.parse_args(argv)
    if args.out is None:
        args.out = _default_out(args.command)

    handlers = {
        "freeze": cmd_freeze,
        "score": cmd_score,
        "status": cmd_status,
        "verify": cmd_verify,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
