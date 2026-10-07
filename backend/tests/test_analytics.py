"""样本内统计分析回归测试（``services.analytics`` + ``/api/stats``）。

覆盖：
- 频次：计数 / 出现率 / 期望值 / 偏差，以及「样本不足以支持强结论」的提示；
- 走势：相邻波动分类在边界上的判定（diff == small_max / normal_max / normal_max + 1）；
- 生肖走势：连出（最长 / 当前）与最近窗口覆盖 / 轮转；
- 回测：手工样本的已知答案（走步回测）、无未来函数验证、波动分解；
- 零开奖数据时全部接口返回 200 且带「数据不足」标记；
- 返回体里不出现「全市场」等越界措辞。

全部用例不依赖真实数据库（强制内存存储）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.analytics import (
    WALK_FORWARD_AXES,
    backtest_stats,
    build_walk_forward_config_space,
    frequency_stats,
    trend_stats,
    walk_forward_axes_size,
    walk_forward_config_search,
    walk_forward_eval_config,
    walk_forward_split,
    zodiac_trend_stats,
)
import services.analytics as analytics
from services.lottery import DEFAULT_SETTINGS, recommend

BACKEND_DIR = Path(__file__).resolve().parents[1]

# 关闭「本轮新增的三类软降权 + 号码点阵 + 重号保留」，让只校验旧引擎已知答案的
# 回测用例不被新规则影响（新规则另有专门用例覆盖）。
LEGACY_ENGINE_OFF: dict = {
    "avoid_cold_enabled": False,
    "repeat_number_weight": 1.0,
    "repeat_zodiac_weight": 1.0,
    "stale_weight": 1.0,
    "lattice_enabled": False,
    "include_repeat_number": False,
}
REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_269.txt"

STATS_GET_PATHS = (
    "/api/stats/frequency",
    "/api/stats/trend",
    "/api/stats/zodiac-trend",
)


@pytest.fixture(autouse=True)
def memory_store_env(monkeypatch):
    """强制内存存储，避免测试进程连到真实数据库。

    注意用**空串**而不是 delenv：main.py 在 import 时 load_dotenv() 会把
    backend/.env 里的 DATABASE_URL 灌回来（python-dotenv 不覆盖已存在的变量，
    但会补上被删掉的）。详见 tests/conftest.py 的说明。
    """
    monkeypatch.setenv("DATABASE_URL", "")
    yield


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client


def _draws(specials: list[int], start: date = date(2026, 6, 1)) -> list[dict]:
    """构造升序的手工期序列。

    起始日固定在 2026 农历马年内（马年春节 2026-02-17），
    因此 01 = HORSE、02 = SNAKE、03 = DRAGON、07 = RAT 等映射稳定。
    """
    return [
        {
            "period": index + 1,
            "draw_date": start + timedelta(days=index),
            "special_number": number,
        }
        for index, number in enumerate(specials)
    ]


def _by_number(result: dict) -> dict[int, dict]:
    return {row["number"]: row for row in result["numbers"]}


def _by_zodiac(result: dict) -> dict[str, dict]:
    return {row["zodiac"]: row for row in result["zodiacs"]}


# --------------------------------------------------------------------------- #
# 2. 频次
# --------------------------------------------------------------------------- #
def test_frequency_known_series():
    result = frequency_stats(_draws([1, 2, 1, 3]))

    assert result["sample_size"] == 4
    assert result["expected_number_rate"] == pytest.approx(1 / 49)

    numbers = _by_number(result)
    assert numbers[1]["appearances"] == 2
    assert numbers[1]["rate"] == pytest.approx(0.5)
    assert numbers[1]["deviation"] == pytest.approx(0.5 - 1 / 49)
    assert numbers[49]["appearances"] == 0
    assert numbers[49]["rate"] == 0.0
    assert numbers[49]["deviation"] == pytest.approx(-1 / 49)

    zodiacs = _by_zodiac(result)
    assert zodiacs["HORSE"]["appearances"] == 2
    # 2026 马年 HORSE 含 5 个号码（01/13/25/37/49），期望率 = 5/49 而非 1/12
    assert zodiacs["HORSE"]["expected_rate"] == pytest.approx(5 / 49)
    assert zodiacs["RAT"]["expected_rate"] == pytest.approx(4 / 49)
    assert zodiacs["HORSE"]["deviation"] == pytest.approx(0.5 - 5 / 49)
    # 12 生肖期望率之和应为 1
    assert sum(row["expected_rate"] for row in result["zodiacs"]) == pytest.approx(1.0)


def test_frequency_states_sample_cannot_support_strong_conclusion():
    result = frequency_stats(_draws([1, 2, 1, 3]))

    assert "不足以支持强结论" in result["note"]
    assert result["sample_power"]["can_support_strong_conclusion"] is False
    assert result["sample_power"]["expected_appearances_per_number"] == pytest.approx(
        4 / 49
    )
    assert result["sample_power"]["strong_conclusion_min_draws"] == 490


# --------------------------------------------------------------------------- #
# 3. 走势与波动边界
# --------------------------------------------------------------------------- #
def test_trend_wave_boundaries():
    # 差值依次为 10（=small_max）、30（=normal_max）、31（=normal_max + 1）
    result = trend_stats(_draws([1, 11, 41, 10]), small_max=10, normal_max=30)

    assert result["sample_size"] == 4
    pairs = [(row["diff"], row["wave_type"]) for row in result["series"]]
    assert pairs == [(None, None), (10, "small"), (30, "normal"), (31, "big")]
    assert [row["wave_label"] for row in result["series"]] == [
        None,
        "小波动",
        "常规波动",
        "大跳",
    ]

    distribution = {
        item["type"]: item["count"] for item in result["wave_distribution"]["items"]
    }
    assert distribution == {"small": 1, "normal": 1, "big": 1}
    assert result["wave_distribution"]["total_pairs"] == 3
    assert result["settings"] == {"small_max": 10, "normal_max": 30, "big_min": 31}


def test_trend_small_max_plus_one_is_normal():
    result = trend_stats(_draws([1, 12]), small_max=10, normal_max=30)

    row = result["series"][1]
    assert row["diff"] == 11  # small_max + 1
    assert row["wave_type"] == "normal"


def test_trend_series_fields_and_scope_note():
    result = trend_stats(_draws([1, 11, 41, 10]), small_max=10, normal_max=30)

    first = result["series"][0]
    assert set(first) == {
        "period",
        "draw_date",
        "special_number",
        "zodiac",
        "zodiac_label",
        "diff",
        "wave_type",
        "wave_label",
    }
    assert result["series"][0]["zodiac"] == "HORSE"
    assert result["series"][0]["zodiac_label"] == "马"
    assert "本池已导入的 4 期" in result["notes"][0]


# --------------------------------------------------------------------------- #
# 4. 生肖走势 / 连出
# --------------------------------------------------------------------------- #
def test_zodiac_trend_streaks():
    # 2026 马年：01/13/25 同为 HORSE，02 为 SNAKE → HORSE 连出 3 期
    result = zodiac_trend_stats(_draws([1, 13, 25, 2]))

    assert [row["zodiac"] for row in result["series"]] == [
        "HORSE",
        "HORSE",
        "HORSE",
        "SNAKE",
    ]

    zodiacs = _by_zodiac(result)
    assert zodiacs["HORSE"]["appearances"] == 3
    assert zodiacs["HORSE"]["max_streak"] == 3
    assert zodiacs["HORSE"]["max_streak_end_period"] == 3
    assert zodiacs["HORSE"]["current_streak"] == 0  # 最新一期不是 HORSE
    assert zodiacs["SNAKE"]["max_streak"] == 1
    assert zodiacs["SNAKE"]["current_streak"] == 1

    assert result["max_streak_leaders"][0]["zodiac"] == "HORSE"


def test_zodiac_trend_recent_window_and_rotation():
    result = zodiac_trend_stats(_draws([1, 13, 25, 2]), recent_window=12)

    window = result["recent_window"]
    assert window["requested"] == 12
    assert window["used"] == 4  # 样本不足 12 期，按实际期数
    assert window["distinct_count"] == 2
    assert window["present"] == ["SNAKE", "HORSE"]  # 按 ZODIAC_ORDER 排列
    assert window["available"] == 12
    assert window["missing"] == [
        code
        for code in (
            "RAT",
            "OX",
            "TIGER",
            "RABBIT",
            "DRAGON",
            "GOAT",
            "MONKEY",
            "ROOSTER",
            "DOG",
            "PIG",
        )
    ]
    assert result["rotation"]["distinct_in_recent"] == 2
    assert result["rotation"]["available"] == 12
    # 窗口不足 12 期时必须显式声明数据不足
    assert any("数据不足" in note for note in result["notes"])


def test_zodiac_trend_respects_smaller_window():
    result = zodiac_trend_stats(_draws([1, 2, 1, 2]), recent_window=2)

    window = result["recent_window"]
    assert window["requested"] == 2
    assert window["used"] == 2
    assert window["sequence"] == ["HORSE", "SNAKE"]
    assert window["distinct_count"] == 2


def test_zodiac_trend_full_window_has_no_insufficient_note():
    # 窗口 12 期且样本恰好 12 期 → 不再触发「窗口不足」提示
    result = zodiac_trend_stats(_draws([1, 2] * 6), recent_window=12)

    assert result["recent_window"]["used"] == 12
    assert result["recent_window"]["distinct_count"] == 2
    assert not any("数据不足" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# 6. 回测（走步）
# --------------------------------------------------------------------------- #
def test_backtest_known_answer():
    # 第 3 期：latest=20/prev=10 → 小波动池内「遗漏最久且差值最小」为 19，实际也是 19 → 命中
    # 第 4 期：latest=19/prev=20 → 预测 18，实际 05 → 未命中
    # 关闭避冷加权：本用例校验回测机制与旧引擎的已知答案（避冷另有专门用例）
    result = backtest_stats(
        _draws([10, 20, 19, 5]),
        mode="even",
        pick_count=1,
        base_settings=dict(LEGACY_ENGINE_OFF),
    )

    assert result["evaluated"] == 2
    assert result["hits"] == 1
    assert result["hit_rate"] == pytest.approx(0.5)
    assert result["data_status"] == "OK"

    window = result["evaluation_window"]
    assert window["draws_used"] == 4
    assert window["min_prior_draws"] == 2
    assert window["skipped"] == 2
    assert window["evaluated"] == 2
    assert window["first_evaluated_period"] == 3
    assert window["last_evaluated_period"] == 4
    assert "walk-forward" not in window["description"]  # 文案为中文说明
    assert "只用该期之前的数据" in window["description"]

    # 随机参考值口径 = 有效注数 / 49（而非每期可用号码数）
    assert result["random_baseline_hit_rate"] == pytest.approx(1 / 49)
    assert result["hit_rate_minus_baseline"] == pytest.approx(0.5 - 1 / 49)
    assert result["hit_rate_standard_error"] == pytest.approx(
        math.sqrt((1 / 49) * (1 - 1 / 49) / 2)
    )
    # 两期的实际特码都落在候选池内 → 理论上限为 100%
    # 默认不避开重肖：每期可用号码 = 49 - 1（仅排除上期特码本身）= 48
    assert result["actual_in_pool_rate"] == pytest.approx(1.0)
    assert result["average_available_numbers"] == pytest.approx(48.0)
    assert all(row["actual_in_candidate_pool"] for row in result["results"])
    assert result["settings"]["exclude_repeat_zodiac"] is False

    first, second = result["results"]
    assert first["period"] == 3
    assert first["latest_used"] == 20
    assert first["previous_used"] == 10
    assert first["predicted"] == [19]
    assert first["actual"] == 19
    assert first["hit"] is True

    assert second["period"] == 4
    assert second["latest_used"] == 19
    assert second["previous_used"] == 20
    assert second["predicted"] == [18]
    assert second["actual"] == 5
    assert second["hit"] is False


def test_backtest_exclude_repeat_zodiac_shrinks_pool():
    """开启避开重肖时，平均可用号码应从 48 降到约 45（去掉整组同肖）。"""
    draws = _draws([10, 20, 19, 5])
    off = backtest_stats(
        draws, mode="even", pick_count=1, base_settings={"exclude_repeat_zodiac": False}
    )
    on = backtest_stats(
        draws, mode="even", pick_count=1, base_settings={"exclude_repeat_zodiac": True}
    )
    # 默认保留重号：每次可用号码 = 全部 49 个（既不排重号也不排重肖）
    assert off["average_available_numbers"] == pytest.approx(49.0)
    # 开启避开重肖 → 去掉整组同肖（含上期特码本身）
    assert on["average_available_numbers"] == pytest.approx(45.0)
    assert off["settings"]["exclude_repeat_zodiac"] is False
    assert on["settings"]["exclude_repeat_zodiac"] is True

    # 回到旧口径（显式排除上期特码本身）时才是 48
    legacy = backtest_stats(
        draws,
        mode="even",
        pick_count=1,
        base_settings={"exclude_repeat_zodiac": False, "include_repeat_number": False},
    )
    assert legacy["average_available_numbers"] == pytest.approx(48.0)


def test_backtest_wave_breakdown():
    # 关闭避冷加权：校验回测的波动分组统计（旧引擎已知答案）
    result = backtest_stats(
        _draws([10, 20, 19, 5]),
        mode="even",
        pick_count=1,
        base_settings=dict(LEGACY_ENGINE_OFF),
    )

    realized = {item["type"]: item for item in result["wave_breakdown"]["items"]}
    assert realized["small"]["evaluated"] == 1  # |19-20| = 1
    assert realized["small"]["hits"] == 1
    assert realized["small"]["hit_rate"] == pytest.approx(1.0)
    assert realized["normal"]["evaluated"] == 1  # |5-19| = 14
    assert realized["normal"]["hits"] == 0
    assert realized["normal"]["hit_rate"] == pytest.approx(0.0)
    assert realized["big"]["evaluated"] == 0
    assert realized["big"]["hit_rate"] is None

    by_prev = {item["type"]: item for item in result["wave_breakdown_by_prev"]["items"]}
    # 两期的上一期波动都是小波动（|20-10| = 10、|19-20| = 1）
    assert by_prev["small"]["evaluated"] == 2
    assert by_prev["small"]["hits"] == 1
    assert by_prev["small"]["hit_rate"] == pytest.approx(0.5)


def test_backtest_has_no_lookahead():
    # 关闭避冷加权与新增软降权：本用例只校验「不使用未来数据」
    base_settings = dict(LEGACY_ENGINE_OFF)
    base = backtest_stats(
        _draws([10, 20, 19, 5]),
        mode="even",
        pick_count=1,
        base_settings=base_settings,
    )
    # 只改动最后一期（未来）；更早一期的结果必须逐字段完全一致
    modified = backtest_stats(
        _draws([10, 20, 19, 45]),
        mode="even",
        pick_count=1,
        base_settings=base_settings,
    )

    assert base["results"][0] == modified["results"][0]
    # 最后一期的预测只依赖它之前的数据，也必须一致；只有 actual / hit 变了
    assert (
        base["results"][1]["predicted"] == modified["results"][1]["predicted"] == [18]
    )
    assert base["results"][1]["actual"] == 5
    assert modified["results"][1]["actual"] == 45
    assert modified["results"][1]["hit"] is False


def test_backtest_respects_overrides():
    draws = _draws([10, 20, 19, 5])

    single = backtest_stats(draws, mode="single", pick_count=3)
    assert single["settings"]["mode"] == "single"
    assert single["settings"]["effective_pick_count"] == 1
    assert all(len(row["predicted"]) == 1 for row in single["results"])

    wide = backtest_stats(draws, mode="even", pick_count=3)
    assert wide["settings"]["effective_pick_count"] == 3
    assert all(len(row["predicted"]) == 3 for row in wide["results"])

    # 阈值覆盖必须生效（small_max=0 → 差值 1 也算常规波动）
    strict = backtest_stats(draws, mode="even", pick_count=1, small_max=0, normal_max=30)
    assert strict["settings"]["small_max"] == 0
    assert strict["settings"]["big_min"] == 31
    assert strict["results"][0]["realized_wave_type"] == "normal"  # |19-20| = 1


def test_backtest_reflects_avoid_cold_setting():
    """避冷加权随 base_settings 进入回测（与财富密码同源），并如实回报生效值。"""
    draws = _draws([10, 20, 19, 5])

    off = backtest_stats(
        draws,
        mode="even",
        pick_count=1,
        base_settings={"avoid_cold_enabled": False, **LEGACY_ENGINE_OFF},
    )
    on = backtest_stats(
        draws,
        mode="even",
        pick_count=1,
        base_settings={"avoid_cold_enabled": True, "avoid_cold_days": 60},
    )

    # 关闭：旧引擎「遗漏最久优先」→ 小波动桶里 24…19（差 1）先取
    assert off["results"][0]["predicted"] == [19]
    assert off["settings"]["avoid_cold_enabled"] is False
    assert off["settings"]["avoid_cold_days"] == 60

    # 开启：样本内最近出现过的 10 是非冷号 → 排到「从未出现」的冷号之前
    assert on["results"][0]["predicted"] == [10]
    assert on["settings"]["avoid_cold_enabled"] is True
    assert on["settings"]["avoid_cold_days"] == 60


def test_backtest_too_few_draws_is_insufficient():
    result = backtest_stats(_draws([10, 20]))

    assert result["evaluated"] == 0
    assert result["hit_rate"] is None
    assert result["results"] == []
    assert result["data_status"] == "INSUFFICIENT"
    assert result["data_status_label"] == "数据不足"
    assert any("数据不足" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# 6b. 回归：回测传入 recommend 的 history 必须是「最新在前」
# --------------------------------------------------------------------------- #
# 历史 bug（commit 2039786 引入）：backtest_stats 按 draw_date 升序走步，
# 却直接把升序切片 specials[:index] 当 history 传给 recommend。
# 而 services/lottery.py 全篇约定 history **最新在前**：
#   - predict_wave_band → series[:window]（取最近 W 期）
#   - compute_periods_since_last → result[n] = 首次出现的下标
#   - compute_days_since_last  → ref = dates[0]
#   - resolve_zodiac_date      → dates[0] 决定农历年
#   - window_frequency         → series[:limit]
# routers/lottery.py 也确实按「最新在前」传（见该文件 `# 最新在前` 注释）。
# 后果：回测里「最近一次出现」被算成「最早一次出现」，冷号 / 重号 / 自然日 /
# 波动线取样窗口整体反向 —— 回测等于在评估另一个策略。
def test_backtest_passes_history_newest_first(monkeypatch):
    """锁定契约：传给 recommend 的 history / history_dates 必须最新在前。"""
    captured: list[dict] = []
    real_recommend = analytics.recommend

    def spy(**kwargs):
        captured.append(kwargs)
        return real_recommend(**kwargs)

    monkeypatch.setattr(analytics, "recommend", spy)

    specials = [10, 20, 19, 5, 30, 12, 41]
    draws = _draws(specials)
    backtest_stats(
        draws, mode="even", pick_count=1, base_settings=dict(LEGACY_ENGINE_OFF)
    )

    assert captured, "回测必须调用 recommend"
    # 走步从第 3 期（index=2）起，每期调用一次
    assert len(captured) == len(specials) - 2
    for offset, call in enumerate(captured):
        index = 2 + offset
        history = list(call["history_numbers"])
        dates = list(call["history_dates"])
        assert history == list(reversed(specials[:index])), (
            f"第 {index + 1} 期 history 顺序错误（应为最新在前）：{history}"
        )
        assert dates == list(reversed([d["draw_date"] for d in draws[:index]]))
        # 最新在前 → 首元素就是该期之前那一期，末元素才是样本内最早一期
        assert history[0] == specials[index - 1]
        assert history[-1] == specials[0]
        assert dates == sorted(dates, reverse=True)
        # 长度必须严格等于「该期之前」的期数（无未来函数）
        assert len(history) == index


def test_backtest_picks_match_live_newest_first_history():
    """回测口径必须与线上 `POST /api/recommend` 同源：同一期 / 同一设置 /
    同一「最新在前」历史 → 同一份号码。

    这组参数对顺序高度敏感（大部分期数两种顺序会给出不同号码），
    因此一旦顺序退回旧的「最旧在前」，本用例会大面积失败。
    """
    specials = [(((i * 37 + 11) % 49) + 1) for i in range(45)]
    draws = _draws(specials)
    settings = {
        **DEFAULT_SETTINGS,
        "mode": "even",
        "pick_count": 3,
        "lattice_window": 3,
        "stale_periods": 5,
        "stale_weight": 0.1,
    }

    result = backtest_stats(draws, base_settings=settings)
    assert result["evaluated"] == len(specials) - 2

    order_sensitive = 0
    for row in result["results"]:
        index = row["period"] - 1
        prefix_dates = [d["draw_date"] for d in draws[:index]]

        newest_first = recommend(
            latest=specials[index - 1],
            previous=specials[index - 2],
            history_numbers=list(reversed(specials[:index])),
            history_dates=list(reversed(prefix_dates)),
            settings=settings,
            mode="even",
            period=row["period"],
        )
        oldest_first = recommend(
            latest=specials[index - 1],
            previous=specials[index - 2],
            history_numbers=list(specials[:index]),
            history_dates=list(prefix_dates),
            settings=settings,
            mode="even",
            period=row["period"],
        )
        picks_new = [pick["number"] for pick in newest_first["picks"]]
        picks_old = [pick["number"] for pick in oldest_first["picks"]]

        assert row["predicted"] == picks_new, (
            f"第 {row['period']} 期回测号码与线上口径（最新在前）不一致："
            f"backtest={row['predicted']} live={picks_new}"
        )
        order_sensitive += picks_new != picks_old

    # 保证本用例真的有能力捕获顺序 bug（否则断言形同虚设）
    assert order_sensitive > len(result["results"]) // 2


# --------------------------------------------------------------------------- #
# 7. 零开奖数据：所有接口都必须安全返回
# --------------------------------------------------------------------------- #
def test_pure_functions_with_zero_draws_do_not_crash():
    assert frequency_stats([])["numbers"] == []
    assert frequency_stats([])["sample_power"]["can_support_strong_conclusion"] is False
    assert "数据不足" in frequency_stats([])["note"]

    assert trend_stats([])["series"] == []
    assert trend_stats([])["wave_distribution"]["total_pairs"] == 0
    assert trend_stats([])["wave_distribution"]["dominant"] is None

    assert zodiac_trend_stats([])["series"] == []
    assert zodiac_trend_stats([])["recent_window"]["used"] == 0

    assert backtest_stats([])["evaluated"] == 0


def test_stats_endpoints_with_zero_draws_return_200(client):
    for path in STATS_GET_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["sample_size"] == 0
        assert body["scope"] == "本池已导入 0 期数据内"
        assert body["data_status"] == "INSUFFICIENT"
        assert body["data_status_label"] == "数据不足"

    backtest = client.post("/api/stats/backtest", json={})
    assert backtest.status_code == 200
    body = backtest.json()
    assert body["evaluated"] == 0
    assert body["results"] == []
    assert body["data_status"] == "INSUFFICIENT"
    assert body["data_status_label"] == "数据不足"

    # 空 body 也必须可用（参数全部可选）
    assert client.post("/api/stats/backtest").status_code == 200


# --------------------------------------------------------------------------- #
# 8. 真实 200 期数据（HTTP 端到端） + 口径防越界
# --------------------------------------------------------------------------- #
def _import_real_draws(client: TestClient) -> None:
    text = REAL_DRAWS_FILE.read_text(encoding="utf-8")
    response = client.post("/api/draws/import", json={"text": text})
    assert response.status_code == 200
    assert response.json()["imported"] == 200


def test_stats_endpoints_against_real_200_draws(client):
    _import_real_draws(client)

    frequency = client.get("/api/stats/frequency").json()
    assert frequency["sample_size"] == 200
    assert sum(row["appearances"] for row in frequency["numbers"]) == 200
    assert sum(row["appearances"] for row in frequency["zodiacs"]) == 200
    assert frequency["sample_power"]["can_support_strong_conclusion"] is False

    trend = client.get("/api/stats/trend").json()
    assert trend["sample_size"] == 200
    assert len(trend["series"]) == 200
    assert trend["wave_distribution"]["total_pairs"] == 199
    assert sum(
        item["count"] for item in trend["wave_distribution"]["items"]
    ) == 199
    latest = trend["series"][-1]
    assert latest["period"] == 269
    assert latest["special_number"] == 22
    assert latest["zodiac"] == "ROOSTER"
    assert latest["zodiac_label"] == "鸡"

    limited_trend = client.get("/api/stats/trend", params={"limit": 10}).json()
    assert limited_trend["sample_size"] == 10
    assert limited_trend["wave_distribution"]["total_pairs"] == 9

    zodiac_trend = client.get("/api/stats/zodiac-trend").json()
    assert zodiac_trend["sample_size"] == 200
    assert len(zodiac_trend["series"]) == 200
    assert zodiac_trend["recent_window"]["used"] == 12
    assert zodiac_trend["recent_window"]["distinct_count"] <= 12


def test_backtest_against_real_200_draws(client):
    _import_real_draws(client)

    body = client.post("/api/stats/backtest", json={}).json()

    assert body["sample_size"] == 200
    assert body["evaluated"] == 198  # 200 - min_prior_draws(2)
    assert body["evaluation_window"]["skipped"] == 2
    assert body["evaluation_window"]["first_evaluated_period"] == 72
    assert body["evaluation_window"]["last_evaluated_period"] == 269
    assert body["hits"] <= body["evaluated"]
    assert body["hit_rate"] == pytest.approx(body["hits"] / 198)
    assert len(body["results"]) == 198

    override = client.post(
        "/api/stats/backtest",
        json={"mode": "single", "pick_count": 3, "limit": 50},
    ).json()
    assert override["sample_size"] == 50
    assert override["evaluated"] == 48
    assert override["settings"]["effective_pick_count"] == 1


def test_payloads_never_use_market_wide_wording(client):
    _import_real_draws(client)

    texts = [client.get(path).text for path in STATS_GET_PATHS]
    texts.append(client.post("/api/stats/backtest", json={}).text)

    for text in texts:
        for banned in ("全市场", "全量市场", "市场最"):
            assert banned not in text
        assert "本池已导入" in text


def test_backtest_rejects_invalid_mode(client):
    response = client.post("/api/stats/backtest", json={"mode": "allin"})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 9. 回测默认取「当前用户已存配置」（而非钉死 DEFAULT_SETTINGS）
# --------------------------------------------------------------------------- #
def test_backtest_base_settings_not_wiped_by_missing_overrides():
    """未显式覆盖的项必须保留 base_settings —— 不能回退成 DEFAULT_SETTINGS。

    这是 ``/api/stats/trend`` 修过的同类 bug：把 ``None`` 展开进合并字典会
    抹掉已存阈值（``clamp_settings`` 跳过 ``None``，于是回退到默认值）。
    """
    draws = _draws([10, 20, 19, 5])
    result = backtest_stats(
        draws,
        base_settings={
            "small_max": 5,
            "normal_max": 25,
            "mode": "single",
            "pick_count": 3,
            "bet_unit": 10,
        },
    )

    assert result["settings"]["small_max"] == 5
    assert result["settings"]["normal_max"] == 25  # 不是 DEFAULT 的 30
    assert result["settings"]["big_min"] == 26
    assert result["settings"]["mode"] == "single"
    assert result["settings"]["effective_pick_count"] == 1
    assert result["parameter_sources"] == {
        "small_max": "saved_settings",
        "normal_max": "saved_settings",
        "mode": "saved_settings",
        "pick_count": "saved_settings",
    }


def test_backtest_without_base_settings_falls_back_to_defaults():
    result = backtest_stats(_draws([10, 20, 19, 5]))
    assert result["settings"]["normal_max"] == 30
    assert result["settings"]["mode"] == "even"
    assert all(
        source == "default" for source in result["parameter_sources"].values()
    )


def test_backtest_endpoint_uses_saved_settings_by_default(client):
    _import_real_draws(client)
    client.put(
        "/api/settings",
        json={"small_max": 10, "normal_max": 25, "mode": "single", "pick_count": 3},
    )

    body = client.post("/api/stats/backtest", json={}).json()

    assert body["settings"]["normal_max"] == 25
    assert body["settings"]["small_max"] == 10
    assert body["settings"]["mode"] == "single"
    assert body["settings"]["effective_pick_count"] == 1  # single 恒为 1 注
    assert body["settings"]["big_min"] == 26
    assert all(
        source == "saved_settings" for source in body["parameter_sources"].values()
    )
    assert all(len(row["predicted"]) == 1 for row in body["results"])


def test_backtest_endpoint_request_override_beats_saved_settings(client):
    _import_real_draws(client)
    client.put(
        "/api/settings",
        json={"small_max": 10, "normal_max": 25, "mode": "single", "pick_count": 3},
    )

    # 只覆盖 mode：未覆盖的 normal_max / pick_count 必须沿用已存值
    body = client.post("/api/stats/backtest", json={"mode": "even"}).json()

    assert body["settings"]["mode"] == "even"
    assert body["settings"]["normal_max"] == 25  # 未覆盖 → 用已存 25，而非默认 30
    assert body["settings"]["effective_pick_count"] == 3  # 用已存的 pick_count=3
    assert body["parameter_sources"]["mode"] == "request_override"
    assert body["parameter_sources"]["normal_max"] == "saved_settings"
    assert body["parameter_sources"]["pick_count"] == "saved_settings"
    assert all(len(row["predicted"]) == 3 for row in body["results"])


def test_real_database_connection_is_hard_blocked(monkeypatch):
    """即使 DATABASE_URL 被重新灌成真实 DSN，也不允许真的建池（conftest 硬闸）。"""
    import asyncio

    import db

    asyncio.run(db.close_pool())  # 确保 _pool 为空
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/not_a_real_db"
    )
    with pytest.raises(RuntimeError):
        asyncio.run(db.init_pool())


# --------------------------------------------------------------------------- #
# 10. 参数扫描（方案 A：样本内对照，只读，不写库）
# --------------------------------------------------------------------------- #
def test_backtest_sweep_known_grid_and_honesty():
    """默认网格 = bias×window×avoid_cold；每行带判定；文案禁止「提高命中率」。"""
    from services.analytics import backtest_sweep

    # 足够走步：min_prior=2 → 可评估 8 期
    draws = _draws([10, 20, 15, 25, 12, 30, 18, 22, 14, 28])
    result = backtest_sweep(
        draws,
        base_settings={
            "mode": "even",
            "pick_count": 3,
            "trend_bias": "neutral",
            "trend_window": 30,
            "avoid_cold_enabled": False,
        },
        # 缩网格，加快单测：2×2×2 = 8 组
        trend_biases=["neutral", "hot"],
        trend_windows=[30, 0],
        avoid_cold_values=[False, True],
    )

    assert result["combo_count"] == 8
    assert len(result["rows"]) == 8
    assert result["axes"]["trend_bias"] == ["neutral", "hot"]
    assert result["held_settings"]["pick_count"] == 3
    # 排序：命中率降序（None 垫底）
    rates = [row["hit_rate"] for row in result["rows"] if row["hit_rate"] is not None]
    assert rates == sorted(rates, reverse=True)
    # 每行都有判定
    for row in result["rows"]:
        assert row["verdict"]["kind"] in ("insufficient", "noise", "beyond")
        assert "label" in row["verdict"] and "text" in row["verdict"]
        assert "trend_bias_label" in row
        assert "trend_window_label" in row
    # 当前设置那一行必须能标出来
    assert any(row["matches_baseline"] for row in result["rows"])
    # 诚实口径：禁止包装成「已优化 / 提高命中率」
    joined = "；".join(result["notes"])
    assert "只读" in joined
    assert "不是" in joined  # 「不是最优策略」
    assert "禁止" in joined and "提高命中率" in joined
    for banned in ("全市场", "全量市场", "市场最"):
        assert banned not in joined


def test_backtest_sweep_include_results_off_keeps_response_lean():
    """扫描路径不得拖出逐期明细（响应体要能进前端表格）。"""
    from services.analytics import backtest_stats

    draws = _draws([10, 20, 15, 25, 12, 30])
    lean = backtest_stats(
        draws,
        base_settings={"pick_count": 3, "mode": "even"},
        include_results=False,
        include_wave_breakdown=False,
    )
    assert "results" not in lean
    assert "wave_breakdown" not in lean
    assert "verdict" in lean
    assert "trend_bias" in lean["settings"]
    assert "trend_window" in lean["settings"]

    full = backtest_stats(
        draws, base_settings={"pick_count": 3, "mode": "even"}
    )
    assert "results" in full and "wave_breakdown" in full
    assert "verdict" in full


def test_backtest_sweep_http_endpoint(client):
    """POST /api/stats/backtest/sweep：只读、缩网格可复现、不改设置。"""
    # 手工导入少量期数（够走步即可）；避免拉全量 200 期拖慢扫网格
    lines = []
    start = date(2026, 6, 1)
    for index, number in enumerate([10, 20, 15, 25, 12, 30, 18, 22]):
        day = start + timedelta(days=index)
        lines.append(f"{index + 1}期 {day.isoformat()}：+{number:02d}马")
    imported = client.post(
        "/api/draws/import", json={"text": "\n".join(lines), "replace_existing": True}
    )
    assert imported.status_code == 200

    before = client.get("/api/settings").json()

    body = client.post(
        "/api/stats/backtest/sweep",
        json={
            "trend_biases": ["neutral", "hot"],
            "trend_windows": [30],
            "avoid_cold_values": [False],
            "pick_count": 3,
        },
    ).json()

    assert body["combo_count"] == 2
    assert len(body["rows"]) == 2
    assert body["held_settings"]["pick_count"] == 3
    assert all(row["verdict"]["kind"] for row in body["rows"])
    # 扫描不得改写设置
    after = client.get("/api/settings").json()
    assert after["trend_bias"] == before["trend_bias"]
    assert after["trend_window"] == before["trend_window"]
    assert after["avoid_cold_enabled"] == before["avoid_cold_enabled"]

    # 文案禁词
    text = client.post(
        "/api/stats/backtest/sweep",
        json={
            "trend_biases": ["neutral"],
            "trend_windows": [30],
            "avoid_cold_values": [False],
        },
    ).text
    for banned in ("全市场", "全量市场", "市场最"):
        assert banned not in text
    assert "本池已导入" in text


def test_backtest_single_includes_verdict_field(client):
    """单次回测响应也带 verdict（与扫描同口径），前端可选用。"""
    lines = []
    start = date(2026, 6, 1)
    for index, number in enumerate([10, 20, 15, 25, 12, 30]):
        day = start + timedelta(days=index)
        lines.append(f"{index + 1}期 {day.isoformat()}：+{number:02d}马")
    imported = client.post(
        "/api/draws/import", json={"text": "\n".join(lines), "replace_existing": True}
    )
    assert imported.status_code == 200
    body = client.post("/api/stats/backtest", json={"pick_count": 3}).json()
    assert "verdict" in body
    assert body["verdict"]["kind"] in ("insufficient", "noise", "beyond")
    assert "trend_bias" in body["settings"]
    assert "trend_window" in body["settings"]


# --------------------------------------------------------------------------- #
# 7. 通用 walk-forward 配置搜索（训练窗选参 / 验证窗打分）
# --------------------------------------------------------------------------- #
def test_walk_forward_axes_size_matches_declared_product():
    """声明空间大小 = 笛卡尔积去重后（关掉的轴不翻倍）的真实组合数。"""
    axes = WALK_FORWARD_AXES
    # 必含轴（deliverable 里声明的旋钮）
    for key in (
        "pick_strategy",
        "trend_bias",
        "trend_window",
        "lattice_enabled",
        "lattice_window",
        "repeat_number_weight",
        "repeat_zodiac_weight",
        "stale_periods",
        "stale_weight",
        "avoid_cold_enabled",
        "avoid_cold_days",
        "include_repeat_number",
        "exclude_repeat_zodiac",
        "score_w_focus",
        "score_w_mid",
        "score_w_omit",
        "score_w_diff",
    ):
        assert key in axes and len(axes[key]) >= 2

    # 独立复算：先把「关掉即失效」的轴折叠掉，再乘策略分支
    other = 1
    for key, values in axes.items():
        if key in (
            "lattice_enabled",
            "lattice_window",
            "avoid_cold_enabled",
            "avoid_cold_days",
            "pick_strategy",
            "score_w_focus",
            "score_w_mid",
            "score_w_omit",
            "score_w_diff",
        ):
            continue
        other *= len(values)
    lattice = 1 + (len(axes["lattice_enabled"]) - 1) * len(axes["lattice_window"])
    cold = 1 + (len(axes["avoid_cold_enabled"]) - 1) * len(axes["avoid_cold_days"])
    weights = 1
    for key in ("score_w_focus", "score_w_mid", "score_w_omit", "score_w_diff"):
        weights *= len(axes[key])
    strategies = 1 + (len(axes["pick_strategy"]) - 1) * weights
    assert walk_forward_axes_size() == other * lattice * cold * strategies
    assert walk_forward_axes_size() == 1_726_272


def test_walk_forward_split_windows_are_disjoint_and_ordered():
    draws = _draws(list(range(1, 49)) * 3)  # 144 期
    split = walk_forward_split(draws, train_ratio=0.7)
    assert split["data_status"] == "OK"
    assert split["eval_count"] == len(draws) - 2
    assert split["train_eval"] == int((len(draws) - 2) * 0.7)
    assert split["valid_eval"] == split["eval_count"] - split["train_eval"]
    # 训练窗最后一期 < 验证窗第一期（严格不重叠）
    assert split["train_last_period"] < split["valid_first_period"]
    assert split["valid_first_period"] == (
        split["train_last_period"] + 1
    )
    # 验证子序列保留 min_prior 期做前置历史
    assert len(split["valid_draws"]) == split["valid_eval"] + 2


def test_walk_forward_split_insufficient_when_too_short():
    split = walk_forward_split(_draws([1, 2, 3, 4, 5]))
    assert split["data_status"] == "INSUFFICIENT"
    assert split["data_status_label"] == "数据不足"


def test_build_config_space_is_seeded_and_canonical():
    base = {"pick_count": 6, "mode": "even"}
    first = build_walk_forward_config_space(sample_size=24, seed=7, base_settings=base)
    second = build_walk_forward_config_space(sample_size=24, seed=7, base_settings=base)
    third = build_walk_forward_config_space(sample_size=24, seed=8, base_settings=base)
    assert first["evaluated_size"] == 24
    assert [c["pick_strategy"] for c in first["configs"]] == [
        c["pick_strategy"] for c in second["configs"]
    ]
    assert first["declared_size"] == 1_726_272
    # 换 seed 应给出不同抽样（否则等于没抽样）
    assert [repr(c) for c in first["configs"]] != [
        repr(c) for c in third["configs"]
    ]
    # 不改变行为的规范化：wave_round 不读打分权重；点阵关闭时不读窗口
    for cfg in first["configs"]:
        if cfg["pick_strategy"] == "wave_round":
            for key in (
                "score_w_focus",
                "score_w_mid",
                "score_w_omit",
                "score_w_diff",
            ):
                assert cfg[key] == DEFAULT_SETTINGS[key]
        if not cfg["lattice_enabled"]:
            assert cfg["lattice_window"] == DEFAULT_SETTINGS["lattice_window"]
        # 非 neutral 偏好必须带显式标记，否则读取端会回退 neutral
        assert cfg["trend_bias_explicit"] is True


def test_walk_forward_eval_config_matches_direct_backtests():
    draws = _draws(list(range(1, 49)) * 3)
    split = walk_forward_split(draws, train_ratio=0.7)
    config = {"pick_count": 4, "mode": "even", "lattice_enabled": False}
    row = walk_forward_eval_config(
        split["series"], config, split_index=split["split_index"]
    )
    train = backtest_stats(
        split["train_draws"], base_settings=config, include_results=False
    )
    valid = backtest_stats(
        split["valid_draws"], base_settings=config, include_results=False
    )
    assert row["train_hits"] == train["hits"]
    assert row["train_evaluated"] == train["evaluated"]
    assert row["valid_hits"] == valid["hits"]
    assert row["valid_evaluated"] == valid["evaluated"]
    assert row["valid_delta"] == pytest.approx(valid["hit_rate_minus_baseline"])


def test_walk_forward_config_search_selects_on_train_not_validation():
    """选参只允许用训练窗：selected 必须是训练窗 Δ 的 argmax，且验证字段来自验证窗。"""
    draws = _draws(list(range(1, 49)) * 3)
    space = build_walk_forward_config_space(
        sample_size=12,
        seed=3,
        base_settings={"pick_count": 4, "mode": "even"},
    )
    result = walk_forward_config_search(draws, configs=space["configs"])
    assert result["grid_size"] == 12
    rows = result["rows"]
    best_train = max(row["train_delta"] for row in rows)
    assert result["selected"]["train_delta"] == pytest.approx(best_train)
    assert result["selected"] is rows[0]
    assert result["selected_valid_delta"] == result["selected"]["valid_delta"]
    # 验证窗最大 Δ 只是事后对照，可能高于被选中者的验证 Δ
    assert result["max_valid_delta"] == pytest.approx(
        max(row["valid_delta"] for row in rows)
    )
    # 口径纪律：只能以否定 / 禁止的形式出现「优化 / 提高命中率」，不得正向宣称
    joined = "".join(result["notes"])
    assert "禁止" in joined
    assert "已提高命中率" not in joined
    assert "已优化命中率" not in joined.replace("说成「已优化命中率」", "")
    assert "全市场" not in joined


def test_walk_forward_config_search_insufficient_draws():
    result = walk_forward_config_search(
        _draws([1, 2, 3, 4, 5]),
        configs=[{"pick_count": 2}],
    )
    assert result["data_status"] == "INSUFFICIENT"
    assert result["data_status_label"] == "数据不足"
    assert result["selected"] is None
    assert result["rows"] == []
    assert any("数据不足" in note for note in result["notes"])


def test_tune_delta_score_weights_reuses_walk_forward_split(monkeypatch):
    """存量打分权重调参器复用同一套 walk-forward 切分，避免两份口径漂移。"""
    draws = _draws(list(range(1, 49)) * 2)  # 96 期
    split = walk_forward_split(draws)
    calls: list[tuple[int, str]] = []

    def fake_backtest_stats(subset, **kwargs):
        settings = kwargs.get("base_settings") or {}
        calls.append((len(subset), settings.get("pick_strategy")))
        return {
            "evaluated": max(0, len(subset) - 2),
            "hits": 0,
            "hit_rate": 0.0,
            "hit_rate_minus_baseline": 0.0,
            "verdict": {"kind": "noise"},
        }

    monkeypatch.setattr(analytics, "backtest_stats", fake_backtest_stats)
    result = analytics.tune_delta_score_weights(draws)
    assert result["split_index"] == split["split_index"]
    assert result["baseline_train"]["evaluated"] == split["train_eval"]
    assert result["baseline_valid"]["evaluated"] == split["valid_eval"]
    # 训练窗 = series[:split_index]；验证窗保留 min_prior 期前置
    assert (split["split_index"], "wave_round") in calls
    assert (split["sample_size"] - (split["split_index"] - 2), "wave_round") in calls

