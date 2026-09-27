"""样本内统计分析 API（``/api/stats``）。

口径说明（**禁止**升格为全量 / 市场结论）：
- 所有接口只统计「本池已导入的 N 期数据」，返回体首字段即 ``scope`` 与 ``sample_size``；
- 样本为 0 或某分析样本过小时返回 ``data_status == "INSUFFICIENT"`` +
  ``data_status_label == "数据不足"``，绝不编造结论；
- 计算一律按 ``draw_date`` **升序**进行（本层负责把存储的「最新在前」翻转），
  所有接口都支持 ``?limit=N`` 只分析最近 N 期；
- 波动 / 推荐语义完全复用 ``services.lottery``，本层不改动任何算法。

权限（见 ``dependencies``，受 ``AUTH_ENFORCED`` 灰度开关控制）：
``/trend``、``/frequency``、``/zodiac-trend`` **匿名可读**（只读样本统计）；
``GET /pnl``、``POST /backtest`` 属个人能力，**仅 VIP 或 ADMIN**。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from dependencies import actor_user_id, current_user_optional, require_vip
from repository import get_store
from services.analytics import (
    DEFAULT_RECENT_WINDOW,
    backtest_stats,
    frequency_stats,
    trend_stats,
    zodiac_trend_stats,
)
from services.lottery import (
    MODES,
    NUMBER_MAX,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    clamp_settings,
)
from services.pnl import summarize_rounds

router = APIRouter(prefix="/api/stats", tags=["stats"])

# 单次分析的期数上限（本池当前 200 期，留足余量）
LIMIT_MAX = 1000


async def _draws_ascending(limit: int | None) -> list[dict[str, Any]]:
    """取最近 ``limit`` 期（``limit=None`` 取全部），并翻转为 draw_date 升序。"""
    store = await get_store()
    newest_first = await store.list_draws(limit)
    return list(reversed(newest_first))


@router.get("/frequency")
async def get_frequency(
    limit: int | None = Query(default=None, ge=1, le=LIMIT_MAX),
) -> dict:
    """号码 / 生肖的出现次数、出现率、期望值与偏差。公开只读。"""
    return frequency_stats(await _draws_ascending(limit))


@router.get("/trend")
async def get_trend(
    limit: int | None = Query(default=None, ge=1, le=LIMIT_MAX),
    small_max: int | None = Query(default=None, ge=0, le=NUMBER_MAX),
    normal_max: int | None = Query(default=None, ge=0, le=NUMBER_MAX),
    actor=Depends(current_user_optional),
) -> dict:
    """特码走势序列 + 相邻波动分类与分布（阈值可按参数覆盖，便于复现）。

    波动阈值取**当前登录用户**的配置（匿名取全局默认）。公开只读。
    """
    store = await get_store()
    # 注意：查询参数未给时**不能**写成 {**settings, "small_max": None}——
    # 字典展开会用 None 覆盖掉用户已存的阈值，而 clamp_settings 会跳过 None，
    # 最终回退到 DEFAULT_SETTINGS（历史 bug：本池已存的 normal_max 被忽略）。
    overrides = {
        key: value
        for key, value in {"small_max": small_max, "normal_max": normal_max}.items()
        if value is not None
    }
    cfg = clamp_settings(
        {**await store.get_settings(actor_user_id(actor)), **overrides}
    )
    return trend_stats(
        await _draws_ascending(limit), cfg["small_max"], cfg["normal_max"]
    )


@router.get("/zodiac-trend")
async def get_zodiac_trend(
    limit: int | None = Query(default=None, ge=1, le=LIMIT_MAX),
    recent: int = Query(
        default=DEFAULT_RECENT_WINDOW,
        ge=1,
        le=49,
        description="最近 N 期观察窗口（默认 12）",
    ),
) -> dict:
    """生肖序列 + 连出统计 + 最近窗口覆盖 / 轮转情况。公开只读。"""
    return zodiac_trend_stats(await _draws_ascending(limit), recent_window=recent)


@router.get("/pnl")
async def get_pnl(
    recent: int = Query(default=20, ge=1, le=100, description="近期明细条数"),
    actor=Depends(require_vip),
) -> dict:
    """收益仪表：模拟买入快照的累计成本 / 兑付 / 净盈亏与命中率。

    口径：只统计当前用户已采用的推荐快照（当前默认 stake_mode=SIMULATED）；
    按生成号码与设定赔率试算，非真实投注记录。命中率是已结算期的经验频率。
    赔率取自当前设置（展示用）；各期兑付仍按采用当时快照里的赔率计算。
    """
    store = await get_store()
    uid = actor_user_id(actor)
    settings = await store.get_settings(uid)
    rounds = await store.list_recommend_rounds(uid)
    return summarize_rounds(
        rounds,
        current_odds=settings.get("odds", 47),
        recent_limit=recent,
    )


class BacktestIn(BaseModel):
    """回测参数。全部可覆盖，保证同一组参数可复现同一次回测。"""

    mode: str | None = Field(default=None, pattern=f"^({'|'.join(MODES)})$")
    pick_count: int | None = Field(
        default=None, ge=PICK_COUNT_MIN, le=PICK_COUNT_MAX
    )
    small_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    normal_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    limit: int | None = Field(default=None, ge=1, le=LIMIT_MAX)


@router.post("/backtest")
async def post_backtest(
    payload: BacktestIn | None = None,
    limit: int | None = Query(default=None, ge=1, le=LIMIT_MAX),
    actor=Depends(require_vip),
) -> dict:
    """推荐引擎的走步回测（严格无未来函数）。

    ``limit`` 可来自请求体（优先）或查询参数。基线配置取**当前登录用户**的
    有效设置（``Store.get_settings``：全局模板 + 用户自己的行；未登录 / 灰度期
    即全局模板），请求体里显式给出的 ``mode`` / ``pick_count`` / ``small_max``
    / ``normal_max`` 再覆盖基线 —— 这样每个用户回测的就是自己正在用的策略。
    响应体的 ``settings`` 与 ``parameter_sources`` 记录实际生效值与来源，
    同一组参数仍可复现同一次回测。
    """
    payload = payload or BacktestIn()
    effective_limit = payload.limit if payload.limit is not None else limit
    store = await get_store()
    # 基线一律来自已解析的有效配置；None 由 backtest_stats 内部省略，
    # 不能在这里拼 {**settings, "small_max": None}（会抹掉用户已存阈值）。
    base_settings = await store.get_settings(actor_user_id(actor))
    return backtest_stats(
        await _draws_ascending(effective_limit),
        mode=payload.mode,
        pick_count=payload.pick_count,
        small_max=payload.small_max,
        normal_max=payload.normal_max,
        base_settings=base_settings,
    )
