from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from services.lottery import (
    AMOUNT_UNIT_MAX,
    AMOUNT_UNIT_MIN,
    AVOID_COLD_DAYS_MAX,
    AVOID_COLD_DAYS_MIN,
    MODE_PATTERN,
    NUMBER_MAX,
    NUMBER_MIN,
    ODDS_MAX,
    ODDS_MIN,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    PICK_STRATEGY_PATTERN,
    TOTAL_AMOUNT_MAX,
    TOTAL_AMOUNT_MIN,
    TREND_BIAS_PATTERN,
    TREND_WINDOW_MAX,
    TREND_WINDOW_MIN,
)


class SettingsOut(BaseModel):
    small_max: int
    normal_max: int
    # 预算真值：所有模式都从它取预算
    total_amount: int
    # 金额最小单位（注码粒度：所有模式下各注金额必须是它的正整数倍）
    amount_unit: int
    # 只读派生（deprecated）：最大投注换单位后按注数均分再 × unit，不是输入真值
    bet_unit: int
    mode: str
    pick_count: int
    # 特码兑付倍数（用户设定；默认 47）
    odds: float
    # 避开重肖：True=候选池排除最新同肖；默认 False
    exclude_repeat_zodiac: bool
    # 近期走势加权：英文枚举 neutral|hot|cold|mid
    trend_bias: str
    # 近窗期数；0=全部样本
    trend_window: int
    # 避冷权重：True=距上次出现超过 avoid_cold_days 天的号权重递减、金额递减；默认 True
    avoid_cold_enabled: bool
    # 避冷阈值（自然日）：距上次出现 ≤ 该值不惩罚；默认 60，范围 1..999
    avoid_cold_days: int
    # 选号策略：wave_round|score_top
    pick_strategy: str
    score_w_focus: float
    score_w_mid: float
    score_w_omit: float
    score_w_diff: float
    # 只读派生字段：恒等于 normal_max + 1，不接受外部写入
    big_min: int


class SettingsPatch(BaseModel):
    small_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    normal_max: int | None = Field(default=None, ge=0, le=NUMBER_MAX)
    total_amount: int | None = Field(
        default=None, ge=TOTAL_AMOUNT_MIN, le=TOTAL_AMOUNT_MAX
    )
    amount_unit: int | None = Field(
        default=None, ge=AMOUNT_UNIT_MIN, le=AMOUNT_UNIT_MAX
    )
    # 已降级为派生值：这里**仍然接收**（向后兼容老调用方，避免 422），但不会生效/落库
    bet_unit: int | None = Field(
        default=None, ge=1, le=10000, description="只读派生值，接收但忽略"
    )
    pick_count: int | None = Field(
        default=None, ge=PICK_COUNT_MIN, le=PICK_COUNT_MAX
    )
    odds: float | None = Field(
        default=None,
        ge=ODDS_MIN,
        le=ODDS_MAX,
        description="特码兑付倍数（用户设定，默认 47）",
    )
    mode: str | None = Field(default=None, pattern=MODE_PATTERN)
    exclude_repeat_zodiac: bool | None = Field(
        default=None, description="True=避开最新一期同肖；默认 False"
    )
    trend_bias: str | None = Field(default=None, pattern=TREND_BIAS_PATTERN)
    trend_window: int | None = Field(
        default=None, ge=TREND_WINDOW_MIN, le=TREND_WINDOW_MAX
    )
    avoid_cold_enabled: bool | None = Field(
        default=None, description="True=开启避冷权重（距上次出现越久金额越低）；默认 True"
    )
    avoid_cold_days: int | None = Field(
        default=None,
        ge=AVOID_COLD_DAYS_MIN,
        le=AVOID_COLD_DAYS_MAX,
        description="避冷阈值（自然日），默认 60",
    )
    pick_strategy: str | None = Field(
        default=None,
        pattern=PICK_STRATEGY_PATTERN,
        description="wave_round=波动轮取；score_top=打分Top-N对照Δ",
    )
    score_w_focus: float | None = Field(default=None, ge=-5, le=5)
    score_w_mid: float | None = Field(default=None, ge=-5, le=5)
    score_w_omit: float | None = Field(default=None, ge=-5, le=5)
    score_w_diff: float | None = Field(default=None, ge=-5, le=5)
    # 注意：不声明 big_min，PUT 时请求体里的 big_min 会被 Pydantic 忽略


class HistoryRecordOut(BaseModel):
    """历史序列中的一期（数据源 = 开奖总表 draws）。

    兼容旧前端：``number`` 仍是字段名，值取 ``draws.special_number``；
    另附上 ``draw_date`` / ``period`` 便于展示真实期号与日期。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    number: int
    created_at: datetime
    draw_date: date | None = None
    period: int | None = None
    wave_type: str | None = None
    wave_label: str | None = None
    diff: int | None = None


class RecommendRequest(BaseModel):
    mode: str | None = Field(default=None, pattern=MODE_PATTERN)
    number: int | None = Field(
        default=None, ge=NUMBER_MIN, le=NUMBER_MAX, description="临时指定最新号，不落库"
    )
    # 临场预览用的覆盖项（不落库）：留空则用当前用户的配置
    total_amount: int | None = Field(
        default=None, ge=TOTAL_AMOUNT_MIN, le=TOTAL_AMOUNT_MAX
    )
    amount_unit: int | None = Field(
        default=None, ge=AMOUNT_UNIT_MIN, le=AMOUNT_UNIT_MAX
    )
    # 临场注数（不落库）：波浪买入法页直接改注数；落库默认仍走 settings.pick_count
    bet_count: int | None = Field(
        default=None,
        ge=PICK_COUNT_MIN,
        le=PICK_COUNT_MAX,
        description="临场注数覆盖；写入 settings.pick_count 仅本请求生效",
    )
    # 随机分配的重掷种子：不传时由「期」+ 参数确定性派生（同期重算结果一致）
    amount_seed: int | str | None = Field(
        default=None, description="random 模式的重掷种子（小整数 / 字符串）"
    )
    # 走势加权临场预览（不落库）
    trend_bias: str | None = Field(default=None, pattern=TREND_BIAS_PATTERN)
    trend_window: int | None = Field(
        default=None, ge=TREND_WINDOW_MIN, le=TREND_WINDOW_MAX
    )
