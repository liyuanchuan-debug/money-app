"""逆向博弈叠加层单元 / 推荐集成测试。"""

from __future__ import annotations

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
    consecutive_zodiac_streak,
    detect_wang_zodiac,
    pick_prev_guard_slot,
    stake_tier_counts,
)


def test_consecutive_zodiac_streak_and_direction_thresholds():
    assert zodiac_group(10) == zodiac_group(22) == zodiac_group(34)
    streak, group = consecutive_zodiac_streak([34, 22, 10, 5])
    assert streak == 3
    assert group == zodiac_group(34)
    streak2, _ = consecutive_zodiac_streak([34, 22, 5])
    assert streak2 == 2


def test_detect_wang_zodiac_four_of_three():
    is_wang, group, hits = detect_wang_zodiac([34, 5, 22, 10])
    assert is_wang is True
    assert group == zodiac_group(34)
    assert hits == 3
    is_wang2, _, _ = detect_wang_zodiac([34, 5, 8, 10])
    assert is_wang2 is False


def test_classify_number_tag_priority():
    assert (
        classify_number_tag(1, days_since_last=40, in_band=True, hot_zodiac_group=0)
        == TAG_PREVIEW
    )
    assert (
        classify_number_tag(2, days_since_last=1, in_band=False, hot_zodiac_group=None)
        == "ultra_hot"
    )
    assert (
        classify_number_tag(3, days_since_last=None, in_band=False, hot_zodiac_group=None)
        == TAG_ULTRA_COLD
    )


def test_stake_tier_counts_two_three_rest():
    assert stake_tier_counts(9) == (2, 3, 4)
    assert stake_tier_counts(8) == (2, 3, 3)


def test_analyze_macro_zone_hot_streak_prefers_cold():
    history = [3, 2, 1, 13, 14, 6, 5, 4, 3, 2, 1]
    macro = analyze_macro_zone(history, limit=6)
    assert macro["zone"] == MACRO_HOT
    assert macro["streak"] >= 3
    assert macro["reverse"] is True
    assert macro["prefer_zone"] == PREFER_COLD


def test_analyze_macro_zone_cold_streak_prefers_hot():
    history = [9, 8, 7, 1, 2, 3, 4, 5, 6, 13, 14]
    macro = analyze_macro_zone(history, limit=6)
    assert macro["zone"] == MACRO_COLD
    assert macro["streak"] >= 3
    assert macro["reverse"] is True
    assert macro["prefer_zone"] == PREFER_HOT


def test_analyze_macro_zone_follow_continues_current_board():
    hot_follow = analyze_macro_zone([1, 2, 3, 13, 14, 6, 5, 4, 7, 8], limit=6)
    assert hot_follow["zone"] == MACRO_HOT
    assert hot_follow["streak"] == 2
    assert hot_follow["reverse"] is False
    assert hot_follow["prefer_zone"] == PREFER_HOT

    cold_follow = analyze_macro_zone([9, 8, 1, 2, 3, 4, 5, 6, 13, 14], limit=6)
    assert cold_follow["zone"] == MACRO_COLD
    assert cold_follow["streak"] == 2
    assert cold_follow["reverse"] is False
    assert cold_follow["prefer_zone"] == PREFER_COLD


def test_pick_prev_guard_slot_prefers_suitable_same_zodiac():
    """有合适遗漏同肖号 → 防重肖；否则 → 防重号原号。"""
    days = {n: 5 for n in range(1, 50)}
    days[34] = 0
    days[10] = 35  # 合适
    days[22] = 90
    days[46] = 120
    number, kind = pick_prev_guard_slot(
        latest=34, blocked=set(), days_since_last=days
    )
    assert kind == GUARD_KIND_REPEAT_ZODIAC
    assert number == 10

    days2 = {n: 5 for n in range(1, 50)}
    days2[34] = 0
    days2[10] = 3
    days2[22] = 2
    days2[46] = 1
    number2, kind2 = pick_prev_guard_slot(
        latest=34, blocked=set(), days_since_last=days2
    )
    assert kind2 == GUARD_KIND_REPEAT_NUMBER
    assert number2 == 34


