"""设置敏感度审计（``scripts/settings_sensitivity.py``）的回归测试。

覆盖四件事：

1. **分类正确**：扰动 k 个已知开关，必须能把「真的会改号码的」与「被开关挡住 /
   根本不是引擎输入的」区分开；
2. **扰动确定性**：同一组输入跑两次，逐字段一致（含缓存命中与否两条路径）；
3. **不污染状态**：审计只读，不修改调用方传入的配置、不修改模块级配置快照，
   在 ``MemoryStore`` 上「改设置 → 回测 → 还原设置 → 回测」也必须回到原值；
4. **引擎行为回归**：``services/lottery`` 的公开契约与走步回测结果被冻结成快照，
   ``main.py`` / ``routers`` 的导入与必需路由集合保持不变。

口径：全部为**样本内**对照；命中数变化不代表命中概率变化，期望值恒为 ``赔率/49-1``。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:  # pragma: no cover - pytest 通常已加
    sys.path.insert(0, str(BACKEND_DIR))

from repository import MemoryStore  # noqa: E402
from services.analytics import (  # noqa: E402
    NUMBER_MAX,
    NUMBER_MIN,
    backtest_stats,
    binomial_tail_p,
    normalize_draws,
)
from services.lottery import (  # noqa: E402
    DEFAULT_SETTINGS,
    MODES,
    PICK_STRATEGIES,
    TREND_BIASES,
    TREND_BIAS_EXPLICIT_KEY,
    clamp_settings,
    recommend,
)  # noqa: E402
from scripts.settings_sensitivity import (  # noqa: E402
    AUDIT_CONFIGS,
    CLASS_ACTIVE,
    CLASS_COSMETIC,
    CLASS_DEAD,
    CLASS_INERT,
    LIVE_SETTINGS,
    classify,
    consistency_check,
    evaluate_setting,
    load_series,
    power_report,
    render_markdown,
    resolve_effective,
    run_audit,
    sweep_for,
    walk_forward,
)

REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_279.json"

# 冻结的走步回测快照（合成序列 + 现场配置）：
# 若 services/lottery 的选号或预算规则被无意改动，这些数字会一起变，测试立刻报警。
SYNTHETIC_LENGTH = 64
FROZEN_SYNTHETIC_HITS = 20
FROZEN_LIVE_HITS = 38
FROZEN_LIVE_EVALUATED = 208


# --------------------------------------------------------------------------- #
# 夹具 / 构造器
# --------------------------------------------------------------------------- #
def _synthetic_raw(length: int = SYNTHETIC_LENGTH) -> list[dict]:
    """确定性手工序列（升序；步长 17 与 49 互质，60+ 期后必然出现冷号）。

    注意：本序列**相邻差值恒为 17**，因此所有依赖「差值分布」的规则
    （预测波动带 / 点阵）在它上面是常量 —— 需要波动敏感的用例请用 ``_varied_raw``。
    """
    start = date(2026, 3, 1)
    return [
        {
            "period": 100 + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": ((index * 17) % 49) + 1,
        }
        for index in range(length)
    ]


def _varied_raw(length: int = SYNTHETIC_LENGTH) -> list[dict]:
    """差值会变化的确定性序列（用于测试对「波动带」敏感的开关）。"""
    start = date(2026, 3, 1)
    rows: list[dict] = []
    number = 7
    for index in range(length):
        if index:
            step = 1 + ((index * 7) % 23)  # 1..23，跨度覆盖三类波动
            number = ((number - 1 + step) % 49) + 1
        rows.append(
            {
                "period": 200 + index,
                "draw_date": (start + timedelta(days=index)).isoformat(),
                "special_number": number,
            }
        )
    return rows


def _series(length: int = SYNTHETIC_LENGTH) -> list[dict]:
    return normalize_draws(_synthetic_raw(length))


def _varied_series(length: int = SYNTHETIC_LENGTH) -> list[dict]:
    return normalize_draws(_varied_raw(length))


def _hits(rows: list[dict]) -> int:
    return sum(1 for row in rows if row["hit"])


def _real_series() -> list[dict]:
    if not REAL_DRAWS_FILE.exists():  # pragma: no cover - 依赖本地 data/*
        pytest.skip(f"缺少本地真实开奖文件：{REAL_DRAWS_FILE}")
    return load_series(str(REAL_DRAWS_FILE))


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """每个用例都从空缓存开始，避免用例之间通过缓存互相影响。"""
    from scripts import settings_sensitivity as module

    module._CONFIG_CACHE.clear()
    yield
    module._CONFIG_CACHE.clear()


# --------------------------------------------------------------------------- #
# 1. 扫描范围与分类
# --------------------------------------------------------------------------- #
def test_sweep_for_bool_takes_the_negation_and_never_current_value():
    assert sweep_for("lattice_enabled", True) == (False,)
    assert sweep_for("exclude_repeat_zodiac", False) == (True,)
    assert sweep_for(TREND_BIAS_EXPLICIT_KEY, True) == (False,)
    assert sweep_for("avoid_cold_enabled", False) == (True,)


def test_sweep_for_enum_and_numeric_exclude_current_value():
    modes = sweep_for("mode", "even")
    assert "even" not in modes
    assert set(modes) <= set(MODES)

    biases = sweep_for("trend_bias", "mid")
    assert "mid" not in biases
    assert set(biases) <= set(TREND_BIASES)

    assert 15 not in sweep_for("small_max", 15)
    assert sweep_for("small_max", 15)


def test_sweep_detects_known_active_setting_lattice_enabled():
    """``lattice_enabled`` 在现场配置下真的改号码（不是「看着像开关」）。"""
    series = _series()
    effect = evaluate_setting(
        "lattice_enabled", "live", LIVE_SETTINGS, series, (False,)
    )
    assert effect["numbers_changed"] is True
    assert effect["max_periods_picks_changed"] > 0
    assert effect["numbers_changed_values"] == [False]
    assert effect["alternatives"][0]["periods_picks_changed"] > 0

    cls, _reason = classify(effect, {}, "lattice_enabled")
    assert cls == CLASS_ACTIVE


def test_dead_key_big_min_is_not_an_engine_input():
    """``big_min`` 是只读派生值：clamp_settings 会忽略它，任何取值都改不动号码。"""
    series = _series()
    effect = evaluate_setting("big_min", "live", LIVE_SETTINGS, series, (10, 40))
    assert effect["numbers_changed"] is False
    assert effect["max_periods_picks_changed"] == 0
    assert effect["hits_values"] == [effect["hits_base"]]
    assert effect["amounts_changed"] is False

    cls, _reason = classify(effect, {}, "big_min")
    assert cls == CLASS_DEAD


def test_gated_weight_is_inert_under_live_but_active_under_score_top():
    """``score_w_*`` 只在 pick_strategy=score_top 时被读 → 现场配置下被 gate 掉。"""
    series = _series()
    live_effect = evaluate_setting(
        "score_w_omit", "live", LIVE_SETTINGS, series, sweep_for("score_w_omit", 0.0)
    )
    assert live_effect["numbers_changed"] is False
    assert live_effect["max_periods_picks_changed"] == 0

    score_top_effect = evaluate_setting(
        "score_w_omit",
        "score_top",
        AUDIT_CONFIGS["score_top"],
        series,
        sweep_for("score_w_omit", 0.0),
    )
    assert score_top_effect["numbers_changed"] is True

    cls, reason = classify(
        live_effect, {"score_top": score_top_effect}, "score_w_omit"
    )
    assert cls == CLASS_INERT
    assert "score_top" in reason


def test_zero_weight_multiplied_to_zero_is_cosmetic_at_most():
    """``odds`` 只进兑付换算，不参与选号 → 号码与注数一条都不变。"""
    series = _series()
    effect = evaluate_setting("odds", "live", LIVE_SETTINGS, series, (40.0, 60.0))
    assert effect["numbers_changed"] is False
    assert effect["pick_count_changed"] is False
    assert effect["amounts_changed"] is False
    # 赔率改了 → 兑付必然变（这是「COSMETIC」而不是「DEAD」的判据）
    assert effect["payout_changed"] is True

    cls, _reason = classify(effect, {}, "odds")
    assert cls == CLASS_COSMETIC


def test_role_weights_need_budget_slack_to_do_anything():
    """现场 50 元 / 5 元 / 10 注 = 刚好最低注 → 角色配额没有余量可分配。"""
    series = _series()
    live_effect = evaluate_setting(
        "role_w_primary", "live", LIVE_SETTINGS, series, (5.0,)
    )
    assert live_effect["numbers_changed"] is False
    assert live_effect["amounts_changed"] is False

    slack_effect = evaluate_setting(
        "role_w_primary",
        "slack_budget",
        AUDIT_CONFIGS["slack_budget"],
        series,
        (5.0,),
    )
    # 预算有余量时，角色配额只改金额分布 —— 号码仍然一个都不动。
    assert slack_effect["numbers_changed"] is False
    assert slack_effect["amounts_changed"] is True

    cls, _reason = classify(live_effect, {"slack_budget": slack_effect}, "role_w_primary")
    assert cls == CLASS_INERT


def test_engine_bug_is_fixed_and_cell_is_now_evaluable():
    """回归护栏：score_top + 预算不足 1 个注码单位过去会让引擎抛 KeyError。

    该崩溃已由 ``plan_budget`` 统一预算归一化修掉（改为返回显式 ``no_ticket``
    与 ``BUDGET_TOO_SMALL_FOR_ONE_UNIT``），因此这一格不再报错，而是被正常评估。
    本用例与审计的 ``engine_error_values`` 口径互为反向护栏：过去断言「必须记下
    崩溃」，现在断言「崩溃不再发生、且该格确实被评估到」。
    """
    series = _series()
    effect = evaluate_setting(
        "amount_unit", "score_top", AUDIT_CONFIGS["score_top"], series, (100,)
    )
    assert effect["engine_error_values"] == []
    # 该格不再被算作「引擎出错」而排除，因此确实被评估到了
    assert effect["alternatives_evaluated"] == 1


# --------------------------------------------------------------------------- #
# 2. 确定性
# --------------------------------------------------------------------------- #
def test_perturbation_is_deterministic():
    # 用差值会变化的序列：本序列下 lattice_window 真的会改号码
    series = _varied_series()
    first = evaluate_setting(
        "lattice_window", "live", LIVE_SETTINGS, series, sweep_for("lattice_window", 30)
    )
    second = evaluate_setting(
        "lattice_window", "live", LIVE_SETTINGS, series, sweep_for("lattice_window", 30)
    )
    assert first == second
    assert first["max_periods_picks_changed"] > 0
    assert first["hits_values"] == second["hits_values"]


def test_constant_diff_series_makes_wave_window_inert():
    """恒定相邻差值的序列上，预测波动带与窗口无关 → 这是夹具的已知性质，必须显式记录。"""
    constant = evaluate_setting(
        "lattice_window", "live", LIVE_SETTINGS, _series(), sweep_for("lattice_window", 30)
    )
    assert constant["max_periods_picks_changed"] == 0

    varied = evaluate_setting(
        "lattice_window",
        "live",
        LIVE_SETTINGS,
        _varied_series(),
        sweep_for("lattice_window", 30),
    )
    assert varied["max_periods_picks_changed"] > 0


def test_walk_forward_is_deterministic_and_matches_backtest_stats():
    series = _series()
    rows_a = walk_forward(series, LIVE_SETTINGS)
    rows_b = walk_forward(series, LIVE_SETTINGS)
    assert rows_a == rows_b

    outcome = backtest_stats(
        _synthetic_raw(),
        base_settings=resolve_effective(LIVE_SETTINGS),
        include_results=False,
        include_wave_breakdown=False,
    )
    assert _hits(rows_a) == int(outcome["hits"])
    assert len(rows_a) == int(outcome["evaluated"])


# --------------------------------------------------------------------------- #
# 3. 不污染状态
# --------------------------------------------------------------------------- #
def test_evaluate_setting_does_not_mutate_caller_settings():
    series = _series()
    raw = copy.deepcopy(LIVE_SETTINGS)
    before = copy.deepcopy(raw)
    evaluate_setting(
        "lattice_window", "live", raw, series, sweep_for("lattice_window", 30)
    )
    assert raw == before


def test_run_audit_does_not_mutate_module_snapshots():
    series = _series(24)
    before = copy.deepcopy(AUDIT_CONFIGS)
    payload = run_audit(
        series,
        config_names=("live", "snapshot"),
        keys=("lattice_enabled", "big_min"),
        include_power=False,
    )
    assert AUDIT_CONFIGS == before
    assert set(payload["tables"]) == {"live", "snapshot"}
    assert payload["consistency"]["live"]["matches_backtest_stats"] is True
    assert payload["consistency"]["snapshot"]["matches_backtest_stats"] is True


def test_audit_is_read_only_against_memory_store_and_restores_settings():
    """在内存 store 上「改设置 → 回测 → 还原 → 回测」必须回到原值。"""
    draws = _synthetic_raw()
    series = normalize_draws(draws)

    async def _run() -> tuple[dict, int, dict, int, dict]:
        store = MemoryStore()
        original = await store.get_settings(None)
        baseline_hits = int(
            backtest_stats(
                draws,
                base_settings=resolve_effective(original),
                include_results=False,
                include_wave_breakdown=False,
            )["hits"]
        )

        # 默认已改为 lattice_enabled=False（2026-10-07）；为证明「改动确实生效」，
        # 这里把开关拨到**非默认**的 True（否则改一个等于没改的值，断言失去意义）。
        await store.update_settings({"lattice_enabled": True})
        toggled_hits = int(
            backtest_stats(
                draws,
                base_settings=resolve_effective(await store.get_settings(None)),
                include_results=False,
                include_wave_breakdown=False,
            )["hits"]
        )

        # 还原：把原始配置整体写回，并再次读出来比对
        await store.update_settings(dict(original))
        restored = await store.get_settings(None)
        restored_hits = int(
            backtest_stats(
                draws,
                base_settings=resolve_effective(restored),
                include_results=False,
                include_wave_breakdown=False,
            )["hits"]
        )
        return original, baseline_hits, restored, restored_hits, {"toggled": toggled_hits}

    original, baseline_hits, restored, restored_hits, extra = asyncio.run(_run())

    assert original == restored
    assert baseline_hits == restored_hits
    # 备份票据：改动确实生效过（否则「还原」没有意义）
    toggled_hits = extra["toggled"]
    assert toggled_hits != baseline_hits or _hits(
        walk_forward(series, {**original, "lattice_enabled": True})
    ) != _hits(walk_forward(series, original))


def test_resolve_effective_returns_a_new_dict():
    raw = copy.deepcopy(LIVE_SETTINGS)
    resolved = resolve_effective(raw)
    assert resolved is not raw
    resolved["small_max"] = 99
    assert raw["small_max"] == LIVE_SETTINGS["small_max"]


# --------------------------------------------------------------------------- #
# 4. 引擎行为回归
# --------------------------------------------------------------------------- #
def test_frozen_walk_forward_snapshot_synthetic():
    rows = walk_forward(_series(), LIVE_SETTINGS)
    assert len(rows) == SYNTHETIC_LENGTH - 2
    assert _hits(rows) == FROZEN_SYNTHETIC_HITS


def test_frozen_walk_forward_snapshot_real_pool():
    series = _real_series()
    rows = walk_forward(series, LIVE_SETTINGS)
    assert len(rows) == FROZEN_LIVE_EVALUATED
    assert _hits(rows) == FROZEN_LIVE_HITS

    consistency = consistency_check(series, ("live",))
    assert consistency["live"] == {
        "hits": FROZEN_LIVE_HITS,
        "evaluated": FROZEN_LIVE_EVALUATED,
        "matches_backtest_stats": True,
        "verdict": "noise",
    }


def test_power_report_matches_analytics_and_ev_is_invariant():
    series = _real_series()
    power = power_report(series, LIVE_SETTINGS)

    assert power["hits"] == FROZEN_LIVE_HITS
    assert power["evaluated"] == FROZEN_LIVE_EVALUATED
    assert power["picks_per_period"] == 10
    assert power["random_baseline_hit_rate"] == pytest.approx(10 / 49)
    # 低于随机，且落在一个标准误内 → 与随机不可区分
    assert power["hit_rate"] < power["random_baseline_hit_rate"]
    assert power["verdict"] == "noise"
    assert power["within_noise"] is True
    assert power["binomial_p_two_sided"] == pytest.approx(0.4916, abs=5e-4)
    assert power["binomial_p_two_sided"] > 0.05
    assert power["minimum_detectable_delta"] == pytest.approx(0.0783, abs=1e-3)
    # 期望值与选号、权重、注数完全无关
    assert power["ev_per_100"] == pytest.approx(47 / 49 * 100 - 100)
    assert power["ev_per_100"] == pytest.approx(-4.0816, abs=1e-3)


def test_binomial_p_helper_matches_analytics_import():
    """审计用的 p 值就是 analytics 的实现，不允许自己再写一份。"""
    from services import analytics

    assert binomial_tail_p(38, 208, 10 / 49) == analytics.binomial_tail_p(
        38, 208, 10 / 49
    )


def test_lottery_public_contract_unchanged():
    assert set(DEFAULT_SETTINGS) == {
        "small_max",
        "normal_max",
        "total_amount",
        "amount_unit",
        "mode",
        "pick_count",
        "odds",
        "exclude_repeat_zodiac",
        "trend_bias",
        "trend_window",
        "avoid_cold_enabled",
        "avoid_cold_days",
        "pick_strategy",
        "score_w_focus",
        "score_w_mid",
        "score_w_omit",
        "score_w_diff",
        TREND_BIAS_EXPLICIT_KEY,
        "include_repeat_number",
        "repeat_number_weight",
        "repeat_zodiac_weight",
        "stale_periods",
        "stale_weight",
        "lattice_enabled",
        "lattice_window",
        "role_w_primary",
        "role_w_secondary",
        "role_w_defense",
    }
    assert MODES == ["even", "weighted", "single", "random"]
    assert PICK_STRATEGIES == ["wave_round", "score_top"]
    assert TREND_BIASES == ["neutral", "hot", "cold", "mid"]

    # 号码范围 1..49 不得被改动
    assert (NUMBER_MIN, NUMBER_MAX) == (1, 49)


def test_clamp_settings_invariants_hold():
    clamped = clamp_settings({"small_max": 40, "normal_max": 2, "amount_unit": 7})
    assert clamped["normal_max"] >= clamped["small_max"] + 1
    assert clamped["amount_unit"] % 5 == 0
    # 未知键（big_min）被忽略，不会进入有效配置
    assert "big_min" not in clamp_settings({"big_min": 3})


def test_recommend_shape_is_stable():
    series = _series(32)
    outcome = recommend(
        latest=int(series[-1]["special_number"]),
        previous=int(series[-2]["special_number"]),
        history_numbers=[row["special_number"] for row in reversed(series)],
        settings=clamp_settings(dict(LIVE_SETTINGS)),
        mode="even",
        period=int(series[-1]["period"]),
        history_dates=[row["draw_date"] for row in reversed(series)],
    )
    assert len(outcome["picks"]) == 10
    for pick in outcome["picks"]:
        assert NUMBER_MIN <= int(pick["number"]) <= NUMBER_MAX
        assert int(pick["amount"]) % 5 == 0
        assert int(pick["amount"]) >= 5
    assert outcome["staked_total"] == sum(int(p["amount"]) for p in outcome["picks"])


def test_main_and_routers_import_and_expose_expected_routes():
    from main import app

    paths = {route.path for route in app.routes}
    required = {
        "/api/health",
        "/api/settings",
        "/api/recommend",
        "/api/stats/backtest",
        "/api/stats/backtest/sweep",
    }
    assert required <= paths, f"缺少必需路由：{sorted(required - paths)}"


# --------------------------------------------------------------------------- #
# 5. 报告渲染
# --------------------------------------------------------------------------- #
def test_render_markdown_lists_every_setting_and_class():
    payload = run_audit(
        _series(24),
        config_names=("live", "snapshot"),
        keys=("lattice_enabled", "big_min", "odds"),
        include_power=False,
    )
    text = render_markdown(payload)
    assert text.startswith("# ")
    for code in (CLASS_ACTIVE, CLASS_COSMETIC, CLASS_DEAD, CLASS_INERT):
        assert code in text
    assert "`lattice_enabled`" in text
    assert "`big_min`" in text
    assert "禁止升格为全量" in text


def test_audit_json_payload_is_utf8_serialisable():
    payload = run_audit(
        _series(24),
        config_names=("live",),
        keys=("lattice_enabled",),
        include_power=False,
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    assert json.loads(encoded) == payload
