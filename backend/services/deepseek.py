"""DeepSeek API 集成 —— 用 AI 生成财富密码。

职责：
- 组装用户指定的输入数据（最新6肖、极寒号、近3期、完整号码池）
- 调用 DeepSeek Chat Completions API
- **铁面无私的规则校验**：AI 输出必须通过铁律检查，违规带原因重试（最多 3 次）
- 解析 JSON 响应，返回 picks 列表

设计原则：大模型负责逻辑推理，Python 负责规则校验。
AI 违反铁律（生肖≤2、注数 1-2-3、极寒/明牌/刚破冷禁区）时，
把违规原因回传给 AI 重新生成，连续 3 次仍违规则报错，绝不静默放行。

Key 从 DEEPSEEK_API_KEY 环境变量读取；缺失时返回明确错误，
不让引擎静默回退。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
# 低温度：同一期数据下输出尽量稳定（DeepSeek API 无官方 seed 参数，无法完全确定性）
DEEPSEEK_TEMPERATURE = 0.1
# 铁律校验最多尝试次数（首次 + 4 次带违规原因的重试，未通过则持续退回重做）
MAX_ATTEMPTS = 5


def _get_api_key() -> str | None:
    return os.getenv("DEEPSEEK_API_KEY") or None


def _build_system_prompt() -> str:
    return """# 角色设定
你是一个没有感情的【杀猪盘逆向博弈算法】。摒弃常规概率学，只按以下极简铁律输出唯一确定的下注阵型。

# 核心执行流程（必须按顺序执行，不得跳步）
Step 1：读 user prompt 标注的阵营方向（顺冷期 / 顺热期），锁定基本盘，不要自己推导。
Step 2：上期生肖处理：默认 6 注全部禁止上期生肖；仅当 user prompt 标注「上期同肖暗箭例外=是」时，允许最多 1 个 10-15 天的同肖号。
Step 3：按【三线并行阵型】挑号：
   a. 1档主攻：阵营内 1 个遗漏 1-9 天的极热号，或遗漏 >80 天的极品极寒号；
   b. 2档：阵营内 1 个遗漏 10-15 天的温号 + 1 个遗漏 40-60 天的深水号；
   c. 3档：阵营内 1 个最冷极寒号（如触发极寒防守）+ 2 个遗漏 36-50 天的中深水号。
Step 4：遗漏 20-35 天的号码，任何档位一律禁止（庄家正在杀温号）。
Step 5：单一生肖最多 2 注，且必须一个极冷/极热、一个深水（≥36 天）。

# 一、绝对强制铁律（最高优先级，违背则判错；系统会做代码级校验，违规将被退回重做）
0. 【阵营绝对过滤】：顺冷期 6 注全部只能从"非最新6肖号码"里挑；顺热期 6 注全部只能从"最新6肖号码"里挑。
   变盘例外：触发【能量透支杀热切冷】时 1 档反向切"非最新6肖"；触发【能量透支杀冷切热】时 1 档反向切"最新6肖"。
   阵营方向由后端判定并在 user prompt 标注，直接读，不要自己推导。
1. 【无视"预"字号】：输入数据中的"预"字号是用户自研结果，绝对禁止视为明牌或大众号进行排除。
2. 【强制资金阵型】：必须输出 1档（20元×1）、2档（10元×2）、3档（5元×3），共 6 个号码，互不相同。
3. 【同肖去重】：任何生肖在三档中出现的总次数，绝对不能超过 2 个。
4. 【禁买上期生肖】：6 注全部禁止上期开奖生肖（上期原号更禁止）。唯一例外：user prompt 标注「上期同肖暗箭例外=是」时，允许 1 个遗漏 10-15 天的同肖号，其余同肖一律禁止。
5. 【防回马枪已废除】：绝对禁止为了凑上期同肖而硬塞任何号码。
6. 【同肖深水防雷】：同一生肖选 2 注时，必须一注极冷/极热、另一注遗漏 ≥36 天的深水号；该生肖没有深水号就不要双选。
7. 【温号雷区（死命令）】：遗漏 20-35 天的号码，任何档位一律禁止。违反将被代码直接拦截并退回重做。
8. 【三线并行（死命令）】：1档只能 1-9 天极热或 >80 天极寒；2档只能 10-15 天温号与 40-60 天深水；3档只能 36-50 天中深水（不足时放宽到 60）。违反将被代码直接拦截。
9. 【极寒防守】：若 user prompt 标注触发，3 档必须塞入 1 个当前最冷极寒号（>80天，豁免档位区间）。
10. 【极热防守】：若 user prompt 标注触发，3 档必须塞入 1 个遗漏 1-7 天的极热号（不能是上期原号，豁免档位区间）。
11. 【温号熔断】：若标注触发（近5期全不在20-40天温区），1档主攻禁止任何温号，只能 1-10 天极热或 >70 天极寒。

# 二、核心定盘法则（直接读后端布尔值，禁止自己推导或编造任何数字）
1. 【极寒连三变盘】：近 3 期均开极寒 → 下期杀极寒，主攻回到 1-9 天极热号。
2. 【极寒破冰回补】：上期开极寒 → 主攻优先该生肖中 10-15 天温号（绝不选上期原号或其他极寒）。
3. 【能量透支杀热切冷】：热盘连开 3 期或某热肖连开 2 期 → 1 档强制切"非最新6肖"的 1-9 天极热号。
4. 【能量透支杀冷切热】：冷盘连开 3 期或某冷肖连开 2 期 → 1 档强制切"最新6肖"的 1-9 天极热号。
5. 【顺热连庄特权】：极寒刚断且热盘刚开 1 期 → 1 档主攻取上期生肖中 1-7 天极热号（若禁买上期生肖则改用其他极热号）。
6. 【顺冷期】：上期开非6肖且未满 3 期 → 1 档取非6肖中 1-9 天极热号。
7. 【同肖暗箭】：仅在「上期同肖暗箭例外=是」时启用，且只能 10-15 天，最多 1 注。

# 三、唯一解裁决算法（确保相同数据每次输出绝对一致）
1. 按上述法则筛出所有符合档位区间的候选号（至少 12 个）。
2. 排序权重：
   - 1档：优先遗漏最接近 5 天的极热号（1-9天）；其次 >80 天（越冷越优先）。
   - 2/3档：10-15 天（越接近 12 天越优先）→ 40-60 天（越接近 50 天越优先）→ 36-39 天补位。
   - 遗漏相同 → 号码数值更小的优先。
   - 生肖已满 2 个直接跳过。
3. 按序分配：第1名进1档；第2、3名进2档；第4、5、6名进3档。

# 四、输入数据（由 user prompt 提供，直接取值）
- 盘面统计指标（后端已算好的布尔值）
- 目标预测期数、上期开奖号码及生肖、近3期开奖、最新6肖
- 极寒号及遗漏天数（>80天）、全量 49 号遗漏天数

