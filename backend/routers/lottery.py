"""推荐 / 历史 / 设置 / 导入导出 API。

**单一事实来源**：特码只存在 ``draws`` 一张表里。

- 历史序列、推荐上下文一律读 ``draws``（最新在前）；
- ``records``（快捷录入草稿表）已废弃：快捷录入请用 ``POST /api/draws/quick``，
  它直接写进 ``draws``；
- 导出 / 导入只搬 ``draws``（version 2），不再有 ``records`` 键。

权限（见 ``dependencies``，受 ``AUTH_ENFORCED`` 灰度开关控制）：

| 接口 | 要求 |
|------|------|
| ``GET /api/history`` | **匿名可读**（波动阈值用全局默认；已登录则用本人设置） |
| ``GET`` / ``PUT /api/settings`` | 任意已审批用户（**按用户隔离**，见下） |
| ``POST /api/recommend``（财富密码）、``GET /api/export`` | **VIP 或 ADMIN** |
| ``POST /api/import`` | **仅 ADMIN**（会整表替换开奖数据，属破坏性写入） |

``settings`` 按用户隔离：登录用户读写的是自己的那一行；未登录（灰度期匿名）
落到 ``user_id = 0`` 的全局模板，行为与改造前完全一致。
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import (
    actor_user_id,
    current_user_optional,
    require_actor,
    require_admin,
    require_vip,
)
from models.lottery import (
    HistoryRecordOut,
    RecommendRequest,
    SettingsOut,
    SettingsPatch,
)
from repository import get_store
from services.lottery import (
    NUMBER_MAX,
    NUMBER_MIN,
    WAVE_LABELS,
    SettingsValidationError,
    classify_wave,
    lattice_band_hit_stats,
    recommend,
    replay_predict_wave_band,
    with_derived_settings,
)
from services.pnl import apply_settlement, build_pending_round

router = APIRouter(prefix="/api", tags=["lottery"])


@router.get("/lattice/replay")
async def get_lattice_replay(
    period: int | None = Query(
        default=None, ge=1, description="要回放的已开奖期号；与 offset 二选一"
    ),
    offset: int = Query(
        default=1,
        ge=1,
        le=500,
        description="最新在前的下标偏移：1=上一期（最近已开奖那一期）",
    ),
    actor=Depends(current_user_optional),
):
    """回放某一期「当时」的预测波动带（严格无未来函数）。

    默认 ``offset=1``：回放最近已开奖那一期 —— 也就是「昨天 / 上一期」当时看到的预测带，
    并用该期实开特码标出是否落在带内。匿名可读。
    """
    store = await get_store()
    draws = await store.list_draws()  # 最新在前
    if len(draws) < 3:
        raise HTTPException(
            status_code=400,
            detail="本池至少要 3 期开奖记录才能回放预测带（目标期 + 之前至少两期）",
        )
    settings = await store.get_settings(actor_user_id(actor))
    specials = [int(draw["special_number"]) for draw in draws]
    if period is not None:
        target_index = next(
            (
                index
                for index, draw in enumerate(draws)
                if int(draw["period"]) == int(period)
            ),
            None,
        )
        if target_index is None:
            raise HTTPException(status_code=404, detail="找不到该期开奖记录")
    else:
        target_index = int(offset) - 1
        if target_index >= len(draws):
            raise HTTPException(status_code=400, detail="offset 超出本池期数")

    replay = replay_predict_wave_band(
        specials,
        target_index=target_index,
        window=int(settings.get("lattice_window") or 0),
        small_max=int(settings["small_max"]),
        normal_max=int(settings["normal_max"]),
    )
    target = draws[target_index]
    anchor_draw = draws[target_index + 1] if target_index + 1 < len(draws) else None
    if replay is None:
        return {
            "ok": False,
            "code": "INSUFFICIENT",
            "message": "目标期之前不足两期，无法估出当时的预测带",
            "target_period": int(target["period"]),
            "target_date": target.get("draw_date"),
            "target_index": target_index,
            "anchor_period": int(anchor_draw["period"]) if anchor_draw else None,
            "replay": None,
        }
    return {
        "ok": True,
        "code": "OK",
        "message": "已回放目标期之前的预测带",
        "target_period": int(target["period"]),
        "target_date": target.get("draw_date"),
        "target_index": target_index,
        "anchor_period": int(anchor_draw["period"]) if anchor_draw else None,
        "anchor_date": anchor_draw.get("draw_date") if anchor_draw else None,
        "replay": replay,
    }


@router.get("/lattice/hit-rate")
async def get_lattice_hit_rate(
    recent_limit: int = Query(
        default=20,
        ge=0,
        le=200,
        description="recent 列表最多返回多少期（最新在前）；0 = 不附明细",
    ),
    actor=Depends(current_user_optional),
):
    """统计本池「实开差值落在当时预测带」的命中率（walk-forward，无未来函数）。

    用当前用户的 ``lattice_window`` / 波动阈值；匿名取全局默认。
    录入 / 纠正 / 导入开奖后无需落库缓存——下次读取即按最新 ``draws`` 重算。
    """
    store = await get_store()
    draws = await store.list_draws()  # 最新在前
    settings = await store.get_settings(actor_user_id(actor))
    stats = lattice_band_hit_stats(
        [int(draw["special_number"]) for draw in draws],
        periods=[int(draw["period"]) for draw in draws],
        window=int(settings.get("lattice_window") or 0),
        small_max=int(settings["small_max"]),
        normal_max=int(settings["normal_max"]),
        recent_limit=recent_limit,
    )
    return {
        "ok": stats["data_status"] == "OK",
        "code": stats["data_status"],
        "message": (
            f"本池已评估 {stats['evaluated']} 期，带内命中 {stats['hits']} 期"
            if stats["evaluated"]
            else "本池至少要 3 期开奖记录才能统计预测带命中率"
        ),
        **stats,
    }


@router.get("/history", response_model=list[HistoryRecordOut])
async def list_history(actor=Depends(current_user_optional)):
    """全部历史开奖（最新在前），附带上期相对波动类型。

    数据源为开奖总表 ``draws``；``number`` 是 ``special_number`` 的兼容字段名。
    波动分类阈值：已登录取本人配置，匿名取全局默认。匿名可读。
    """
    store = await get_store()
    draws = await store.list_draws()  # 最新在前
    settings = await store.get_settings(actor_user_id(actor))
    history: list[dict[str, Any]] = []
    for index, draw in enumerate(draws):
        # 相邻的「上一期」是更早的一条（列表中的下一条）
        older = draws[index + 1] if index + 1 < len(draws) else None
        if older is None:
            wave_type = wave_label = diff = None
        else:
            diff = abs(draw["special_number"] - older["special_number"])
            wave_type = classify_wave(
                diff, settings["small_max"], settings["normal_max"]
            )
            wave_label = WAVE_LABELS[wave_type]
        history.append(
            {
                "id": draw["id"],
                "number": draw["special_number"],
                "created_at": draw["created_at"],
                "draw_date": draw["draw_date"],
                "period": draw["period"],
                "wave_type": wave_type,
                "wave_label": wave_label,
                "diff": diff,
            }
        )
    return history


@router.get("/settings", response_model=SettingsOut)
async def get_settings(actor=Depends(require_actor)):
    """读取**当前用户**的配置（匿名 / 灰度期读全局模板）。"""
    store = await get_store()
    return with_derived_settings(await store.get_settings(actor_user_id(actor)))


@router.put("/settings", response_model=SettingsOut)
async def update_settings(payload: SettingsPatch, actor=Depends(require_actor)):
    """更新**当前用户自己的**配置，不影响其他用户与全局模板。

    预算 / 注码 / 注数不自洽（如 ``amount_unit > total_amount``）时返回 422，
    ``detail`` 为 ``"<REASON_CODE>: <中文说明>"``；脏配置不会落库。
    """
    store = await get_store()
    # SettingsPatch 未声明 big_min，model_dump() 不会带出该字段
    try:
        saved = await store.update_settings(
            payload.model_dump(), actor_user_id(actor)
        )
    except SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return with_derived_settings(saved)


@router.post("/recommend")
async def post_recommend(
    payload: RecommendRequest | None = None, actor=Depends(require_vip)
):
    """生成一期推荐（DeepSeek AI 驱动）。

    数据来源：draws 表最新几期开奖 → 组装成 AI 输入 → 调 DeepSeek API → 返回候选号码。

    输入给 AI 的数据：
    - 最新一期特码
    - 近几期开奖号码（最新在前）
    - 最新 6 肖（按最近 6 期开奖的生肖，去重）
    - 极寒号（>80 天未出现的号码 + 遗漏天数）
    - 完整号码池（1-49）

    返回结构与原本地引擎尽量兼容，前端无需大改。
    """
    from datetime import timedelta, date as date_cls

    from services.deepseek import call_deepseek
    from services.mark_six import (
        zodiac_of,
        zodiac_numbers,
        zodiac_label,
        zodiac_table,
        known_zodiac_years,
        ZODIAC_LABELS,
    )
    from services.lottery import (
        classify_wave,
        WAVE_LABELS,
        ROLE_LABELS,
        MODE_LABELS,
        build_copy_text,
    )

    payload = payload or RecommendRequest()
    store = await get_store()

    draws = await store.list_draws()  # 最新在前
    specials = [draw["special_number"] for draw in draws]

    if payload.number is not None:
        latest = payload.number
        previous = specials[0] if specials else None
    else:
        if not specials:
            raise HTTPException(
                status_code=400,
                detail="暂无开奖记录，请先导入开奖总表，或在开奖总表页快捷录入最新一期",
            )
        latest = specials[0]
        previous = specials[1] if len(specials) > 1 else None

    settings = await store.get_settings(actor_user_id(actor))
    for key in ("total_amount", "amount_unit", "trend_bias", "trend_window", "stake_top"):
        value = getattr(payload, key)
        if value is not None:
            settings[key] = value
    if payload.bet_count is not None:
        settings["pick_count"] = payload.bet_count

    # --- 组装 AI 输入数据 ---

    # 参照日 = 最新一期开奖日（用于生肖计算）
    ref_date = None
    ref_draw_date = None
    if draws:
        raw = draws[0].get("draw_date")
        if raw is not None:
            ref_draw_date = raw if isinstance(raw, date_cls) else date_cls.fromisoformat(str(raw))

    # 近几期特码（最新在前，不含最新一期本身）
    # 正常口径：latest = specials[0]，所以从 specials[1:] 起才算「上期及更早」；
    # 假设号码预览（payload.number）：latest 是假设值，真实序列 specials[0] 就是它的上期。
    if payload.number is not None:
        history_tail = specials
    else:
        history_tail = specials[1:]
    previous_numbers = history_tail[:5]

    # 最新 6 肖：从「上期及更早」的开奖里取不重复的生肖
    # 关键：排除最新一期本身（draws[0]）。否则上期生肖永远落在 6 肖里，
    # 阵营判定（上期是否属于 6 肖）就永远为「是」，规则失效。
    zodiac_6_recent: list[dict] = []
    if ref_draw_date is not None and known_zodiac_years():
        seen_animals: set[str] = set()
        for d in draws[1:13]:  # 跳过最新一期，最多再看 12 期
            num = int(d["special_number"])
            dd = d.get("draw_date")
            if dd is None:
                continue
            draw_date = dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd))
            animal = zodiac_of(num, draw_date)
            if animal and animal not in seen_animals:
                seen_animals.add(animal)
                zodiac_6_recent.append(
                    {
                        "animal": zodiac_label(animal),
                        "code": animal,
                        "numbers": zodiac_numbers(num, draw_date),
                        "trigger_number": num,
                    }
                )
            if len(zodiac_6_recent) >= 6:
                break

    # 极寒号：距上次出现 > 80 天
    extreme_cold: list[dict] = []
    COLD_THRESHOLD_DAYS = 80
    if ref_draw_date is not None:
        # 用 draw_date 数组计算每个号码上次出现日期
        last_seen: dict[int, date_cls] = {}
        for d in draws:
            num = int(d["special_number"])
            dd = d.get("draw_date")
            if dd is None:
                continue
            draw_date = dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd))
            if num not in last_seen:
                last_seen[num] = draw_date

        for num in range(1, 50):
            last = last_seen.get(num)
            if last is None:
                # 从未出现过 → 按样本内最长间隔算
                # 取最早一期的日期作为"上次出现"
                oldest = None
                for d in reversed(draws):
                    dd = d.get("draw_date")
                    if dd is None:
                        continue
                    oldest = dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd))
                    break
                if oldest is None:
                    continue
                days = (ref_draw_date - oldest).days + 1
            else:
                days = (ref_draw_date - last).days

            if days > COLD_THRESHOLD_DAYS:
                extreme_cold.append({"number": num, "days": days})

    # 全量 49 号遗漏天数字典（供 AI 唯一性排序算 |d-25| 用）
    missing_days: dict[int, int] = {}
    if ref_draw_date is not None:
        for num in range(1, 50):
            last = last_seen.get(num)
            if last is None:
                oldest = None
                for d in reversed(draws):
                    dd = d.get("draw_date")
                    if dd is None:
                        continue
                    oldest = dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd))
                    break
                days = (ref_draw_date - oldest).days + 1 if oldest is not None else 999
            else:
                days = (ref_draw_date - last).days
            missing_days[num] = max(0, days)

    # 完整号码池
    number_pool = list(range(1, 50))

    # --- 校验数据（供铁律硬校验用） ---
    # 全年 49 号生肖映射（中文标签）；无农历年数据时为空，校验器跳过生肖规则
    zodiac_map: dict[int, str] = {}
    if ref_draw_date is not None and known_zodiac_years():
        for num in range(1, 50):
            animal = zodiac_of(num, ref_draw_date)
            if animal:
                zodiac_map[num] = zodiac_label(animal)

    # 极寒号集合（>80天）
    extreme_cold_numbers = {int(item["number"]) for item in extreme_cold}

    # 刚破冷号集合：最近 3 期内开出、且距上次开出 >80 天的号（极易回冷，禁主攻）
    recent_cold_break: set[int] = set()
    if draws:
        ordered: list[tuple[int, date_cls]] = []
        for d in draws:
            dd = d.get("draw_date")
            if dd is None:
                continue
            ordered.append(
                (int(d["special_number"]),
                 dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd)))
            )
        last_seen_before: dict[int, date_cls] = {}
        for i, (num, dt) in enumerate(ordered):
            if i < 3:
                prior = last_seen_before.get(num)
                if prior is not None and (dt - prior).days > COLD_THRESHOLD_DAYS:
                    recent_cold_break.add(num)
            if num not in last_seen_before:
                last_seen_before[num] = dt

    # --- 盘面统计指标（后端算好传给 AI，避免 AI 自己推导出错）---
    # 核心改进：直接算好定盘法则的布尔触发条件，AI 只需要读"是/否"
    streak_stats: dict[str, Any] = {}
    z6_set: set[str] = set()  # 最新6肖中文标签集合（提至外层供 reason 生成用）
    if draws and zodiac_map:
        z_seq = [zodiac_map.get(int(d["special_number"]), "?") for d in draws[:12]]
        streak_stats["zodiac_sequence"] = " → ".join(z_seq)
        # 各生肖最新连开期数
        streaks: list[str] = []
        i = 0
        while i < len(z_seq):
            z = z_seq[i]
            cnt = 0
            j = i
            while j < len(z_seq) and z_seq[j] == z:
                cnt += 1
                j += 1
            streaks.append(f"{z}连开{cnt}期")
            i = j
        streak_stats["zodiac_streaks"] = "，".join(streaks)

        # --- 定盘法则直接触发条件（布尔值，AI 用这个不要自己算）---
        z6_set = {item.get("animal") for item in zodiac_6_recent if item.get("animal")}
        if z6_set:
            # 近3期生肖是否全部落在最新6肖里（能量透支杀热切冷触发条件1）
            recent3_in_z6 = all(z in z6_set for z in z_seq[:3])
            streak_stats["近3期全部在最新6肖"] = "是" if recent3_in_z6 else "否"
            # 某热肖是否连开≥2期（能量透支杀热切冷触发条件2）
            has_hot_streak2 = any(
                z in z6_set and cnt >= 2
                for z, cnt in zip(
                    [z_seq[0]] if z_seq else [],
                    [sum(1 for z in z_seq if z == z_seq[0])] if z_seq else [0],
                )
            )
            # 更精确：扫描每个生肖的最新连开
            has_hot_streak2 = False
            i = 0
            while i < len(z_seq):
                z = z_seq[i]
                cnt = 0
                j = i
                while j < len(z_seq) and z_seq[j] == z:
                    cnt += 1
                    j += 1
                if z in z6_set and cnt >= 2:
                    has_hot_streak2 = True
                    break
                i = j
            streak_stats["最新6肖中是否有生肖连开≥2期"] = "是" if has_hot_streak2 else "否"
            # 能量透支杀热切冷是否触发（近3期全在z6 或 z6中某肖连开≥2）
            kill_hot_triggered = recent3_in_z6 or has_hot_streak2
            streak_stats["能量透支杀热切冷是否触发"] = "是" if kill_hot_triggered else "否"
            streak_stats["能量透支杀热切冷触发原因"] = (
                f"近3期全部在最新6肖" if recent3_in_z6 else
                f"最新6肖中有生肖连开≥2期" if has_hot_streak2 else
                "未触发"
            )

            # 能量透支杀冷切热（法则4）：近3期全在6肖外，或6肖外某生肖连开≥2期
            recent3_out_z6 = all(z not in z6_set for z in z_seq[:3])
            streak_stats["近3期全部非最新6肖"] = "是" if recent3_out_z6 else "否"
            has_cold_streak2 = False
            i = 0
            while i < len(z_seq):
                z = z_seq[i]
                cnt = 0
                j = i
                while j < len(z_seq) and z_seq[j] == z:
                    cnt += 1
                    j += 1
                if z not in z6_set and cnt >= 2:
                    has_cold_streak2 = True
                    break
                i = j
            streak_stats["6肖外是否有生肖连开≥2期"] = "是" if has_cold_streak2 else "否"
            kill_cold_triggered = recent3_out_z6 or has_cold_streak2
            streak_stats["能量透支杀冷切热是否触发"] = "是" if kill_cold_triggered else "否"
            streak_stats["能量透支杀冷切热触发原因"] = (
                "近3期全部非最新6肖" if recent3_out_z6 else
                "6肖外有生肖连开≥2期" if has_cold_streak2 else
                "未触发"
            )

            # 最新6肖大盘连续命中期数（仅算近5期，不要无限往回数）
            z6_hits_recent = sum(1 for z in z_seq[:5] if z in z6_set)
            streak_stats["近5期最新6肖命中数"] = f"{z6_hits_recent}/5"

        # 极寒号连续开出期数（定盘法则1触发条件）
        # 口径修正：用"开出时属于刚破冰极寒号"(recent_cold_break) 判断，
        # 不能用 extreme_cold_numbers —— 刚开出的号遗漏必然很小，永远不算极寒。
        cold_hits = 0
        for d in draws[:6]:
            num = int(d["special_number"])
            if num in recent_cold_break:
                cold_hits += 1
            else:
                break
        streak_stats["极寒号连续开出期数"] = cold_hits
        streak_stats["极寒连三变盘是否触发"] = "是" if cold_hits >= 3 else "否"
        # 上期是否极寒号（极寒破冰回补触发条件）：上期 = 最新一期(latest)
        prev_is_cold = bool(latest and int(latest) in recent_cold_break)
        streak_stats["上期是否极寒号"] = "是" if prev_is_cold else "否"
        streak_stats["极寒破冰回补是否触发"] = "是" if prev_is_cold else "否"

    # --- 阵营绝对过滤（最高优先级）：先算大盘方向 ---
    # 「上期」= 最新一期开奖（预测目标的上一期），即 latest
    # 顺热期（上期生肖∈最新6肖）→ 全部注强制从最新6肖对应号码里选
    # 顺冷期（上期生肖∉最新6肖）→ 全部注强制从非最新6肖号码里选
    camp_direction = "heat"  # 默认顺热
    if z6_set and zodiac_map:
        latest_zodiac = zodiac_map.get(int(latest), "")
        if latest_zodiac and latest_zodiac not in z6_set:
            camp_direction = "cold"  # 上期跳出6肖 → 顺冷期

    # --- 主攻阵营 / 排序基准（变盘反向 & 顺热连庄特权）---
    kb_hit = streak_stats.get("能量透支杀热切冷是否触发", "否") == "是"
    kc_hit = streak_stats.get("能量透支杀冷切热是否触发", "否") == "是"
    cb_hit = streak_stats.get("极寒连三变盘是否触发", "否") == "是"
    pb_hit = streak_stats.get("极寒破冰回补是否触发", "否") == "是"
    kb_reason_txt = streak_stats.get("能量透支杀热切冷触发原因", "")
    kc_reason_txt = streak_stats.get("能量透支杀冷切热触发原因", "")
    cold_hits_n = int(streak_stats.get("极寒号连续开出期数", 0) or 0)

    # 顺热连庄特权（法则5）：上期已回到6肖，且上上期是刚破冰的极寒号
    lianzhuang = (camp_direction == "heat") and bool(previous in recent_cold_break)
    last_zodiac_name = zodiac_map.get(int(latest), "") if zodiac_map else ""

    # 主攻阵营：极寒类法则优先；否则按变盘方向反向；连庄即顺热
    primary_camp: str | None = None
    if cb_hit or pb_hit or lianzhuang:
        primary_camp = None
    elif kb_hit:
        primary_camp = "cold"   # 杀热切冷：主攻切非6肖
    elif kc_hit:
        primary_camp = "heat"   # 杀冷切热：主攻切6肖

    sort_base = 5 if lianzhuang else 25
    primary_zodiac_required = last_zodiac_name if (lianzhuang and last_zodiac_name) else None

    if lianzhuang:
        streak_stats["主攻阵营要求"] = (
            f"最新6肖，且必须是上期生肖「{last_zodiac_name}」、遗漏1-7天的极热号（顺热连庄特权）"
        )
    elif primary_camp == "cold":
        streak_stats["主攻阵营要求"] = f"非最新6肖（变盘反向：杀热切冷 {kb_reason_txt}）"
    elif primary_camp == "heat":
        streak_stats["主攻阵营要求"] = f"最新6肖（变盘反向：杀冷切热 {kc_reason_txt}）"
    else:
        streak_stats["主攻阵营要求"] = (
            "非最新6肖" if camp_direction == "cold" else "最新6肖"
        )
    streak_stats["唯一解排序基准"] = (
        "距5天最近优先（连庄特权）" if lianzhuang else "距25天最近优先"
    )

    # --- 深水号统计（补丁2：同肖深水防雷） ---
    deep_water = {n: d for n, d in missing_days.items() if d > 50}
    deep_mid = [n for n, d in deep_water.items() if d <= 80]  # 50-80 天非极寒深水
    streak_stats["深水号个数（遗漏>50天）"] = len(deep_water)
    streak_stats["深水号清单（50-80天）"] = sorted(deep_mid)

    # --- 调 DeepSeek（含铁律硬校验 + 违规重试） ---
    try:
        ai_result = await call_deepseek(
            latest_number=int(latest),
            previous_numbers=[int(n) for n in previous_numbers],
            zodiac_6_recent=zodiac_6_recent,
            extreme_cold=extreme_cold,
            number_pool=number_pool,
            total_amount=int(settings.get("total_amount", 50)),
            amount_unit=int(settings.get("amount_unit", 5)),
            zodiac_map=zodiac_map,
            extreme_cold_numbers=extreme_cold_numbers,
            recent_cold_break=recent_cold_break,
            previous_number=int(previous) if previous is not None else None,
            missing_days=missing_days,
            streak_stats=streak_stats or None,
            z6_set=z6_set,
            camp_direction=camp_direction,
            primary_camp=primary_camp,
            sort_base=sort_base,
            primary_zodiac_required=primary_zodiac_required,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    ai_picks = ai_result.get("picks", [])
    if not ai_picks:
        raise HTTPException(status_code=502, detail="DeepSeek 未返回有效 picks")

    market_judgment = ai_result.get("market_judgment", "")
    ai_attempts = int(ai_result.get("attempts", 1))
    ai_violations = list(ai_result.get("violations", []))

    # --- 补齐前端需要的元信息 ---
    small_max = int(settings["small_max"])
    normal_max = int(settings["normal_max"])
    latest_zodiac: list[int] = []
    latest_zodiac_code: str | None = None
    latest_zodiac_label: str | None = None
    zodiac_date_str: str | None = None
    zodiac_year_val: int | None = None

    if ref_draw_date is not None and known_zodiac_years():
        animal = zodiac_of(int(latest), ref_draw_date)
        if animal:
            latest_zodiac = zodiac_numbers(int(latest), ref_draw_date)
            latest_zodiac_code = animal
            latest_zodiac_label = zodiac_label(animal)
        zodiac_date_str = ref_draw_date.isoformat()
        # 推农历年
        lunar_year = None
        for item in known_zodiac_years():
            starts = item.get("starts_on")
            if starts is None:
                continue
            starts_date = starts if isinstance(starts, date_cls) else date_cls.fromisoformat(str(starts))
            if ref_draw_date >= starts_date:
                lunar_year = item.get("lunar_year")
        zodiac_year_val = lunar_year

    prev_wave_block: dict | None = None
    if previous is not None:
        prev_diff = abs(int(latest) - int(previous))
        prev_type = classify_wave(prev_diff, small_max, normal_max)
        prev_wave_block = {
            "number": int(previous),
            "diff": prev_diff,
            "type": prev_type,
            "label": WAVE_LABELS[prev_type],
        }

    picks_out: list[dict] = []
    for p in ai_picks:
        num = int(p["number"])
        diff = abs(num - int(latest))
        wave_type = classify_wave(diff, small_max, normal_max)
        role = str(p.get("role", "defense"))

        z_code: str | None = None
        z_label: str | None = None
        if ref_draw_date is not None and known_zodiac_years():
            animal = zodiac_of(num, ref_draw_date)
            if animal:
                z_code = animal
                z_label = zodiac_label(animal)

        is_repeat_zodiac = bool(
            latest_zodiac_code and z_code and z_code == latest_zodiac_code
        )

        picks_out.append(
            {
                "number": num,
                "diff": diff,
                "wave_type": wave_type,
                "wave_label": WAVE_LABELS[wave_type],
                "role": role,
                "role_label": ROLE_LABELS.get(role, role),
                "amount": int(p.get("amount", 0)),
                "is_repeat_zodiac": is_repeat_zodiac,
                "zodiac": z_code,
                "zodiac_label": z_label,
                "reason": str(p.get("reason", "")),
            }
        )

    # copy_text
    copy_text = build_copy_text(str(settings.get("mode", "ladder")), picks_out)

    # --- 代码自动生成逻辑简述（覆盖 AI 可能编造的数字）---
    # AI 经常编造"连开10期"这种数字，用 streak_stats 的布尔值精确生成
    primary_num = picks_out[0]["number"] if picks_out else 0
    primary_z = picks_out[0].get("zodiac_label", "") if picks_out else ""
    kb = streak_stats.get("能量透支杀热切冷是否触发", "否")
    kb_reason = streak_stats.get("能量透支杀热切冷触发原因", "")
    cb = streak_stats.get("极寒连三变盘是否触发", "否")
    pb = streak_stats.get("极寒破冰回补是否触发", "否")

    # 阵营方向（最高优先级）
    camp_label = "顺热期" if camp_direction == "heat" else "顺冷期"
    primary_in_z6 = bool(primary_z and primary_z in z6_set)

    # logic_brief：阵营优先 + 定盘法则补充
    if cb == "是":
        logic_brief = f"{camp_label}·极寒连三变盘，主攻切回温号{primary_num}（{primary_z}）"
    elif pb == "是":
        logic_brief = f"{camp_label}·极寒破冰回补，主攻选上期极寒号同肖温号{primary_num}（{primary_z}）"
    elif kb == "是":
        if camp_direction == "heat":
            # 顺热期 + energy 杀热切冷 → 阵营优先不许跳非6肖，只能在6肖内选
            logic_brief = f"{camp_label}·盘面过热（{kb_reason}），阵营铁律优先，主攻在最新6肖内选温号{primary_num}（{primary_z}）"
        else:
            logic_brief = f"{camp_label}·6肖有生肖连坐（{kb_reason}），主攻选非6肖温号{primary_num}（{primary_z}）"
    else:
        if camp_direction == "heat":
            logic_brief = f"{camp_label}·主攻顺应最新6肖活跃温号{primary_num}（{primary_z}）"
        else:
            logic_brief = f"{camp_label}·主攻选非最新6肖温号{primary_num}（{primary_z}）"

    # --- 代码硬生成每个 pick 的 reason（彻底覆盖 AI 编造的数字）---
    # 先把 AI 原始理由留存为 ai_reason，供复盘对照
    for _p in picks_out:
        _p["ai_reason"] = _p.get("reason", "")
    primary_z_str = picks_out[0].get("zodiac_label", "") if picks_out else ""
    primary_days = missing_days.get(primary_num, 0) if picks_out else 0
    primary_is_cold = primary_num in extreme_cold_numbers
    last_zodiac_cn = zodiac_map.get(int(latest), "") if zodiac_map else ""

    def _gap_desc(days: int) -> str:
        """距 25 天黄金基准的差值，用于解释唯一解排序依据。"""
        gap = abs(days - 25)
        return "正好踩中25天基准" if gap == 0 else f"距25天基准{gap}天"

    # 主攻 reason：完整逆向博弈逻辑（数字全部来自真实数据，AI 原值另存 ai_reason）
    z6_desc = "最新6肖" if primary_in_z6 else "非最新6肖"
    _pg = f"遗漏{primary_days}天·{_gap_desc(primary_days)}"
    _base_txt = "距5天基准" if lianzhuang else "距25天基准"
    _reversed = primary_camp is not None and primary_camp != camp_direction
    if lianzhuang:
        _p_reason = (
            f"顺热连庄特权·上期生肖{last_zodiac_name}·主攻取近期极热同肖（遗漏{primary_days}天），"
            f"唯一解按{_base_txt}排序，主攻成立"
        )
    elif cb_hit:
        _p_reason = (
            f"极寒连三变盘（极寒号连开{cold_hits_n}期）·切回{z6_desc}温号（{_pg}），"
            f"三档中{_base_txt}最近，主攻成立"
        )
    elif pb_hit:
        _p_reason = (
            f"极寒破冰回补·{z6_desc}同肖温号（{_pg}），三档中{_base_txt}最近，主攻成立"
        )
    elif kb_hit:
        _tag = "主攻反向切" if _reversed else "主攻守在"
        _p_reason = (
            f"{camp_label}·能量透支杀热切冷（{kb_reason_txt}）·{_tag}{z6_desc}温号（{_pg}），"
            f"三档中{_base_txt}最近，主攻成立"
        )
    elif kc_hit:
        _tag = "主攻反向切" if _reversed else "主攻守在"
        _p_reason = (
            f"{camp_label}·能量透支杀冷切热（{kc_reason_txt}）·{_tag}{z6_desc}温号（{_pg}），"
            f"三档中{_base_txt}最近，主攻成立"
        )
    else:
        _p_reason = (
            f"{camp_label}·{z6_desc}活跃温号（{_pg}），三档中{_base_txt}最近，主攻成立"
        )
    picks_out[0]["reason"] = _p_reason

    # 次选 & 防守 reason：完整逆向博弈逻辑，数字全部来自真实统计
    primary_z_code = picks_out[0].get("zodiac", "") if picks_out else ""
    # 生肖成员表，用于解释"同肖双保险"
    _z_members: dict[str, list[int]] = {}
    _role_by_num: dict[int, str] = {}
    for _q in picks_out:
        _role_by_num[_q["number"]] = _q.get("role", "")
        _zq = _q.get("zodiac_label", "")
        if _zq:
            _z_members.setdefault(_zq, []).append(_q["number"])

    def _sibling_desc(num: int, z: str) -> str:
        """若同档位里有同肖的另一注，返回双保险说明；否则空串。

        深水防雷：当前号遗漏>50天时标注深水位，否则普通温位。
        """
        others = [x for x in _z_members.get(z, []) if x != num]
        if not others or not z:
            return ""
        ref = others[0]
        _ref_role = {"primary": "主攻", "secondary": "次选", "defense": "防守"}.get(
            _role_by_num.get(ref, ""), "另一注"
        )
        _my_days = missing_days.get(num, 0)
        if _my_days > 50:
            return f"深水双保险·与{_ref_role}{ref:02d}同属{z}（本注深水防雷）"
        return f"同肖双保险·与{_ref_role}{ref:02d}同属{z}"

    for _i, _p in enumerate(picks_out[1:], start=1):
        _n = _p["number"]
        _z = _p.get("zodiac_label", "")
        _role = _p.get("role", "")
        _days = missing_days.get(_n, 0)
        _is_cold = _n in extreme_cold_numbers
        _in_z6 = bool(_z and _z in z6_set)
        _is_horse = bool(_z and last_zodiac_cn and _z == last_zodiac_cn)
        _side = _sibling_desc(_n, _z)
        _z6_txt = "最新6肖" if _in_z6 else "非6肖"
        _gap_txt = _gap_desc(_days)

        if _is_cold:
            _p["reason"] = (
                f"极寒兜底·遗漏{_days}天（超80天），只配5元防爆冷"
                if _role == "defense"
                else f"极寒防守位·遗漏{_days}天（超80天）"
            )
        elif _is_horse:
            _p["reason"] = (
                f"防回马枪·上期开{int(latest):02d}({_z})，从{_z}窝取号防同肖回马（遗漏{_days}天）"
            )
        elif _side:
            if "深水双保险" in _side:
                _p["reason"] = (
                    f"{_side}（遗漏{_days}天，>50天深水区），"
                    f"庄家避开温号挖深水时仍可中双份"
                )
            else:
                _p["reason"] = f"{_side}（遗漏{_days}天·{_gap_txt}），该生肖连爆可中双份"
        elif _role == "secondary":
            _p["reason"] = f"{_z6_txt}浅温号·遗漏{_days}天（{_gap_txt}），为主攻打掩护"
        else:
            _p["reason"] = f"{_z6_txt}中温号·遗漏{_days}天，防守补位"

    total_amount = sum(p["amount"] for p in picks_out)

    # --- 本地引擎算点阵 / 走势分布等纯统计展示字段 ---
    # AI 负责选号；本地引擎的 lattice / trend_distributions / wave_breakdown
    # 是纯统计（predict_wave_band / build_number_lattice 等不涉及选号），
    # 直接调一次 recommend() 丢弃它的 picks，只取统计字段。
    local_lattice: dict | None = None
    local_trend_dist: list | None = None
    local_wave_breakdown: dict | None = None
    local_wave_alloc: dict | None = None
    local_number_freq: dict | None = None
    local_missing_waves: list | None = None
    try:
        from services.lottery import (
            recommend as local_recommend,
            MODES,
            MODE_SINGLE,
            MODE_LADDER,
            MODE_RANDOM,
        )
        # 本地引擎用当前用户 settings，但强制 mode=single 让它选 9 注（点阵需要足够多号码）
        # 实际 picks 会被 AI 的替换，只取统计字段
        local_mode = str(settings.get("mode") or MODE_LADDER)
        if local_mode not in (MODE_SINGLE, MODE_LADDER, MODE_RANDOM):
            local_mode = MODE_LADDER
        # 构建 history_numbers（最新在前）+ history_dates
        local_history = [int(d["special_number"]) for d in draws]
        local_history_dates = []
        for d in draws:
            dd = d.get("draw_date")
            local_history_dates.append(
                dd if isinstance(dd, date_cls) else date_cls.fromisoformat(str(dd))
            )
        local_outcome = local_recommend(
            latest=int(latest),
            previous=int(previous) if previous is not None else None,
            history_numbers=local_history,
            settings=settings,
            mode=local_mode,
            period=str(draws[0].get("period") or ""),
            history_dates=local_history_dates,
        )
        local_lattice = local_outcome.get("lattice")
        local_trend_dist = local_outcome.get("trend_distributions")
        local_wave_breakdown = local_outcome.get("wave_breakdown")
        local_wave_alloc = local_outcome.get("wave_alloc")
        local_number_freq = local_outcome.get("number_frequency")
        local_missing_waves = local_outcome.get("missing_waves")
    except Exception as exc:
        import traceback as _tb
        err_msg = f"[LOCAL_ENGINE_FAIL] {exc}\n{_tb.format_exc()}"
        with open("d:/myproject/wave-money/backend/_local_engine_err.txt", "a", encoding="utf-8") as _f:
            _f.write(err_msg + "\n---\n")

    result: dict = {
        "status": "ok",
        "source": "deepseek",
        "latest": int(latest),
        "latest_zodiac": latest_zodiac,
        "latest_zodiac_code": latest_zodiac_code,
        "latest_zodiac_label": latest_zodiac_label,
        "zodiac_date": zodiac_date_str,
        "zodiac_year": zodiac_year_val,
        "previous": int(previous) if previous is not None else None,
        "prev_wave": prev_wave_block,
        "zodiac_6_recent": zodiac_6_recent,
        "extreme_cold": extreme_cold,
        "mode": str(settings.get("mode", "ladder")),
        "mode_label": MODE_LABELS.get(str(settings.get("mode", "ladder")), "档位"),
        "picks": picks_out,
        "copy_text": copy_text,
        "staked_total": total_amount,
        "settings": settings,
        "market_judgment": market_judgment,
        "logic_brief": logic_brief,
        "ai_attempts": ai_attempts,
        "ai_violations": ai_violations,
        # 本地引擎纯统计字段（点阵 / 走势分布 / 波动分解）
        "lattice": local_lattice,
        "trend_distributions": local_trend_dist,
        "wave_breakdown": local_wave_breakdown,
        "wave_alloc": local_wave_alloc,
        "number_frequency": local_number_freq,
        "missing_waves": local_missing_waves,
        "notes": [
            "本期推荐由 DeepSeek AI 生成，不构成任何中奖保证。"
            "每个号码等概率，单期命中概率 = 选号数 / 49。",
            f"资金阵型：1档 20元×1 + 2档 10元×2 + 3档 5元×3，共 6 注 {total_amount} 元。",
        ],
    }

    # snapshot 写入（recommend_rounds）—— AI 推荐也走 PnL 追踪
    if payload.number is None and draws and picks_out:
        from services.pnl import apply_settlement, build_pending_round

        target_period = await store.suggest_next_period()
        user_id = actor_user_id(actor)
        uid = 0 if user_id is None else int(user_id)
        round_row = build_pending_round(
            user_id=uid,
            period=target_period,
            base_period=int(draws[0]["period"]),
            picks=picks_out,
            odds=float(settings.get("odds", 47)),
            mode=str(settings.get("mode", "ladder")),
        )
        already = next(
            (d for d in draws if int(d["period"]) == int(target_period)), None
        )
        if already is not None:
            round_row = apply_settlement(
                round_row,
                int(already["special_number"]),
                already.get("draw_date"),
            )
        saved = await store.upsert_recommend_round(round_row)
        result["adopted_round"] = {
            "id": saved.get("id"),
            "period": saved.get("period"),
            "status": saved.get("status"),
            "stake_mode": saved.get("stake_mode"),
            "cost": saved.get("cost"),
            "odds": saved.get("odds"),
            "hit": saved.get("hit"),
            "payout": saved.get("payout"),
            "profit": saved.get("profit") if saved.get("status") == "SETTLED" else None,
        }

    return result


@router.get("/export")
async def export_data(actor=Depends(require_vip)):
    """导出 JSON。口径：开奖总表 + **当前用户**的配置。"""
    store = await get_store()
    data = await store.export_data(actor_user_id(actor))
    data["storage"] = store.backend
    return data


@router.post("/import")
async def import_data(
    payload: dict,
    replace: bool = Query(default=True),
    admin=Depends(require_admin),
):
    """导入导出文件（version 2，只含 ``draws`` + ``settings``）。

    旧的 ``records`` 键不再接受：它只有号码与写入时间，无法可靠还原开奖日期，
    强行迁移会污染唯一事实来源。需要迁移旧数据请跑
    ``backend/scripts/migrate_records_to_draws.py``。

    文件里的 ``settings`` 写入**发起导入的管理员自己**的配置，不覆盖全局模板。
    """
    store = await get_store()
    draws = payload.get("draws")
    if draws is None or not isinstance(draws, list):
        raise HTTPException(
            status_code=422,
            detail="JSON 缺少 draws 数组（导出文件 version 2 格式）",
        )

    for item in draws:
        try:
            special_number = int(item["special_number"])
            int(item["period"])
            date.fromisoformat(str(item["draw_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"非法开奖记录: {item!r}"
            ) from exc
        if not NUMBER_MIN <= special_number <= NUMBER_MAX:
            raise HTTPException(
                status_code=422, detail=f"特码越界: {special_number}"
            )

    return await store.import_data(
        payload, replace=replace, user_id=actor_user_id(admin)
    )
