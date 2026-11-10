"""逆向博弈叠加层单元 / 推荐集成测试。"""

from __future__ import annotations

from datetime import date, timedelta

from services.lottery import MODE_LADDER, recommend, zodiac_group
from services.reverse_game import (
    DIRECTION_FOLLOW,
    DIRECTION_REVERSE,
    GUARD_KIND_REPEAT_NUMBER,
    GUARD_KIND_REPEAT_ZODIAC,
    MACRO_COLD,
    MACRO_HOT,
    PREFER_COLD,
    PREFER_HOT,
    TAG_PREVIEW,
    TAG_ULTRA_COLD,
    analyze_macro_zone,
    apply_reverse_game,
    classify_number_tag,
    compute_latest_pre_omit_days,
    consecutive_zodiac_streak,
    detect_wang_zodiac,
    is_cold_break,
    pick_prev_guard_slot,
    stake_tier_counts,
)


def test_consecutive_zodiac_streak_and_direction_thresholds():
    assert zodiac_group(10) == zodiac_group(22) == zodiac_group(34)
    streak, group = consecutive_zodiac_streak([34, 22, 10, 5])
    assert streak == 3
    assert group == zodiac_group(34)


def test_detect_wang_zodiac_four_of_three():
    is_wang, group, hits = detect_wang_zodiac([34, 5, 22, 10])
    assert is_wang is True
    assert group == zodiac_group(34)
    assert hits == 3


def test_classify_number_tag_priority():
    assert (
        classify_number_tag(1, days_since_last=40, in_band=True, hot_zodiac_group=0)
        == TAG_PREVIEW
    )
    assert (
        classify_number_tag(3, days_since_last=None, in_band=False, hot_zodiac_group=None)
        == TAG_ULTRA_COLD
    )


def test_stake_tier_counts_two_three_rest():
    assert stake_tier_counts(9) == (2, 3, 4)


def test_analyze_macro_zone_hot_streak_prefers_cold():
    history = [3, 2, 1, 13, 14, 6, 5, 4, 3, 2, 1]
    macro = analyze_macro_zone(history, limit=6)
    assert macro["zone"] == MACRO_HOT
    assert macro["reverse"] is True
    assert macro["prefer_zone"] == PREFER_COLD


def test_analyze_macro_zone_cold_streak_prefers_hot():
    history = [9, 8, 7, 1, 2, 3, 4, 5, 6, 13, 14]
    macro = analyze_macro_zone(history, limit=6)
    assert macro["zone"] == MACRO_COLD
    assert macro["prefer_zone"] == PREFER_HOT


def test_analyze_macro_zone_follow_continues_current_board():
    hot_follow = analyze_macro_zone([1, 2, 3, 13, 14, 6, 5, 4, 7, 8], limit=6)
    assert hot_follow["prefer_zone"] == PREFER_HOT
    cold_follow = analyze_macro_zone([9, 8, 1, 2, 3, 4, 5, 6, 13, 14], limit=6)
    assert cold_follow["prefer_zone"] == PREFER_COLD


def test_cold_break_and_pre_omit_days():
    assert is_cold_break(94) is True
    assert is_cold_break(80) is False
    assert is_cold_break(None) is True
    tip = date(2026, 11, 10)
    older = tip - timedelta(days=94)
    history = [43, 38, 34, 46, 13, 43]
    dates = [tip, tip - timedelta(days=1), tip - timedelta(days=2), tip - timedelta(days=3), tip - timedelta(days=4), older]
    assert compute_latest_pre_omit_days(history, dates, 43) == 94


def test_apply_reverse_game_cold_relay_forces_tip_and_bans_warm_trap():
    """极寒破局：31(107天)进10元；禁19(28天)；原号43仅3元。"""
    latest = 43
    history = [43, 38, 34, 46, 13, 1, 2, 3, 4, 5]
    tip = date(2026, 11, 10)
    dates = [tip - timedelta(days=i) for i in range(len(history))]
    # 伪造：43 上次出现在 94 天前
    dates[-1] = tip - timedelta(days=94)
    history = [43, 38, 34, 46, 13, 43]
    dates = [
        tip,
        tip - timedelta(days=1),
        tip - timedelta(days=2),
        tip - timedelta(days=3),
        tip - timedelta(days=4),
        tip - timedelta(days=94),
    ]
    days = {n: 40 for n in range(1, 50)}
    days[43] = 0
    days[19] = 28
    days[31] = 107
    days[7] = 57
    picks = [
        {"number": n, "diff": abs(n - 43), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (19, 11, 44, 40, 5, 22, 32, 24, 35)
    ]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=latest,
        previous=38,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=[],
        small_max=10,
        normal_max=30,
        history_dates=dates,
    )
    assert out["summary"]["cold_break"] is True
    assert out["summary"]["cold_relay_number"] == 31
    assert 31 in out["summary"]["tip_numbers"]
    tip_pick = next(p for p in out["picks"] if int(p["number"]) == 31)
    assert int(tip_pick["amount"]) == 10
    assert 19 not in out["summary"]["tip_numbers"]
    assert 19 not in out["summary"]["mid_numbers"]
    assert 19 not in out["summary"]["defense_numbers"]
    assert 43 in out["summary"]["defense_numbers"]
    guard = next(p for p in out["picks"] if int(p["number"]) == 43)
    assert int(guard["amount"]) == 3


