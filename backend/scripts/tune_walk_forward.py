r"""out-of-sample 调参搜索 + best-of-N 置换零分布 + 过拟合对照（可复现脚本）。

回答「就这两百多期数据，怎么调优超过随机、越高越好」。

做法（三件事，缺一不可）：
1. **声明配置空间**：在 ``services.analytics.WALK_FORWARD_AXES`` 里声明轴与取值，
   全量笛卡尔积约 172 万组；本脚本固定 seed 均匀随机抽样 N 组（默认 256）来评估。
2. **训练窗选参 / 验证窗打分**：70/30 切分，参数只在训练窗上挑，验证窗只打分。
3. **best-of-N 置换零分布**：把特码序列随机重排（日期/期号不动，只打乱号码），
   在每次重排上**重复同一套选参流程**（训练窗选冠军 → 读验证窗 Δ），得到
   「best-of-N 在零假设下的 Δ 分布」。观测到的冠军若不比这个零分布高，就不是优势。

口径铁律：只针对 **本池已导入的 N 期样本内**；不得升格为全量 / 市场结论；
样本不足时打印「数据不足」；禁止把样本内排名说成「已优化命中率」。

运行（离线，不碰数据库；置空 DATABASE_URL 可强制内存 store，避免占用连接池）：
    cd backend
    $env:DATABASE_URL=''
    .\.venv\Scripts\python.exe scripts\tune_walk_forward.py --stage all --jobs 12
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import analytics as A  # noqa: E402
from services.lottery import DEFAULT_SETTINGS, PICK_STRATEGY_SCORE_TOP  # noqa: E402

# 本仓 dev 全局设置快照（user_id=0，见 GET /api/settings；仅作默认基线，可被 --base-json 覆盖）
SAVED_SETTINGS_SNAPSHOT: dict[str, Any] = {
    "mode": "even",
    "pick_count": 10,
    "small_max": 15,
    "normal_max": 20,
    "total_amount": 50,
    "amount_unit": 5,
    "odds": 47.0,
    "exclude_repeat_zodiac": True,
    "trend_bias": "mid",
    "trend_window": 20,
    "trend_bias_explicit": True,
    "avoid_cold_enabled": False,
    "avoid_cold_days": 60,
    "pick_strategy": "wave_round",
}

# 置换零分布的随机种子（与抽样 seed 分离，便于复现）
PERM_SEED_BASE = 981_001

_WORKER: dict[str, Any] = {}


def load_draws(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(
            f"开奖数据文件不存在：{file}\n"
            "本仓 backend/data/* 被 .gitignore 忽略，换机器需先从运行中的后端导出一份：\n"
            "  cd backend; .\\.venv\\Scripts\\python.exe -c \""
            "import json,urllib.request,pathlib;"
            "d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/draws'));"
            "d.sort(key=lambda r:(r['draw_date'],r['period']));"
            "pathlib.Path('data/draws_70_279.json').write_text("
            "json.dumps([{'period':int(r['period']),'draw_date':str(r['draw_date']),"
            "'special_number':int(r['special_number'])} for r in d],ensure_ascii=False),encoding='utf-8')\""
        )
    raw = json.loads(file.read_text(encoding="utf-8"))
    return [
        {
            "period": int(r["period"]),
            "draw_date": str(r["draw_date"]),
            "special_number": int(r["special_number"]),
        }
        for r in raw
    ]


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


# --------------------------------------------------------------------------- #
# 多进程 worker：每个进程初始化一次基线序列 / 配置池，避免反复 pickle
# --------------------------------------------------------------------------- #
def _init_worker(
    series: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    split_index: int,
    min_prior_draws: int,
) -> None:
    _WORKER["series"] = series
    _WORKER["configs"] = configs
    _WORKER["split_index"] = split_index
    _WORKER["min_prior_draws"] = min_prior_draws


def _shuffle_series(series: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """只打乱特码的出现位置（日期 / 期号不动）→ 检验「号码序列是否存在时间结构」。"""
    rng = random.Random(seed)
    numbers = [int(item["special_number"]) for item in series]
    rng.shuffle(numbers)
    return [
        {**item, "special_number": numbers[index]} for index, item in enumerate(series)
    ]


def _perm_worker(perm_index: int) -> dict[str, Any]:
    series = _shuffle_series(_WORKER["series"], PERM_SEED_BASE + perm_index)
    selected: dict[str, Any] | None = None
    max_valid = None
    for config in _WORKER["configs"]:
        row = A.walk_forward_eval_config(
            series,
            config,
            split_index=_WORKER["split_index"],
            min_prior_draws=_WORKER["min_prior_draws"],
        )
        train_delta = row.get("train_delta")
        valid_delta = row.get("valid_delta")
        if valid_delta is not None and (max_valid is None or valid_delta > max_valid):
            max_valid = valid_delta
        if train_delta is None or valid_delta is None:
            continue
        if selected is None or train_delta > selected["train_delta"]:
            selected = {"train_delta": train_delta, "valid_delta": valid_delta}
    return {
        "perm_index": perm_index,
        "selected_train_delta": selected["train_delta"] if selected else None,
        "selected_valid_delta": selected["valid_delta"] if selected else None,
        "max_valid_delta": max_valid,
    }


def _full_worker(config_index: int) -> float | None:
    config = _WORKER["configs"][config_index]
    out = A.backtest_stats(
        _WORKER["series"],
        base_settings=config,
        min_prior_draws=_WORKER["min_prior_draws"],
        include_results=False,
        include_wave_breakdown=False,
    )
    return out.get("hit_rate_minus_baseline")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_pool(
    base: dict[str, Any],
    sample_size: int,
    seed: int,
) -> dict[str, Any]:
    anchors = [
        dict(base),
        {**base, "pick_strategy": PICK_STRATEGY_SCORE_TOP},
        {**base, "exclude_repeat_zodiac": False},
        {**base, "lattice_enabled": False},
        {**base, "trend_bias": "neutral"},
        {**base, "avoid_cold_enabled": True},
    ]
    return A.build_walk_forward_config_space(
        sample_size=sample_size,
        seed=seed,
        base_settings=base,
        anchors=anchors,
    )


def run_permutation_null(
    series: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    split_index: int,
    min_prior_draws: int,
    perms: int,
    jobs: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    workers = max(1, int(jobs))
    results: list[dict[str, Any]] = []
    with mp.Pool(
        processes=workers,
        initializer=_init_worker,
        initargs=(series, configs, split_index, min_prior_draws),
    ) as pool:
        for index, row in enumerate(
            pool.imap_unordered(_perm_worker, range(perms), chunksize=1), start=1
        ):
            results.append(row)
            if index % 10 == 0 or index == perms:
                elapsed = time.perf_counter() - started
                print(
                    f"  [null] {index}/{perms} 置换完成（{elapsed:.0f}s",
                    flush=True,
                )
    results.sort(key=lambda r: r["perm_index"])
    return {
        "perms": perms,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "rows": results,
    }


def summarize_null(values: list[float], observed: float | None) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0}
    mean = statistics.fmean(clean)
    sd = statistics.pstdev(clean) if len(clean) > 1 else 0.0
    exceed = sum(1 for v in clean if observed is not None and v >= observed)
    p = (exceed + 1) / (len(clean) + 1) if observed is not None else None
    ordered = sorted(clean)
    def q(frac: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        pos = frac * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    return {
        "n": len(clean),
        "mean": mean,
        "sd": sd,
        "min": ordered[0],
        "p05": q(0.05),
        "p50": q(0.50),
        "p95": q(0.95),
        "max": ordered[-1],
        "observed": observed,
        "observed_z": ((observed - mean) / sd) if (observed is not None and sd > 0) else None,
        "exceed_count": exceed,
        "p_one_sided": p,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="walk-forward 调参 + best-of-N 置换零分布")
    parser.add_argument("--draws-json", default=str(BACKEND_ROOT / "data" / "draws_70_279.json"))
    parser.add_argument("--base-json", default="", help="基线设置 JSON（缺省用 dev 快照）")
    parser.add_argument("--stage", choices=["search", "null", "all"], default="all")
    parser.add_argument("--sample-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--perms", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--out", default=str(BACKEND_ROOT / "data" / "tune_walk_forward_result.json"))
    args = parser.parse_args()

    base = dict(SAVED_SETTINGS_SNAPSHOT)
    if args.base_json:
        base.update(json.loads(Path(args.base_json).read_text(encoding="utf-8")))
    draws = load_draws(args.draws_json)
    split_info = A.walk_forward_split(draws, train_ratio=args.train_ratio)
    series = split_info["series"]

    print("=== 样本与切分（本池样本内）===")
    print(
        f"样本 {split_info['sample_size']} 期；训练窗可评估 {split_info['train_eval']} 期"
        f"（…{split_info['train_last_period']} 期）"
    )
    print(
        f"验证窗可评估 {split_info['valid_eval']} 期"
        f"（{split_info['valid_first_period']}…{split_info['valid_last_period']} 期，"
        f"{split_info['valid_first_date']}…{split_info['valid_last_date']}）"
    )

    pool = build_pool(base, args.sample_size, args.seed)
    print(
        f"声明配置空间 {pool['declared_size']} 组；实际评估 {pool['evaluated_size']} 组"
        f"（含 {pool['anchor_count']} 个锚点）"
    )

    result: dict[str, Any] = {
        "scope": f"本池已导入 {split_info['sample_size']} 期样本内",
        "sample_size": split_info["sample_size"],
        "base_settings": base,
        "pool": {
            "declared_size": pool["declared_size"],
            "evaluated_size": pool["evaluated_size"],
            "anchor_count": pool["anchor_count"],
            "seed": pool["seed"],
            "axes": pool["axes"],
        },
        "split": {
            key: split_info[key]
            for key in (
                "train_ratio",
                "train_eval",
                "valid_eval",
                "split_index",
                "train_first_period",
                "train_last_period",
                "valid_first_period",
                "valid_last_period",
                "valid_first_date",
                "valid_last_date",
            )
        },
    }

    search = None
    if args.stage in ("search", "all"):
        started = time.perf_counter()
        search = A.walk_forward_config_search(
            draws,
            configs=pool["configs"],
            train_ratio=args.train_ratio,
        )
        print(f"\n=== 训练/验证搜索（{search['grid_size']} 组，{time.perf_counter() - started:.0f}s）===")
        selected = search["selected"]
        print(
            f"训练窗 Δ 冠军：train Δ={_fmt_pct(selected['train_delta'])} "
            f"（{selected['train_hits']}/{selected['train_evaluated']}）→ "
            f"验证窗 Δ={_fmt_pct(selected['valid_delta'])} "
            f"（{selected['valid_hits']}/{selected['valid_evaluated']}）"
        )
        print(f"验证窗最大 Δ（事后择优，仅作对照）：{_fmt_pct(search['max_valid_delta'])}")

        # 过拟合对照 1：全池样本内命中率最高的配置 → 它的验证窗表现（收缩）
        with mp.Pool(
            processes=max(1, args.jobs),
            initializer=_init_worker,
            initargs=(series, pool["configs"], split_info["split_index"], 2),
        ) as pool_proc:
            full_deltas = pool_proc.map(_full_worker, range(len(pool["configs"])))
        rows_by_key = {
            tuple(sorted((k, repr(row["config"][k])) for k in A.WALK_FORWARD_AXES)): row
            for row in search["rows"]
        }
        best_full_index = max(
            range(len(full_deltas)), key=lambda i: (full_deltas[i] is not None, full_deltas[i] or -9)
        )
        best_full_cfg = pool["configs"][best_full_index]
        best_full_row = rows_by_key[
            tuple(sorted((k, repr(best_full_cfg[k])) for k in A.WALK_FORWARD_AXES))
        ]
        print(
            f"全池样本内 Δ 冠军：全池 Δ={_fmt_pct(full_deltas[best_full_index])} → "
            f"训练窗 Δ={_fmt_pct(best_full_row['train_delta'])}、"
            f"验证窗 Δ={_fmt_pct(best_full_row['valid_delta'])}（收缩）"
        )
        result["search"] = {
            "grid_size": search["grid_size"],
            "split": search["split"],
            "selected": {
                "config": selected["config"],
                "train_hits": selected["train_hits"],
                "train_evaluated": selected["train_evaluated"],
                "train_hit_rate": selected["train_hit_rate"],
                "train_delta": selected["train_delta"],
                "valid_hits": selected["valid_hits"],
                "valid_evaluated": selected["valid_evaluated"],
                "valid_hit_rate": selected["valid_hit_rate"],
                "valid_delta": selected["valid_delta"],
                "valid_verdict": selected["valid_verdict"],
            },
            "max_valid_delta": search["max_valid_delta"],
            "selected_valid_delta": search["selected_valid_delta"],
            "rows": search["rows"],
            "notes": search["notes"],
        }
        result["overfitting_exhibit"] = {
            "in_sample_winner_config": best_full_cfg,
            "in_sample_full_pool_delta": full_deltas[best_full_index],
            "in_sample_winner_train_delta": best_full_row["train_delta"],
            "in_sample_winner_valid_delta": best_full_row["valid_delta"],
            "shrinkage_full_to_valid": (
                None
                if best_full_row["valid_delta"] is None
                else full_deltas[best_full_index] - best_full_row["valid_delta"]
            ),
        }

    if args.stage in ("null", "all"):
        if search is None:
            search = A.walk_forward_config_search(draws, configs=pool["configs"], train_ratio=args.train_ratio)
            result["search"] = {
                "grid_size": search["grid_size"],
                "split": search["split"],
                "selected": search["selected"],
                "max_valid_delta": search["max_valid_delta"],
                "selected_valid_delta": search["selected_valid_delta"],
                "notes": search["notes"],
            }
        observed = search["selected_valid_delta"]
        print(f"\n=== best-of-{pool['evaluated_size']} 置换零分布（{args.perms} 次，{args.jobs} 进程）===")
        null = run_permutation_null(
            series,
            pool["configs"],
            split_info["split_index"],
            2,
            args.perms,
            args.jobs,
        )
        selected_null = summarize_null(
            [r["selected_valid_delta"] for r in null["rows"]], observed
        )
        max_null = summarize_null([r["max_valid_delta"] for r in null["rows"]], search["max_valid_delta"])
        print(
            f"零分布（按训练窗选冠军的验证窗 Δ）：mean={_fmt_pct(selected_null.get('mean'))} "
            f"sd={_fmt_pct(selected_null.get('sd'))} p95={_fmt_pct(selected_null.get('p95'))}"
        )
        print(
            f"观测冠军 Δ={_fmt_pct(observed)} → z={selected_null.get('observed_z')} "
            f"单尾 p={selected_null.get('p_one_sided')}"
        )
        print(
            f"零分布（事后最大验证 Δ）：p95={_fmt_pct(max_null.get('p95'))}；"
            f"观测最大 Δ={_fmt_pct(search['max_valid_delta'])} → p={max_null.get('p_one_sided')}"
        )
        result["null"] = {
            "perms": null["perms"],
            "elapsed_seconds": null["elapsed_seconds"],
            "selected_statistic": selected_null,
            "max_statistic": max_null,
            "rows": null["rows"],
        }
        survives = bool(
            observed is not None
            and observed > 0
            and selected_null.get("p_one_sided") is not None
            and selected_null["p_one_sided"] <= 0.05
        )
        result["verdict"] = {
            "observed_selected_valid_delta": observed,
            "null_p_one_sided": selected_null.get("p_one_sided"),
            "survives_best_of_n_null": survives,
        }
        print(f"\n结论：训练窗冠军的验证窗 Δ {'超过' if survives else '未超过'} best-of-N 零分布 → "
              f"{'可进一步验证' if survives else '不构成优于随机的证据'}")

    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
