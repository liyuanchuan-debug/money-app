"""「种子随机 + 点阵分布」新选号口径的确定性 / 分布 / 形状护栏。

覆盖四类断言（全部**确定性可复现**，不含随机波动）：

1. **种子公式**：``sampling_seed_key`` 是固定材料的 SHA-256；只跟「结构设置」
   （抽样模式 / 配额口径 / 池规模阈值 / 重复号开关 / 注数）走，跟金额与**加权旋钮**
   无关（否则会把惰性参数伪装成敏感参数）；
2. **确定性**：同期同设置逐位一致；换期换号；**跨进程 + 不同 PYTHONHASHSEED**
   也必须一致（真护栏：哈希随机化不得漏进抽样）；
3. **分布是真的**：点阵带内号码被抽到的频率高于「配额内均匀」的期望，
   且高于旧 ranked 口径 —— 说明点阵是概率、不是装饰；
4. **形状没退化**：balanced 配额下没有任何一期出现「单个波动桶供给 ≥7 注」，
   每期恒 10 注互不相同。
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # pragma: no cover - pytest 通常已加
    sys.path.insert(0, str(BACKEND_DIR))

import random  # noqa: E402

from services import lottery as L  # noqa: E402
from services.analytics import normalize_draws  # noqa: E402

REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_279.json"

# 与库里全局模板行一致的有效配置（small 10 / normal 30 / 不排同肖 / 走势 neutral）。
PRODUCTION_LIKE = {
    "small_max": 10,
    "normal_max": 30,
    "trend_bias": "neutral",
    "exclude_repeat_zodiac": False,
    "pick_count": 10,
    "amount_unit": 5,
    "total_amount": 50,
    "mode": "even",
    "odds": 47,
}

HISTORY_30 = [
    18, 7, 33, 12, 41, 5, 26, 9, 44, 3, 17, 38, 11, 22, 6, 31, 15, 47,
    24, 2, 39, 14, 28, 8, 45, 19, 35, 1, 42, 21,
]


def _cfg(**overrides) -> dict:
    return L.clamp_settings({**PRODUCTION_LIKE, **overrides})


def _recommend(period: int, cfg: dict | None = None):
    return L.recommend(
        latest=HISTORY_30[0],
        previous=HISTORY_30[1],
        history_numbers=list(HISTORY_30),
        settings=cfg if cfg is not None else _cfg(lattice_enabled=True),
        mode="even",
        period=period,
    )


def _payload(outcome) -> str:
    """号码 + 金额 + 角色 + 波动桶的规范化 JSON（用于逐字节比对 / 哈希）。"""
    return json.dumps(
        [
            {
                "number": int(pick["number"]),
                "amount": int(pick["amount"]),
                "role": pick.get("role"),
                "wave": pick["wave_type"],
            }
            for pick in outcome["picks"]
        ],
        sort_keys=True,
        ensure_ascii=False,
    )


def _real_series() -> list[dict]:
    if not REAL_DRAWS_FILE.exists():  # pragma: no cover - 依赖本地 data/*
        pytest.skip(f"缺少本地真实开奖文件：{REAL_DRAWS_FILE}")
    return normalize_draws(json.loads(REAL_DRAWS_FILE.read_text(encoding="utf-8")))


def _walk_forward(overrides: dict | None = None, *, with_band: bool = False):
    """逐期走步：每期只用该期之前的数据 recommend（与 backtest_stats 同口径）。"""
    series = _real_series()
    cfg = _cfg(**(overrides or {}))
    specials = [int(row["special_number"]) for row in series]
    periods = [row["period"] for row in series]
    dates = [row["draw_date"] for row in series]
    rows = []
    for index in range(2, len(series)):
        latest = specials[index - 1]
        history = list(reversed(specials[:index]))
        outcome = L.recommend(
            latest=latest,
            previous=specials[index - 2],
            history_numbers=history,
            history_dates=list(reversed(dates[:index])),
            settings=cfg,
            mode=cfg["mode"],
            period=periods[index],
        )
        row = {
            "period": periods[index],
            "latest": latest,
            "picks": [int(pick["number"]) for pick in outcome["picks"]],
            "waves": Counter(pick["wave_type"] for pick in outcome["picks"]),
            "amounts": [int(pick["amount"]) for pick in outcome["picks"]],
            "hit": specials[index] in {int(p["number"]) for p in outcome["picks"]},
        }
        if with_band:
            band = L.predict_wave_band(
                history,
                window=int(cfg["lattice_window"]),
                small_max=cfg["small_max"],
                normal_max=cfg["normal_max"],
            )
            row["band"] = band
            pools = {wave: [] for wave in L.WAVE_ORDER}
            for number in range(L.NUMBER_MIN, L.NUMBER_MAX + 1):
                pools[
                    L.classify_wave(
                        abs(number - latest), cfg["small_max"], cfg["normal_max"]
                    )
                ].append(number)
            row["pools"] = pools
            row["quotas"] = L.balanced_wave_quotas(
                cfg["pick_count"], {wave: len(v) for wave, v in pools.items()}
            )
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# 1. 种子公式
# --------------------------------------------------------------------------- #
def test_sampling_seed_key_is_a_pinned_sha256_of_the_declared_material():
    """种子是「域标签 + 期号 + latest + previous + 白名单设置」的 SHA-256（钉死值）。"""
    seed = L.sampling_seed_key(
        period=279,
        latest=18,
        previous=7,
        settings=_cfg(lattice_enabled=True),
    )
    assert isinstance(seed, str) and len(seed) == 64
    int(seed, 16)  # 必须是合法十六进制
    assert seed == "1da21cbebee390605dd213e285128880bbf0e2fb15d241cfa0dc626d32659874"

    # 逐字节可复现：同参数两次相同
    again = L.sampling_seed_key(
        period=279,
        latest=18,
        previous=7,
        settings=_cfg(lattice_enabled=True),
    )
    assert again == seed


def test_sampling_seed_key_ignores_dict_order_and_amount_keys():
    cfg = _cfg(lattice_enabled=True)
    shuffled = dict(reversed(list(cfg.items())))
    assert L.sampling_seed_key(
        period=100, latest=5, previous=6, settings=cfg
    ) == L.sampling_seed_key(period=100, latest=5, previous=6, settings=shuffled)

    # 金额类改动不换种子（预算不该改号）
    richer = _cfg(lattice_enabled=True, total_amount=100)
    assert L.sampling_seed_key(
        period=100, latest=5, previous=6, settings=cfg
    ) == L.sampling_seed_key(period=100, latest=5, previous=6, settings=richer)


def test_sampling_seed_key_follows_structure_not_weight_knobs():
    """结构设置进种子；加权旋钮不进（否则「权重没变」的参数会被伪装成敏感参数）。"""
    base = _cfg(lattice_enabled=True)
    seed = lambda **overrides: L.sampling_seed_key(  # noqa: E731
        period=200,
        latest=9,
        previous=3,
        settings=_cfg(**{"lattice_enabled": True, **overrides}),
    )
    anchor = seed()

    # 加权旋钮：改它们不换种子
    for knob, value in (
        ("lattice_enabled", False),
        ("lattice_window", 5),
        ("trend_bias", "hot"),
        ("trend_window", 60),
        ("avoid_cold_enabled", True),
        ("avoid_cold_days", 10),
        ("score_w_mid", 4.0),
        ("role_w_primary", 5.0),
    ):
        assert seed(**{knob: value}) == anchor, knob

    # 结构设置：改它们必须换种子
    for knob, value in (
        ("pick_sampling", L.PICK_SAMPLING_RANKED),
        ("wave_alloc", L.WAVE_ALLOC_DRAIN),
        ("pick_count", 8),
        ("small_max", 12),
        ("normal_max", 25),
        ("exclude_repeat_zodiac", True),
        ("include_repeat_number", False),
        ("pick_strategy", "score_top"),
    ):
        assert seed(**{knob: value}) != anchor, knob
    assert base["pick_sampling"] == L.DEFAULT_PICK_SAMPLING


# --------------------------------------------------------------------------- #
# 2. 确定性
# --------------------------------------------------------------------------- #
def test_same_period_is_byte_identical_and_different_periods_differ():
    first = _payload(_recommend(279))
    second = _payload(_recommend(279))
    assert first == second
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(
        second.encode()
    ).hexdigest()

    # 同一份输入只换期号 → 换样本（期号是种子的主键）
    others = {_payload(_recommend(period)) for period in (275, 276, 277, 278, 280)}
    assert len(others) == 5
    assert first not in others


def test_picks_change_when_a_relevant_setting_changes():
    base = _payload(_recommend(279, _cfg(lattice_enabled=True)))
    changed = _payload(_recommend(279, _cfg(lattice_enabled=False)))
    assert base != changed

    ranked = _payload(
        _recommend(279, _cfg(lattice_enabled=True, pick_sampling=L.PICK_SAMPLING_RANKED))
    )
    assert ranked != base


def test_engine_is_identical_across_processes_with_different_pythonhashseed():
    """真护栏：两个**独立进程**、不同 ``PYTHONHASHSEED`` → 号码 / 金额逐字节一致。"""
    script = (
        "import hashlib, json, sys\n"
        f"sys.path.insert(0, {str(BACKEND_DIR)!r})\n"
        "from services.lottery import clamp_settings, recommend\n"
        f"settings = clamp_settings({{**{PRODUCTION_LIKE!r}, 'lattice_enabled': True}})\n"
        f"history = {HISTORY_30!r}\n"
        "out = recommend(latest=history[0], previous=history[1],"
        " history_numbers=list(history), settings=settings, mode='even', period=279)\n"
        "payload = json.dumps([[int(p['number']), int(p['amount']), p.get('role'),"
        " p['wave_type']] for p in out['picks']], sort_keys=True)\n"
        "print(hashlib.sha256(payload.encode()).hexdigest())\n"
        "print(payload)\n"
    )
    results = []
    for hash_seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(BACKEND_DIR),
            env=env,
            check=True,
        )
        results.append(completed.stdout.strip())
    assert results[0] == results[1] == results[2]
    digest, payload = results[0].splitlines()
    assert len(digest) == 64
    assert json.loads(payload)  # 非空且可解析


# --------------------------------------------------------------------------- #
# 3. 分布语义（点阵是概率，不是装饰）
# --------------------------------------------------------------------------- #
def test_weighted_sampling_actually_biases_toward_the_band():
    """单元级：同一份权重下反复不放回抽样，带内号码出现频率显著高于均匀份额。"""
    band = {"low": 16.0, "high": 29.0}
    latest = 25
    weighted = [(n, L.lattice_weight(abs(n - latest), band)) for n in range(1, 50)]
    in_band = {
        n for n, _ in weighted if band["low"] <= abs(n - latest) <= band["high"]
    }
    uniform_share = len(in_band) / 49
    drawn = []
    for trial in range(2000):
        rng = random.Random(f"trial-{trial}")
        drawn.extend(L.weighted_sample_distinct(rng, weighted, 10))
    measured = sum(1 for n in drawn if n in in_band) / len(drawn)
    assert measured > uniform_share * 1.2  # 明显高于均匀（实测约 1.45x）
    assert measured < 1.0


def test_lattice_band_is_sampled_more_often_than_the_quota_aware_uniform_expectation():
    """引擎级（208 期真实池）：带内实际入选数 > 「配额内均匀」期望；且高于旧 ranked 口径。

    关键口径说明：单纯比较「带内入选率 vs 全 49 号带内占比」是**被配额结构混淆**的
    —— 预测带绝大多数期落在 ``{small, normal}`` 两个桶里（见 band bucket 直方图），
    而 balanced 给 normal 桶的配额有限，所以带内入选率会低于全空间占比。
    正确的对照是「同桶内均匀」期望：``Σ_b 配额_b × 桶 b 的带内占比``。
    """
    rows = _walk_forward(with_band=True)
    assert len(rows) == 208

    measured = []
    expected = []
    cond_in = cond_out = 0
    avail_in = avail_out = 0
    band_buckets: Counter = Counter()
    for row in rows:
        band, pools, quotas, picks = (
            row["band"],
            row["pools"],
            row["quotas"],
            set(row["picks"]),
        )
        assert band is not None
        low, high, latest = float(band["low"]), float(band["high"]), row["latest"]

        def in_band(number: int) -> bool:
            return low <= abs(number - latest) <= high

        measured.append(sum(1 for n in row["picks"] if in_band(n)))
        expected.append(
            sum(
                quotas[wave]
                * (sum(1 for n in pools[wave] if in_band(n)) / len(pools[wave]))
                for wave in L.WAVE_ORDER
                if pools[wave]
            )
        )
        band_buckets[tuple(sorted({wave for wave in L.WAVE_ORDER if any(in_band(n) for n in pools[wave])}))] += 1
        for wave, pool in pools.items():
            for number in pool:
                if in_band(number):
                    avail_in += 1
                    cond_in += 1 if number in picks else 0
                elif wave in {
                    w for w in L.WAVE_ORDER if any(in_band(n) for n in pools[w])
                }:
                    avail_out += 1
                    cond_out += 1 if number in picks else 0

    measured_mean = statistics.fmean(measured)
    expected_mean = statistics.fmean(expected)
    # 点阵加权把带内入选推高到「同桶均匀」期望之上（实测 +0.459 注/期）
    assert measured_mean > expected_mean
    assert measured_mean - expected_mean == pytest.approx(0.459, abs=0.05)
    # 同桶内条件概率：带内 > 带外
    assert cond_in / avail_in > cond_out / avail_out
    # 预测带绝大多数期同时覆盖 normal 与 small 两个桶
    assert band_buckets.most_common(1)[0][0] == ("normal", "small")

    # 对照：旧 ranked 口径下点阵不参与取号 → 带内入选反而**低于**均匀期望
    ranked = _walk_forward({"pick_sampling": L.PICK_SAMPLING_RANKED}, with_band=True)
    ranked_measured = []
    ranked_expected = []
    for row in ranked:
        band, quotas, pools = row["band"], row["quotas"], row["pools"]
        low, high, latest = float(band["low"]), float(band["high"]), row["latest"]
        ranked_measured.append(
            sum(1 for n in row["picks"] if low <= abs(n - latest) <= high)
        )
        ranked_expected.append(
            sum(
                quotas[wave]
                * (
                    sum(1 for n in pools[wave] if low <= abs(n - latest) <= high)
                    / len(pools[wave])
                )
                for wave in L.WAVE_ORDER
                if pools[wave]
            )
        )
    assert statistics.fmean(ranked_measured) < statistics.fmean(ranked_expected)
    assert statistics.fmean(ranked_measured) < measured_mean


# --------------------------------------------------------------------------- #
# 4. 形状（clumping 没有回来）
# --------------------------------------------------------------------------- #
def test_seeded_sampling_keeps_exact_count_distinctness_and_balanced_shape():
    """208 期真实池：毎期恒 10 注互不相同，且**没有任何一期**被单个波动桶包办。"""
    rows = _walk_forward()

    one_bucket_ge7 = 0
    max_bucket_counts = []
    for row in rows:
        picks = row["picks"]
        assert len(picks) == 10
        assert len(set(picks)) == 10
        assert all(L.NUMBER_MIN <= n <= L.NUMBER_MAX for n in picks)
        assert sum(row["amounts"]) > 0
        counts = row["waves"]
        top = max(counts.values())
        max_bucket_counts.append(top)
        if top >= 7:
            one_bucket_ge7 += 1
        # balanced：每个桶最多「非空桶数均分上取整」，10/3 → 至多 4（桶为空时并给其它桶）
        assert top <= 5

    assert one_bucket_ge7 == 0
    assert statistics.fmean(max_bucket_counts) == pytest.approx(4.346, abs=0.02)


def test_legacy_drain_is_still_available_and_still_clumps():
    """历史审计口径（drain）必须仍可复现，且**确实**保留旧的「一坨」形状。"""
    rows = _walk_forward({"wave_alloc": L.WAVE_ALLOC_DRAIN})
    one_bucket_ge7 = sum(
        1 for row in rows if max(row["waves"].values()) >= 7
    )
    assert one_bucket_ge7 == 208  # 100%：正是 balanced 要消除的形状
    assert L.DEFAULT_WAVE_ALLOC == L.WAVE_ALLOC_BALANCED


# --------------------------------------------------------------------------- #
# 5. 三项软降权不再影响输出（但标签仍在）
# --------------------------------------------------------------------------- #
def test_three_penalty_knobs_no_longer_move_numbers_amounts_or_roles():
    base = _recommend(279, _cfg(lattice_enabled=True))
    tweaked = _recommend(
        279,
        _cfg(
            lattice_enabled=True,
            repeat_number_weight=0.0,
            repeat_zodiac_weight=0.0,
            stale_weight=0.0,
            stale_periods=1,
        ),
    )
    assert _payload(base) == _payload(tweaked)
    assert [p["amount"] for p in base["picks"]] == [
        p["amount"] for p in tweaked["picks"]
    ]


def test_penalty_flags_stay_honest_even_though_weights_are_inert():
    outcome = L.recommend(
        latest=HISTORY_30[0],
        previous=HISTORY_30[1],
        history_numbers=list(HISTORY_30),
        settings=_cfg(lattice_enabled=True, stale_periods=60),
        mode="even",
        period=279,
    )
    for pick in outcome["picks"]:
        # 权重恒 1.0、恒未降权，但标签照旧如实产出
        assert pick["soft_weight"] == 1.0
        assert pick["soft_penalized"] is False
        assert isinstance(pick["is_repeat_number"], bool)
        assert isinstance(pick["is_repeat_zodiac"], bool)
        assert isinstance(pick["is_stale"], bool)
        assert isinstance(pick["soft_reasons"], list)
        periods = pick["periods_since_last"]
        if periods is not None:
            assert pick["is_stale"] is (int(periods) >= 60)
    assert outcome["soft_weights"]["applied"] is False
    assert outcome["soft_weights"]["penalized_picks"] == 0