def test_apply_reverse_game_prev_guard_xor_without_cold_break():
    """非极寒破局：仍二选一仅 1 个进 3 元。"""
    history = [12, 5, 8, 1, 2, 3, 4, 6, 7, 9]
    tip = date(2026, 11, 1)
    # 12 上次仅 10 天前 → 非破局
    history = [12, 5, 8, 1, 2, 12]
    dates = [
        tip,
        tip - timedelta(days=1),
        tip - timedelta(days=2),
        tip - timedelta(days=3),
        tip - timedelta(days=4),
        tip - timedelta(days=10),
    ]
    days = {n: 40 for n in range(1, 50)}
    days[12] = 0
    days[24] = 45  # 同肖合适且避开 10-40
    days[36] = 28  # 温号陷阱，非破局时也不进 suitable（10-40 已排除）
    picks = [
        {"number": n, "diff": abs(n - 12), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (24, 36, 12, 14, 16, 18, 26, 28, 30)
    ]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=12,
        previous=5,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=[],
        small_max=10,
        normal_max=30,
        history_dates=dates,
    )
    assert out["summary"]["cold_break"] is False
    assert out["summary"]["cold_relay_number"] is None
    assert out["summary"]["prev_zodiac_count"] == 1
    guard = out["summary"]["repeat_guard_number"]
    assert guard in out["summary"]["defense_numbers"]
    assert guard not in out["summary"]["tip_numbers"]


def test_apply_reverse_game_blocks_preview_from_tip():
    latest = 20
    picks = [
        {"number": n, "diff": abs(n - 20), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (21, 22, 23, 24, 25, 26, 27, 28, 29)
    ]
    days = {n: 40 for n in range(1, 50)}
    days[22] = 120
    in_band = [23]
    history = [20, 7, 15, 8, 3, 1, 2, 4, 5, 6]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=latest,
        previous=7,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=in_band,
        small_max=10,
        normal_max=30,
    )
    assert 23 not in out["summary"]["tip_numbers"]


def test_apply_reverse_game_micro_reverse_avoids_hot_zodiac_on_tip():
    history = [34, 22, 10, 5, 8, 1, 2, 3, 4, 6]
    hot = zodiac_group(34)
    days = {n: 40 for n in range(1, 50)}
    picks = [
        {"number": n, "diff": abs(n - 34), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (12, 14, 16, 18, 26, 28, 30, 32, 36)
    ]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=34,
        previous=22,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=[],
        small_max=10,
        normal_max=30,
        streak2_blocked=[10, 22, 34, 46],
    )
    # 若未触发极寒接力，tip 不含连肖主攻肖；接力号除外
    relay = out["summary"].get("cold_relay_number")
    for number in out["summary"]["tip_numbers"]:
        if relay is not None and number == relay:
            continue
        assert zodiac_group(number) != hot or out["summary"].get("cold_break")


def test_recommend_reverse_game_off_is_noop_for_amounts_shape():
    history = [12, 5, 20, 30, 8, 15, 22, 3, 9, 41] + list(range(1, 40))
    off = recommend(
        12,
        5,
        history,
        settings={
            "pick_count": 9,
            "mode": "ladder",
            "lattice_enabled": False,
            "reverse_game_enabled": False,
            "restrict_pick_zodiacs": False,
        },
        mode=MODE_LADDER,
        period=1001,
    )
    assert off["reverse_game"]["enabled"] is False
    assert [p["amount"] for p in off["picks"]] == [10, 10, 5, 5, 5, 3, 3, 3, 3]


def test_recommend_reverse_game_on_ladder_tiers_and_contract_key():
    from services.lottery import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["reverse_game_enabled"] is True
    history = [12, 5, 20, 30, 8, 15, 22, 3, 9, 41] + list(range(1, 40))
    on = recommend(
        12,
        5,
        history,
        settings={
            "pick_count": 9,
            "mode": "ladder",
            "lattice_enabled": True,
            "reverse_game_enabled": True,
            "restrict_pick_zodiacs": False,
        },
        mode=MODE_LADDER,
        period=1002,
    )
    assert on["reverse_game"]["enabled"] is True
    assert on["reverse_game"]["direction"] in (DIRECTION_FOLLOW, DIRECTION_REVERSE)
    amounts = [int(p["amount"]) for p in on["picks"]]
    assert amounts.count(10) == 2
    assert amounts.count(5) == 3
    assert amounts.count(3) == 4
