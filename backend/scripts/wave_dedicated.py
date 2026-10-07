r"""波动法（wave）**专项**评估 CLI —— 纯离线，不写数据库、不改线上配置。

============================================================================
这个脚本回答什么
============================================================================
问题原话：「我就用特码走势，波动法，也不行吗」。

上一轮 ``scripts/dist_audit.py`` 把 23 条假设放进**同一个** Holm 家族，结果
``survivor_count = 0``；家族里最极端的一行是 ``wave_lattice``（180 期命中 48 次 =
26.67%，置换零均值 36.6885、z = 2.1203、原始 p = 0.0285、Holm 后 0.6552）。

「23 条里最好的一条」是**统计论证**，不是对波动法本身的测量。本脚本因此做一次
**专门的、预先登记的、做过稳健性检查的**评估：

    10|1. **不继承**那 23 条假设的惩罚（家族不同）；
2. 但**如实计入**波动法自己能试多少种变体，用 ``services.analytics.holm_adjusted_p``
   校正（复用，不重写）；
3. 关键检验是**同一变体网格上的 max 零分布**（选择性推断的正确尺度）；
4. 对半 / 三分 / 滚动窗稳定性 + 严格 walk-forward 四把尺子（命中率 / 平均排名 /
   log-loss / Brier）；
5. 精确样本量算术：要多少期、多少个月才能把「真的 / 假的」讲到能定论。

实现细节全部在 ``services/wave_study.py``（纯函数，无 IO）；本脚本只做编排、
多进程置换与 JSON 落盘。

============================================================================
子命令
============================================================================
    # 1) 完整专项研究（预登记 + 变体网格 + max 零分布 + 稳定性 + 功效）
    cd backend; .\.venv\Scripts\python.exe scripts\wave_dedicated.py study `
        --draws-json data\draws_70_279.json --out ..\.tmp-wave\wave_study.json `
        --perms 200 --jobs 12

    # 2) 只跑置换矩阵（网格命中 × perms），可复用到别的分析
    cd backend; .\.venv\Scripts\python.exe scripts\wave_dedicated.py matrix `
    30|        --draws-json data\draws_70_279.json --out ..\.tmp-wave\wave_matrix.json --perms 200

    # 3) 点阵 / 重肖开关的两种配置并排消融（审计快照 vs 线上快照）
    cd backend; .\.venv\Scripts\python.exe scripts\wave_dedicated.py ablation `
        --draws-json data\draws_70_279.json --out ..\.tmp-wave\wave_ablation.json `
        --live-settings-json ..\.tmp-wave\live_settings.json

    # 4) 白话解释（不依赖数据文件）
    cd backend; .\.venv\Scripts\python.exe scripts\wave_dedicated.py explain

口径铁律：所有结论只针对本池已导入的 N 期样本；不涉及任何全市场数据；
落库 / JSON 里的枚举一律英文码，汉字只出现在 label / 文档 / message。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import dist_engine as DE  # noqa: E402
from services import wave_study as WS  # noqa: E402
from services.forward_ledger import load_draws  # noqa: E402
from services.max_fit import K_DEFAULT, NUM_STATES, ODDS_DEFAULT  # noqa: E402

# --------------------------------------------------------------------------- #
# 默认参数
# --------------------------------------------------------------------------- #
DEFAULT_DRAWS = "data/draws_70_279.json"
DEFAULT_TMP_DIR = ".tmp-wave"
TOOL_NAME = "wave_dedicated"
TOOL_VERSION = "1.0"
DEFAULT_PERMS = WS.WAVE_PERMS_DEFAULT
DEFAULT_ALPHA = WS.WAVE_ALPHA
# 置换次数低于该值时不做多进程（进程启动开销大于收益）
MIN_PERMS_FOR_POOL = 4

_WORKER: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 输入 / 输出小工具
# --------------------------------------------------------------------------- #
def numbers_of(draws: Sequence[Mapping[str, Any]]) -> list[int]:
    """从规范开奖行取出特码序列（升序）。"""
    return [int(row["special_number"]) for row in draws]


def numbers_digest(numbers: Sequence[int]) -> str:
    """特码序列的 sha256（不含时间戳 → 同样输入必得同样摘要）。"""
    payload = ",".join(str(int(value)) for value in numbers)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_out(path: str | None, default_name: str) -> str:
    """``--out`` 未给时落到仓库根的临时目录（``.tmp*`` 已在 .gitignore 内）。"""
    if path:
        return path
    target = Path(BACKEND_ROOT).parent / DEFAULT_TMP_DIR / default_name
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def write_json(path: str, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[written] {path}")


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------------------- #
# 置换：同一变体网格上的命中矩阵（多进程）
# --------------------------------------------------------------------------- #
def _init_grid(
    numbers: Sequence[int],
    variants: Sequence[Mapping[str, Any]],
    warmup: int,
    k: int,
) -> None:
    _WORKER["numbers"] = [int(value) for value in numbers]
    _WORKER["variants"] = [dict(row) for row in variants]
    _WORKER["warmup"] = int(warmup)
    _WORKER["k"] = int(k)


def _grid_worker(index: int) -> dict[str, Any]:
    """第 ``index`` 次置换：洗牌后在同一张网格上算命中（返回 id → 命中数）。"""
    shuffled = WS.shuffled_values(
        _WORKER["numbers"], WS.WAVE_SEED_BASE + int(index)
    )
    return {
        "index": int(index),
        "hits": WS.grid_hits(
            shuffled,
            _WORKER["variants"],
            warmup=_WORKER["warmup"],
            k=_WORKER["k"],
        ),
    }


def _run_pool(
    worker: Any,
    initializer: Any,
    init_args: tuple[Any, ...],
    count: int,
    jobs: int,
    *,
    chunksize: int = 1,
) -> list[dict[str, Any]]:
    """跑 ``count`` 次置换；``jobs <= 1`` 或次数太少时退回单进程。"""
    if count <= 0:
        return []
    if jobs <= 1 or count < MIN_PERMS_FOR_POOL:
        initializer(*init_args)
        return [worker(index) for index in range(count)]
    context = mp.get_context("spawn")
    with context.Pool(
        processes=int(jobs), initializer=initializer, initargs=init_args
    ) as pool:
        return pool.map(worker, range(count), chunksize=max(1, int(chunksize)))


def collect_grid_null(
    numbers: Sequence[int],
    variants: Sequence[Mapping[str, Any]] | None = None,
    *,
    perms: int = DEFAULT_PERMS,
    warmup: int = WS.WAVE_WARMUP,
    k: int = WS.WAVE_K,
    jobs: int = 1,
) -> dict[str, Any]:
    """收集 ``perms`` 次置换的网格命中矩阵（用 ``WS.shuffled_values`` 同一种洗牌）。"""
    rows = list(variants if variants is not None else WS.variant_grid())
    started = time.perf_counter()
    outputs = _run_pool(
        _grid_worker,
        _init_grid,
        (numbers, rows, warmup, k),
        int(max(0, perms)),
        int(max(1, jobs)),
        chunksize=1,
    )
    null = [dict(row["hits"]) for row in outputs]
    return {
        "null": null,
        "meta": {
            "permutation_scheme": "shuffle_special_number_positions_keep_period_and_date",
            "seed_base": WS.WAVE_SEED_BASE,
            "perms": int(max(0, perms)),
            "jobs": int(max(1, jobs)),
            "family_size": len(rows),
            "warmup": int(warmup),
            "k": int(k),
            "seconds": round(time.perf_counter() - started, 3),
        },
    }


# --------------------------------------------------------------------------- #
# 线上设置（只读；缺省完全不联网）
# --------------------------------------------------------------------------- #
def _settings_from_payload(raw: Any) -> dict[str, Any]:
    """``GET /api/settings`` 响应体可能是扁平设置，也可能包一层 ``{"settings": {...}}``。"""
    if isinstance(raw, Mapping):
        nested = raw.get("settings")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(raw)
    return {}


def load_live_settings(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    """读线上设置快照（只读 GET；失败 / 未指定时返回 ``None``，绝不改线上配置）。"""
    if getattr(args, "live_settings_json", None):
        # ``utf-8-sig``：兼容 PowerShell ``Set-Content -Encoding utf8`` 带出的 BOM
        raw = json.loads(
            Path(args.live_settings_json).read_text(encoding="utf-8-sig")
        )
        settings = _settings_from_payload(raw)
        if not settings:
            print(f"[warn] 快照文件里没有设置项：{args.live_settings_json}")
            return None, "empty"
        return WS.config_from_settings(settings), f"file:{args.live_settings_json}"
    url = getattr(args, "settings_url", None)
    if not url:
        return None, "none"
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=float(args.timeout)) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 只读探测失败不影响离线结论
        print(f"[warn] 读取线上设置失败（{url}）：{exc}；本轮只用审计快照配置。")
        return None, "error"
    settings = _settings_from_payload(raw)
    if not settings:
        print(f"[warn] 线上响应里没有设置项（{url}）；本轮只用审计快照配置。")
        return None, "empty"
    return WS.config_from_settings(settings), f"http:{url}"


# --------------------------------------------------------------------------- #
# 子命令：matrix
# --------------------------------------------------------------------------- #
def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    variants = WS.variant_grid()
    observed = WS.grid_hits(numbers, variants, warmup=args.warmup, k=K_DEFAULT)
    collected = collect_grid_null(
        numbers,
        variants,
        perms=args.perms,
        warmup=args.warmup,
        k=K_DEFAULT,
        jobs=args.jobs,
    )
    print("=" * 78)
    print(f"置换矩阵 · {TOOL_NAME} v{TOOL_VERSION}")
    print(
        f"变体家族 {len(variants)} 条 · 置换 {args.perms} 次 · 进程 {args.jobs} · "
        f"耗时 {collected['meta']['seconds']}s"
    )
    print("=" * 78)
    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "command": "matrix",
        "scope": WS.scope_note(len(numbers), len(numbers) - int(args.warmup)),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
        },
        "parameters": {
            "perms": int(args.perms),
            "jobs": int(args.jobs),
            "warmup": int(args.warmup),
            "k": K_DEFAULT,
            "alpha": float(args.alpha),
            "seed_base": WS.WAVE_SEED_BASE,
        },
        "observed": observed,
        "null": collected["null"],
        "null_meta": collected["meta"],
        "variant_ids": [str(row["variant_id"]) for row in variants],
        "preregistration": WS.preregistration_block(),
    }
    write_json(resolve_out(args.out, "wave_matrix.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：study
# --------------------------------------------------------------------------- #
def _stability_for(block: Mapping[str, Any]) -> dict[str, Any]:
    return WS.stability_block(
        block.get("hits_series") or [], baseline_rate=WS.WAVE_BASELINE_RATE
    )


def _walk_forward_scores(block: Mapping[str, Any]) -> dict[str, Any]:
    """单变体的四把尺子 + 均匀参考 + 差值（严格 walk-forward）。"""
    reference = WS.reference_block()
    return {
        "variant_id": block.get("variant_id"),
        "definition": block.get("definition"),
        "window": block.get("window"),
        "bias": block.get("bias"),
        "param": block.get("param"),
        "evaluated": block.get("evaluated"),
        "hits": block.get("hits"),
        "hit_rate": block.get("hit_rate"),
        "mean_rank": block.get("mean_rank"),
        "log_loss": block.get("log_loss"),
        "brier": block.get("brier"),
        "reference": reference,
        "delta_hit_rate": (
            (block["hit_rate"] - reference["hit_rate"])
            if block.get("hit_rate") is not None
            else None
        ),
        "delta_mean_rank": (
            (block["mean_rank"] - reference["mean_rank"])
            if block.get("mean_rank") is not None
            else None
        ),
        "delta_log_loss": (
            (block["log_loss"] - reference["log_loss"])
            if block.get("log_loss") is not None
            else None
        ),
        "delta_brier": (
            (block["brier"] - reference["brier"])
            if block.get("brier") is not None
            else None
        ),
        "p_binomial": block.get("p_binomial"),
        "data_status": block.get("data_status"),
        "data_status_label": block.get("data_status_label"),
    }


def run_study(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    variants = WS.variant_grid()
    prereg = WS.preregistration_block()
    span = WS.period_span(draws)

    print("=" * 78)
    print(f"波动法专项评估 · {TOOL_NAME} v{TOOL_VERSION}")
    print(
        f"本池已导入 {len(numbers)} 期（第 {span.get('first_period')}.."
        f"{span.get('last_period')} 期，{span.get('first_date')} ~ "
        f"{span.get('last_date')}）；评估窗跳过最早 {args.warmup} 期。"
    )
    print(f"预登记假设（先声明后看数）：{prereg['hypothesis']}")
    print(
        f"预登记变体家族大小 = {prereg['family_size']}（定义 × 窗口 × 偏好 × 参数）；"
        f"保守核子家族 = {prereg['core_subfamily_size']}；主变体 = {prereg['primary_variant']}"
    )
    print(WS.model_family_note())
    print(f"合法检验：{prereg['legitimate_test']}")
    print("=" * 78)

    started = time.perf_counter()
    observed = WS.grid_hits(numbers, variants, warmup=args.warmup, k=K_DEFAULT)
    grid_seconds = time.perf_counter() - started
    print(f"[1/6] 观测网格命中完成（{grid_seconds:.2f}s）")

    collected = collect_grid_null(
        numbers,
        variants,
        perms=args.perms,
        warmup=args.warmup,
        k=K_DEFAULT,
        jobs=args.jobs,
    )
    null = collected["null"]
    print(
        f"[2/6] 置换零分布完成（{collected['meta']['seconds']}s，"
        f"{args.perms} 次 × {len(variants)} 变体，进程 {args.jobs}）"
    )

    started = time.perf_counter()
    holm = WS.holm_family(observed, null, alpha=args.alpha)
    max_block = WS.max_over_grid(observed, null)
    core_max = WS.max_over_grid(observed, null, keys=WS.core_variant_ids())
    print(
        f"[3/6] Holm 校正 + max 零分布完成（{time.perf_counter() - started:.2f}s）："
        f"最小原始 p={_fmt_num(holm.get('min_p_raw'))}、"
        f"最小校正后 p={_fmt_num(holm.get('min_p_adjusted'))}、"
        f"幸存 {holm.get('survivor_count')} 条"
    )
    print(
        f"      同网格 max：观测 {max_block.get('observed_max')} 命中"
        f"（{max_block.get('best_variant')}）；零分布均值 "
        f"{_fmt_num((max_block.get('null_max') or {}).get('mean'))}、"
        f"95 分位 {(max_block.get('null_max') or {}).get('p95')} → "
        f"合法 p={_fmt_num(max_block.get('p_value'))}"
    )

    started = time.perf_counter()
    walk = WS.grid_walk_forward(numbers, variants, warmup=args.warmup, k=K_DEFAULT)
    table = WS.variant_table(walk, holm)
    print(f"[4/6] 逐变体 walk-forward 四把尺子完成（{time.perf_counter() - started:.2f}s）")

    primary_id = WS.PRIMARY_VARIANT_ID
    primary_block = walk.get(primary_id) or {}
    best_id = str(max_block.get("best_variant") or "")
    best_block = walk.get(best_id) or {}
    stability = {
        "primary": _stability_for(primary_block),
        "observed_max": _stability_for(best_block),
    }
    print(
        f"[5/6] 稳定性：主变体对半提升 "
        f"{[ _fmt_pct(row.get('lift')) for row in stability['primary'].get('halves', []) ]}、"
        f"符号一致性 {_fmt_num(stability['primary'].get('sign_consistency'), 3)}"
    )

    evaluated = int(primary_block.get("evaluated") or 0)
    hits = int(primary_block.get("hits") or 0)
    power = WS.power_and_verdict(evaluated, hits, extra_per_day=args.draws_per_day)
    z_max = WS.null_max_z(
        evaluated, (max_block.get("null_max") or {}).get("mean") or 0.0
    )
    regression = WS.regression_path(evaluated, hits, expected_best_z=z_max)
    print(
        f"[6/6] 功效：观测 Δ={_fmt_pct(power.get('observed_delta'))}，"
        f"MDD={_fmt_pct(power.get('minimum_detectable_delta'))}"
    )
    for target in power.get("targets", []):
        print(
            f"      目标 Δ={_fmt_pct(target['delta'])} → 80% 功效需 "
            f"{target['required_draws_80_power']} 期；再追加 "
            f"{target['additional_draws_needed']} 期 ≈ "
            f"{_fmt_num(target['months_at_one_draw_per_day'], 1)} 个月"
        )

    summary = WS.study_summary(walk, observed, holm, max_block)

    print("-" * 78)
    print(f"{'变体':<34}{'命中':>6}{'命中率':>9}{'提升':>9}{'原始 p':>9}{'Holm p':>9}  判定")
    for row in table[:12]:
        print(
            f"{str(row['variant_id']):<34}"
            f"{str(row['hits']):>6}"
            f"{_fmt_num(row['hit_rate'], 4):>9}"
            f"{_fmt_pct(row['lift']):>9}"
            f"{_fmt_num(row['p_value'], 4):>9}"
            f"{_fmt_num(row['p_adjusted'], 4):>9}  {row['verdict']}"
        )
    print("（仅列前 12 行，全表见 JSON）")
    print("-" * 78)
    print(f"结论：{summary['statement']}")

    ablation = None
    live_cfg, live_source = (None, "none")
    if args.with_ablation:
        live_cfg, live_source = load_live_settings(args)
        ablation = WS.ablation_side_by_side(
            draws, audit_settings=WS.audit_config(), live_settings=live_cfg
        )
        ablation["live_settings_source"] = live_source

    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "command": "study",
        "claim": WS.CLAIM_NO_EDGE,
        "claim_label": WS.CLAIM_LABELS[WS.CLAIM_NO_EDGE],
        "scope": WS.scope_note(len(numbers), evaluated),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
            "period_span": span,
        },
        "parameters": {
            "alpha": float(args.alpha),
            "perms": int(args.perms),
            "jobs": int(args.jobs),
            "warmup": int(args.warmup),
            "k": K_DEFAULT,
            "odds": float(ODDS_DEFAULT),
            "seed_base": WS.WAVE_SEED_BASE,
            "draws_per_day": float(args.draws_per_day),
            "projection_days_per_draw": 1.0,
        },
        "preregistration": prereg,
        "model_family": WS.distinct_probability_models(),
        "model_family_note": WS.model_family_note(),
        "variant_table": table,
        "variant_hits_observed": observed,
        "holm": {
            key: holm[key]
            for key in (
                "family_size",
                "tested_size",
                "alpha",
                "correction",
                "bonferroni_threshold",
                "min_p_raw",
                "min_p_adjusted",
                "survivors",
                "survivor_count",
                "reused_function",
            )
        },
        "holm_rows": holm.get("rows"),
        "max_over_grid": {
            key: max_block.get(key)
            for key in (
                "family_size",
                "observed_max",
                "best_variant",
                "p_value",
                "permitted_alpha_floor",
                "data_status",
                "data_status_label",
            )
        },
        "max_over_grid_null": max_block.get("null_max"),
        "core_subfamily_max": {
            key: core_max.get(key)
            for key in (
                "family_size",
                "observed_max",
                "best_variant",
                "p_value",
                "permitted_alpha_floor",
            )
        },
        "core_subfamily_null": core_max.get("null_max"),
        "hit_distribution": WS.uniformity_of_variant_hits(list(observed.values())),
        "stability": stability,
        "walk_forward": {
            "primary": _walk_forward_scores(primary_block),
            "observed_max": _walk_forward_scores(best_block),
            "reference": WS.reference_block(),
        },
        "power": power,
        "regression": regression,
        "summary": summary,
        "null_meta": collected["meta"],
        "timings": {"observed_grid_seconds": round(grid_seconds, 3)},
        "legitimate_note": WS.legitimate_test_note(),
        "disclaimer": DE.DISCLAIMER,
    }
    if ablation is not None:
        payload["ablation"] = ablation
    write_json(resolve_out(args.out, "wave_study.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：ablation
# --------------------------------------------------------------------------- #
def run_ablation(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    live_cfg, live_source = load_live_settings(args)
    if live_cfg is None:
        print(
            "[warn] 没有拿到线上设置快照：只用审计快照配置跑一轮；"
            "如需并排对照请给 --live-settings-json 或 --settings-url（只读 GET）。"
        )
    ablation = WS.ablation_side_by_side(
        draws, audit_settings=WS.audit_config(), live_settings=live_cfg
    )
    ablation["live_settings_source"] = live_source

    print("=" * 78)
    print(f"点阵 / 重肖开关消融 · {TOOL_NAME} v{TOOL_VERSION}")
    print(WS.mechanism_note())
    print("=" * 78)
    for label, entry in ablation["configs"].items():
        settings = entry["settings"]
        print(
            f"配置 [{label}] small_max={settings.get('small_max')} "
            f"normal_max={settings.get('normal_max')} "
            f"trend_bias={settings.get('trend_bias')} "
            f"exclude_repeat_zodiac={settings.get('exclude_repeat_zodiac')} "
            f"lattice_enabled={settings.get('lattice_enabled')} "
            f"lattice_window={settings.get('lattice_window')}"
        )
        for name in ("lattice", "repeat_zodiac"):
            row = entry[name]
            print(
                f"  {row['handle']:<24} on {row['on']['hits']}/{row['on']['evaluated']} "
                f"vs off {row['off']['hits']}/{row['off']['evaluated']} → "
                f"Δhits {row['delta_hits']:+d} · 选号逐期相同 {row['pick_set_identical_positions']}"
                f"/{row['positions']} · 命中翻转 {row['hit_outcome_flips']} "
                f"· inert={row['inert']}"
            )
        print(f"  预测号码波动桶分布 on={entry['lattice']['pick_wave_mix_on']} "
              f"off={entry['lattice']['pick_wave_mix_off']}")
        if entry.get("lattice_primary_wave_counts"):
            print(f"  lattice_primary 投票分布 = {entry['lattice_primary_wave_counts']}")

    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "command": "ablation",
        "claim": WS.CLAIM_NO_EDGE,
        "claim_label": WS.CLAIM_LABELS[WS.CLAIM_NO_EDGE],
        "scope": WS.scope_note(len(numbers), 0),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
        },
        "ablation": ablation,
        "mechanism_note": WS.mechanism_note(),
        "disclaimer": DE.DISCLAIMER,
    }
    write_json(resolve_out(args.out, "wave_ablation.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：explain
# --------------------------------------------------------------------------- #
def explain_text() -> str:
    lines = [
        "=" * 78,
        "「我就用特码走势，波动法，也不行吗」—— 白话回答",
        "=" * 78,
        "1) 波动法没有被「冤枉」：它在 23 条假设里确实是最极端的一条，",
        "   但同一条规则换一个窗口 / 换一个偏好就能得到不同成绩 —— 这类自由",
        "   度本身就是多重比较，所以必须先把家族大小声明清楚再校正。",
        "",
        "2) 本脚本的合法检验是同网格 max 零分布：每次置换都在同一张变体网格",
        "   上取最大值，再看观测最大值落在零分布哪里。单变体的原始 p 只在",
        "   「事先指定且只测这一条」时合法，看了整张网格再挑最高的一条就不合法。",
        "",
        "3) 稳定性：真实信号应当在对半 / 各三分段同号；只集中在一个子区间",
        "   的偏离按噪声处理（见 study 输出的 stability 段）。",
        "",
        "4) 真正卡住的是样本量：本池 k=10 的随机基线是 10/49=20.41%，",
        "   保本线是 10/47=21.28%（只差 +0.87pp）。要在 80% 功效下证明",
        "   +6.26pp 需要约 345 期；证明 +5pp 需要约 520 期；证明保本边际",
        "   +0.87pp 需要约 17,000 期 —— 一把一天开一期，后者要几十年。",
        "",
        "5) max 零分布的预测：如果 26.67% 是噪声，那么随着期数增加，",
        "   **网格冠军也会按 1/√n 往下掉**，累计命中率会向 20.41% 收敛；",
        "   如果它是真的，它会稳在 26.67% 附近不再回归。",
        "",
        "6) 结论：本池样本内「没证据说明波动法有优势」，而不是「证明了没有优势」。",
        "   要把它定论，唯一诚实的做法是继续攒期数（见 study 的 power 段）。",
        "=" * 78,
    ]
    return "\n".join(lines)


def run_explain(args: argparse.Namespace) -> dict[str, Any]:
    text = explain_text()
    print(text)
    prereg = WS.preregistration_block()
    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "command": "explain",
        "claim": WS.CLAIM_NO_EDGE,
        "claim_label": WS.CLAIM_LABELS[WS.CLAIM_NO_EDGE],
        "preregistration": prereg,
        "variant_grid_size": WS.variant_family_size(),
        "variant_grid": [
            {
                "variant_id": row["variant_id"],
                "definition": row["definition"],
                "definition_label": row["definition_label"],
                "window": row["window"],
                "bias": row["bias"],
                "param": row["param"],
                "is_primary": row["is_primary"],
            }
            for row in WS.variant_grid()
        ],
        "reference": WS.reference_block(),
        "break_even_rate": WS.WAVE_BREAK_EVEN_RATE,
        "break_even_edge": WS.WAVE_BREAK_EVEN_EDGE,
        "legitimate_note": WS.legitimate_test_note(),
        "text": text,
        "disclaimer": DE.DISCLAIMER,
    }
    if args.out:
        write_json(args.out, payload)
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wave_dedicated.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, default_out: str) -> None:
        sub.add_argument("--draws-json", default=DEFAULT_DRAWS, help="开奖 JSON 路径")
        sub.add_argument("--out", default=None, help=f"输出 JSON（默认 {default_out}）")
        sub.add_argument("--perms", type=int, default=DEFAULT_PERMS, help="置换次数")
        sub.add_argument("--jobs", type=int, default=None, help="进程数（默认 CPU 数，最多 12）")
        sub.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="显著性水平（默认 0.05）")
        sub.add_argument("--warmup", type=int, default=WS.WAVE_WARMUP, help="前置历史期数（默认 30）")

    def add_live(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--live-settings-json",
            default=None,
            help="线上设置快照 JSON（离线，优先；`GET /api/settings` 的响应体）",
        )
        sub.add_argument(
            "--settings-url",
            default=None,
            help="只读 GET 线上设置（例如 http://127.0.0.1:8000/api/settings）；缺省不联网",
        )
        sub.add_argument("--timeout", type=float, default=5.0, help="只读 GET 超时秒数")

    study = subparsers.add_parser(
        "study",
        help="完整专项研究：预登记 + 变体网格 + max 零分布 + 稳定性 + 功效",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(study, default_out="wave_study.json")
    study.add_argument(
        "--with-ablation",
        action="store_true",
        help="同时跑点阵 / 重肖开关的两种配置并排消融（默认关闭；需要线上快照才是双配置）",
    )
    study.add_argument(
        "--draws-per-day",
        type=float,
        default=1.0,
        help="每日期数（默认 1，用于把「再要多少期」换算成月数）",
    )
    add_live(study)
    study.set_defaults(func=run_study)

    matrix = subparsers.add_parser(
        "matrix",
        help="只跑置换矩阵（网格命中 × perms）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(matrix, default_out="wave_matrix.json")
    matrix.set_defaults(func=run_matrix)

    ablation = subparsers.add_parser(
        "ablation",
        help="点阵 / 重肖开关在两种配置下的并排消融 + 机制说明",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ablation.add_argument("--draws-json", default=DEFAULT_DRAWS, help="开奖 JSON 路径")
    ablation.add_argument("--out", default=None, help="输出 JSON（默认 wave_ablation.json）")
    add_live(ablation)
    ablation.set_defaults(func=run_ablation)

    explain = subparsers.add_parser(
        "explain",
        help="白话解释（不依赖数据文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    explain.add_argument("--out", default=None, help="输出 JSON（可选）")
    explain.set_defaults(func=run_explain)

    return parser


def _resolve_jobs(args: argparse.Namespace) -> None:
    if getattr(args, "jobs", None) is None and hasattr(args, "jobs"):
        args.jobs = max(1, min(12, os.cpu_count() or 1))


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # 控制台编码兜底：宁可丢个别字符也不要因为编码崩掉
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非 tty 环境
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_jobs(args)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
