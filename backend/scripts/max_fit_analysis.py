r"""最大拟合算法：拟合到极限，样本外仍是随机（可复现 CLI）。

本脚本把 ``services.max_fit`` 的算法族跑到本池已导入的开奖数据上，回答一个可测量的
命题：**能不能造一条把 210 期开奖拟合到极限的规则，同时它在没见过的期上仍然有用？**

做法：

1. **样本内 vs 样本外**：每条模型都同时给出
   - 样本内（「反推」口径：目标期本身进入拟合）与
   - 样本外（严格 walk-forward：``series[:i]`` 只含该期之前的数据）；
2. 每边都给 **四把尺子**：``hit_rate``（二值命中）、``mean_rank``（实际号在完整排序里的
   平均名次，均匀期望 25.0）、``log_loss``（均匀 ``ln 49``）、``brier``
   （逐类平均口径，均匀 ``(1/49)(1−1/49)``）；
3. **匹配置换零分布**：打乱本池号码位置 ``--perms`` 次，**每次重跑同一套选参 + 预测流程**
   （``fit_hyperparameters`` 也在打乱数据上重跑），给出 best-of-N 的单侧 p 值；
4. **正则化扫描**：把正则化强度 α 从 0 扫到 100，展示「正则化越弱 → 样本内拟合越完美，
   但样本外反而越差」；
5. **拟合点阵带**：中心偏移 + 半宽都由数据拟合（覆盖 100% = 「尽可能贴近开奖号」），
   报告样本内 / 样本外覆盖率；
6. **best_fit_picks**：拟合最强的模型在若干具体期上的实际输出（top-10 集合 vs 实际特码）。

口径铁律（违反即为缺陷）：
- 全部结论只针对 **本池已导入的 N 期数据** 这个样本；**禁止写「全市场」**（本池没有
  全市场数据，任何升格都是编造）。
- 样本不足显式输出 ``data_status = INSUFFICIENT`` + ``数据不足``，绝不用 0 冒充。
- 严格 walk-forward：预测第 i 期时只允许使用第 i 期之前的数据。
- 本脚本**只读**：不改 ``DEFAULT_SETTINGS``、不改 ``recommend()`` 引擎、不写数据库，
  也**不把任何 max_fit 模型接入线上推荐路径**。

复用（不重写）：置换零分布的归纳、打乱、原子写 JSON、读盘与百分比格式全部来自
``scripts/fit_capacity_analysis``；本脚本只新增 max_fit 专属的选参 + 评估流程。

运行（离线，不碰数据库）：
    cd backend
    .\.venv\Scripts\python.exe scripts\max_fit_analysis.py --stage all --perms 200 --jobs 8

输出：``backend/data/max_fit_analysis.json``（``backend/data/*`` 已被 .gitignore 忽略）。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _path in (BACKEND_ROOT, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from services import max_fit as MF  # noqa: E402

# 复用既有脚本：归纳 / 打乱 / 原子写 / 读盘 / 百分比（同一 seed ⇒ 可交叉核对）
import fit_capacity_analysis as FC  # noqa: E402

SEED = 20261007
PERM_SEED_BASE = 771_007
# 用于结果表的对称键（英文枚举，落库 / 传输口径）
_METRIC_KEYS: tuple[str, ...] = ("hit_rate", "mean_rank", "log_loss", "brier")
# 越小越好的指标（置换 p 需要取「更小」方向）
_LOWER_IS_BETTER = {"mean_rank", "log_loss", "brier"}

_WORKER: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 评估小工具
# --------------------------------------------------------------------------- #
def _metrics(evaluation: dict[str, Any]) -> dict[str, Any]:
    """从 ``max_fit.evaluate`` 的返回体里抽出可比的标量指标（丢弃逐期明细）。"""
    return {
        "model": evaluation["model"],
        "mode": evaluation["mode"],
        "evaluated": evaluation["evaluated"],
        "hits": evaluation["hits"],
        "hit_rate": evaluation["hit_rate"],
        "mean_rank": evaluation["mean_rank"],
        "rank_buckets": evaluation["rank_buckets"],
        "log_loss": evaluation["log_loss"],
        "brier": evaluation["brier"],
    }


def _params_for(fit: dict[str, Any], model: str) -> dict[str, Any]:
    entry = (fit.get("models") or {}).get(model) or {}
    return dict(entry.get("params") or {})


def fit_and_evaluate(
    series: Sequence[int], k: int, warmup: int
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """选参（训练窗内层 walk-forward）后，对每条模型同时评样本内 / 样本外。"""
    fit = MF.fit_hyperparameters(series, k)
    out: dict[str, dict[str, Any]] = {}
    for model in MF.MODEL_IDS:
        params = _params_for(fit, model)
        in_sample = MF.evaluate(model, series, k, mode="in_sample", params=params, warmup=warmup)
        walk_forward = MF.evaluate(
            model, series, k, mode="walk_forward", params=params, warmup=warmup
        )
        out[model] = {
            "params": params,
            "in_sample": _metrics(in_sample),
            "walk_forward": _metrics(walk_forward),
        }
    return fit, out


def fit_and_walk_forward(
    series: Sequence[int], k: int, warmup: int
) -> dict[str, dict[str, Any]]:
    """置换零分布专用：重跑同一套选参流程，只取样本外指标（省内存 / 省时间）。"""
    fit = MF.fit_hyperparameters(series, k)
    out: dict[str, dict[str, Any]] = {}
    for model in MF.MODEL_IDS:
        params = _params_for(fit, model)
        walk_forward = MF.evaluate(
            model, series, k, mode="walk_forward", params=params, warmup=warmup
        )
        out[model] = _metrics(walk_forward)
    return out


def _gap(in_sample: dict[str, Any], walk_forward: dict[str, Any]) -> dict[str, Any]:
    """样本内 − 样本外：命中率看「掉了多少」，排名 / 密度指标看「差了多少」。"""
    return {
        "hit_rate_drop": (
            None
            if in_sample["hit_rate"] is None or walk_forward["hit_rate"] is None
            else in_sample["hit_rate"] - walk_forward["hit_rate"]
        ),
        "mean_rank_worsening": (
            None
            if in_sample["mean_rank"] is None or walk_forward["mean_rank"] is None
            else walk_forward["mean_rank"] - in_sample["mean_rank"]
        ),
        "log_loss_worsening": (
            None
            if in_sample["log_loss"] is None or walk_forward["log_loss"] is None
            else walk_forward["log_loss"] - in_sample["log_loss"]
        ),
        "brier_worsening": (
            None
            if in_sample["brier"] is None or walk_forward["brier"] is None
            else walk_forward["brier"] - in_sample["brier"]
        ),
    }


def _sort_key(row: dict[str, Any]) -> tuple:
    """按**样本内拟合**降序：命中率越高、平均排名越小、log-loss 越小越靠前。"""
    ins = row["in_sample"]
    hit = ins["hit_rate"] if ins["hit_rate"] is not None else -1.0
    rank = ins["mean_rank"] if ins["mean_rank"] is not None else 1e9
    loss = ins["log_loss"] if ins["log_loss"] is not None else 1e9
    return (-float(hit), float(rank), float(loss), str(row["model"]))


def _summarize_metric(
    null_values: Sequence[float | None], observed: float | None, lower_is_better: bool
) -> dict[str, Any]:
    """复用 ``fit_capacity_analysis.summarize_null`` 的归纳。

    低值更优的指标（mean_rank / log_loss / brier）取负号后再归纳，这样
    ``p_one_sided`` 始终是「纯噪声达到或超过观测值」的单侧概率。为了让 JSON 自解释，
    归纳结果里额外补上 ``direction`` 与「原单位」的 ``mean_original`` /
    ``observed_original``，避免读的人被负号绕晕。
    """
    if lower_is_better:
        values = [None if value is None else -float(value) for value in null_values]
        target = None if observed is None else -float(observed)
    else:
        values = list(null_values)
        target = observed
    summary = FC.summarize_null(values, target)
    sign = -1.0 if lower_is_better else 1.0
    summary["direction"] = "LOWER_IS_BETTER" if lower_is_better else "HIGHER_IS_BETTER"
    summary["mean_original"] = None if summary.get("mean") is None else sign * summary["mean"]
    summary["observed_original"] = None if observed is None else float(observed)
    return summary


# --------------------------------------------------------------------------- #
# 置换零分布（多进程，复用 fit_capacity_analysis 的 workers 模式）
# --------------------------------------------------------------------------- #
def _init_worker(series, k, warmup) -> None:
    _WORKER["series"] = list(series)
    _WORKER["k"] = int(k)
    _WORKER["warmup"] = int(warmup)


def _perm_worker(perm_index: int) -> dict[str, Any]:
    series = FC._shuffle_series(_WORKER["series"], PERM_SEED_BASE + perm_index)
    try:
        metrics = fit_and_walk_forward(series, _WORKER["k"], _WORKER["warmup"])
    except Exception as error:  # pragma: no cover - 让失败可见，不静默
        return {"perm_index": perm_index, "error": repr(error), "metrics": {}}
    return {"perm_index": perm_index, "metrics": metrics}


def run_permutation_null(series, k, warmup, perms, jobs, flush_path=None, flush_every=10):
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with mp.Pool(
        processes=max(1, int(jobs)),
        initializer=_init_worker,
        initargs=(list(series), int(k), int(warmup)),
    ) as pool:
        for done, row in enumerate(pool.imap_unordered(_perm_worker, range(perms)), start=1):
            rows.append(row)
            if (flush_path is not None) and (done % flush_every == 0 or done == perms):
                elapsed = time.perf_counter() - started
                print(f"  [null] {done}/{perms} 置换完成（{elapsed:.0f}s）", flush=True)
                FC._write_json_atomic(
                    flush_path,
                    {
                        "partial": True,
                        "perms_done": done,
                        "perms_total": perms,
                        "elapsed_seconds": round(elapsed, 1),
                        "rows": sorted(rows, key=lambda item: item["perm_index"]),
                    },
                )
    rows.sort(key=lambda item: item["perm_index"])
    return {
        "perms": int(perms),
        "jobs": int(jobs),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "rows": rows,
    }


def _null_value(rows: Sequence[dict[str, Any]], model: str, metric: str) -> list[float | None]:
    values: list[float | None] = []
    for row in rows:
        entry = (row.get("metrics") or {}).get(model) or {}
        values.append(entry.get(metric))
    return values


# --------------------------------------------------------------------------- #
# 专项：正则化扫描 / 拟合点阵带 / 示例期
# --------------------------------------------------------------------------- #
def regularization_sweep(
    series: Sequence[int], k: int, warmup: int
) -> dict[str, Any]:
    """正则化强度 α 从 0（纯记忆化）扫到 100（几乎均匀）：拟合极限 vs 样本外。

    阶数固定为 3（回退链 3→2→1→均匀）。α = 0 是「正则化 → 0，退化为纯记忆化」的极限；
    α 越大越平滑。每档同时给样本内 / 样本外四把尺子。
    """
    alphas = list(MF.REG_ALPHA_GRID) + [100.0]
    rows: list[dict[str, Any]] = []
    for alpha in alphas:
        params = {"order": MF.NGRAM_ORDER_CHAIN, "alpha": float(alpha)}
        in_sample = MF.evaluate(
            MF.MODEL_REGULARIZED_BEST_FIT, series, k, mode="in_sample", params=params, warmup=warmup
        )
        walk_forward = MF.evaluate(
            MF.MODEL_REGULARIZED_BEST_FIT,
            series,
            k,
            mode="walk_forward",
            params=params,
            warmup=warmup,
        )
        rows.append(
            {
                "alpha": float(alpha),
                "regularisation": float(alpha),
                "in_sample": _metrics(in_sample),
                "walk_forward": _metrics(walk_forward),
            }
        )
    return {
        "model": MF.MODEL_REGULARIZED_BEST_FIT,
        "order": MF.NGRAM_ORDER_CHAIN,
        "rows": rows,
        "note": (
            "α = 0 即「正则化 → 0」，退化为纯记忆化：样本内 log-loss / Brier → 0（完美拟合），"
            "但样本外 log-loss 反而爆炸（过度自信地把错号也押成近乎必然）；"
            "α 越大越靠近均匀基线。拟合越强，样本外越差 —— 这就是本演示的中心结论。"
        ),
    }


def _band_coverage(diffs: Sequence[int], band: dict[str, Any]) -> float | None:
    """给定差值序列落在带内（``|diff − offset| ≤ half_width``）的比例。"""
    values = [int(value) for value in diffs]
    if not values or band.get("samples") in (None, 0):
        return None
    offset = int(band["offset"])
    half_width = int(band["half_width"])
    return sum(1 for value in values if abs(value - offset) <= half_width) / len(values)


def lattice_block(series: Sequence[int], k: int, warmup: int, fit: dict[str, Any]) -> dict[str, Any]:
    """拟合点阵带：中心偏移 + 半宽都由数据拟合（覆盖分位由内层 walk-forward 选）。

    - ``max_fit_band``：在本池**全部**相邻差值上拟合、覆盖分位取 1.0 —— 这是
      「尽可能贴近开奖号」的字面极限（半宽加到能盖住每一个已观测差值，覆盖率必然 100%）；
    - ``coverage_sweep``：覆盖分位 0.5→1.0 逐档，在**训练段**拟合带，分别看
      训练段覆盖率、**样本外覆盖率**，以及该带驱动下的样本内 / 样本外命中与平均排名。

    这张表要说明的是：覆盖率是可以用半宽买到的，但买到 100% 覆盖的同时也买没了选择性
    （带内几乎就是 1..49），样本外命中因此仍回到 k/49。
    """
    sequence = [int(value) for value in series]
    length = len(sequence)
    split = int(round(length * MF.DEFAULT_TRAIN_RATIO))
    train = sequence[:split]
    valid = sequence[split:]
    train_diffs = [train[index] - train[index - 1] for index in range(1, len(train))]
    oos_diffs = [valid[index] - valid[index - 1] for index in range(1, len(valid))]

    max_fit_band = MF.fit_lattice_band(sequence, coverage=1.0)
    sweep: list[dict[str, Any]] = []
    for coverage in MF.LATTICE_COVERAGE_GRID:
        params = {"coverage": float(coverage)}
        train_band = MF.fit_lattice_band(train, coverage=float(coverage))
        in_sample = MF.evaluate(
            MF.MODEL_LATTICE_FIT, sequence, k, mode="in_sample", params=params, warmup=warmup
        )
        walk_forward = MF.evaluate(
            MF.MODEL_LATTICE_FIT, sequence, k, mode="walk_forward", params=params, warmup=warmup
        )
        sweep.append(
            {
                "coverage": float(coverage),
                "train_band": train_band,
                "train_coverage": _band_coverage(train_diffs, train_band),
                "out_of_sample_coverage": _band_coverage(oos_diffs, train_band),
                "in_sample": _metrics(in_sample),
                "walk_forward": _metrics(walk_forward),
            }
        )

    # 内层选中的覆盖分位（只在训练窗内选出）
    selected = dict(_params_for(fit, MF.MODEL_LATTICE_FIT))
    selected_band = (
        MF.fit_lattice_band(train, coverage=float(selected.get("coverage", 0.75)))
        if selected
        else None
    )
    return {
        "model": MF.MODEL_LATTICE_FIT,
        "max_fit_band": max_fit_band,
        "max_fit_band_oos_coverage": _band_coverage(oos_diffs, max_fit_band),
        "train_diffs": len(train_diffs),
        "out_of_sample_diffs": len(oos_diffs),
        "coverage_sweep": sweep,
        "inner_selected": selected,
        "inner_selected_train_band": selected_band,
        "note": (
            "半宽与中心偏移都由数据拟合：覆盖分位取 1.0 时半宽被撑到能盖住所有已观测差值"
            "（本池 = ±47，样本内覆盖率必然 100%，但带内几乎就是 1..49，等于放弃选择性）；"
            "覆盖分位越小带越窄，样本内覆盖率随之下降，而**样本外覆盖率始终贴着『定义期望』**"
            "——没有任何一档能在样本外把覆盖率抬高，说明这条带不含可迁移的位置信息。"
        ),
    }


def best_fit_picks(
    series: Sequence[int],
    dates: Sequence[Any],
    periods: Sequence[int],
    model: str,
    params: dict[str, Any],
    k: int,
    warmup: int,
    limit: int = 6,
) -> dict[str, Any]:
    """拟合最强模型在若干具体期上的实际输出（样本内 vs 样本外 top-10 集合）。"""
    sequence = [int(value) for value in series]
    targets = list(range(warmup, len(sequence)))
    if not targets:
        return {"model": model, "k": k, "examples": [], "note": "数据不足"}
    chosen = targets[-int(limit):]
    examples: list[dict[str, Any]] = []
    for index in chosen:
        actual = sequence[index]
        in_sample = MF.predict_in_sample(model, sequence, index, k, params)
        walk_forward = MF.predict_walk_forward(model, sequence, index, k, params)
        examples.append(
            {
                "period": int(periods[index]),
                "draw_date": str(dates[index]),
                "actual": actual,
                "in_sample_top_k": list(in_sample["picks"]),
                "in_sample_rank": int(in_sample["ranks"][actual]),
                "walk_forward_top_k": list(walk_forward["picks"]),
                "walk_forward_rank": int(walk_forward["ranks"][actual]),
                "walk_forward_hit": bool(walk_forward["ranks"][actual] <= k),
            }
        )
    return {
        "model": model,
        "model_label": MF.MODEL_LABELS.get(model, model),
        "k": k,
        "examples": examples,
        "note": (
            "样本内 top-k 是「允许反推答案」时模型会输出的集合（因此几乎必中）；"
            "样本外 top-k 是严格只用该期之前数据时的真实输出 —— 用户要看的正是后者。"
        ),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="最大拟合算法：拟合到极限，样本外仍是随机")
    parser.add_argument("--draws-json", default=str(BACKEND_ROOT / "data" / "draws_70_279.json"))
    parser.add_argument("--stage", choices=["observed", "null", "all"], default="all")
    parser.add_argument("--perms", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    parser.add_argument("--pick-count", type=int, default=MF.K_DEFAULT)
    parser.add_argument("--out", default=str(BACKEND_ROOT / "data" / "max_fit_analysis.json"))
    args = parser.parse_args()

    series, dates = FC.load_draws(args.draws_json)
    raw = json.loads(Path(args.draws_json).read_text(encoding="utf-8"))
    raw.sort(key=lambda row: (str(row["draw_date"]), int(row["period"])))
    periods = [int(row["period"]) for row in raw]
    sample_size = len(series)
    k = max(1, min(MF.NUM_STATES, int(args.pick_count)))
    warmup = MF.WARMUP
    baseline = k / MF.NUM_STATES
    out_path = Path(args.out)

    scope = (
        f"本池已导入 {sample_size} 期样本内"
        f"（第 {periods[0]}…{periods[-1]} 期，{dates[0].isoformat()}…{dates[-1].isoformat()}）"
    )

    print(f"{scope}；可评估 {sample_size - warmup} 期；k={k}；随机基线 {baseline:.4%}")
    print("=== 选参（训练窗内层 walk-forward）===")
    fit, evaluation = fit_and_evaluate(series, k, warmup)
    for model in MF.MODEL_IDS:
        entry = fit["models"].get(model) or {}
        print(
            f"{model:22s} params={entry.get('params')} "
            f"inner_hit={FC._pct(entry.get('inner_hit_rate'))}"
        )

    print("=== 样本内 vs 样本外（按样本内拟合降序）===")
    headline: list[dict[str, Any]] = []
    for model in MF.MODEL_IDS:
        spec = MF.model_spec(model)
        entry = evaluation[model]
        free_parameters = spec["free_parameters"]
        if spec.get("dynamic_free_parameters") or free_parameters is None:
            # 期序号记忆：参数数 = 被评估的期数（每期背一个答案）
            free_parameters = entry["walk_forward"]["evaluated"]
        row = {
            "model": model,
            "label": spec["label"],
            "family": spec["family"],
            "desc": spec["desc"],
            "free_parameters": free_parameters,
            "dynamic_free_parameters": bool(spec.get("dynamic_free_parameters")),
            "cells": spec["cells"],
            "params": entry["params"],
            "in_sample": entry["in_sample"],
            "walk_forward": entry["walk_forward"],
            "gap": _gap(entry["in_sample"], entry["walk_forward"]),
        }
        headline.append(row)
        ins = entry["in_sample"]
        oos = entry["walk_forward"]
        print(
            f"{model:22s} 参数={str(free_parameters):>7s} "
            f"样本内命={FC._pct(ins['hit_rate'])} 排名={_num(ins['mean_rank'])} "
            f"log-loss={_num(ins['log_loss'], 4)} | "
            f"样本外命={FC._pct(oos['hit_rate'])} 排名={_num(oos['mean_rank'])} "
            f"log-loss={_num(oos['log_loss'], 4)}"
        )
    headline.sort(key=_sort_key)

    best = headline[0] if headline else None
    best_model = best["model"] if best else MF.MODEL_MEMORIZE
    best_params = (best or {}).get("params") or {}
    picks_block = best_fit_picks(
        series, dates, periods, best_model, best_params, k, warmup, limit=6
    )

    regularization = regularization_sweep(series, k, warmup)
    print("=== 正则化扫描（order=3，α=0 → 100）===")
    for row in regularization["rows"]:
        ins = row["in_sample"]
        oos = row["walk_forward"]
        print(
            f"α={row['alpha']:<6} 样本内命={FC._pct(ins['hit_rate'])} "
            f"排名={_num(ins['mean_rank'])} log-loss={_num(ins['log_loss'], 4)} | "
            f"样本外命={FC._pct(oos['hit_rate'])} log-loss={_num(oos['log_loss'], 4)}"
        )

    lattice = lattice_block(series, k, warmup, fit)
    band = lattice["max_fit_band"]
    print(
        f"=== 拟合点阵带 ===\n"
        f"max-fit band: offset={band.get('offset')} half_width={band.get('half_width')} "
        f"样本内覆盖={FC._pct(band.get('fitted_coverage'), 1)} "
        f"样本外覆盖={FC._pct(lattice.get('max_fit_band_oos_coverage'), 1)}\n"
        f"内层选中：{lattice.get('inner_selected')}"
    )
    for row in lattice["coverage_sweep"]:
        print(
            f"  覆盖分位={row['coverage']:<5} 半宽={row['train_band'].get('half_width'):<3} "
            f"训练覆盖={FC._pct(row['train_coverage'], 1)} "
            f"样本外覆盖={FC._pct(row['out_of_sample_coverage'], 1)} | "
            f"样本内命={FC._pct(row['in_sample']['hit_rate'])} 排名={_num(row['in_sample']['mean_rank'])} | "
            f"样本外命={FC._pct(row['walk_forward']['hit_rate'])} 排名={_num(row['walk_forward']['mean_rank'])}"
        )

    result: dict[str, Any] = {
        "scope": scope,
        "sample_size": sample_size,
        "evaluated": sample_size - warmup,
        "k": k,
        "odds": MF.ODDS_DEFAULT,
        "baseline_hit_rate": baseline,
        "reference": {
            "hit_rate": baseline,
            "mean_rank": MF.UNIFORM_MEAN_RANK,
            "log_loss": MF.UNIFORM_LOG_LOSS,
            "brier": MF.UNIFORM_BRIER,
        },
        "seed": SEED,
        "perm_seed_base": PERM_SEED_BASE,
        "warmup": warmup,
        "fit_role": MF.FIT_ROLE,
        "fit_disclaimer": MF.DISCLAIMER,
        "headline": headline,
        "best_fit": {"model": best_model, "params": best_params},
        "best_fit_picks": picks_block,
        "regularization_sweep": regularization,
        "lattice": lattice,
        "notes": [
            scope + "；统计口径仅限本池样本内，禁止升格为更大口径的结论。",
            "样本内 = 允许引用被预测的观测本身（「反推」口径），命中率 / 平均排名 / log-loss 可被刷到完美。",
            "样本外 = 严格 walk-forward（预测第 i 期只读 series[:i]），一律回到均匀基线附近。",
            "置换零分布每次重跑同一套选参 + 预测流程，是「纯噪声能刷到多高」的标尺。",
            "本脚本只读，不改 DEFAULT_SETTINGS 与 recommend() 引擎，也不接入线上推荐路径。",
        ],
    }

    if args.stage in ("null", "all"):
        print(f"=== 置换零分布（{args.perms} 次，{args.jobs} 进程）===")
        null = run_permutation_null(
            series,
            k,
            warmup,
            args.perms,
            args.jobs,
            flush_path=out_path.with_name(out_path.stem + ".partial" + out_path.suffix),
            flush_every=10,
        )
        for row in headline:
            model = row["model"]
            observed = row["walk_forward"]
            row["null"] = {}
            for metric in _METRIC_KEYS:
                summary = _summarize_metric(
                    _null_value(null["rows"], model, metric),
                    observed.get(metric),
                    lower_is_better=metric in _LOWER_IS_BETTER,
                )
                row["null"][metric] = summary
            # 头条 p：样本外命中率
            row["null_p_one_sided"] = row["null"]["hit_rate"].get("p_one_sided")
            row["null_mean_hit_rate"] = row["null"]["hit_rate"].get("mean")
        result["null"] = {
            "perms": null["perms"],
            "jobs": null["jobs"],
            "elapsed_seconds": null["elapsed_seconds"],
            "rows": null["rows"],
        }
        print("=== 终表（含置换 p，单侧）===")
        for row in headline:
            print(
                f"{row['model']:22s} 样本内命={FC._pct(row['in_sample']['hit_rate'])} "
                f"样本外命={FC._pct(row['walk_forward']['hit_rate'])} "
                f"零点均值={FC._pct(row['null_mean_hit_rate'])} "
                f"p={_num(row['null_p_one_sided'], 4)}"
            )

    FC._write_json_atomic(out_path, result)
    print(f"\n[written] {out_path}")
    if best is not None:
        print(
            f"最佳样本内拟合：{best['label']}（{best['model']}），"
            f"样本内命中 {FC._pct(best['in_sample']['hit_rate'])} / "
            f"平均排名 {_num(best['in_sample']['mean_rank'])}；"
            f"样本外命中 {FC._pct(best['walk_forward']['hit_rate'])} / "
            f"平均排名 {_num(best['walk_forward']['mean_rank'])}。"
        )


def _num(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


if __name__ == "__main__":
    main()