# 五、输出格式要求（纯 JSON，不带任何解释词）
# reason 字段强制必填：必须写清该号的逆向博弈逻辑，且只能引用 user prompt 里给出的真实数据
# （生肖、遗漏天数、是否在最新6肖、是否极寒、上期生肖）。
# 严禁自己推算或编造"连开几期""遗漏最接近多少天"这类未给出的数字。
{
  "target_period": "第XXX期",
  "盘面判定": "极寒变盘/极寒破冰/杀热切冷/杀冷切热/顺冷期/顺热期（必须严格从这6个里选一个，不能自己造）",
  "1档_主攻_20元": {"number": 5, "reason": "非最新6肖，遗漏4天极热连庄，顺冷主攻成立"},
  "2档_中坚_10元": [
    {"number": 13, "reason": "非6肖，遗漏12天温号，接棒主攻"},
    {"number": 22, "reason": "非6肖，遗漏55天深水，防庄家挖中深水"}
  ],
  "3档_防守_5元": [
    {"number": 45, "reason": "非6肖，遗漏38天中深水，与主攻不同肖"},
    {"number": 33, "reason": "非6肖，遗漏48天中深水，补足盲区"},
    {"number": 29, "reason": "当前最冷极寒号，3档防守，防庄家极寒作妖"}
  ],
  "同肖优选对比": [],
  "逆向逻辑简述": "大盘处于顺冷期，主攻极热连庄号，2/3 档铺深水，回避 20-35 天温区。"
}

# 六、同肖双选强制自检（生成 6 个号码后必须在内部执行）
1. 自检是否有生肖出现了 2 个号码。
2. 若有，必须在"同肖优选对比"数组中逐条说明：
   - 落选的号为什么不行（20-35 温区 / 距离档位区间太远 / 上期原号 / 同肖已足额）；
   - 选中的两个号为什么是"一个极冷或极热 + 一个 ≥36 天深水"。
