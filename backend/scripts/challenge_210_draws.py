r"""正面回应「就这两百多期，验证窗口缩小不就行了？」（可复现脚本）。

背景：此前结论是「本池 210 期里挑不出优于随机的配置」，并给出「要看清 +1% 需要
约 6500 期」的功效口径。使用者不接受，提出两条：① 就用这 210 期评测；② 把验证
窗口缩小。本脚本把这两条当命题逐条检验，并且**主动找支持使用者的证据**。

口径铁律：
- 全部结论只针对 **本池已导入的 N 期样本内**，不描述更大范围的历史，
  也不得升格为任何全量 / 市场结论。
- 样本不足时输出「数据不足」（英文码 INSUFFICIENT），绝不用 0 值冒充结论。
- 任何 best-of-N 统计量都必须配一个**重跑同一套选参流程**的置换零分布，
  否则「扫得多所以冠军高」会冒充成优势。

六个实验：
1. ``exp1``：只用整池（无留出窗口）挑 256 组配置里整池 Δ 最高的那组，并给出
   best-of-256 的整池置换零分布 —— 这是「就用这 210 期」的判决定量。
2. ``prespecified``：**事先指定**的单组配置（不做搜索）对 20.408% 做精确二项检验，
   并报告 256 组里最小的原始 p 是否扛得住 Bonferroni / Holm。
3. ``exp3``：验证窗 63 / 40 / 30 / 20 / 10 期的网格 —— 训练窗选冠军、验证窗打分，
   附该窗口标准误、2σ 阈值与匹配的 best-of-N 置换零分布。
4. ``exp4``：把整池换成「最近 N 期」（210 / 150 / 120 / 90 / 60），在**该窗口内**
   重新搜索并构造**该窗口内的**置换零分布，看「旧数据是过期行情」是否成立。
5. ``power``：把结论翻译成「本样本能排除多大的真实优势」（MDD）。
6. ``adversarial``：扫描全部统计量，凡是 p<0.05 的都在 ``findings`` 里点名。

两种验证窗口径都会同时给出（重要，见 ``notes.harness_history_truncation``）：
- ``harness``：与 ``services.analytics.walk_forward_eval_config`` 逐位一致 ——
  它在验证窗子序列上**重新起跑**回测，因此验证窗前几期只能看到约 2 期历史；
- ``full_history``：整池回测的前缀 / 后缀切片，每期都带完整历史 —— 这才是一般
  意义上的 walk-forward。两者训练窗完全相同，只有验证窗因历史长度不同而分叉。

运行（离线，不碰数据库；置空 DATABASE_URL 强制内存 store）：
    cd backend
    $env:DATABASE_URL=''
    .\.venv\Scripts\python.exe scripts\challenge_210_draws.py --stage all --perms 100 --jobs 11

输出：``data/challenge_210_draws.json``（``backend/data/*`` 已被 .gitignore 忽略）。
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

from services import analytics as A  # noqa: E402
from services.lottery import DEFAULT_SETTINGS  # noqa: E402

# 复用既有脚本的配置池构造 / 打乱逻辑 / 零点归纳（同一 seed ⇒ 可交叉核对）
import tune_walk_forward as T  # noqa: E402

# 验证窗档位（可评估期数）；10 期低于 harness 的 20 期下限，用于演示「硬缩」
DEFAULT_EXP3_VALID_WINDOWS: tuple[int, ...] = (63, 40, 30, 20, 10)
# 「最近 N 期」档位（整池 = 210）
DEFAULT_EXP4_RECENT_WINDOWS: tuple[int, ...] = (210, 150, 120, 90, 60)

_WORKER: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 基础：命中序列与 Δ
# --------------------------------------------------------------------------- #
def period_hits(
    series: Sequence[dict[str, Any]], config: dict[str, Any], min_prior_draws: int
) -> dict[str, Any]:
    """对一组配置跑整段回测，返回逐期命中布尔序列（与 ``series`` 的
    ``eval_start`` 对齐）与随机基线。"""
    out = A.backtest_stats(
        series,
        base_settings=config,
        min_prior_draws=min_prior_draws,
        include_results=True,
        include_wave_breakdown=False,
    )
    return {
        "hits": [bool(row["hit"]) for row in out.get("results", [])],
        "evaluated": out.get("evaluated", 0),
        "baseline": out.get("random_baseline_hit_rate"),
        "periods": [row["period"] for row in out.get("results", [])],
    }


def _delta(hits: Sequence[bool], baseline_rate: float) -> float | None:
    if not hits:
        return None
    return sum(1 for value in hits if value) / len(hits) - baseline_rate


def _hit_count(hits: Sequence[bool]) -> int:
    return sum(1 for value in hits if value)


def harness_valid_segment(
    series: Sequence[dict[str, Any]],
    config: dict[str, Any],
    split_index: int,
    min_prior_draws: int,
) -> dict[str, Any]:
    """与 ``walk_forward_eval_config`` 逐位同口径的验证窗打分。

    它把验证窗子序列 ``series[split_index - min_prior_draws:]`` 交给回测**重新起跑**，
    所以验证窗第一期只能看到 ``min_prior_draws`` 期历史（harness 的既有设计）。
    """
    subset = list(series[max(0, split_index - min_prior_draws) :])
    out = A.backtest_stats(
        subset,
        base_settings=config,
        min_prior_draws=min_prior_draws,
        include_results=False,
        include_wave_breakdown=False,
    )
    return {
        "hits": out.get("hits"),
        "evaluated": out.get("evaluated"),
        "hit_rate": out.get("hit_rate"),
        "delta": out.get("hit_rate_minus_baseline"),
    }


# --------------------------------------------------------------------------- #
# 核心：把一次整池回测展开成 exp1 / exp3 / exp4 的全部统计量
# --------------------------------------------------------------------------- #
def evaluate_all(
    series: Sequence[dict[str, Any]],
    configs: Sequence[dict[str, Any]],
    *,
    exp3_windows: Sequence[int] = DEFAULT_EXP3_VALID_WINDOWS,
    exp4_windows: Sequence[int] = DEFAULT_EXP4_RECENT_WINDOWS,
    min_prior_draws: int = A.MIN_PRIOR_DRAWS,
    want_grid: bool = False,
) -> dict[str, Any]:
    """对一段（可被打乱的）序列算出 exp1 / exp3 / exp4 的统计量。

    观测值与置零值走**同一个函数**，保证「同一套选参流程」。
    """
    sample_size = len(series)
    runs = [period_hits(series, config, min_prior_draws) for config in configs]
    baseline = runs[0]["baseline"]
    eval_count = runs[0]["evaluated"]
    eval_start = max(1, int(min_prior_draws))
    full_deltas = [_delta(run["hits"], baseline) for run in runs]

    grid: list[dict[str, Any]] = []
    if want_grid:
        for index, (config, run) in enumerate(zip(configs, runs)):
            hits = _hit_count(run["hits"])
            evaluated = run["evaluated"]
            grid.append(
                {
                    "index": index,
                    "config": config,
                    "hits": hits,
                    "evaluated": evaluated,
                    "hit_rate": (hits / evaluated) if evaluated else None,
                    "delta": full_deltas[index],
                    "p_greater": A.binomial_tail_p(
                        hits, evaluated, baseline, alternative="greater"
                    ),
                    "p_two_sided": A.binomial_tail_p(
                        hits, evaluated, baseline, alternative="two-sided"
                    ),
                }
            )

    usable = [d for d in full_deltas if d is not None]
    exp1_best = max(range(len(full_deltas)), key=lambda i: (-9.0 if full_deltas[i] is None else full_deltas[i]))
    exp1 = {
        "evaluated": eval_count,
        "max_full_delta": full_deltas[exp1_best],
        "argmax_index": exp1_best,
        "argmax_hits": _hit_count(runs[exp1_best]["hits"]),
        "argmax_hit_rate": (
            _hit_count(runs[exp1_best]["hits"]) / eval_count if eval_count else None
        ),
        "median_full_delta": sorted(usable)[len(usable) // 2] if usable else None,
        "mean_full_delta": (sum(usable) / len(usable)) if usable else None,
        "positive_delta_count": sum(1 for d in usable if d > 0),
        "config_count": len(configs),
    }

    exp3: dict[str, Any] = {}
    for window in exp3_windows:
        valid_eval = int(window)
        if valid_eval <= 0 or valid_eval >= eval_count:
            continue
        offset = eval_count - valid_eval
        train_deltas: list[float | None] = []
        valid_harness: dict[str, Any] = {"deltas": [], "hits": []}
        valid_full: list[float | None] = []
        for config, run in zip(configs, runs):
            segments = A.window_delta_from_hits(run["hits"], offset, baseline)
            train_deltas.append(segments["train"]["delta"])
            valid_full.append(segments["valid"]["delta"])
            harness = harness_valid_segment(
                series, config, eval_start + offset, min_prior_draws
            )
            valid_harness["deltas"].append(harness["delta"])
            valid_harness["hits"].append(harness["hits"])
        selected_index = max(
            range(len(configs)),
            key=lambda i: (-9.0 if train_deltas[i] is None else train_deltas[i]),
        )
        selected = {
            "config_index": selected_index,
            "train_delta": train_deltas[selected_index],
            "valid_delta_harness": valid_harness["deltas"][selected_index],
            "valid_delta_full_history": valid_full[selected_index],
            "valid_hits_harness": valid_harness["hits"][selected_index],
        }
        exp3[str(valid_eval)] = {
            "valid_eval": valid_eval,
            "train_eval": eval_count - valid_eval,
            "split_index": eval_start + offset,
            "selected": selected,
            "power": A.minimum_detectable_delta(valid_eval, baseline),
            "max_valid_delta_harness": max(
                (d for d in valid_harness["deltas"] if d is not None), default=None
            ),
            "max_valid_delta_full_history": max(
                (d for d in valid_full if d is not None), default=None
            ),
            "baseline": baseline,
        }

    exp4: dict[str, Any] = {}
    for recent in exp4_windows:
        recent = int(recent)
        if recent <= min_prior_draws or recent > sample_size:
            continue
        if recent == sample_size:
            deltas = full_deltas
            evaluated = eval_count
            hits_list = [_hit_count(run["hits"]) for run in runs]
        else:
            subset = list(series[sample_size - recent :])
            sub_runs = [period_hits(subset, config, min_prior_draws) for config in configs]
            deltas = [_delta(run["hits"], baseline) for run in sub_runs]
            evaluated = sub_runs[0]["evaluated"]
            hits_list = [_hit_count(run["hits"]) for run in sub_runs]
        best = max(range(len(deltas)), key=lambda i: (-9.0 if deltas[i] is None else deltas[i]))
        exp4[str(recent)] = {
            "recent_draws": recent,
            "evaluated": evaluated,
            "max_full_delta": deltas[best],
            "argmax_index": best,
            "argmax_hits": hits_list[best],
            "argmax_hit_rate": (hits_list[best] / evaluated) if evaluated else None,
            "positive_delta_count": sum(1 for d in deltas if d is not None and d > 0),
            "config_count": len(configs),
        }

    return {
        "sample_size": sample_size,
        "baseline": baseline,
        "eval_count": eval_count,
        "exp1": exp1,
        "exp3": exp3,
        "exp4": exp4,
        "grid": grid,
    }


# --------------------------------------------------------------------------- #
# 置换（零）分布
# --------------------------------------------------------------------------- #
def _perm_seed(index: int, window: int) -> int:
    """N=210 时与 ``tune_walk_forward`` 的置换 seed 相同，便于交叉核对。"""
    if window >= 210:
        return T.PERM_SEED_BASE + index
    return T.PERM_SEED_BASE + index * 100_003 + window


def _init_worker(
    series: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    exp3_windows: tuple[int, ...],
    exp4_windows: tuple[int, ...],
    min_prior_draws: int,
) -> None:
    _WORKER.update(
        {
            "series": series,
            "configs": configs,
            "exp3_windows": exp3_windows,
            "exp4_windows": exp4_windows,
            "min_prior_draws": min_prior_draws,
        }
    )


def apply_permutation(series: Sequence[dict[str, Any]], index: int, window: int) -> list[dict[str, Any]]:
    """窗口内打乱特码位置（期号 / 日期不动）：N 期窗口用 N 个数自己的重排。"""
    subset = list(series[len(series) - window :]) if window < len(series) else list(series)
    return T._shuffle_series(subset, _perm_seed(index, window))


def _perm_worker(index: int) -> dict[str, Any]:
    configs = _WORKER["configs"]
    min_prior = _WORKER["min_prior_draws"]
    exp3_windows = _WORKER["exp3_windows"]
    exp4_windows = _WORKER["exp4_windows"]
    sample_size = len(_WORKER["series"])

    full = apply_permutation(_WORKER["series"], index, sample_size)
    core = evaluate_all(
        full,
        configs,
        exp3_windows=exp3_windows,
        exp4_windows=(),
        min_prior_draws=min_prior,
    )
    row: dict[str, Any] = {
        "perm_index": index,
        "exp1": core["exp1"],
        "exp3": core["exp3"],
        "exp4": {},
    }
    for window in exp4_windows:
        window = int(window)
        if window <= min_prior or window > sample_size:
            continue
        if window == sample_size:
            # 整池窗口与 exp1 同一批回测，直接复用（否则会白跑一遍）
            row["exp4"][str(window)] = core["exp1"]
            continue
        subset = apply_permutation(_WORKER["series"], index, window)
        sub_core = evaluate_all(
            subset,
            configs,
            exp3_windows=(),
            exp4_windows=(),
            min_prior_draws=min_prior,
        )
        row["exp4"][str(window)] = sub_core["exp1"]
    return row


def run_permutation_null(
    series: Sequence[dict[str, Any]],
    configs: Sequence[dict[str, Any]],
    *,
    perms: int,
    jobs: int,
    exp3_windows: Sequence[int],
    exp4_windows: Sequence[int],
    min_prior_draws: int,
    flush_path: Path | None = None,
    flush_every: int = 10,
) -> dict[str, Any]:
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with mp.Pool(
        processes=max(1, int(jobs)),
        initializer=_init_worker,
        initargs=(
            list(series),
            list(configs),
            tuple(exp3_windows),
            tuple(exp4_windows),
            int(min_prior_draws),
        ),
    ) as pool:
        for done, row in enumerate(pool.imap_unordered(_perm_worker, range(perms)), start=1):
            rows.append(row)
            if done % flush_every == 0 or done == perms:
                elapsed = time.perf_counter() - started
                print(
                    f"  [null] {done}/{perms} 次置换完成（{elapsed:.0f}s，"
                    f"预计 {(elapsed / done) * (perms - done):.0f}s 剩余）",
                    flush=True,
                )
                if flush_path is not None:
                    _write_json_atomic(
                        flush_path,
                        {
                            "partial": True,
                            "perms_done": done,
                            "perms_total": perms,
                            "elapsed_seconds": round(elapsed, 1),
                            "rows": sorted(rows, key=lambda r: r["perm_index"]),
                        },
                    )
    rows.sort(key=lambda r: r["perm_index"])
    return {
        "perms": perms,
        "jobs": int(jobs),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "rows": rows,
    }


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# 指定配置（Experiment 2）与功效（Experiment 5）
# --------------------------------------------------------------------------- #
def prespecified_configs(saved: dict[str, Any], in_sample_winner: dict[str, Any]) -> list[dict[str, Any]]:
    """事先指定的单组配置（不做搜索），外加「整池事后冠军」做对照。"""
    return [
        {"name": "当前已保存设置", "config": dict(saved), "pre_specified": True},
        {
            "name": "lattice_enabled=false",
            "config": {**saved, "lattice_enabled": False},
            "pre_specified": True,
        },
        {
            "name": "exclude_repeat_zodiac=false",
            "config": {**saved, "exclude_repeat_zodiac": False},
            "pre_specified": True,
        },
        {
            "name": "pick_strategy=score_top",
            "config": {**saved, "pick_strategy": "score_top"},
            "pre_specified": True,
        },
        {
            "name": "trend_bias=neutral",
            "config": {**saved, "trend_bias": "neutral", "trend_bias_explicit": True},
            "pre_specified": True,
        },
        {
            "name": "整池样本内冠军（事后挑出，非事前指定）",
            "config": dict(in_sample_winner),
            "pre_specified": False,
        },
    ]


def prespecified_table(
    series: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    *,
    baseline_rate: float,
    min_prior_draws: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        run = period_hits(series, entry["config"], min_prior_draws)
        hits = _hit_count(run["hits"])
        evaluated = run["evaluated"]
        rate = (hits / evaluated) if evaluated else None
        rows.append(
            {
                "name": entry["name"],
                "config": entry["config"],
                "pre_specified": entry["pre_specified"],
                "hits": hits,
                "evaluated": evaluated,
                "hit_rate": rate,
                "delta": _delta(run["hits"], baseline_rate),
                "p_greater": A.binomial_tail_p(hits, evaluated, baseline_rate, alternative="greater"),
                "p_two_sided": A.binomial_tail_p(hits, evaluated, baseline_rate, alternative="two-sided"),
            }
        )
    return rows


def power_block(baseline_rate: float, windows: Sequence[int]) -> dict[str, Any]:
    """MDD：本样本能排除多大的真实优势（Experiment 5）。"""
    return {
        str(int(window)): A.minimum_detectable_delta(int(window), baseline_rate)
        for window in windows
    }


# --------------------------------------------------------------------------- #
# 零点归纳与对抗性扫描
# --------------------------------------------------------------------------- #
def summarize_rows(
    rows: Sequence[dict[str, Any]],
    observed: dict[str, Any],
    exp3_windows: Sequence[int],
    exp4_windows: Sequence[int],
) -> dict[str, Any]:
    """把置换行归纳成每个统计量的零点（复用 ``tune_walk_forward.summarize_null``）。"""
    out: dict[str, Any] = {
        "exp1": {
            "max_full_delta": T.summarize_null(
                [r["exp1"]["max_full_delta"] for r in rows],
                observed["exp1"]["max_full_delta"],
            )
        },
        "exp3": {},
        "exp4": {},
    }
    for window in exp3_windows:
        key = str(int(window))
        if key not in observed["exp3"]:
            continue
        out["exp3"][key] = {
            "selected_valid_delta_harness": T.summarize_null(
                [r["exp3"][key]["selected"]["valid_delta_harness"] for r in rows],
                observed["exp3"][key]["selected"]["valid_delta_harness"],
            ),
            "selected_valid_delta_full_history": T.summarize_null(
                [r["exp3"][key]["selected"]["valid_delta_full_history"] for r in rows],
                observed["exp3"][key]["selected"]["valid_delta_full_history"],
            ),
            "max_valid_delta_harness": T.summarize_null(
                [r["exp3"][key]["max_valid_delta_harness"] for r in rows],
                observed["exp3"][key]["max_valid_delta_harness"],
            ),
            "max_valid_delta_full_history": T.summarize_null(
                [r["exp3"][key]["max_valid_delta_full_history"] for r in rows],
                observed["exp3"][key]["max_valid_delta_full_history"],
            ),
        }
    for window in exp4_windows:
        key = str(int(window))
        if key not in observed["exp4"]:
            continue
        out["exp4"][key] = {
            "max_full_delta": T.summarize_null(
                [r["exp4"][key]["max_full_delta"] for r in rows],
                observed["exp4"][key]["max_full_delta"],
            )
        }
    return out


def adversarial_findings(
    *,
    null: dict[str, Any] | None,
    prespecified: Sequence[dict[str, Any]],
    grid_significance: dict[str, Any] | None,
    alpha: float = 0.05,
) -> list[dict[str, Any]]:
    """主动找支持使用者的证据：列出所有 p<alpha 的检验。"""
    findings: list[dict[str, Any]] = []

    if null is not None:
        for window, block in null.get("exp3", {}).items():
            for statistic, summary in block.items():
                p = summary.get("p_one_sided")
                if p is not None and p < alpha:
                    findings.append(
                        {
                            "family": f"exp3 验证窗 {window} 期",
                            "statistic": statistic,
                            "observed": summary.get("observed"),
                            "null_mean": summary.get("mean"),
                            "p_one_sided": p,
                            "hypotheses_tried": summary.get("n"),
                        }
                    )
        for window, block in null.get("exp4", {}).items():
            summary = block.get("max_full_delta") or {}
            p = summary.get("p_one_sided")
            if p is not None and p < alpha:
                findings.append(
                    {
                        "family": f"exp4 最近 {window} 期",
                        "statistic": "max_full_delta",
                        "observed": summary.get("observed"),
                        "null_mean": summary.get("mean"),
                        "p_one_sided": p,
                        "hypotheses_tried": summary.get("n"),
                    }
                )
        exp1_summary = (null.get("exp1") or {}).get("max_full_delta") or {}
        p = exp1_summary.get("p_one_sided")
        if p is not None and p < alpha:
            findings.append(
                {
                    "family": "exp1 整池 best-of-N",
                    "statistic": "max_full_delta",
                    "observed": exp1_summary.get("observed"),
                    "null_mean": exp1_summary.get("mean"),
                    "p_one_sided": p,
                    "hypotheses_tried": exp1_summary.get("n"),
                }
            )

    for row in prespecified:
        p_greater = row.get("p_greater")
        if p_greater is not None and p_greater < alpha:
            findings.append(
                {
                    "family": "exp2 单组配置精确二项（仅原始 p，未校正）",
                    "statistic": row.get("name"),
                    "observed": row.get("hit_rate"),
                    "null_mean": None,
                    "p_one_sided": p_greater,
                    "hypotheses_tried": len(prespecified),
                }
            )

    if grid_significance is not None:
        best = grid_significance.get("best_p_greater") or {}
        if best.get("p_greater") is not None and best["p_greater"] < alpha:
            findings.append(
                {
                    "family": "exp2 整池 256 组里的最小原始 p（未校正）",
                    "statistic": f"config_index={best.get('index')}",
                    "observed": best.get("hit_rate"),
                    "null_mean": None,
                    "p_one_sided": best.get("p_greater"),
                    "hypotheses_tried": grid_significance.get("tested"),
                }
            )
    return findings


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="正面回应「只用这两百多期 + 缩小验证窗」"
    )
    parser.add_argument(
        "--draws-json", default=str(BACKEND_ROOT / "data" / "draws_70_279.json")
    )
    parser.add_argument("--base-json", default="", help="基线设置 JSON（缺省用 dev 快照）")
    parser.add_argument("--stage", choices=["observed", "null", "all"], default="all")
    parser.add_argument("--sample-size", type=int, default=256, help="配置池大小")
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--perms", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument(
        "--out", default=str(BACKEND_ROOT / "data" / "challenge_210_draws.json")
    )
    args = parser.parse_args()

    base = dict(T.SAVED_SETTINGS_SNAPSHOT)
    if args.base_json:
        base.update(json.loads(Path(args.base_json).read_text(encoding="utf-8")))
    draws = T.load_draws(args.draws_json)
    split = A.walk_forward_split(draws)
    series = split["series"]
    sample_size = split["sample_size"]
    min_prior = A.MIN_PRIOR_DRAWS

    pool = T.build_pool(base, args.sample_size, args.seed)
    configs = pool["configs"]
    print(
        f"本池已导入 {sample_size} 期；配置空间 {pool['declared_size']} 组，"
        f"实际评估 {pool['evaluated_size']} 组（含 {pool['anchor_count']} 个锚点）"
    )

    out_path = Path(args.out)
    result: dict[str, Any] = {}
    if out_path.exists():
        try:
            result = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {}

    result.update(
        {
            "scope": f"本池已导入 {sample_size} 期样本内",
            "sample_size": sample_size,
            "base_settings": base,
            "pool": {
                "declared_size": pool["declared_size"],
                "evaluated_size": pool["evaluated_size"],
                "anchor_count": pool["anchor_count"],
                "seed": pool["seed"],
            },
        }
    )

    exp3_windows = tuple(DEFAULT_EXP3_VALID_WINDOWS)
    exp4_windows = tuple(DEFAULT_EXP4_RECENT_WINDOWS)

    observed = None
    if args.stage in ("observed", "all"):
        started = time.perf_counter()
        observed = evaluate_all(
            series, configs, exp3_windows=exp3_windows, exp4_windows=exp4_windows,
            min_prior_draws=min_prior, want_grid=True,
        )
        baseline = observed["baseline"]
        print(f"\n=== 观测（本池整池，{time.perf_counter() - started:.0f}s）===")
        exp1 = observed["exp1"]
        print(
            f"exp1 整池 best-of-{exp1['config_count']}：Δ={exp1['max_full_delta']:+.4f} "
            f"（{exp1['argmax_hits']}/{exp1['evaluated']}="
            f"{exp1['argmax_hit_rate']:.4f}），零均值={exp1['mean_full_delta']:+.4f}，"
            f"{exp1['positive_delta_count']}/{exp1['config_count']} 组 Δ>0"
        )
        for window, block in observed["exp3"].items():
            selected = block["selected"]
            print(
                f"exp3 验证窗 {window} 期：冠军 train Δ={selected['train_delta']:+.4f} → "
                f"harness valid Δ={selected['valid_delta_harness']:+.4f}、"
                f"full-history valid Δ={selected['valid_delta_full_history']:+.4f}"
            )
        for window, block in observed["exp4"].items():
            print(
                f"exp4 最近 {window} 期：max Δ={block['max_full_delta']:+.4f} "
                f"（{block['argmax_hits']}/{block['evaluated']}）"
            )

        in_sample_winner = dict(configs[exp1["argmax_index"]])
        rows = prespecified_table(
            series,
            prespecified_configs(base, in_sample_winner),
            baseline_rate=baseline,
            min_prior_draws=min_prior,
        )
        print("\n=== 事先指定的单组配置（精确二项 vs 20.408%）===")
        for row in rows:
            print(
                f"{row['name']}：{row['hits']}/{row['evaluated']}="
                f"{row['hit_rate']:.4f}，Δ={row['delta']:+.4f}，"
                f"单侧 p={row['p_greater']:.4f}，双侧 p={row['p_two_sided']:.4f}"
            )

        grid = observed["grid"]
        pvalues = [entry["p_greater"] for entry in grid]
        holm = A.holm_adjusted_p(pvalues, 0.05)
        best_index = min(range(len(grid)), key=lambda i: pvalues[i])
        grid_significance = {
            "tested": len(grid),
            "alpha": 0.05,
            "bonferroni_threshold": 0.05 / len(grid),
            "holm_survivors": holm["significant_count"],
            "bonferroni_survivors": sum(
                1 for p in pvalues if p <= 0.05 / len(grid)
            ),
            "min_holm_adjusted_p": min(holm["adjusted"]),
            "best_p_greater": grid[best_index],
            "holm_adjusted": holm["adjusted"],
        }
        print(
            f"{len(grid)} 组整池 Δ 的最小原始单侧 p={pvalues[best_index]:.5f}；"
            f"Bonferroni 阈值={0.05 / len(grid):.6f}，扛住={grid_significance['bonferroni_survivors']} 组；"
            f"Holm 扛住={holm['significant_count']} 组（最小校正 p={grid_significance['min_holm_adjusted_p']:.4f}）"
        )

        print("\n=== 功效：本样本能排除多大的真实优势（MDD）===")
        power = power_block(baseline, [observed["eval_count"]] + list(exp4_windows))
        for key, block in power.items():
            print(
                f"可评估 {key} 期：标准误={block['standard_error']:.4f}，"
                f"2σ={block['two_sigma_delta']:.4f}，80% 功效 MDD={block['minimum_detectable_delta']:.4f}"
            )

        result["observed"] = {
            "baseline": baseline,
            "eval_count": observed["eval_count"],
            "exp1": observed["exp1"],
            "exp3": observed["exp3"],
            "exp4": observed["exp4"],
            "prespecified": rows,
            "grid_significance": grid_significance,
            "power": power,
            "grid": grid,
        }
        result["notes"] = {
            "scope": f"本池已导入 {sample_size} 期样本内",
            "harness_history_truncation": (
                "harness 口径（walk_forward_eval_config）在验证窗子序列上重新起跑回测，"
                "验证窗前几期只有约 2 期历史；full_history 口径从整池回测切片，"
                "每期都带完整历史。两者训练窗完全相同，差异只来自验证窗可见历史长度。"
            ),
            "baseline_note": "随机参考值 = 有效注数 / 49 = 10/49 = 20.408%。",
        }
        _write_json_atomic(out_path, result)

    if args.stage in ("null", "all"):
        if observed is None:
            # 复用上一次 ``--stage observed`` 写下的观测值，避免重跑 ~5 分钟；
            # 只有在同一进程内才需要现场计算。
            observed = result.get("observed")
        if observed is None:
            observed = evaluate_all(
                series, configs, exp3_windows=exp3_windows, exp4_windows=exp4_windows,
                min_prior_draws=min_prior,
            )
        print(
            f"\n=== best-of-N 置换零分布（{args.perms} 次，{args.jobs} 进程，"
            f"{len((exp3_windows)) + len(exp4_windows)} 个统计量族）==="
        )
        null = run_permutation_null(
            series,
            configs,
            perms=args.perms,
            jobs=args.jobs,
            exp3_windows=exp3_windows,
            exp4_windows=exp4_windows,
            min_prior_draws=min_prior,
            flush_path=out_path.with_name(out_path.stem + ".partial" + out_path.suffix),
            flush_every=max(1, args.flush_every),
        )
        summaries = summarize_rows(null["rows"], observed, exp3_windows, exp4_windows)
        result["null"] = {
            "perms": null["perms"],
            "jobs": null["jobs"],
            "elapsed_seconds": null["elapsed_seconds"],
            "summaries": summaries,
            "rows": null["rows"],
        }
        if "observed" not in result:
            result["observed"] = {
                "baseline": observed["baseline"],
                "eval_count": observed["eval_count"],
                "exp1": observed["exp1"],
                "exp3": observed["exp3"],
                "exp4": observed["exp4"],
            }

        print("\n=== 零点判读 ===")
        exp1_summary = summaries["exp1"]["max_full_delta"]
        print(
            f"exp1 整池：观测 {exp1_summary['observed']:+.4f}，零点均值 "
            f"{exp1_summary['mean']:+.4f}（sd {exp1_summary['sd']:.4f}，p95 "
            f"{exp1_summary['p95']:+.4f}）→ 单侧 p={exp1_summary['p_one_sided']:.4f}"
        )
        for window, block in summaries["exp3"].items():
            harness = block["selected_valid_delta_harness"]
            full = block["selected_valid_delta_full_history"]
            print(
                f"exp3 验证窗 {window} 期：harness 观测 {harness['observed']:+.4f}"
                f"（零点均值 {harness['mean']:+.4f}，p={harness['p_one_sided']:.4f}）· "
                f"full_history 观测 {full['observed']:+.4f}"
                f"（零点均值 {full['mean']:+.4f}，p={full['p_one_sided']:.4f}）"
            )
        for window, block in summaries["exp4"].items():
            summary = block["max_full_delta"]
            print(
                f"exp4 最近 {window} 期：观测 {summary['observed']:+.4f}，"
                f"零点均值 {summary['mean']:+.4f}（p95 {summary['p95']:+.4f}）→ "
                f"单侧 p={summary['p_one_sided']:.4f}"
            )

        grid_significance = (result.get("observed") or {}).get("grid_significance")
        findings = adversarial_findings(
            null=summaries,
            prespecified=(result.get("observed") or {}).get("prespecified") or [],
            grid_significance=grid_significance,
        )
        result["findings"] = findings
        if findings:
            print("\n=== 对抗性扫描：p<0.05 的检验（含事后挑出的）===")
            for finding in findings:
                print(
                    f"- {finding['family']} · {finding['statistic']}：观测 "
                    f"{finding['observed']}，单侧 p={finding['p_one_sided']:.4f}"
                )
        else:
            print("\n=== 对抗性扫描：没有任何检验达到 p<0.05 ===")
        _write_json_atomic(out_path, result)

    print(f"\n[written] {out_path}")


if __name__ == "__main__":
    main()