def test_apply_reverse_game_blocks_preview_and_ultra_cold_from_tip():
    latest = 20
    picks = [
        {"number": n, "diff": abs(n - 20), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (21, 22, 23, 24, 25, 26, 27, 28, 29)
    ]
    days = {n: 40 for n in range(1, 50)}
    days[21] = 1
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
    tip = set(out["summary"]["tip_numbers"])
    assert 22 not in tip and 23 not in tip
    guard = out["summary"]["repeat_guard_number"]
    assert guard in out["summary"]["defense_numbers"]
    for pick in out["picks"]:
        if int(pick["amount"]) == 10:
            assert pick.get("reverse_tag") not in (TAG_PREVIEW, TAG_ULTRA_COLD)
        if int(pick["number"]) == guard:
            assert int(pick["amount"]) == 3


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
    assert out["summary"]["zodiac_streak"] == 3
    for number in out["summary"]["tip_numbers"]:
        assert zodiac_group(number) != hot


def test_apply_reverse_game_prev_guard_xor_one_slot():
    """有且仅 1 个上期名额：原号与同肖号不可兼买；只进 3 元。"""
    history = [34, 5, 8, 1, 2, 3, 4, 6, 7, 9]
    days = {n: 40 for n in range(1, 50)}
    days[34] = 0
    days[10] = 35  # 合适同肖 → 应选防重肖，不要原号
    days[22] = 50
    days[46] = 120
    picks = [
        {"number": n, "diff": abs(n - 34), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (10, 22, 34, 46, 12, 14, 16, 18, 26)
    ]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=34,
        previous=5,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=[],
        small_max=10,
        normal_max=30,
    )
    prev_g = zodiac_group(34)
    tip = out["summary"]["tip_numbers"]
    mid = out["summary"]["mid_numbers"]
    defense = out["summary"]["defense_numbers"]
    all_nums = tip + mid + defense
    prev_nums = [n for n in all_nums if zodiac_group(n) == prev_g]
    assert len(prev_nums) == 1
    guard = out["summary"]["repeat_guard_number"]
    assert prev_nums == [guard]
    assert guard == 10
    assert out["summary"]["prev_guard_kind"] == GUARD_KIND_REPEAT_ZODIAC
    assert 34 not in all_nums  # 原号与同肖号不可兼买
    assert guard in defense
    assert guard not in tip and guard not in mid
    for n in tip + mid:
        assert zodiac_group(n) != prev_g
    guard_pick = next(p for p in out["picks"] if int(p["number"]) == guard)
    assert int(guard_pick["amount"]) == 3


def test_apply_reverse_game_prev_guard_falls_back_to_latest():
    """无合适同肖遗漏 → 只留原号防重号。"""
    history = [34, 5, 8, 1, 2, 3, 4, 6, 7, 9]
    days = {n: 3 for n in range(1, 50)}
    days[34] = 0
    days[10] = 2
    days[22] = 1
    days[46] = 1
    picks = [
        {"number": n, "diff": abs(n - 34), "wave_type": "small", "amount": 3, "role": "defense"}
        for n in (10, 22, 34, 46, 12, 14, 16, 18, 26)
    ]
    out = apply_reverse_game(
        picks,
        enabled=True,
        mode=MODE_LADDER,
        latest=34,
        previous=5,
        history_numbers=history,
        days_since_last=days,
        in_band_numbers=[],
        small_max=10,
        normal_max=30,
    )
    assert out["summary"]["prev_guard_kind"] == GUARD_KIND_REPEAT_NUMBER
    assert out["summary"]["repeat_guard_number"] == 34
    assert 34 in out["summary"]["defense_numbers"]
    assert out["summary"]["prev_zodiac_count"] == 1


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

    assert "reverse_game_enabled" in DEFAULT_SETTINGS
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
    assert "macro_zone" in on["reverse_game"]
    assert "prefer_label" in on["reverse_game"]
    assert on["reverse_game"].get("prev_zodiac_count", 0) <= 1
    amounts = [int(p["amount"]) for p in on["picks"]]
    assert amounts.count(10) == 2
    assert amounts.count(5) == 3
    assert amounts.count(3) == 4
    assert any("逆向博弈叠加已开启" in note for note in on["notes"])