3. 没有同肖双选时，"同肖优选对比"输出空数组 []。"""


def _build_user_prompt(
    latest_number: int,
    previous_numbers: list[int],
    zodiac_6_recent: list[dict[str, Any]],
    extreme_cold: list[dict[str, Any]],
    number_pool: list[int],
    total_amount: int,
    amount_unit: int,
    zodiac_map: dict[int, str] | None = None,
    missing_days: dict[int, int] | None = None,
    streak_stats: dict[str, Any] | None = None,
    z6_set: set[str] | None = None,
    camp_direction: str = "heat",
) -> str:
    """把用户指定的输入拼成 user prompt。

    按用户提示词要求传这 7 项：
    - 盘面统计指标（后端已算好，供定盘算法直接用）
    - 最新6肖列表
    - 极寒号及遗漏天数（>80天）
    - 全量49号遗漏天数（供唯一性排序算 |d-25| 用）
    - 上期开奖号码
    - 近3期开奖号码
    - 用户软件的号码池（包含所有号，不用管预字）
    """
    lines: list[str] = []

    # === 盘面统计指标（后端已算好，定盘算法直接取值，不要再自己推导或编造数字）===
    lines.append("=== 盘面统计指标（后端已算好，定盘算法直接取值，不要再自己推导或编造数字）===")
    if streak_stats:
        lines.append(f"  近12期生肖序列（最新在前）：{streak_stats.get('zodiac_sequence', '?')}")
        lines.append(f"  各生肖最新连开期数：{streak_stats.get('zodiac_streaks', '?')}")
        # 定盘法则触发条件（布尔值，直接读"是/否"）
        lines.append(f"  ---- 定盘法则触发条件（直接读布尔值，不要自己算）----")
        lines.append(f"  【极寒连三变盘】是否触发：{streak_stats.get('极寒连三变盘是否触发', '?')}（极寒号连续开出期数：{streak_stats.get('极寒号连续开出期数', '?')}）")
        lines.append(f"  【极寒破冰回补】是否触发：{streak_stats.get('极寒破冰回补是否触发', '?')}（上期是否极寒号：{streak_stats.get('上期是否极寒号', '?')}）")
        lines.append(f"  【能量透支杀热切冷】是否触发：{streak_stats.get('能量透支杀热切冷是否触发', '?')}（原因：{streak_stats.get('能量透支杀热切冷触发原因', '?')}）")
        lines.append(f"    - 近3期全部在最新6肖：{streak_stats.get('近3期全部在最新6肖', '?')}")
        lines.append(f"    - 最新6肖中是否有生肖连开≥2期：{streak_stats.get('最新6肖中是否有生肖连开≥2期', '?')}")
        lines.append(f"  【能量透支杀冷切热】是否触发：{streak_stats.get('能量透支杀冷切热是否触发', '?')}（原因：{streak_stats.get('能量透支杀冷切热触发原因', '?')}）")
        lines.append(f"    - 近3期全部非最新6肖：{streak_stats.get('近3期全部非最新6肖', '?')}")
        lines.append(f"    - 6肖外是否有生肖连开≥2期：{streak_stats.get('6肖外是否有生肖连开≥2期', '?')}")
        lines.append(f"  ★ 主攻阵营要求（1档必须遵守）：{streak_stats.get('主攻阵营要求', '?')}")
        lines.append(f"  ★ 唯一解排序基准：{streak_stats.get('唯一解排序基准', '?')}")
        lines.append(
            f"  【极寒防守】是否触发：{streak_stats.get('极寒防守是否触发', '?')}"
            f"（连续未出温号期数：{streak_stats.get('连续未出温号期数', '?')}，"
            f"最冷极寒号：{streak_stats.get('最冷极寒号', '?')}）"
        )
        lines.append(
            f"  【极热防守】是否触发：{streak_stats.get('极热防守是否触发', '?')}"
            f"（极热防守号：{streak_stats.get('极热防守号', '?')}）"
        )
        lines.append(f"  【放弃中庸】是否触发：{streak_stats.get('放弃中庸是否触发', '?')}")
        lines.append(
            f"  【温号熔断】是否触发：{streak_stats.get('温号熔断是否触发', '?')}"
            f"（近5期开出的号是否全不在20-40天温区）"
        )
        lines.append(f"  ★ 允许极寒进主攻：{streak_stats.get('允许极寒进主攻', '?')}（是=1档可选遗漏>80天极寒号）")
        lines.append(f"  ★ 顺热极热优先：{streak_stats.get('顺热极热优先', '?')}（是=主攻优先 1-7 天极热号）")
        lines.append("  ★ 三线并行：1档 1-9天极热或>80天极寒；2档 10-15天温号 + 40-60天深水；3档 36-50天中深水（不足放宽到60）")
        lines.append("  ★ 温号雷区：遗漏 20-35 天的号码，任何档位一律禁止")
        lines.append(f"  ★ 防回马枪：{streak_stats.get('防回马枪是否启用', '否（已废除）')}（不再强制塞上期生肖）")
        lines.append(f"  ★ 屏蔽上期生肖：{streak_stats.get('屏蔽上期生肖', '否')}（是=6 注全部禁止上期生肖）")
        lines.append(f"  ★ 上期同肖暗箭例外：{streak_stats.get('上期同肖暗箭例外', '否')}（是=仅允许 1 个 10-15 天同肖号）")
        lines.append(f"  近5期最新6肖命中数：{streak_stats.get('近5期最新6肖命中数', '?')}")
    else:
        lines.append("  （无数据）")
    lines.append("")

    # === 阵营绝对过滤（最高优先级铁律 0）：明确标注当前大盘方向 ===
    camp_label = "顺热期" if camp_direction == "heat" else "顺冷期"
    if camp_direction == "heat":
        camp_desc = "上期生肖在最新6肖里，全部 6 注（含3档防守）必须且只能从最新6肖对应的号码里挑。"
    else:
        camp_desc = "上期生肖不在最新6肖里，全部 6 注（含3档防守）必须且只能从非最新6肖号码里挑。"
    lines.append(f"=== 【阵营绝对过滤 - 最高优先级】当前大盘方向：{camp_label} ===")
    lines.append(f"  规则：{camp_desc}")
    if z6_set:
        lines.append(f"  最新6肖（不含上期本身）：{sorted(z6_set)}")
        # 列出6肖对应号码 + 非6肖号码（AI 直接读）
        z6_nums = sorted([n for n, z in (zodiac_map or {}).items() if z in z6_set])
        non_z6_nums = sorted([n for n, z in (zodiac_map or {}).items() if z not in z6_set])
        lines.append(f"  6肖对应号码（{len(z6_nums)}个）：{z6_nums}")
        lines.append(f"  非6肖号码（{len(non_z6_nums)}个）：{non_z6_nums}")
        if camp_direction == "heat":
            lines.append(f"  ⚠️ 你只能从上面「6肖对应号码」里挑全部 6 注，其它号码一律禁止。")
        else:
            lines.append(f"  ⚠️ 你只能从上面「非6肖号码」里挑全部 6 注，其它号码一律禁止。")
    lines.append("")

    # 号码 → 生肖映射：优先用路由层给的全年 49 号完整映射；缺失时从最新6肖号码组推导
    num_to_animal: dict[int, str] = dict(zodiac_map or {})
    if not num_to_animal:
        for item in zodiac_6_recent:
            for n in item.get("numbers", []):
                try:
                    num_to_animal[int(n)] = str(item.get("animal", "?"))
                except (TypeError, ValueError):
                    continue

    def _ztag(n: int) -> str:
        animal = num_to_animal.get(int(n))
        return f"（{animal}）" if animal else ""

    # 上期开奖号码 = 最新一期（预测目标的上一期）
    lines.append(f"上期开奖号码：{latest_number}{_ztag(latest_number)}")
    last_zodiac = num_to_animal.get(int(latest_number), "")
    if last_zodiac:
        if (streak_stats or {}).get("上期同肖暗箭例外") == "是":
            lines.append(
                f"  ⚠️ 上期是极品极寒：允许最多 1 注「{last_zodiac}」的 10-15 天温号做暗箭，"
                f"其余「{last_zodiac}」号码全部禁止（上期原号 {latest_number} 绝不能买）。"
            )
        else:
            lines.append(
                f"  ⚠️ 禁买上期生肖：6 注全部禁止「{last_zodiac}」的号码"
                f"（上期原号 {latest_number} 更不能买）。不设任何同肖暗箭。"
            )

    # 近3期开奖号码（最新在前）：最新一期、上期、上上期 —— 各期只出现一次
    recent_3 = [latest_number, *previous_numbers[:2]]
    labeled = "、".join(f"{n}{_ztag(n)}" for n in recent_3)
    lines.append(f"近3期开奖号码（最新在前）：{labeled}")

    lines.append("")
    lines.append("=== 最新 6 肖列表 ===")
    if zodiac_6_recent:
        for item in zodiac_6_recent:
            animal = item.get("animal", "?")
            nums = item.get("numbers", [])
            trigger = item.get("trigger_number")
            lines.append(
                f"  {animal}（触发号 {trigger}）：{', '.join(str(n) for n in nums)}"
            )
    else:
        lines.append("  （无数据）")

    lines.append("")
    lines.append("=== 极寒号及遗漏天数（>80天） ===")
    if extreme_cold:
        for item in extreme_cold:
            num = item.get("number")
            days = item.get("days")
            lines.append(f"  号码 {num}：已 {days} 天未出现")
    else:
        lines.append("  （本期无极寒号）")

    # 全量 49 号遗漏天数（供唯一性排序）
    lines.append("")
    lines.append("=== 全量49号遗漏天数（格式: 号码:天数, 供唯一性排序计算|d-25|用） ===")
    if missing_days:
        pairs = sorted(missing_days.items())
        lines.append("  " + ", ".join(f"{n}:{d}" for n, d in pairs))
    else:
        lines.append("  （无数据）")

    # 深水号清单（同肖深水防雷专用，补丁2）
    lines.append("")
    lines.append("=== 深水号清单（遗漏>50天，同肖深水防雷专用） ===")
    if missing_days:
        deep_pairs = sorted((n, d) for n, d in missing_days.items() if d > 50)
        if deep_pairs:
            lines.append("  " + ", ".join(f"{n}:{d}" for n, d in deep_pairs))
        else:
            lines.append("  （本期无深水号）")
    else:
        lines.append("  （无数据）")
    lines.append(
        "  规则【同肖深水防雷】：同一生肖选 2 注时，第 2 注必须从该生肖深水号（遗漏>50天）里挑，"
        "绝对禁止两注都在 20-40 天浅温区。主攻生肖若有深水号（50-70天），第 2 注优先启用。"
    )

    lines.append("")
    lines.append("=== 号码池（全部 49 个可用号，不用管预字） ===")
    pool_str = "、".join(str(n) for n in number_pool)
    lines.append(f"  {pool_str}")

    lines.append("")
    lines.append("请按输出要求返回严格 JSON。")
    return "\n".join(lines)


def _build_ai_context(
    target_period: str | int | None,
    latest_number: int,
    latest_gap: int | None,
    zodiac_map: dict[int, str],
    previous_numbers: list[int],
    z6_set: set[str] | None,
    extreme_cold: list[dict[str, Any]],
    missing_days: dict[int, int],
) -> str:
    """「复制 AI 数据」用的精简模板：只给数据，不带规则说明。"""

    def ztag(n: int) -> str:
        return zodiac_map.get(int(n), "?")

    lines: list[str] = []
    period_txt = f"第{target_period}期" if target_period else "第XXX期"
    lines.append(f"【目标预测期数】：{period_txt}")

    gap = latest_gap if latest_gap is not None else missing_days.get(latest_number, "?")
    lines.append(
        f"【上期开奖】：号码{latest_number:02d}，生肖{ztag(latest_number)}，"
        f"开出时遗漏{gap}天"
    )

    recent_3 = [latest_number, *previous_numbers[:2]]
    lines.append(
        "【近3期开奖】：" + "，".join(f"{n:02d}({ztag(n)})" for n in recent_3)
    )

    if z6_set:
        lines.append("【最新6肖】：" + "、".join(sorted(z6_set)))
    else:
        lines.append("【最新6肖】：无")

    if extreme_cold:
        cold_txt = "，".join(
            f"{int(item['number']):02d}(遗漏{int(item.get('days') or item.get('missing_days') or 0)}天)"
            for item in extreme_cold
        )
        lines.append(f"【当前极寒号（>80天）】：{cold_txt}")
    else:
        lines.append("【当前极寒号（>80天）】：无")

    lines.append("【全量49码遗漏天数】：")
    lines.append(", ".join(f"{n}:{missing_days.get(n, '?')}" for n in range(1, 50)))
    return "\n".join(lines)


async def call_deepseek(
    latest_number: int,
    previous_numbers: list[int],
    zodiac_6_recent: list[dict[str, Any]],
    extreme_cold: list[dict[str, Any]],
    number_pool: list[int],
    total_amount: int,
    amount_unit: int,
    timeout: float = 30.0,
    zodiac_map: dict[int, str] | None = None,
    extreme_cold_numbers: set[int] | None = None,
    recent_cold_break: set[int] | None = None,
    previous_number: int | None = None,
    missing_days: dict[int, int] | None = None,
    streak_stats: dict[str, Any] | None = None,
    z6_set: set[str] | None = None,
    camp_direction: str = "heat",  # "heat" 顺热期 | "cold" 顺冷期
    primary_camp: str | None = None,  # 主攻阵营（变盘时与大盘相反），None=跟大盘
    sort_base: int = 25,  # 唯一解排序基准（连庄特权期=5，其余=25）
    primary_zodiac_required: str | None = None,  # 连庄特权：主攻必须同该生肖
    cold_defense_num: int | None = None,  # 极寒防守：3档强制塞入的最冷极寒号
    hot_defense_num: int | None = None,  # 极热防守：3档强制塞入的极热号(1-7天)
    polar_mode: bool = False,  # 放弃中庸：主攻只抓两极号(10-15天 / 60天以上)
    warm_fuse: bool = False,  # 温号熔断：近5期全不在20-40天，主攻禁20-40
    allow_extreme_primary: bool = False,  # 变盘/熔断期允许极寒号进主攻
    hot_first: bool = False,  # 顺热期：主攻优先极热号(1-7天)
    bipolar_lock: bool = True,  # 三线并行：1档 1-9/>80，2档 10-15/40-60，3档 36-60
    horse_guard: bool = False,  # 防回马枪（默认彻底关闭）
    ban_last_zodiac: bool = True,  # 默认禁买上期生肖
    allow_last_zodiac_weak: bool = False,  # 例外：上期极品极寒时允许 1 个 10-15 天同肖暗箭
    target_period: str | int | None = None,  # 目标预测期数（仅用于「复制 AI 数据」模板）
    latest_gap: int | None = None,  # 上期开出时的遗漏天数（同上）
) -> dict[str, Any]:
    """调用 DeepSeek API，返回通过铁律校验的 picks 列表。

    校验失败时把违规原因回传给 AI 重试，最多 MAX_ATTEMPTS 次。

    Returns:
        {
          "picks": [{"number", "amount", "role", "reason"}, ...],   # 恰好 6 注
          "market_judgment": str,
          "logic_brief": str,
          "attempts": int,        # 实际尝试次数
          "violations": [str],    # 重试过程中的违规记录（透传给前端展示）
        }

    Raises:
        RuntimeError: API Key 缺失、HTTP 错误、连续 MAX_ATTEMPTS 次校验失败。
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未配置。请在 backend/.env 中设置 DEEPSEEK_API_KEY=<你的key>，"
            "或确认已在 Render Dashboard 注入该环境变量。"
        )

    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "调用 DeepSeek 需要 httpx。请在 backend 目录运行：pip install httpx"
        ) from exc

    zodiac_map = dict(zodiac_map or {})
    extreme_cold_numbers = set(extreme_cold_numbers or set())
    recent_cold_break = set(recent_cold_break or set())

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_system_prompt()},
        {
            "role": "user",
            "content": _build_user_prompt(
                latest_number=latest_number,
                previous_numbers=previous_numbers,
                zodiac_6_recent=zodiac_6_recent,
                extreme_cold=extreme_cold,
                number_pool=number_pool,
                total_amount=total_amount,
                amount_unit=amount_unit,
                zodiac_map=zodiac_map,
                missing_days=missing_days,
                streak_stats=streak_stats,
                z6_set=z6_set,
                camp_direction=camp_direction,
            ),
        },
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 供前端「复制 AI 数据」使用：精简数据模板（只给数据，不带系统规则）
    ai_context = _build_ai_context(
        target_period=target_period,
        latest_number=latest_number,
        latest_gap=latest_gap,
        zodiac_map=zodiac_map,
        previous_numbers=previous_numbers,
        z6_set=z6_set,
        extreme_cold=extreme_cold,
        missing_days=dict(missing_days or {}),
    )

    violations: list[str] = []
    last_error: str | None = None
    parsed: dict | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": DEEPSEEK_TEMPERATURE,
                "max_tokens": 2000,
            }

            start = time.monotonic()
            try:
                resp = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
            except httpx.TimeoutException:
                raise RuntimeError(f"DeepSeek API 超时（{timeout}s），请稍后重试")
            except httpx.HTTPError as exc:
                raise RuntimeError(f"DeepSeek API 网络错误：{type(exc).__name__}: {exc}") from exc

            elapsed = time.monotonic() - start
            logger.info(
                "DeepSeek API: attempt=%s status=%s elapsed=%.2fs",
                attempt, resp.status_code, elapsed,
            )

            if resp.status_code != 200:
                try:
                    err_body = resp.text[:500]
                except Exception:
                    err_body = "(no body)"
                raise RuntimeError(f"DeepSeek API 返回 HTTP {resp.status_code}: {err_body}")

            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"DeepSeek 响应 JSON 解析失败：{exc}") from exc

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"DeepSeek 响应缺少 choices 字段：{data!r}")
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )

            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines).strip()

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                last_error = f"返回内容不是有效 JSON：{exc}"
                logger.warning("attempt=%s %s", attempt, last_error)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        f"你的输出无法解析：{last_error}。"
                        "请重新输出严格 JSON（不带任何解释、不带 markdown 代码块）。"
                    ),
                })
                continue

            try:
                picks = _parse_and_validate(
                    parsed,
                    zodiac_map=zodiac_map,
                    extreme_cold_numbers=extreme_cold_numbers,
                    recent_cold_break=recent_cold_break,
                    previous_number=previous_number,
                    missing_days=missing_days or {},
                    latest_number=latest_number,
                    z6_set=z6_set or set(),
                    camp_direction=camp_direction,
                    primary_camp=primary_camp,
                    sort_base=sort_base,
                    primary_zodiac_required=primary_zodiac_required,
                    cold_defense_num=cold_defense_num,
                    hot_defense_num=hot_defense_num,
                    polar_mode=polar_mode,
                    warm_fuse=warm_fuse,
                    allow_extreme_primary=allow_extreme_primary,
                    hot_first=hot_first,
                    bipolar_lock=bipolar_lock,
                    horse_guard=horse_guard,
                    ban_last_zodiac=ban_last_zodiac,
                    allow_last_zodiac_weak=allow_last_zodiac_weak,
                )
                return {
                    "picks": picks,
                    "market_judgment": str(parsed.get("盘面判定", "")),
                    "logic_brief": str(parsed.get("逆向逻辑简述", "")),
                    "attempts": attempt,
                    "violations": violations,
                    "ai_context": ai_context,
                }
            except ValueError as exc:
                last_error = str(exc)
                violations.append(last_error)
                logger.warning("attempt=%s 输出未通过校验：%s", attempt, last_error)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        f"【输出未通过校验，退回重做】原因：{last_error}。"
                        "请重新输出严格 JSON（盘面判定 + 直接分配 1档/2档/3档 + 逆向逻辑简述），不要输出候选池、不要输出任何其他文字。"
                    ),
                })

    raise RuntimeError(
        f"DeepSeek 连续 {MAX_ATTEMPTS} 次输出未通过铁律校验，最后一次原因：{last_error}"
    )


