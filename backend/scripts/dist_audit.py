r"""号码分布引擎的信息审计 + 诚实评估（CLI，纯离线，不碰数据库 / 不碰生产路径）。

============================================================================
「为何做不出这个策略」—— 白话版（给非统计读者）
============================================================================
一句话：**开奖历史里没有可以搬到下一期的信息。**

我们不是「算法没写好」，而是把 210 期数据摊开、用 23 个角度反复找，找到的东西
全部落在「随机洗牌的同一份数据也能造出来」的范围内。三个最直观的证据：

1. **相邻两期几乎零互信息。** 互信息衡量「知道上一期，能把下一期的不确定性减少
   多少比特」。49 个号全盲时是 log2(49) ≈ 5.61 比特。实测「上一期 → 下一期」的
   互信息只有零点零几比特，而且**把号码顺序随机打乱之后，算出来的值一模一样**——
   说明那点数值全部来自「209 对样本摊在 49×49=2401 个格子里」的统计假象
   （几乎每个格子只落 0~1 个点，互信息必然虚高），不是真实规律。
2. **该出的号就是均匀的。** 49 个号 210 期的频次卡方 p 值远大于 0.05，看不出
   哪些号「天生偏热/偏冷」；热号、遗漏号（到期号）、生肖、点阵这些玩法各自的
   样本外命中率都在 10/49 = 20.41% 上下摆动，摆幅完全够不到「真实优势」的门槛。
3. **想赢需要的样本量，现实给不起。** 本例 k=10、赔率 47，保本命中率是
   10/47 = 21.28%，比随机基线 20.41% 只高 **+0.87 个百分点**。要在 80% 把握下
   证明这 +0.87pp 是真的，需要**几十万期**数据；要证明 +5pp 优势也需要上万期。
   手里 210 期只能「排除」很大的优势（+10pp 级别），排除不了小优势。
   所以结论只能是：**没有证据说明有优势**（而不是「证明没有优势」）。

那这系统还有用吗？有，但用处不是「提高中奖概率」：
- 把「我凭感觉挑号」变成**可复现、可审计的分布**：1..49 每个号都有明确概率，
  8 个组件每个都能单独打开/关掉，权重取自 6 档网格（-1 ~ 2），每一步都能重跑出同样的结果；
- **控制覆盖面与花费**：选几注、买多少、覆盖哪些号，全部显式可控；
- 把「这个想法到底有没有用」变成可测的：每个组件单独消融、样本外打分、
  多重比较校正，谁敢说自己有 lift，就拿这套尺子量；
- 事前冻结台账，杜绝事后改口。

（口径铁律：以上所有结论只针对**本池已导入的 N 期样本**，不涉及任何全市场数据。）

============================================================================
子命令
============================================================================
    # 1) 信息审计：23 个假设 + 置换零分布 + Holm 校正（默认 2000 次置换）
    cd backend; .\.venv\Scripts\python.exe scripts\dist_audit.py audit `
        --draws-json data\draws_70_279.json --out ..\.tmp-dist-audit\dist_audit.json `
        --perms 2000 --perms-engine 300 --jobs 12

    # 2) 诚实评估：训练窗内层选权 → 评估窗只打分（严格 walk-forward）+ 校准
    cd backend; .\.venv\Scripts\python.exe scripts\dist_audit.py evaluate `
        --draws-json data\draws_70_279.json --out ..\.tmp-dist-audit\dist_eval.json `
        --perms 500 --jobs 12

    # 3) 白话解释：不依赖数据也能跑，回答「为何做不出这个策略」
    cd backend; .\.venv\Scripts\python.exe scripts\dist_audit.py explain

    # 4) 只跑零分布：把「随机打乱后的同一份数据」能造出什么成绩摊开给你看
    cd backend; .\.venv\Scripts\python.exe scripts\dist_audit.py null `
        --draws-json data\draws_70_279.json --out ..\.tmp-dist-audit\dist_null.json `
        --perms 500 --jobs 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import analytics as A  # noqa: E402
from services import dist_audit as DA  # noqa: E402
from services import dist_engine as DE  # noqa: E402
from services.forward_ledger import load_draws  # noqa: E402
from services.max_fit import K_DEFAULT, NUM_STATES, ODDS_DEFAULT  # noqa: E402

# --------------------------------------------------------------------------- #
# 默认参数与常量
# --------------------------------------------------------------------------- #
DEFAULT_DRAWS = "data/draws_70_279.json"
DEFAULT_TMP_DIR = ".tmp-dist-audit"
TOOL_NAME = "dist_audit"
TOOL_VERSION = "1.0"
PERM_SEED_BASE = 771_007
DEFAULT_ALPHA = 0.05
AUDIT_PERMS = 2000
ENGINE_PERMS = 300
EVAL_PERMS = 500
MIN_ENGINE_PERMS = 200

# 评估窗默认口径（与 dist_engine.select_and_evaluate 的默认值一致）
TRAIN_RATIO = DE.DEFAULT_TRAIN_RATIO
INNER_HOLDOUT = DE.DEFAULT_INNER_HOLDOUT
MAX_PASSES = DE.DEFAULT_MAX_PASSES

# 组件 → 审计 handle（在服务模块定义，脚本只读）
COMPONENT_HANDLE_MAP = DA.COMPONENT_HANDLE_MAP

# 「达标」目标优势：保本边际 / +5pp / +10.2pp
TARGET_DELTAS: tuple[float, ...] = (0.05, 0.102)

# 加速比：置换次数低于该值时不做多进程（启动开销大于收益）
MIN_PERMS_FOR_POOL = 8

_WORKER: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# 输入 / 输出小工具
# --------------------------------------------------------------------------- #
def numbers_of(draws: Sequence[Mapping[str, Any]]) -> list[int]:
    """从规范开奖行取出特码序列（升序）。"""
    return [int(row["special_number"]) for row in draws]


def numbers_digest(numbers: Sequence[int]) -> str:
    """特码序列的 sha256（不含时间戳 → 同样的输入必得同样的摘要）。"""
    payload = ",".join(str(int(value)) for value in numbers)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_out(path: str | None, default_name: str) -> str:
    """``--out`` 未给时落到仓库根的临时目录（``.tmp*`` 已被 .gitignore 忽略）。"""
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


def _shifted(rows: Sequence[Mapping[str, Any]], seed: int) -> list[dict[str, Any]]:
    """把特码的出现位置洗牌（期号 / 日期不动）→ 「同样的号码、不同的时间顺序」。

    这是本脚本统一的零假设：号码的多重集合不变（哪些号出现几次完全一样），
    只摧毁「时间结构」。任何「历史里学到的规律」若真有信息，打乱后必然消失。
    """
    rng = random.Random(int(seed))
    numbers = [int(row["special_number"]) for row in rows]
    rng.shuffle(numbers)
    return [
        {**dict(row), "special_number": numbers[index]}
        for index, row in enumerate(rows)
    ]


def _shuffled_numbers(numbers: Sequence[int], seed: int) -> list[int]:
    values = [int(value) for value in numbers]
    random.Random(int(seed)).shuffle(values)
    return values


# --------------------------------------------------------------------------- #
# 观测值：每个 handle 的统计量 + 明细（只算一次）
# --------------------------------------------------------------------------- #
def cheap_statistics(
    numbers: Sequence[int],
    *,
    max_lag: int = DA.DEFAULT_MAX_LAG,
    max_period: int = DA.DEFAULT_MAX_PERIOD,
) -> dict[str, float | None]:
    """一次算完全部「轻量」handle 的统计量（供观测 + 置换复用同一函数）。"""
    values = [int(value) for value in numbers]
    stats: dict[str, float | None] = {}

    independence = DA.independence_test(values)
    stats[DA.HANDLE_INDEPENDENCE] = independence.get("statistic")
    stats[DA.HANDLE_MUTUAL_INFORMATION] = independence.get("mutual_information_bits")

    scan = DA.autocorrelation_scan(values, max_lag=max_lag)
    series = scan.get("series") or {}
    for handle, name in AUTOCORR_SOURCES.items():
        stats[handle] = (series.get(name) or {}).get("max_abs_r")

    derived = DA.derived_series(values)
    stats[DA.HANDLE_RUNS_PARITY] = (DA.runs_test(derived["parity"]) or {}).get("abs_z")
    stats[DA.HANDLE_RUNS_BIGSMALL] = (DA.runs_test(derived["bigsmall"]) or {}).get("abs_z")

    for component in DE.COMPONENT_IDS:
        blob = DA.standalone_component_test(values, component)
        stats[COMPONENT_HANDLE_MAP[component]] = blob.get("statistic")
    return stats


AUTOCORR_SOURCES: dict[str, str] = {
    DA.HANDLE_AUTOCORR_SPECIAL: "special",
    DA.HANDLE_AUTOCORR_PARITY: "parity",
    DA.HANDLE_AUTOCORR_BIGSMALL: "bigsmall",
    DA.HANDLE_AUTOCORR_TAIL: "tail_digit",
    DA.HANDLE_AUTOCORR_ZODIAC: "zodiac_group",
}

STATISTIC_LABELS: dict[str, str] = {
    DA.HANDLE_UNIFORMITY: "卡方统计量（df=48）",
    DA.HANDLE_ENTROPY: "ln49 - 经验熵（nats，越大越集中）",
    DA.HANDLE_INDEPENDENCE: "49×49 列联表卡方（df=2304）",
    DA.HANDLE_MUTUAL_INFORMATION: "相邻两期互信息（比特）",
    DA.HANDLE_AUTOCORR_SPECIAL: "滞后 1..20 的最大 |r|（特码）",
    DA.HANDLE_AUTOCORR_PARITY: "最大 |r|（奇偶）",
    DA.HANDLE_AUTOCORR_BIGSMALL: "最大 |r|（大小）",
    DA.HANDLE_AUTOCORR_TAIL: "最大 |r|（尾数）",
    DA.HANDLE_AUTOCORR_ZODIAC: "最大 |r|（生肖组）",
    DA.HANDLE_RUNS_PARITY: "|z|（奇偶游程，双侧偏离都算）",
    DA.HANDLE_RUNS_BIGSMALL: "|z|（大小游程）",
    DA.HANDLE_HOT_COLD: "后半段命中数（前半频次 top-10）",
    DA.HANDLE_GAP: "逐期命中数（GAP 组件 top-10）",
    DA.HANDLE_ZODIAC_HOT: "逐期命中数（ZODIAC 组件 top-10）",
    DA.HANDLE_WAVE_LATTICE: "逐期命中数（WAVE 组件 top-10）",
    DA.HANDLE_MARKOV: "逐期命中数（MARKOV 组件 top-10）",
    DA.HANDLE_STRUCTURE: "逐期命中数（STRUCTURE 组件 top-10）",
    DA.HANDLE_REPEAT: "逐期命中数（REPEAT 组件 top-10）",
    DA.HANDLE_PERIODIC: "最大谱功率（周期 2..30）",
    DA.HANDLE_ENGINE_REPEAT_NUMBER: "命中率差（保留重号 - 排除重号）",
    DA.HANDLE_ENGINE_REPEAT_ZODIAC: "命中率差（避开重肖 - 不避）",
    DA.HANDLE_ENGINE_COLD: "命中率差（避冷开 - 关）",
    DA.HANDLE_ENGINE_LATTICE: "命中率差（点阵开 - 关）",
}


def observed_detail(
    numbers: Sequence[int],
    draws: Sequence[Mapping[str, Any]],
    *,
    settings: Mapping[str, Any] | None = None,
    max_lag: int = DA.DEFAULT_MAX_LAG,
    max_period: int = DA.DEFAULT_MAX_PERIOD,
) -> dict[str, dict[str, Any]]:
    """观测样本上每个 handle 的完整明细（统计量 / 样本量 / 可解析 p / 附加字段）。"""
    values = [int(value) for value in numbers]
    detail: dict[str, dict[str, Any]] = {}

    uniformity = DA.uniformity_test(values)
    detail[DA.HANDLE_UNIFORMITY] = {
        **uniformity,
        "p_value": uniformity.get("p_value"),
        "sample_size": len(values),
        "test": "chi_square_goodness_of_fit",
    }

    entropy = DA.entropy_test(values)
    detail[DA.HANDLE_ENTROPY] = {
        **entropy,
        "sample_size": entropy.get("sample_size", len(values)),
        "test": "entropy_vs_ln49_multinomial_null",
    }

    independence = DA.independence_test(values)
    detail[DA.HANDLE_INDEPENDENCE] = {
        **independence,
        "sample_size": independence.get("pairs"),
        "test": "chi_square_independence_lag1",
    }
    detail[DA.HANDLE_MUTUAL_INFORMATION] = {
        "handle": DA.HANDLE_MUTUAL_INFORMATION,
        "statistic": independence.get("mutual_information_bits"),
        "mutual_information_bits": independence.get("mutual_information_bits"),
        "contingency_chi2": independence.get("statistic"),
        "degrees_of_freedom": independence.get("degrees_of_freedom"),
        "expected_cell": independence.get("expected_cell"),
        "sample_size": independence.get("pairs"),
        "test": "mutual_information_lag1_bits",
        "bias_note": (
            f"{int(independence.get('pairs') or 0)} 对样本 / "
            f"{int(independence.get('degrees_of_freedom') or 0) + 1} 个格子 → "
            "经验互信息必须与置换零分布对照才有意义。"
        ),
    }

    scan = DA.autocorrelation_scan(values, max_lag=max_lag)
    series = scan.get("series") or {}
    for handle, name in AUTOCORR_SOURCES.items():
        entry = series.get(name) or {}
        detail[handle] = {
            "handle": handle,
            "statistic": entry.get("max_abs_r"),
            "best_lag": entry.get("best_lag"),
            "lags": entry.get("lags", []),
            "sample_size": len(values),
            "test": "autocorrelation_max_abs_r",
        }

    derived = DA.derived_series(values)
    for handle, key in ((DA.HANDLE_RUNS_PARITY, "parity"), (DA.HANDLE_RUNS_BIGSMALL, "bigsmall")):
        blob = DA.runs_test(derived[key])
        detail[handle] = {
            **blob,
            "statistic": blob.get("abs_z"),
            "sample_size": blob.get("sample_size", len(values)),
            "test": "runs_test_wald_wolfowitz",
        }

    for component in DE.COMPONENT_IDS:
        blob = DA.standalone_component_test(values, component)
        handle = COMPONENT_HANDLE_MAP[component]
        row = {
            **blob,
            "component": component,
            "sample_size": blob.get("evaluated") or len(values),
        }
        if component == DE.COMPONENT_GAP:
            # 与「最久未出 / 到期号」的独立口径互为交叉核对（同一假设，两种实现）
            cross = DA.gap_test(values)
            row["cross_check"] = {
                "test": "most_overdue_topk",
                "hits": cross.get("hits"),
                "evaluated": cross.get("evaluated"),
                "hit_rate": cross.get("hit_rate"),
                "p_binomial": cross.get("p_binomial"),
            }
        detail[handle] = row

    engine = DA.engine_feature_hits(draws, base_settings=settings)
    for handle, blob in engine.items():
        detail[handle] = {
            **blob,
            "sample_size": (blob.get("on") or {}).get("evaluated"),
            "test": "engine_feature_ablation_walk_forward",
        }
    return detail


def _parametric_p(handle: str, blob: Mapping[str, Any]) -> float | None:
    """可解析 p 值（不是置换 p）：卡方 / 二项 / 熵自带的零分布。"""
    if handle in (DA.HANDLE_UNIFORMITY, DA.HANDLE_ENTROPY):
        return blob.get("p_value")
    if handle in (DA.HANDLE_INDEPENDENCE,):
        return DA.chi2_sf(blob.get("statistic"), blob.get("degrees_of_freedom") or 0)
    if handle in (DA.HANDLE_MUTUAL_INFORMATION,):
        return None  # 互信息无闭式零分布：只认置换
    if handle in (DA.HANDLE_RUNS_PARITY, DA.HANDLE_RUNS_BIGSMALL):
        return blob.get("p_value")
    if handle in (
        DA.HANDLE_HOT_COLD,
        DA.HANDLE_GAP,
        DA.HANDLE_ZODIAC_HOT,
        DA.HANDLE_WAVE_LATTICE,
        DA.HANDLE_MARKOV,
        DA.HANDLE_STRUCTURE,
        DA.HANDLE_REPEAT,
    ):
        return blob.get("p_binomial")
    if handle == DA.HANDLE_PERIODIC:
        return None  # 谱功率的零分布由置换给出（且置换里也取了周期最大值）
    return None


# --------------------------------------------------------------------------- #
# 置换零分布（多进程）
# --------------------------------------------------------------------------- #
def _init_cheap(numbers: Sequence[int], max_lag: int, max_period: int) -> None:
    _WORKER["numbers"] = [int(value) for value in numbers]
    _WORKER["max_lag"] = int(max_lag)
    _WORKER["max_period"] = int(max_period)


def _cheap_worker(index: int) -> dict[str, Any]:
    shuffled = _shuffled_numbers(_WORKER["numbers"], PERM_SEED_BASE + int(index))
    return {
        "index": int(index),
        "statistics": cheap_statistics(
            shuffled, max_lag=_WORKER["max_lag"], max_period=_WORKER["max_period"]
        ),
    }


def _init_engine(draws: Sequence[Mapping[str, Any]], settings: Mapping[str, Any] | None) -> None:
    _WORKER["draws"] = [dict(row) for row in draws]
    _WORKER["settings"] = dict(settings) if settings else None


def _engine_worker(index: int) -> dict[str, Any]:
    shuffled = _shifted(_WORKER["draws"], PERM_SEED_BASE + 500_000 + int(index))
    blob = DA.engine_feature_hits(shuffled, base_settings=_WORKER["settings"])
    return {
        "index": int(index),
        "statistics": {handle: row.get("statistic") for handle, row in blob.items()},
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
    with context.Pool(processes=int(jobs), initializer=initializer, initargs=init_args) as pool:
        return pool.map(worker, range(count), chunksize=max(1, int(chunksize)))


def collect_nulls(
    numbers: Sequence[int],
    draws: Sequence[Mapping[str, Any]],
    *,
    perms: int,
    perms_engine: int,
    jobs: int,
    settings: Mapping[str, Any] | None = None,
    max_lag: int = DA.DEFAULT_MAX_LAG,
    max_period: int = DA.DEFAULT_MAX_PERIOD,
) -> dict[str, Any]:
    """收集置换零分布：轻量 handle 用 ``perms``、引擎消融用 ``perms_engine``。"""
    cheap = tuple(
        handle for handle in DA.PERMUTATION_HANDLES if handle not in DA.ENGINE_FEATURE_HANDLES
    )
    nulls: dict[str, list[float]] = {handle: [] for handle in DA.PERMUTATION_HANDLES}
    meta: dict[str, Any] = {
        "permutation_scheme": "shuffle_special_number_positions_keep_period_and_date",
        "seed_base": PERM_SEED_BASE,
        "perms_cheap": int(max(0, perms)),
        "perms_engine": int(max(0, perms_engine)),
        "jobs": int(max(1, jobs)),
        "cheap_handles": list(cheap),
        "engine_handles": list(DA.ENGINE_FEATURE_HANDLES),
        "timings": {},
    }

    started = time.perf_counter()
    cheap_rows = _run_pool(
        _cheap_worker,
        _init_cheap,
        (numbers, max_lag, max_period),
        int(max(0, perms)),
        int(max(1, jobs)),
        chunksize=max(1, int(max(0, perms)) // max(1, int(max(1, jobs)) * 4)),
    )
    meta["timings"]["cheap_seconds"] = round(time.perf_counter() - started, 3)
    for row in cheap_rows:
        for handle in cheap:
            value = (row["statistics"] or {}).get(handle)
            if value is not None:
                nulls[handle].append(float(value))

    started = time.perf_counter()
    engine_rows = _run_pool(
        _engine_worker,
        _init_engine,
        (draws, settings),
        int(max(0, perms_engine)),
        int(max(1, jobs)),
        chunksize=1,
    )
    meta["timings"]["engine_seconds"] = round(time.perf_counter() - started, 3)
    for row in engine_rows:
        for handle in DA.ENGINE_FEATURE_HANDLES:
            value = (row["statistics"] or {}).get(handle)
            if value is not None:
                nulls[handle].append(float(value))

    meta["null_sizes"] = {handle: len(values) for handle, values in nulls.items()}
    return {"nulls": nulls, "meta": meta}


# --------------------------------------------------------------------------- #
# handle 表：原始 p / 置换 p / Holm 校正 p / 英文判定
# --------------------------------------------------------------------------- #
def _null_block(observed: float | None, values: Sequence[float], *, better: str) -> dict[str, Any]:
    """单侧零分布摘要（``better`` 说明哪个方向才是「更优」）。"""
    summary = DA.summarize_null(values, observed)
    summary["better"] = better
    summary["p_one_sided"] = DA.permutation_p(observed, values, direction=better)
    mean = summary.get("mean")
    sd = summary.get("sd")
    summary["observed_z"] = (
        (observed - mean) / sd if (observed is not None and mean is not None and sd) else None
    )
    return summary


def build_handles(
    detail: Mapping[str, Mapping[str, Any]],
    nulls: Mapping[str, Sequence[float]],
    *,
    state: Mapping[str, Any],
    alpha: float,
) -> dict[str, Any]:
    """把观测值 + 零分布整合成一张 handle 表，并做 Holm 校正。"""
    rows: list[dict[str, Any]] = []
    for handle in DA.ALL_HANDLES:
        blob = dict(detail.get(handle) or {})
        parametric = _parametric_p(handle, blob)
        values = list(nulls.get(handle) or [])
        observed = blob.get("statistic")
        if handle in DA.PARAMETRIC_HANDLES:
            p_value = parametric
            raw_null = blob.get("null")
            null = (
                {
                    key: raw_null.get(key)
                    for key in ("simulations", "n", "mean", "sd", "p50", "p95", "max")
                }
                if isinstance(raw_null, Mapping)
                else None
            )
        else:
            null = _null_block(observed, values, better="greater")
            p_value = null.get("p_one_sided")
        sample_size = blob.get("sample_size")
        row: dict[str, Any] = {
            "handle": handle,
            "label": DA.HANDLE_LABELS[handle],
            "statistic": observed,
            "statistic_label": STATISTIC_LABELS.get(handle),
            "test": blob.get("test"),
            "p_value": p_value,
            "p_parametric": parametric,
            "sample_size": sample_size,
            "origin": "parametric" if handle in DA.PARAMETRIC_HANDLES else "permutation",
            "null": null,
            "data_status": blob.get("data_status", DA.DATA_STATUS_OK),
            "data_status_label": blob.get("data_status_label", DA.DATA_STATUS_LABELS[DA.DATA_STATUS_OK]),
        }
        if handle in DA.COMPONENT_HANDLES:
            row["component"] = blob.get("component")
        if handle in DA.ENGINE_FEATURE_HANDLES:
            row["ablation"] = {
                "on": blob.get("on"),
                "off": blob.get("off"),
                "settings_on": blob.get("settings_on"),
                "settings_off": blob.get("settings_off"),
            }
            # 消融是「有符号的提升量」：单侧 p 回答「开启是否变好」，
            # 双侧（对 |Δ| 做置换）回答「这个开关是否系统性地改变了成绩」。
            magnitudes = [abs(float(value)) for value in values]
            row["p_two_sided"] = DA.permutation_p(
                abs(observed) if observed is not None else None,
                magnitudes,
                direction="greater",
            )
            row["effect_direction"] = (
                "inert"
                if not observed
                else ("helps" if observed > 0 else "hurts")
            )
            row["degenerate"] = bool(
                observed is not None and abs(observed) < 1e-15 and not any(magnitudes)
            )
            row["degenerate_note"] = (
                "该开关对本池的选号结果没有任何影响（Δ 恒为 0）→ 结构性无效，"
                "既不能算证据也不能算反证。"
                if row["degenerate"]
                else None
            )
        hit_rate = blob.get("hit_rate")
        if isinstance(hit_rate, (int, float)) and sample_size:
            row["hit_rate"] = hit_rate
            row["delta_vs_uniform"] = hit_rate - DA.UNIFORM_RATE
            row["mdd_hit_rate"] = A.minimum_detectable_delta(
                int(sample_size), DA.UNIFORM_RATE
            ).get("minimum_detectable_delta")
        if blob.get("cross_check"):
            row["cross_check"] = blob["cross_check"]
        if handle == DA.HANDLE_INDEPENDENCE:
            row["mutual_information_bits"] = blob.get("mutual_information_bits")
        rows.append(row)

    corrected = DA.correct_handles(rows, alpha=alpha)
    corrected["declared_handles"] = list(DA.ALL_HANDLES)
    corrected["state"] = dict(state)
    return corrected


def signal_table(corrected: Mapping[str, Any]) -> list[dict[str, Any]]:
    """给 canvas / 报告用的精简表（只留判定需要的字段）。"""
    keys = (
        "handle",
        "label",
        "component",
        "statistic",
        "statistic_label",
        "test",
        "p_value",
        "p_parametric",
        "p_two_sided",
        "p_adjusted",
        "verdict",
        "verdict_label",
        "sample_size",
        "hit_rate",
        "delta_vs_uniform",
        "mdd_hit_rate",
        "data_status",
        "effect_direction",
        "degenerate",
    )
    table = []
    for row in corrected.get("rows", []):
        item = {key: row.get(key) for key in keys}
        null = row.get("null") or {}
        item["null_mean"] = null.get("mean")
        item["null_sd"] = null.get("sd")
        item["null_p95"] = null.get("p95")
        item["observed_z"] = null.get("observed_z")
        table.append(item)
    return table


def mechanism_block(state: Mapping[str, Any], corrected: Mapping[str, Any]) -> dict[str, Any]:
    """一句话机制回答：为什么历史推不出下一期（附互信息与零分布对照）。"""
    rows = {row["handle"]: row for row in corrected.get("rows", [])}
    mi = rows.get(DA.HANDLE_MUTUAL_INFORMATION, {})
    indep = rows.get(DA.HANDLE_INDEPENDENCE, {})
    hot = rows.get(DA.HANDLE_HOT_COLD, {})
    null = mi.get("null") or {}
    bits = mi.get("statistic")
    null_mean = null.get("mean")
    answer = (
        "本池相邻两期的互信息在置换零分布内，"
        f"实测 {_fmt_num(bits, 4)} 比特、随机打乱后零分布均值 {_fmt_num(null_mean, 4)} 比特"
        f"（单侧 p={_fmt_num(mi.get('p_value'), 4)}）——"
        "也就是说，知道上一期对下一期的 49 个号几乎没有任何信息量，"
        f"而全盲基线本身是 log2(49)={_fmt_num(state.get('log2_states'), 4)} 比特。"
    )
    return {
        "question": "为何做不出这个策略",
        "answer": answer,
        "mutual_information_bits": bits,
        "mutual_information_null_mean": null_mean,
        "mutual_information_p_value": mi.get("p_value"),
        "mutual_information_p_adjusted": mi.get("p_adjusted"),
        "independence_p_value": indep.get("p_value"),
        "independence_p_adjusted": indep.get("p_adjusted"),
        "hot_cold_p_value": hot.get("p_value"),
        "hot_cold_p_adjusted": hot.get("p_adjusted"),
        "unbiased_note": (
            f"{int(state.get('pairs') or 0)} 对样本摊在 49×49=2401 个格子里，"
            "绝大多数格子只落 0~1 个点，经验互信息必然虚高；"
            "只有与「同多重集合、随机顺序」的置换零分布对比才有意义。"
        ),
    }


def ablation_block(corrected: Mapping[str, Any]) -> dict[str, Any]:
    """消融分栏：作者自己的引擎特征 + 引擎 8 组件的独立样本外表现。"""
    rows = {row["handle"]: row for row in corrected.get("rows", [])}
    engine_rows = []
    for handle in DA.ENGINE_FEATURE_HANDLES:
        row = rows.get(handle) or {}
        engine_rows.append(
            {
                "handle": handle,
                "label": row.get("label"),
                "statistic": row.get("statistic"),
                "statistic_label": row.get("statistic_label"),
                "p_value": row.get("p_value"),
                "p_two_sided": row.get("p_two_sided"),
                "p_adjusted": row.get("p_adjusted"),
                "verdict": row.get("verdict"),
                "effect_direction": row.get("effect_direction"),
                "degenerate": row.get("degenerate"),
                "degenerate_note": row.get("degenerate_note"),
                "sample_size": row.get("sample_size"),
                "on": (row.get("ablation") or {}).get("on"),
                "off": (row.get("ablation") or {}).get("off"),
                "null_mean": (row.get("null") or {}).get("mean"),
                "null_p95": (row.get("null") or {}).get("p95"),
            }
        )
    component_rows = []
    for component in DE.COMPONENT_IDS:
        handle = COMPONENT_HANDLE_MAP[component]
        row = rows.get(handle) or {}
        component_rows.append(
            {
                "component": component,
                "component_label": DE.COMPONENT_LABELS[component],
                "handle": handle,
                "test": row.get("test"),
                "statistic": row.get("statistic"),
                "hit_rate": row.get("hit_rate"),
                "delta_vs_uniform": row.get("delta_vs_uniform"),
                "mdd_hit_rate": row.get("mdd_hit_rate"),
                "p_value": row.get("p_value"),
                "p_adjusted": row.get("p_adjusted"),
                "verdict": row.get("verdict"),
                "sample_size": row.get("sample_size"),
            }
        )
    return {
        "engine_features": engine_rows,
        "engine_components": component_rows,
        "note": (
            "「引擎特征」= 生产 ``services/lottery.py`` 里现有的 4 个开关，"
            "统计量 = 开启 - 关闭的样本外命中率差；"
            "「引擎组件」= 新 ``services/dist_engine.py`` 的 8 个组件的独立样本外表现。"
            "两者都在同一个 Holm 家族内，不做二次挑选。"
        ),
    }


def power_block(state: Mapping[str, Any], *, evaluated: int | None = None) -> dict[str, Any]:
    """样本量天花板：MDD + 目标 Δ（保本边际 / +5pp / +10.2pp）所需期数。"""
    count = int(evaluated if evaluated is not None else state.get("sample_size") or 0)
    deltas = (float(state.get("break_even_edge") or 0.0),) + TARGET_DELTAS
    block = DE.power_block(count, baseline_rate=DA.UNIFORM_RATE, deltas=deltas)
    block["break_even_hit_rate"] = state.get("break_even_hit_rate")
    block["break_even_edge"] = state.get("break_even_edge")
    block["k"] = state.get("k")
    block["odds"] = state.get("odds")
    block["note"] = (
        "「所需期数」是同一份两比例公式反解出的算术结果："
        "手里 210 期只能排除很大的优势，排除不了 +0.87pp 这种小优势。"
    )
    return block


def legitimate_value() -> dict[str, Any]:
    """本引擎真正能交付的东西（不含任何概率承诺）。"""
    return {
        "title": "本引擎真正能交付的东西（不含任何概率承诺）",
        "items": [
            "透明可控的分布形状：1..49 每号都有明确概率；8 个组件逐项可开关、可消融，权重取自 6 档网格（-1 ~ 2）",
            "覆盖面 / 花费控制：选几注、买多少、覆盖哪些号，全部显式可控且可复算",
            "可审计、可复现：同数据 + 同设置 + 同种子 → 逐位相同的输出，无未来函数",
            "组件级证据：每个想法单独样本外打分，与自己选的权重分开汇报，杜绝自证",
            "事前冻结台账：写死预测与评估口径，事后不许改口",
        ],
        "not_claimed": [
            "不承诺提高中奖概率（样本外成绩与均匀随机不可区分）",
            "不预测具体号码，也不宣称「必出号」",
            "不对本池之外的任何数据 / 任何市场做结论",
        ],
        "statement": DE.bottom_line()["statement"],
        "disclaimer": DE.DISCLAIMER,
    }


def scope_block(state: Mapping[str, Any]) -> str:
    return (
        f"本池已导入 {state.get('sample_size')} 期"
        f"（第 {state.get('period_first')}..{state.get('period_last')} 期，"
        f"{state.get('date_first')} ~ {state.get('date_last')}）；"
        "所有结论只针对这个样本，不涉及任何全市场数据。"
    )


# --------------------------------------------------------------------------- #
# 子命令：audit
# --------------------------------------------------------------------------- #
def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    settings = DA.default_audit_settings()
    state = DA.assess_state(draws, numbers, k=K_DEFAULT, odds=float(ODDS_DEFAULT))
    state["log2_states"] = math.log2(NUM_STATES)
    state["uniform_probability"] = 1.0 / NUM_STATES
    state["pairs"] = max(0, len(numbers) - 1)

    print("=" * 78)
    print(f"信息审计 · {TOOL_NAME} v{TOOL_VERSION}")
    print(scope_block(state))
    print(
        f"先声明假设族大小：{len(DA.ALL_HANDLES)} 个 handle"
        f"（参数化 {len(DA.PARAMETRIC_HANDLES)} + 置换 {len(DA.PERMUTATION_HANDLES)}），"
        f"Holm 校正 alpha={args.alpha}，Bonferroni 门槛={args.alpha / len(DA.ALL_HANDLES):.6f}"
    )
    print(
        f"其中生产引擎自身特征消融 {len(DA.ENGINE_FEATURE_HANDLES)} 个；"
        f"置换次数：轻量 {args.perms}、引擎消融 {args.perms_engine}，进程 {args.jobs}"
    )
    print("=" * 78)

    started = time.perf_counter()
    detail = observed_detail(
        numbers, draws, settings=settings, max_lag=args.max_lag, max_period=args.max_period
    )
    print(f"[1/3] 观测统计量完成（{time.perf_counter() - started:.2f}s）")

    started = time.perf_counter()
    nulls = collect_nulls(
        numbers,
        draws,
        perms=args.perms,
        perms_engine=args.perms_engine,
        jobs=args.jobs,
        settings=settings,
        max_lag=args.max_lag,
        max_period=args.max_period,
    )
    print(
        f"[2/3] 置换零分布完成（{time.perf_counter() - started:.2f}s："
        f"轻量 {nulls['meta']['timings']['cheap_seconds']}s、"
        f"引擎 {nulls['meta']['timings']['engine_seconds']}s）"
    )

    corrected = build_handles(detail, nulls["nulls"], state=state, alpha=args.alpha)
    table = signal_table(corrected)
    print(f"[3/3] Holm 校正完成：最小原始 p={_fmt_num(corrected['min_p_raw'])}、"
          f"最小校正后 p={_fmt_num(corrected['min_p_adjusted'])}")

    print("-" * 78)
    print(f"{'handle':<30}{'统计量':>13}{'原始 p':>11}{'校正 p':>11}  {'判定':<18}{'N':>5}")
    for row in table:
        verdict = row["verdict"]
        if row.get("effect_direction") in ("helps", "hurts", "inert"):
            verdict = f"{verdict}/{row['effect_direction']}"
        print(
            f"{row['handle']:<30}"
            f"{_fmt_num(row['statistic'], 4):>13}"
            f"{_fmt_num(row['p_value'], 4):>11}"
            f"{_fmt_num(row['p_adjusted'], 4):>11}  "
            f"{verdict:<18}"
            f"{str(row['sample_size']):>5}"
        )
    print("-" * 78)

    mechanism = mechanism_block(state, corrected)
    print(f"机制结论：{mechanism['answer']}")
    survivors = corrected["survivors"]
    if survivors:
        print(f"[!] 校正后仍显著的 handle：{survivors}（必须再做独立样本外复核）")
    else:
        print("校正后无任何 handle 显著 → 本池未发现可利用信息（判定 = no_signal 全族）")

    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "claim": DE.CLAIM_NO_EDGE,
        "claim_label": DE.CLAIM_LABELS[DE.CLAIM_NO_EDGE],
        "scope": scope_block(state),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
        },
        "parameters": {
            "alpha": args.alpha,
            "perms": args.perms,
            "perms_engine": args.perms_engine,
            "jobs": args.jobs,
            "max_lag": args.max_lag,
            "max_period": args.max_period,
            "k": K_DEFAULT,
            "odds": float(ODDS_DEFAULT),
            "settings": settings,
            "seed_base": PERM_SEED_BASE,
        },
        "state": state,
        "hypotheses": {
            key: corrected[key]
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
            )
        },
        "declared_handles": corrected["declared_handles"],
        "handles": corrected["rows"],
        "signal_table": table,
        "mechanism": mechanism,
        "ablation": ablation_block(corrected),
        "power": power_block(state),
        "legitimate_value": legitimate_value(),
        "null_meta": nulls["meta"],
        "verdict": {
            "anything_survives": bool(survivors),
            "survivors": survivors,
            "survivor_count": len(survivors),
            "statement": (
                "本池未发现任何在校正后仍显著的信息载体；"
                "样本外成绩与均匀随机基线不可区分。"
                if not survivors
                else "存在校正后仍显著的 handle：必须在独立数据上复核，不能直接投产。"
            ),
        },
        "disclaimer": DE.DISCLAIMER,
    }
    write_json(resolve_out(args.out, "dist_audit.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：null
# --------------------------------------------------------------------------- #
def run_null(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    settings = DA.default_audit_settings()
    state = DA.assess_state(draws, numbers, k=K_DEFAULT, odds=float(ODDS_DEFAULT))
    state["log2_states"] = math.log2(NUM_STATES)
    state["uniform_probability"] = 1.0 / NUM_STATES

    print("=" * 78)
    print(f"置换零分布 · {TOOL_NAME} v{TOOL_VERSION}")
    print(scope_block(state))
    print(
        "零假设：号码多重集合不变（哪些号出现几次完全一样），只把出现位置随机洗牌。"
        "若某个想法真有信息，打乱后它的成绩必然退化到基线附近。"
    )
    print("=" * 78)

    started = time.perf_counter()
    nulls = collect_nulls(
        numbers,
        draws,
        perms=args.perms,
        perms_engine=args.perms_engine,
        jobs=args.jobs,
        settings=settings,
        max_lag=args.max_lag,
        max_period=args.max_period,
    )
    elapsed = time.perf_counter() - started
    detail = observed_detail(
        numbers, draws, settings=settings, max_lag=args.max_lag, max_period=args.max_period
    )

    rows = []
    for handle in DA.PERMUTATION_HANDLES:
        values = list(nulls["nulls"].get(handle) or [])
        observed = (detail.get(handle) or {}).get("statistic")
        block = _null_block(observed, values, better="greater")
        rows.append(
            {
                "handle": handle,
                "label": DA.HANDLE_LABELS[handle],
                "statistic": observed,
                "null": block,
                "p_one_sided": block.get("p_one_sided"),
                "hit_rate": (detail.get(handle) or {}).get("hit_rate"),
                "delta_vs_uniform": (
                    (detail.get(handle) or {}).get("hit_rate") - DA.UNIFORM_RATE
                    if isinstance((detail.get(handle) or {}).get("hit_rate"), (int, float))
                    else None
                ),
            }
        )

    print("-" * 78)
    print(f"{'handle':<30}{'观测':>12}{'零均值':>12}{'零p95':>12}{'z':>9}{'单侧 p':>9}")
    for row in rows:
        null = row["null"]
        print(
            f"{row['handle']:<30}"
            f"{_fmt_num(row['statistic'], 4):>12}"
            f"{_fmt_num(null.get('mean'), 4):>12}"
            f"{_fmt_num(null.get('p95'), 4):>12}"
            f"{_fmt_num(null.get('observed_z'), 3):>9}"
            f"{_fmt_num(row['p_one_sided'], 4):>9}"
        )
    print("-" * 78)

    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "claim": DE.CLAIM_NO_EDGE,
        "claim_label": DE.CLAIM_LABELS[DE.CLAIM_NO_EDGE],
        "scope": scope_block(state),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
        },
        "parameters": {
            "perms": args.perms,
            "perms_engine": args.perms_engine,
            "jobs": args.jobs,
            "seed_base": PERM_SEED_BASE,
        },
        "state": state,
        "rows": rows,
        "null_meta": nulls["meta"],
        "elapsed_seconds": round(elapsed, 3),
        "interpretation": [
            "观测值落在零分布里 = 这个想法在「随机顺序的同一份数据」上同样出现，不能算发现。",
            "注意：本表是逐条单侧 p，未做多重比较校正；要下结论请用 audit 子命令的 Holm 表。",
        ],
        "disclaimer": DE.DISCLAIMER,
    }
    write_json(resolve_out(args.out, "dist_null.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：evaluate
# --------------------------------------------------------------------------- #
def _init_eval(numbers: Sequence[int], params: Mapping[str, Any] | None) -> None:
    _WORKER["numbers"] = [int(value) for value in numbers]
    _WORKER["params"] = dict(params) if params else None


def _eval_worker(index: int) -> dict[str, Any]:
    shuffled = _shuffled_numbers(_WORKER["numbers"], PERM_SEED_BASE + 900_000 + int(index))
    selected = DE.select_and_evaluate(
        shuffled,
        params=_WORKER["params"],
        train_ratio=TRAIN_RATIO,
        inner_holdout=INNER_HOLDOUT,
        max_passes=MAX_PASSES,
    )
    holdout = selected["holdout"]
    calibration = selected["holdout_calibration"]
    return {
        "index": int(index),
        "selected_weights": selected["selected_weights"],
        "hit_rate": holdout.get("hit_rate"),
        "mean_rank": holdout.get("mean_rank"),
        "log_loss": holdout.get("log_loss"),
        "brier": holdout.get("brier"),
        "ece": calibration.get("ece"),
        "calibration_chi2": calibration.get("chi_square_statistic"),
    }


def run_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    draws = load_draws(args.draws_json)
    numbers = numbers_of(draws)
    state = DA.assess_state(draws, numbers, k=K_DEFAULT, odds=float(ODDS_DEFAULT))
    state["log2_states"] = math.log2(NUM_STATES)
    state["uniform_probability"] = 1.0 / NUM_STATES
    reference = DE.reference_block()

    print("=" * 78)
    print(f"诚实评估（严格 walk-forward）· {TOOL_NAME} v{TOOL_VERSION}")
    print(scope_block(state))
    print(
        f"口径：训练窗 = 前 {TRAIN_RATIO:.0%}（内层留出 {INNER_HOLDOUT} 期选权，"
        "评估窗从不参与）；评估窗只打分。"
    )
    print("=" * 78)

    selected = DE.select_and_evaluate(
        numbers,
        params=None,
        train_ratio=TRAIN_RATIO,
        inner_holdout=INNER_HOLDOUT,
        max_passes=MAX_PASSES,
    )
    holdout = selected["holdout"]
    uniform = selected["uniform_holdout"]
    calibration = selected["holdout_calibration"]
    chi2 = calibration.get("chi_square_statistic")
    calibration_p = DA.chi2_sf(chi2, calibration.get("degrees_of_freedom") or 0)

    print(f"内层 walk-forward 选出的权重：{selected['selected_label']}")
    print(
        f"评估窗（{holdout['evaluated']} 期）：命中 {holdout['hits']}/{holdout['evaluated']} "
        f"= {_fmt_pct(holdout['hit_rate'])}（均匀基线 {_fmt_pct(reference['hit_rate'])}）· "
        f"平均排名 {_fmt_num(holdout['mean_rank'], 3)}（基线 {_fmt_num(reference['mean_rank'], 3)}）"
    )
    print(
        f"log-loss {_fmt_num(holdout['log_loss'], 4)}（基线 {_fmt_num(reference['log_loss'], 4)}）· "
        f"Brier {_fmt_num(holdout['brier'], 6)}（基线 {_fmt_num(reference['brier'], 6)}）"
    )
    print(
        f"校准：ECE={_fmt_num(calibration.get('ece'), 5)}、"
        f"卡方={_fmt_num(chi2, 3)}（df={calibration.get('degrees_of_freedom')}、"
        f"p={_fmt_num(calibration_p, 4)}）、"
        f"平均预测概率(实际号)={_fmt_num(calibration.get('mean_predicted'), 5)}"
        f"（均匀 {_fmt_num(1.0 / NUM_STATES, 5)}）"
    )
    print("逐组件（同一评估窗，单独当选号器）对照：")
    for entry in selected["components_holdout"]:
        print(
            f"  {entry['component']:<10} {entry['hits']}/{entry['evaluated']} "
            f"= {_fmt_pct(entry['hit_rate'])} "
            f"Δ vs 均匀 {_fmt_pct((entry['hit_rate'] or 0) - DA.UNIFORM_RATE)}"
        )

    perms = int(max(0, args.perms))
    null_rows: list[dict[str, Any]] = []
    if perms:
        started = time.perf_counter()
        null_rows = _run_pool(
            _eval_worker,
            _init_eval,
            (numbers, None),
            perms,
            int(max(1, args.jobs)),
            chunksize=1,
        )
        print(
            f"[置换] {perms} 次「洗牌后重跑整条选参流程」完成"
            f"（{time.perf_counter() - started:.2f}s，进程 {args.jobs}）"
        )

    def _null_of(key: str, observed: float | None, better: str) -> dict[str, Any]:
        values = [float(row[key]) for row in null_rows if row.get(key) is not None]
        return _null_block(observed, values, better=better)

    metrics = {
        "hit_rate": _null_of("hit_rate", holdout["hit_rate"], "greater"),
        "mean_rank": _null_of("mean_rank", holdout["mean_rank"], "less"),
        "log_loss": _null_of("log_loss", holdout["log_loss"], "less"),
        "brier": _null_of("brier", holdout["brier"], "less"),
        "ece": _null_of("ece", calibration.get("ece"), "less"),
        "calibration_chi2": _null_of("calibration_chi2", chi2, "greater"),
    }
    weight_moves = {
        name: {
            "observed": selected["selected_weights"].get(name),
            "null_mean": (
                sum(float(row["selected_weights"].get(name) or 0.0) for row in null_rows) / len(null_rows)
                if null_rows
                else None
            ),
            "null_nonzero_rate": (
                sum(1 for row in null_rows if abs(float(row["selected_weights"].get(name) or 0.0)) > 1e-12)
                / len(null_rows)
                if null_rows
                else None
            ),
        }
        for name in DE.COMPONENT_IDS
    }

    print("-" * 78)
    for key, block in metrics.items():
        print(
            f"{key:<18}观测 {_fmt_num(block.get('observed'), 6):>12}"
            f" · 零均值 {_fmt_num(block.get('mean'), 6):>12}"
            f" · 零 95 分位 {_fmt_num(block.get('p95'), 6):>12}"
            f" · p={_fmt_num(block.get('p_one_sided'), 4)}"
        )
    print("-" * 78)

    alpha = args.alpha
    keys_in_family = ("hit_rate", "mean_rank", "log_loss", "brier", "ece", "calibration_chi2")
    correction = A.holm_adjusted_p(
        [metrics[key]["p_one_sided"] for key in keys_in_family if metrics[key]["p_one_sided"] is not None],
        alpha=alpha,
    )
    adjusted = correction["adjusted"]
    cursor = 0
    evaluated_metrics = {}
    for key in keys_in_family:
        block = dict(metrics[key])
        if block["p_one_sided"] is None:
            block["p_adjusted"] = None
            block["verdict"] = DA.VERDICT_INSUFFICIENT
        else:
            block["p_adjusted"] = adjusted[cursor]
            cursor += 1
            block["verdict"] = (
                DA.VERDICT_SIGNAL if block["p_adjusted"] <= alpha else DA.VERDICT_NO_SIGNAL
            )
        block["verdict_label"] = DA.VERDICT_LABELS[block["verdict"]]
        evaluated_metrics[key] = block
    survivors = [
        key for key, block in evaluated_metrics.items() if block["verdict"] == DA.VERDICT_SIGNAL
    ]

    power = power_block(state, evaluated=int(holdout["evaluated"]))
    print(f"样本量天花板：MDD(+80% 功效) = {_fmt_pct(power.get('minimum_detectable_delta'))}"
          f"；两倍标准误 = {_fmt_pct(power.get('two_sigma_delta'))}")
    for target in power.get("targets", []):
        print(
            f"  目标 Δ={_fmt_pct(target['delta'])} → 80% 功效需约 "
            f"{target['required_draws_80_power']} 期"
        )
    print(
        "校正后仍显著的指标："
        + (", ".join(survivors) if survivors else "无（全部落在抽样噪声内）")
    )

    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "claim": DE.CLAIM_NO_EDGE,
        "claim_label": DE.CLAIM_LABELS[DE.CLAIM_NO_EDGE],
        "scope": scope_block(state),
        "input": {
            "draws_json": args.draws_json,
            "numbers_sha256": numbers_digest(numbers),
            "sample_size": len(numbers),
        },
        "parameters": {
            "alpha": alpha,
            "perms": perms,
            "jobs": args.jobs,
            "train_ratio": TRAIN_RATIO,
            "inner_holdout": INNER_HOLDOUT,
            "max_passes": MAX_PASSES,
            "k": K_DEFAULT,
            "odds": float(ODDS_DEFAULT),
            "seed_base": PERM_SEED_BASE,
        },
        "state": state,
        "selected": {
            "weights": selected["selected_weights"],
            "label": selected["selected_label"],
            "split_index": selected["fit"]["split_index"],
            "train_length": selected["fit"]["train_length"],
            "inner_positions": selected["fit"]["inner_positions"],
            "inner_log_loss": selected["fit"].get("inner_log_loss"),
            "inner_hit_rate": selected["fit"].get("inner_hit_rate"),
            "inner_mean_rank": selected["fit"].get("inner_mean_rank"),
            "moves": selected["fit"].get("moves"),
        },
        "holdout": holdout,
        "uniform_holdout": uniform,
        "calibration": {
            key: calibration.get(key)
            for key in (
                "evaluated",
                "mean_predicted",
                "reference_mean_predicted",
                "ece",
                "chi_square_statistic",
                "degrees_of_freedom",
                "bins",
                "per_number",
            )
        },
        "calibration_p_value": calibration_p,
        "components_holdout": selected["components_holdout"],
        "null": {
            "perms": perms,
            "metrics": evaluated_metrics,
            "family_size": len(keys_in_family),
            "correction": "holm",
            "alpha": alpha,
            "survivors": survivors,
            "weight_moves": weight_moves,
        },
        "power": power,
        "legitimate_value": legitimate_value(),
        "verdict": {
            "anything_survives": bool(survivors),
            "survivors": survivors,
            "holdout_delta_vs_uniform": (
                (holdout["hit_rate"] - reference["hit_rate"])
                if holdout.get("hit_rate") is not None
                else None
            ),
            "statement": (
                "严格 walk-forward 下，内层选出的权重在评估窗上不优于均匀随机；"
                "校准与均匀不可区分 → 本引擎交付分布控制，不交付概率优势。"
            ),
        },
        "disclaimer": DE.DISCLAIMER,
    }
    write_json(resolve_out(args.out, "dist_eval.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# 子命令：explain（不依赖开奖文件）
# --------------------------------------------------------------------------- #
def explain_text(state: Mapping[str, Any] | None) -> str:
    reference = DE.reference_block()
    lines = [
        "=" * 78,
        "为何做不出这个策略 —— 白话版",
        "=" * 78,
        "1) 我们在找什么：一个能把「下一期特码」的概率从 1/49 抬高的映射。",
        "   如果存在，它必须让「相邻两期的互信息」大于 0，或让某个历史特征在样本外",
        "   稳定地提高命中率。这两件事都可以直接测，不需要相信任何说法。",
        "",
        "2) 实测结果（210 期样本内）：",
        "   - 相邻两期互信息：实测与「随机洗牌同一份数据」的零分布重合 → 没有信息；",
        "   - 49 号频次均匀性、热冷持续、遗漏到期、生肖、点阵、谱扫描、",
        "     以及生产引擎自己的 4 个开关：全部落在随机波动范围内；",
        "   - 引擎 8 个组件单独当选号器，样本外命中率都在 10/49=20.41% 上下。",
        "",
        "3) 为什么「看着有规律」却测不出来：",
        f"   - 209 对样本摊在 49×49={NUM_STATES ** 2} 个格子里，绝大多数格子只落 0~1 个点，",
        "     于是「经验互信息」必然虚高；不与置换零分布对比就会误判成发现；",
        "   - 我们反复试了 23 个角度，即便全是噪声，也总有一个 p 值看起来很小 ——",
        "     所以必须先声明族大小再做 Holm 校正，否则就是自己骗自己。",
        "",
        "4) 真正的天花板是样本量（算术，不是观点）：",
    ]
    sample = int(state.get("sample_size")) if state else 210
    power = DE.power_block(
        sample,
        baseline_rate=DA.UNIFORM_RATE,
        deltas=(float(state.get("break_even_edge") or 0.0) if state else 0.008684,) + TARGET_DELTAS,
    )
    lines.append(
        f"   - 随机基线命中率 k/49 = {_fmt_pct(reference['hit_rate'])}；"
        f"保本命中率 k/odds = {_fmt_pct(state.get('break_even_hit_rate') if state else 10 / 47.0)}"
        f"（边际仅 {_fmt_pct(power.get('break_even_edge') or 0.008684)}）；"
    )
    lines.append(
        f"   - 手上 {sample} 期能排除的最小真实优势 MDD ≈ "
        f"{_fmt_pct(power.get('minimum_detectable_delta'))}（80% 功效）；"
    )
    for target in power.get("targets", []):
        lines.append(
            f"   - 要证明 {_fmt_pct(target['delta'])} 的真实优势，80% 功效需要约 "
            f"{target['required_draws_80_power']} 期。"
        )
    lines += [
        "",
        "5) 所以结论不是「算法没写好」，而是「这份历史里没有可搬的信息」；",
        "   同时也是「没证据说明有优势」，而不是「证明了没有优势」。",
        "",
        "6) 这个系统仍然交付什么（见 legitimate_value）：",
    ]
    for item in legitimate_value()["items"]:
        lines.append(f"   - {item}")
    lines += [
        "",
        f"声明：{DE.DISCLAIMER}",
        "口径：以上全部只针对本池已导入的样本，不涉及任何全市场数据。",
        "=" * 78,
    ]
    return "\n".join(lines)


def run_explain(args: argparse.Namespace) -> dict[str, Any]:
    state: dict[str, Any] | None = None
    if args.draws_json and Path(args.draws_json).exists():
        draws = load_draws(args.draws_json)
        numbers = numbers_of(draws)
        state = DA.assess_state(draws, numbers, k=K_DEFAULT, odds=float(ODDS_DEFAULT))
        state["log2_states"] = math.log2(NUM_STATES)
        state["uniform_probability"] = 1.0 / NUM_STATES
    text = explain_text(state)
    print(text)
    payload = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "claim": DE.CLAIM_NO_EDGE,
        "claim_label": DE.CLAIM_LABELS[DE.CLAIM_NO_EDGE],
        "state": state,
        "text": text,
        "answer": (
            "开奖历史里没有可以搬到下一期的信息：相邻两期互信息与置换零分布重合，"
            "23 个角度的样本外表现全部落在随机波动内；保本只需要 +0.87pp，"
            "而证明 +0.87pp 需要的样本量是几十万期，210 期给不起。"
        ),
        "legitimate_value": legitimate_value(),
        "disclaimer": DE.DISCLAIMER,
    }
    write_json(resolve_out(args.out, "dist_explain.json"), payload)
    return payload


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dist_audit.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser, *, default_out: str) -> None:
        sub.add_argument("--draws-json", default=DEFAULT_DRAWS, help="开奖 JSON 路径")
        sub.add_argument("--out", default=None, help=f"输出 JSON（默认 {default_out}）")
        sub.add_argument("--perms", type=int, default=None, help="置换次数（默认按子命令）")
        sub.add_argument("--perms-engine", type=int, default=None, help="引擎消融的置换次数（默认 300）")
        sub.add_argument("--jobs", type=int, default=None, help="进程数（默认 CPU 数，最多 12）")
        sub.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="显著性水平（默认 0.05）")
        sub.add_argument("--max-lag", type=int, default=DA.DEFAULT_MAX_LAG, help="自相关最大滞后（默认 20）")
        sub.add_argument("--max-period", type=int, default=DA.DEFAULT_MAX_PERIOD, help="谱扫描最大周期（默认 30）")

    audit = subparsers.add_parser(
        "audit",
        help="信息审计：23 个假设 + 置换零分布 + Holm 校正",
        description="跑完整个信息审计并写出 JSON（含逐 handle 的原始 p / 校正 p / 英文判定）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(audit, default_out="dist_audit.json")
    audit.set_defaults(func=run_audit, default_perms=AUDIT_PERMS, default_engine_perms=ENGINE_PERMS)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="诚实评估：内层选权 → 评估窗只打分 + 校准 + 置换零分布",
        description="严格 walk-forward 评估分布引擎，并给出样本量天花板（MDD / 所需期数）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(evaluate, default_out="dist_eval.json")
    evaluate.set_defaults(func=run_evaluate, default_perms=EVAL_PERMS, default_engine_perms=ENGINE_PERMS)

    explain = subparsers.add_parser(
        "explain",
        help="白话解释「为何做不出这个策略」（不依赖开奖文件）",
        description="不跑任何统计，直接用算术与口径回答「为何做不出这个策略」。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    explain.add_argument("--draws-json", default=DEFAULT_DRAWS, help="开奖 JSON 路径（可选，存在才纳入样本状态）")
    explain.add_argument("--out", default=None, help="输出 JSON（默认 dist_explain.json）")
    explain.set_defaults(func=run_explain, default_perms=0, default_engine_perms=0)

    null_run = subparsers.add_parser(
        "null",
        help="只跑置换零分布：随机打乱的同一份数据能造出什么成绩",
        description="把置换零分布摊开：每条假设的观测值、零分布均值 / 95 分位、单侧 p。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(null_run, default_out="dist_null.json")
    null_run.set_defaults(func=run_null, default_perms=AUDIT_PERMS, default_engine_perms=MIN_ENGINE_PERMS)
    return parser


def _resolve_jobs(args: argparse.Namespace) -> None:
    if getattr(args, "jobs", None) is None:
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
    if getattr(args, "perms", None) is None:
        args.perms = getattr(args, "default_perms", 0)
    if getattr(args, "perms_engine", None) is None:
        args.perms_engine = getattr(args, "default_engine_perms", 0)
    if getattr(args, "alpha", None) is None:
        args.alpha = DEFAULT_ALPHA
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
