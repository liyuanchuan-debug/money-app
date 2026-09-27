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
    classify_wave,
    recommend,
    with_derived_settings,
)
from services.pnl import apply_settlement, build_pending_round

router = APIRouter(prefix="/api", tags=["lottery"])


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
    """更新**当前用户自己的**配置，不影响其他用户与全局模板。"""
    store = await get_store()
    # SettingsPatch 未声明 big_min，model_dump() 不会带出该字段
    return with_derived_settings(
        await store.update_settings(payload.model_dump(), actor_user_id(actor))
    )


@router.post("/recommend")
async def post_recommend(
    payload: RecommendRequest | None = None, actor=Depends(require_vip)
):
    """生成一期推荐。历史序列的唯一来源是开奖总表 ``draws``（最新在前）。

    两种取数口径：

    - 未指定 ``number``：``latest`` = 最新一期特码，``previous`` = 上一期特码，
      ``history`` = 全部真实特码（最新在前）。
      ``recommend()`` 只把 ``history_numbers`` 当频次/遗漏的计数器用（顺序无关），
      并会剔除 ``latest`` 本身，所以「最新在前」与「时间正序」等价。
    - 指定 ``number``（前瞻观察：如果下一期开 N 会怎样）：把 N 当作**假设的
      最新一期**叠在真实序列之上，因此 ``previous`` = 当前最新一期特码
      （它才是 N 之前的一期），``history`` = ``[N, *真实特码]``。
      早期版本这里错误地读了几乎为空的 ``records``，等于用空序列算遗漏。
    """
    payload = payload or RecommendRequest()
    store = await get_store()

    draws = await store.list_draws()  # 最新在前
    specials = [draw["special_number"] for draw in draws]

    if payload.number is not None:
        latest = payload.number
        # N 叠在真实序列最前面 → 它之前的一期就是当前最新一期
        previous = specials[0] if specials else None
        history = [latest, *specials]
    else:
        if not specials:
            raise HTTPException(
                status_code=400,
                detail="暂无开奖记录，请先导入开奖总表，或在开奖总表页快捷录入最新一期",
            )
        latest = specials[0]
        previous = specials[1] if len(specials) > 1 else None
        history = specials

    settings = await store.get_settings(actor_user_id(actor))
    # 请求级覆盖（不落库）：让调用方能在不保存设置的前提下试算别的预算 / 走势加权 / 注数
    for key in ("total_amount", "amount_unit", "trend_bias", "trend_window"):
        value = getattr(payload, key)
        if value is not None:
            settings[key] = value
    # bet_count → 本请求的 pick_count（不落库；设置页默认值仍走 PUT /api/settings）
    if payload.bet_count is not None:
        settings["pick_count"] = payload.bet_count

    # 期号用于派生随机分配种子：同一份最新开奖 → 同一份分配（可复现）
    period = draws[0]["period"] if draws else None

    # 与 history 等长、最新在前的开奖日（自然日差口径）
    if payload.number is not None:
        # 假设最新无真实开奖日：用当前最新开奖日作基准叠在前面
        ref_date = draws[0]["draw_date"] if draws else date.today()
        history_dates = [ref_date, *[draw["draw_date"] for draw in draws]]
    else:
        history_dates = [draw["draw_date"] for draw in draws]

    result = recommend(
        latest=latest,
        previous=previous,
        history_numbers=history,
        settings=settings,
        mode=payload.mode,
        amount_seed=payload.amount_seed,
        period=period,
        history_dates=history_dates,
    )

    # 真实推荐（非「假设号码」预览）自动采用：按目标期 upsert 快照。
    # 目标期 = max(period)+1；开奖入库后按同 period 回填命中。
    if payload.number is None and draws:
        target_period = await store.suggest_next_period()
        user_id = actor_user_id(actor)
        # 匿名灰度期 actor_user_id 可能为 None → 落到全局模板 0
        uid = 0 if user_id is None else int(user_id)
        round_row = build_pending_round(
            user_id=uid,
            period=target_period,
            base_period=period,
            picks=result.get("picks") or [],
            odds=settings.get("odds", 47),
            mode=result.get("mode") or settings.get("mode") or "",
        )
        # 若目标期已经开过奖（少见：导入赶在采用之后），立即结算
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
        result["notes"] = list(result.get("notes") or []) + [
            f"已按模拟买入采用为本期快照（目标第 {target_period} 期）；"
            "按生成号码与设定赔率试算，非真实投注记录。"
            "开奖入库后自动对奖。赔率是设定兑付倍数，不是收益承诺。"
        ]

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