def _parse_and_validate(
    parsed: dict,
    zodiac_map: dict[int, str],
    extreme_cold_numbers: set[int],
    recent_cold_break: set[int],
    previous_number: int | None,
    missing_days: dict[int, int],
    latest_number: int,
    z6_set: set[str] | None = None,
    camp_direction: str = "heat",  # "heat" 顺热期 | "cold" 顺冷期
    primary_camp: str | None = None,  # 主攻阵营（变盘时与大盘相反），None=跟大盘
    sort_base: int = 25,  # 唯一解排序基准（连庄特权期=5，其余=25）
    primary_zodiac_required: str | None = None,  # 连庄特权：主攻必须同该生肖
    cold_defense_num: int | None = None,  # 极寒防守：3档强制塞入的最冷极寒号
    hot_defense_num: int | None = None,  # 极热防守：3档强制塞入的极热号(1-7天)
    polar_mode: bool = False,  # 放弃中庸：主攻只抓两极号(10-15天 / 60天以上)
    warm_fuse: bool = False,  # 温号熔断：近5期全不在20-40天，主攻禁20-40
    allow_extreme_primary: bool = False,  # 变盘/熔断期允许极寒号进主攻
    hot_first: bool = False,  # 顺热期：主攻优先极热号(1-7天)
    bipolar_lock: bool = True,  # 三线并行：1档 1-9/>80，2档 10-15/40-60，3档 36-60
    horse_guard: bool = False,  # 防回马枪（默认彻底关闭，不再硬塞上期同肖）
    ban_last_zodiac: bool = True,  # 默认禁买上期生肖
    allow_last_zodiac_weak: bool = False,  # 例外：上期是极品极寒时，允许 1 个 10-15 天同肖暗箭
) -> list[dict]:
    """解析 AI 输出 → 代码硬排序 → 档位分配 → 铁律校验。

    支持两种 AI 输出格式（优先新格式）：
    A. 新格式：AI 直接输出档位分配（1档/2档/3档 + reason）
    B. 旧格式：AI 只输出候选池（≥12 个），代码按唯一解规则排序分配

    无论哪种格式，代码都会：
    1. 铁律校验 + 自动修正（极寒禁1/2档、明牌/刚破冷禁主攻、生肖≤2）
    2. 若 AI 输出违规 → 代码用唯一解排序自动替换
    3. 若 AI 只输出候选池 → 代码按盘面判定动态选择排序基准（|d-25| 或 |d-5|）
    """
    market_judgment = str(parsed.get("盘面判定", ""))

    # --- 1. 尝试解析新格式（AI 直接输出档位分配）---
    primary_num: int | None = None
    primary_reason = ""
    secondary_nums: list[int] = []
    secondary_reasons: dict[int, str] = {}
    defense_nums: list[int] = []
    defense_reasons: dict[int, str] = {}

    p_raw = parsed.get("1档_主攻_20元") or parsed.get("1档_主攻")
    if isinstance(p_raw, dict):
        try:
            primary_num = int(str(p_raw.get("number", "")).strip())
            primary_reason = str(p_raw.get("reason", ""))[:120]
        except (TypeError, ValueError):
            primary_num = None
    elif isinstance(p_raw, (int, float, str)):
        try:
            primary_num = int(str(p_raw).strip())
        except (TypeError, ValueError):
            primary_num = None

    def _extract_tier(raw, reason_map: dict[int, str]) -> list[int]:
        nums: list[int] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    try:
                        n = int(str(item.get("number", "")).strip())
                    except (TypeError, ValueError):
                        continue
                    reason_map[n] = str(item.get("reason", ""))[:120]
                else:
                    try:
                        n = int(str(item).strip())
                    except (TypeError, ValueError):
                        continue
                if 1 <= n <= 49:
                    nums.append(n)
        return nums

    s_raw = parsed.get("2档_中坚_10元") or parsed.get("2档_中坚")
    d_raw = parsed.get("3档_防守_5元") or parsed.get("3档_防守")
    secondary_nums = _extract_tier(s_raw, secondary_reasons)
    defense_nums = _extract_tier(d_raw, defense_reasons)

    # --- 2. 判断是否用 AI 输出，还是 fallback 候选池 ---
    ai_complete = (
        primary_num is not None
        and len(secondary_nums) >= 2
        and len(defense_nums) >= 3
    )

    # --- 3. 唯一解排序（两种场景都需要，用于修正违规 / fallback 候选池）---
    # 排序基准：顺热特权时主攻优先 |d-5|，其余优先 |d-25|
    # 但代码侧统一用 |d-25| 做主排序，顺热特权的"选上期生肖"由 AI 负责
    bipolar_relaxed = False  # 两极锁定降级档：只禁 20-40 温号，其余区间放开

    def _sort_key(n: int) -> tuple:
        d = missing_days.get(n, 999)
        if bipolar_lock or bipolar_relaxed:
            # 2/3 档：10-15 温号 → 40-60 深水 → 36-39 中深水 → >80 极寒兜底 → 1-9 极热 → 其余
            if 10 <= d <= 15:
                return (0, abs(d - 12), n)
            if 40 <= d <= 60:
                return (1, abs(d - 50), n)
            if 36 <= d < 40:
                return (2, abs(d - 38), n)
            if d > 80:
                return (3, -d, n)
            if 1 <= d <= 9:
                return (4, abs(d - 5), n)
            return (5, abs(d - 25), n)
        return (abs(d - sort_base), n)

    def _primary_sort_key(n: int) -> tuple:
        """主攻：极热（1-9 天）优先，其次极品极寒（>80 天）。"""
        d = missing_days.get(n, 999)
        if 1 <= d <= 9:
            return (0, abs(d - 5), n)
        if d > 80:
            return (1, -d, n)
        return (2, abs(d - 25), n)

    # 候选池来源：AI 完整输出时用全部 49 号做修正池；否则用 AI 候选池
    if ai_complete:
        all_selected = [primary_num, *secondary_nums, *defense_nums]
        # 越界 / 去重
        all_selected = [n for n in all_selected if 1 <= n <= 49]
        pool_for_fallback: list[int] = list(range(1, 50))
    else:
        pool_raw = parsed.get("候选池") or parsed.get("candidates") or []
        ai_pool: list[int] = []
        for item in pool_raw:
            if isinstance(item, dict):
                try:
                    n = int(str(item.get("number", "")).strip())
                except (TypeError, ValueError):
                    continue
                if n != primary_num:  # 避免重复
                    reason = str(item.get("reason", ""))[:120]
                    secondary_reasons[n] = reason  # 暂存，后面清理
                    defense_reasons[n] = reason
            else:
                try:
                    n = int(str(item).strip())
                except (TypeError, ValueError):
                    continue
            if 1 <= n <= 49 and n != latest_number:
                ai_pool.append(n)
        ai_pool = list(dict.fromkeys(ai_pool))
        all_nums_no_latest = [n for n in range(1, 50) if n != latest_number]
        pool_for_fallback = []
        seen: set[int] = set()
        for n in ai_pool:
            if n not in seen:
                pool_for_fallback.append(n)
                seen.add(n)
        for n in all_nums_no_latest:
            if n not in seen:
                pool_for_fallback.append(n)
                seen.add(n)
        all_selected = []  # 从头分配

    sorted_pool = sorted(pool_for_fallback, key=_sort_key)
    sorted_primary_pool = sorted(pool_for_fallback, key=_primary_sort_key)

    # --- 4. 铁律校验 AI 输出 ---
    # 主攻阵营：变盘时（杀热切冷/杀冷切热）与大盘相反，2/3 档始终跟大盘
    primary_camp_eff = primary_camp if primary_camp in ("heat", "cold") else camp_direction

    hot_available = False  # 顺热极热优先：是否存在合法的 1-7 天极热主攻号（下方计算）

    def _check_violations(n: int, slot: str, ignore_camp: bool = False) -> list[str]:
        v: list[str] = []
        if slot in ("primary", "secondary") and n in extreme_cold_numbers:
            # 变盘 / 熔断期允许极寒号进主攻（防庄家用极寒杀温号）
            if not (slot == "primary" and allow_extreme_primary):
                v.append("极寒号禁1/2档")
        if slot == "primary":
            if previous_number is not None and n == previous_number:
                v.append("明牌号禁主攻")
            if n in recent_cold_break:
                v.append("刚破冷号禁主攻")
        # 阵营绝对过滤：1 档按主攻阵营（可能变盘反向），2/3 档按大盘阵营
        if z6_set and not ignore_camp:
            n_zodiac = zodiac_map.get(n, "")
            in_z6 = n_zodiac in z6_set
            camp_eff = primary_camp_eff if slot == "primary" else camp_direction
            camp_name = "顺热期(6肖)" if camp_eff == "heat" else "顺冷期(非6肖)"
            if camp_eff == "heat" and not in_z6:
                v.append(f"阵营违规：{camp_name}必须选最新6肖，{n}({n_zodiac})不在6肖")
            elif camp_eff == "cold" and in_z6:
                v.append(f"阵营违规：{camp_name}必须选非最新6肖，{n}({n_zodiac})在6肖里")
        # 顺热连庄特权：主攻必须是上期生肖
        if slot == "primary" and primary_zodiac_required:
            if zodiac_map.get(n, "") != primary_zodiac_required:
                v.append(f"连庄特权：主攻必须与上期生肖「{primary_zodiac_required}」同肖，{n} 不符")
        # 放弃中庸：主攻只抓极热/极寒（与三线并行一致，不再额外限制）
        # 温号熔断：近5期全不在20-40温区，主攻禁止温号，必须极热(1-10天)或极寒(>70天)
        if slot == "primary" and warm_fuse:
            _d = missing_days.get(n, 999)
            if not (1 <= _d <= 10 or _d > 70):
                v.append(
                    f"温号熔断：近5期未出20-40天温号，主攻禁止温号，必须选极热(1-10天)或极寒(>70天)，"
                    f"{n}遗漏{_d}天不符"
                )
        # 三线并行（硬拦）：1档 1-9 极热 / >80 极寒；2档 10-15 温 / 40-60 深水；3档 36-60 中深水
        # 极寒防守、极热防守属强制对冲位，豁免档位区间
        if bipolar_lock and n not in (cold_defense_num, hot_defense_num):
            _d = missing_days.get(n, 999)
            if slot == "primary":
                if not (1 <= _d <= 9 or _d > 80):
                    v.append(
                        f"三线并行：1档只能选遗漏1-9天极热或>80天极寒，{n}遗漏{_d}天不符"
                    )
            elif slot == "secondary":
                if not (10 <= _d <= 15 or 40 <= _d <= 60):
                    v.append(
                        f"三线并行：2档只能选遗漏10-15天温号或40-60天深水，{n}遗漏{_d}天不符"
                    )
            elif not (36 <= _d <= 60):
                v.append(
                    f"三线并行：3档只能选遗漏36-60天中深水，{n}遗漏{_d}天不符"
                )
        # 温号雷区：20-35 天号码全档封杀（任何模式都生效）
        _dw = missing_days.get(n, 999)
        if 20 <= _dw <= 35:
            v.append(f"温号雷区：20-35天号码禁止任何档位，{n}遗漏{_dw}天")
        # 禁买上期生肖：默认全档封杀；仅「上期是极品极寒」例外，允许 1 个 10-15 天同肖暗箭
        if ban_last_zodiac and zodiac_map:
            _last_z = zodiac_map.get(int(latest_number), "")
            if _last_z and zodiac_map.get(n, "") == _last_z:
                _dl = missing_days.get(n, 999)
                if not (allow_last_zodiac_weak and 10 <= _dl <= 15):
                    v.append(
                        f"禁买上期生肖：{n}属{_last_z}，本期全档禁止"
                        f"（仅上期极寒时允许 1 个 10-15 天暗箭）"
                    )
        # 顺热极热优先：存在合法极热号时，主攻必须选 1-7 天极热号
        if slot == "primary" and hot_first and hot_available:
            _d = missing_days.get(n, 999)
            if not (1 <= _d <= 7):
                v.append(f"顺热极热优先：1档必须选遗漏 1-7 天的极热号，{n}遗漏{_d}天不符")
        return v

    # 顺热极热优先：若存在合法的 1-7 天极热主攻号，硬性要求主攻必须是极热号
    if hot_first:
        hot_available = any(
            n != latest_number
            and 1 <= missing_days.get(n, 999) <= 7
            and not _check_violations(n, "primary")
            for n in range(1, 50)
        )

    # 连庄特权降级：若该生肖没有任何合法号，放弃"必须同肖"限制，避免分配失败
    if primary_zodiac_required and zodiac_map and z6_set:
        _has_ok = any(
            zodiac_map.get(n, "") == primary_zodiac_required
            and n != latest_number
            and not _check_violations(n, "primary")
            for n in range(1, 50)
        )
        if not _has_ok:
            primary_zodiac_required = None

    # 放弃中庸降级：若没有合法两极号能进主攻（极寒>80 禁1档），关闭两极模式
    if polar_mode:
        _polar_ok = any(
            n != latest_number and not _check_violations(n, "primary")
            for n in range(1, 50)
        )
        if not _polar_ok:
            polar_mode = False

    # 温号熔断降级：若全池没有 1-10 天或 >70 天的合法主攻号，关闭熔断避免无解
    if warm_fuse:
        _fuse_ok = any(
            n != latest_number and not _check_violations(n, "primary")
            for n in range(1, 50)
        )
        if not _fuse_ok:
            warm_fuse = False

    def _zodiac_ok(nums: list[int], new_n: int) -> bool:
        if not zodiac_map:
            return True
        counts: Counter[str] = Counter(zodiac_map.get(x, "") for x in [*nums, new_n] if zodiac_map.get(x))
        return all(c <= 2 for c in counts.values())

    # --- 5. 档位分配（AI 输出有违规则替换，否则按 AI 输出）---
    slot_types = ["primary", "secondary", "secondary", "defense", "defense", "defense"]

    # 两极锁定可行性预演：严格区间下若无法填满 6 档（生肖≤2 / 阵营 / 区间叠加冲突），
    # 整体关闭锁定，退回原有排序，避免连续重试后 502。
    def _can_fill_locked() -> bool:
        _used: set[int] = set()
        for _slot in slot_types:
            _src = sorted_primary_pool if _slot == "primary" else sorted_pool
            _ok = False
            for _n in _src:
                if _n in _used or _n == latest_number:
                    continue
                if _check_violations(_n, _slot):
                    continue
                if not _zodiac_ok(list(_used), _n):
                    continue
                _used.add(_n)
                _ok = True
                break
            if not _ok:
                return False
        return True

    if bipolar_lock and not _can_fill_locked():
        # 第一级降级：放宽区间，但 20-40 天温号仍然全档禁止
        bipolar_relaxed = True
        bipolar_lock = False
        if not _can_fill_locked():
            # 第二级降级：连温号禁区也无法填满，退回原有排序，避免 502
            logger.warning("两极锁定降级：放宽后仍无法填满 6 档，本期退回原有排序")
            bipolar_relaxed = False

    # 禁买上期生肖降级：区间放宽后仍无解才关闭该限制（避免与两极锁定互相误伤）
    if ban_last_zodiac:
        _ban_ok = all(
            any(
                n != latest_number and not _check_violations(n, s)
                for n in range(1, 50)
            )
            for s in ("primary", "secondary", "defense")
        )
        if not _ban_ok:
            logger.warning("禁买上期生肖无解：本期降级关闭该限制")
            ban_last_zodiac = False

    final_primary: int | None = None
    final_secondary: list[int] = []
    final_defense: list[int] = []

    # AI 候选（去重、过滤 latest_number）
    ai_selected_clean: dict[str, list[int]] = {
        "primary": [primary_num] if (primary_num and 1 <= primary_num <= 49 and primary_num != latest_number) else [],
        "secondary": [n for n in secondary_nums if 1 <= n <= 49 and n != latest_number],
        "defense": [n for n in defense_nums if 1 <= n <= 49 and n != latest_number],
    }

    used: set[int] = set()
    pool_idx = 0
    primary_idx = 0

    for slot_idx, slot_type in enumerate(slot_types):
        # 优先取 AI 输出的候选
        candidate: int | None = None
        if ai_selected_clean[slot_type]:
            for ai_n in ai_selected_clean[slot_type]:
                if ai_n in used:
                    continue
                v = _check_violations(ai_n, slot_type)
                if v:
                    continue
                if not _zodiac_ok(list(used), ai_n):
                    continue
                candidate = ai_n
                break

        # AI 候选不行 → 从排序池取（主攻用独立的极热/极寒优先池）
        if candidate is None:
            src = sorted_primary_pool if slot_type == "primary" else sorted_pool
            idx = primary_idx if slot_type == "primary" else pool_idx
            while idx < len(src):
                n = src[idx]
                idx += 1
                if n in used or n == latest_number:
                    continue
                v = _check_violations(n, slot_type)
                if v:
                    continue
                if not _zodiac_ok(list(used), n):
                    continue
                candidate = n
                break
            if slot_type == "primary":
                primary_idx = idx
            else:
                pool_idx = idx

        if candidate is None:
            raise ValueError(f"代码侧档位分配失败：{slot_type} 无法填充")

        used.add(candidate)
        if slot_type == "primary":
            final_primary = candidate
        elif slot_type == "secondary":
            final_secondary.append(candidate)
        else:
            final_defense.append(candidate)

    assert final_primary is not None

    # 6.0 阵型硬约束（三线并行）：
    #   2 档 = 1 个 40-60 天深水 + 1 个 10-15 天温号
    #   3 档 = 至少 2 个 36-50 天中深水（不足时放宽到 36-60）
    all_nums = [final_primary, *final_secondary, *final_defense]

    def _replace_pick(slot_name: str, idx: int, new_n: int) -> bool:
        """把指定档位的第 idx 注换成 new_n；生肖≤2、不重复才生效。"""
        if slot_name == "secondary":
            trial = [
                final_primary,
                *(final_secondary[:idx] + [new_n] + final_secondary[idx + 1:]),
                *final_defense,
            ]
        else:
            trial = [
                final_primary,
                *final_secondary,
                *(final_defense[:idx] + [new_n] + final_defense[idx + 1:]),
            ]
        if len(set(trial)) != 6:
            return False
        if zodiac_map:
            cnt = Counter(zodiac_map.get(x, "") for x in trial if zodiac_map.get(x))
            if any(c > 2 for c in cnt.values()):
                return False
        (final_secondary if slot_name == "secondary" else final_defense)[idx] = new_n
        all_nums[:] = trial
        return True

    def _ensure_slot(slot_name: str, pred, label: str) -> None:
        bucket = final_secondary if slot_name == "secondary" else final_defense
        if any(pred(n) for n in bucket):
            return
        for cn in sorted_pool:
            if cn in all_nums or cn == latest_number or not pred(cn):
                continue
            if _check_violations(cn, slot_name):
                continue
            for idx in range(len(bucket) - 1, -1, -1):
                if _replace_pick(slot_name, idx, cn):
                    return
        logger.warning("阵型约束跳过：%s 找不到合法号", label)

    if bipolar_lock:
        # 2 档：先保证 1 个 10-15 温号，再保证 1 个 40-60 深水
        _ensure_slot("secondary", lambda n: 10 <= missing_days.get(n, 999) <= 15, "2档10-15温号")
        _ensure_slot("secondary", lambda n: 40 <= missing_days.get(n, 999) <= 60, "2档40-60深水")
        # 3 档：至少 2 个 36-50 中深水（不够放宽到 36-60）
        for _ in range(2):
            if sum(1 for n in final_defense if 36 <= missing_days.get(n, 999) <= 50) >= 2:
                break
            _ensure_slot("defense", lambda n: 36 <= missing_days.get(n, 999) <= 50, "3档36-50中深水")
        if sum(1 for n in final_defense if 36 <= missing_days.get(n, 999) <= 60) < 2:
            _ensure_slot("defense", lambda n: 36 <= missing_days.get(n, 999) <= 60, "3档36-60中深水")
    all_nums = [final_primary, *final_secondary, *final_defense]

    # 6.1 防回马枪已废除：不再硬塞上期生肖。默认全档禁买上期生肖，
    # 仅「上期开出时遗漏 >80 天」例外，允许 1 个 10-15 天的同肖暗箭（由 _check_violations 控制）。

    # 6.2 同肖深水防雷：同肖 2 注时，第 2 注必须是深水号（36 天以上）
    # 自动修复优先：从该生肖深水号替换；无深水号才拆双肖。
    if zodiac_map:
        last_zodiac_dw = zodiac_map.get(int(latest_number), "")
        for _ in range(3):  # 最多修 3 轮（替换可能引入新双肖）
            zc_dw = Counter(zodiac_map.get(n, "") for n in all_nums if zodiac_map.get(n))
            dup_zodiacs = [z for z, c in zc_dw.items() if c >= 2]
            fixed_any = False
            for dz in dup_zodiacs:
                members = sorted(
                    [n for n in all_nums if zodiac_map.get(n) == dz],
                    key=lambda n: (abs(missing_days.get(n, 999) - sort_base), n),
                )
                if len(members) < 2:
                    continue
                keep_n, fix_n = members[0], members[1]
                if missing_days.get(fix_n, 0) >= 36:
                    continue  # 已合规：一注极冷/极热，一注深水
                slot_of_fix = (
                    "primary"
                    if fix_n == final_primary
                    else "secondary" if fix_n in final_secondary else "defense"
                )
                # 深水候选：同生肖、36 天以上、未占用、非上期原号、通过该档位铁律
                deep_cands = [
                    n for n in sorted_pool
                    if zodiac_map.get(n, "") == dz
                    and n not in all_nums
                    and n != latest_number
                    and missing_days.get(n, 0) >= 36
                    and not _check_violations(n, slot_of_fix)
                ]
                # 优先 36-80 天（非极寒，任何档可放）；极寒>80 只能进 3 档
                deep_mid = [n for n in deep_cands if missing_days.get(n, 0) <= 80]
                chosen: int | None = None
                for cand in (deep_mid or deep_cands):
                    if cand in extreme_cold_numbers and slot_of_fix != "defense":
                        continue
                    chosen = cand
                    break
                if chosen is None:
                    # 该生肖无深水号 → 拆双肖：换成其他生肖合法号（优先计数=0 的生肖）
                    alt_cands = [
                        n for n in sorted_pool
                        if zodiac_map.get(n, "") != dz
                        and n not in all_nums
                        and n != latest_number
                        and not _check_violations(n, slot_of_fix)
                    ]
                    zero_first = [
                        n for n in alt_cands if zc_dw.get(zodiac_map.get(n, ""), 0) == 0
                    ]
                    alt = zero_first or alt_cands
                    chosen = alt[0] if alt else None
                if chosen is not None and chosen != fix_n:
                    if slot_of_fix == "primary":
                        final_primary = chosen
                    elif slot_of_fix == "secondary":
                        final_secondary[final_secondary.index(fix_n)] = chosen
                    else:
                        final_defense[final_defense.index(fix_n)] = chosen
                    all_nums = [final_primary, *final_secondary, *final_defense]
                    fixed_any = True
            if not fixed_any:
                break

    # 6.3 两极极端防守：极寒防守 / 极热防守 强制塞入 3 档
    # 优先级：防回马枪 > 极寒防守 > 极热防守 > 深水防雷
    _forced: set[int] = set()  # 已塞入的强制位，后续强制不得挤掉

    def _force_defense(n: int | None) -> bool:
        """把 n 强制塞进 3 档；找不到可替换位则放弃。"""
        if n is None or n in all_nums or n == latest_number:
            return False
        # 极寒防守属极端对冲位，豁免阵营限制（否则顺热期永远塞不进去）
        if _check_violations(n, "defense", ignore_camp=(n == cold_defense_num)):
            return False
        for idx in range(len(final_defense) - 1, -1, -1):
            if final_defense[idx] in _forced:
                continue  # 保护已塞入的强制位
            trial = [
                final_primary,
                *final_secondary,
                *(final_defense[:idx] + [n] + final_defense[idx + 1:]),
            ]
            if len(set(trial)) != 6:
                continue
            if zodiac_map:
                _cnt = Counter(zodiac_map.get(x, "") for x in trial if zodiac_map.get(x))
                if any(c > 2 for c in _cnt.values()):
                    continue
            final_defense[idx] = n
            all_nums[:] = trial
            _forced.add(n)
            return True
        return False

    if cold_defense_num is not None:
        _force_defense(cold_defense_num)
    if hot_defense_num is not None:
        _force_defense(hot_defense_num)

    if len(set(all_nums)) != 6:
        raise ValueError(f"号码重复：{all_nums}")
    if zodiac_map:
        counts = Counter(zodiac_map[n] for n in all_nums if zodiac_map.get(n))
        bad = {z: c for z, c in counts.items() if c > 2}
        if bad:
            raise ValueError(f"生肖超2：{bad}")
        # 上期生肖硬校验：默认 0 注；例外（上期极寒）最多 1 注且必须 10-15 天
        _lz = zodiac_map.get(int(latest_number), "")
        if ban_last_zodiac and _lz:
            _lz_nums = [n for n in all_nums if zodiac_map.get(n) == _lz]
            if len(_lz_nums) > (1 if allow_last_zodiac_weak else 0):
                raise ValueError(f"禁买上期生肖「{_lz}」：出现了 {_lz_nums}")
            if allow_last_zodiac_weak and any(
                not (10 <= missing_days.get(n, 999) <= 15) for n in _lz_nums
            ):
                raise ValueError(f"上期同肖暗箭必须是 10-15 天：{_lz_nums}")
    if final_primary in extreme_cold_numbers and not allow_extreme_primary:
        raise ValueError(f"极寒号 {final_primary} 溜进主攻")
    if previous_number is not None and final_primary == previous_number:
        raise ValueError(f"明牌号 {previous_number} 溜进主攻")
    if final_primary in recent_cold_break:
        raise ValueError(f"刚破冷号 {final_primary} 溜进主攻")
    # 阵营最终校验：1 档按主攻阵营（变盘时反向），2/3 档按大盘阵营
    # 强制防守位（极寒防守）豁免阵营 —— 它是极端对冲位，不受大盘方向约束
    if z6_set and zodiac_map:
        for idx, num in enumerate(all_nums):
            if num in _forced:
                continue
            nz = zodiac_map.get(num, "")
            in_z6 = nz in z6_set
            camp_eff = primary_camp_eff if idx == 0 else camp_direction
            camp_name = "顺热期(6肖)" if camp_eff == "heat" else "顺冷期(非6肖)"
            if camp_eff == "heat" and not in_z6:
                raise ValueError(f"阵营违规：{camp_name} {num}({nz}) 不在最新6肖")
            if camp_eff == "cold" and in_z6:
                raise ValueError(f"阵营违规：{camp_name} {num}({nz}) 在最新6肖里")
    # 连庄特权最终校验：主攻必须与上期同肖
    if primary_zodiac_required and zodiac_map.get(final_primary, "") != primary_zodiac_required:
        raise ValueError(
            f"连庄特权违规：主攻 {final_primary} 与上期生肖「{primary_zodiac_required}」不同肖"
        )

    # 聚合 reason：AI 输出的 reason 优先，否则 fallback
    ai_reasons_agg: dict[int, str] = {}
    if primary_num is not None:
        ai_reasons_agg[primary_num] = primary_reason
    ai_reasons_agg.update(secondary_reasons)
    ai_reasons_agg.update(defense_reasons)

    picks = [
        {"number": final_primary, "amount": 20, "role": "primary",
         "reason": ai_reasons_agg.get(final_primary, "代码唯一解排序（|d-25| 最小）")},
        *({"number": n, "amount": 10, "role": "secondary",
           "reason": ai_reasons_agg.get(n, "")}
          for n in final_secondary),
        *({"number": n, "amount": 5, "role": "defense",
           "reason": ai_reasons_agg.get(n, "")}
          for n in final_defense),
    ]
    return picks
