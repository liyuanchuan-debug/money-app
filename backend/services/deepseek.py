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
你是一个没有感情的【杀猪盘逆向博弈算法】。你的任务是读取用户提供的走势数据，摒弃所有常规概率学，完全基于"资金池收割、反人性、能量消耗"进行推演，输出唯一确定的下注阵型。

# 核心执行流程（必须在内心按顺序严格执行，不得跳步）
Step 1：判定当前是顺冷还是顺热？锁定基本盘（直接读 user prompt 里后端标注的阵营方向，不要自己推导）。
Step 2：检查上期开奖生肖（如 05 虎）。强制从该生肖挑 1 个防回马枪号（1~3 档任意档均可，但绝不能是上期原号本身）。
Step 3：从基本盘中挑选遗漏在【20-50天】的候选号，剔除与防回马枪冲突的号。
Step 4：对候选号进行生肖去重，单一生肖绝不能超过 2 个（即禁止同生肖买重）。
Step 5：按 "★ 唯一解排序基准" 排序（默认距 25 天最近优先；顺热连庄特权期改为距 5 天最近优先），依次填入 1档、2档、3档。

# 一、绝对强制铁律（最高优先级，违背则判错；系统会做代码级校验，违规将被退回重做）
0. 【阵营绝对过滤 - 最高优先级】：先判定大盘方向，2/3 档强制只能从对应阵营选号；1 档以 user prompt 里的「★ 主攻阵营要求」为准。
   - 顺热期（上期生肖∈最新6肖）：2/3 档只能从"最新6肖对应的号码"里挑。
   - 顺冷期（上期生肖∉最新6肖）：2/3 档只能从"非最新6肖号码"里挑。
   - 变盘例外：若触发【能量透支杀热切冷】，1 档主攻强制反向切"非最新6肖"；若触发【能量透支杀冷切热】，1 档主攻强制反向切"最新6肖"。2/3 档不反向。
   - 顺热连庄特权例外：1 档必须是上期生肖、遗漏 1-7 天的极热号。
   - 阵营方向由后端判定并在 user prompt 里明确标注，直接读，不要自己推导。
   - 注意：最新6肖 = 从"上期及更早"的开奖里取不重复生肖，不含上期本身。若上期生肖不在其中，就是顺冷期。
1. 【无视"预"字号】：输入数据中的"预"字号是用户自研结果。绝对禁止将其视为明牌或大众号进行排除。
2. 【强制资金阵型】：必须输出 1档（20元×1个）、2档（10元×2个）、3档（5元×3个），共6个号码。6 个号码必须互不相同。
3. 【同肖去重】：任何生肖在三档中出现的总次数，绝对不能超过 2 个。
4. 【极寒排雷】：遗漏超过 80 天的极寒号，绝对不准进入 1档（20元）和 2档（10元），只能放 3档（5元）防守。
5. 【防回马枪】：6 注里必须至少有 1 注与"上期开奖生肖"同肖（但绝不能是上期原号本身）。缺了直接判违规退回重做。
6. 【同肖深水防雷】：同一生肖选 2 注时，第 1 注按距 25 天基准取温号，第 2 注必须从该生肖遗漏>50 天的深水号里挑；绝对禁止两注都落在 20-40 天浅温区（防庄家避开温号挖深水）。若主攻生肖存在深水号（遗漏 50-70 天），第 2 注必须优先启用它。

# 二、核心定盘法则（按优先级判断当前盘面，必须严格按后端提供的布尔触发条件判断，不要再自己推导或编造任何数字！！！）
# 绝对禁止在"逆向逻辑简述"中编造任何具体数字（如"连开10期""5次命中"等），只能引用后端已提供的布尔结果和生肖序列事实。
1. 【极寒连三变盘】：若近 3 期均开出极寒号（>80天），下期强制杀极寒，切回【20-50天】的温号。
2. 【极寒破冰回补】：若上期开出极寒号，主攻优先选该生肖中遗漏 10-20 天的温号（绝不选上期原号或该生肖其它极寒）。
3. 【能量透支杀热切冷】：若最新6肖（热盘）连开3期，或某热肖连开2期，判定热盘透支。1档主攻强制转向"非最新6肖"的【20-30天】黄金温号。
4. 【能量透支杀冷切热】：若非最新6肖（冷盘）连开3期，或某冷肖连开2期，判定冷盘透支。1档主攻强制切回"最新6肖"的【10-20天】活跃温号。
5. 【顺热连庄特权（防错失明牌）】：若极寒刚断且热盘刚开1期，处于【顺热期】。此时绝对禁止买同肖的20-40天冷号，必须强制从上期开奖生肖中，选 1 个遗漏在【1-7天】的近期极热号，放入 1档主攻！
6. 【顺冷期】：若上期开出"非最新6肖"且未满3期，处于【顺冷期】。1档主攻选非6肖中遗漏【20-50天】的温号。
7. 【同肖暗箭】：若判定上期生肖可能回马枪，必须从该生肖挑 1 个遗漏【10-40天】的温号放入 2档或3档防守（绝不能选上期原号）。

# 三、唯一解裁决算法（确保相同数据每次输出绝对一致）
按以下步骤执行，排序由系统代码硬执行，但你必须按此逻辑挑选候选：
1. 根据上述法则，筛选出所有符合条件的候选号（至少12个）。
2. 强制排序规则（系统代码按此执行，你按优先级排好候选）：
   - 第一权重：主攻号遗漏天数绝对值最接近【25天】的优先（若触发顺热特权，则优先最接近【5天】的）。
   - 第二权重：若遗漏天数相同，强制选择【号码数值更小】的（如11优先于44）。
   - 第三权重：按排名顺延取号，遇到生肖已满 2 个直接跳过。
3. 按序分配：第1名进1档；第2、3名进2档；第4、5、6名进3档。

# 四、输入数据（由用户通过User Prompt提供）
- 盘面统计指标（后端已算好，直接取值，不要再自己推导）
- 目标预测期数
- 上期开奖号码及生肖
- 近3期开奖号码及生肖
- 最新6肖列表
- 极寒号及遗漏天数（>80天）
- 完整号码池（1-49）

# 五、输出格式要求（纯 JSON，不带任何解释词）
# reason 字段强制必填：必须写清该号的逆向博弈逻辑，且只能引用 user prompt 里给出的真实数据
# （生肖、遗漏天数、是否在最新6肖、是否极寒、上期生肖）。
# 严禁自己推算或编造"连开几期""遗漏最接近多少天"这类未给出的数字。
{
  "target_period": "第XXX期",
  "盘面判定": "极寒变盘/极寒破冰/杀热切冷/杀冷切热/顺冷期/顺热期（必须严格从这6个里选一个，不能自己造）",
  "1档_主攻_20元": {"number": 28, "reason": "非最新6肖，遗漏25天，距25天黄金温区最近，顺冷主攻成立"},
  "2档_中坚_10元": [
    {"number": 23, "reason": "非6肖浅温号，遗漏20天，为主攻做掩护"},
    {"number": 8, "reason": "非6肖浅温号，遗漏21天，为主攻做掩护"}
  ],
  "3档_防守_5元": [
    {"number": 40, "reason": "与主攻28同属兔，同肖双保险，兔连爆可中双份"},
    {"number": 32, "reason": "与次选08同肖猪，双保险防猪线连爆"},
    {"number": 17, "reason": "上期05(虎)的同肖暗箭，防庄家回马枪"}
  ],
  "逆向逻辑简述": "大盘处于顺冷期，主打非6肖温号，并防守上期同肖回马枪。"
}
"""


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
        lines.append(
            f"  ⚠️ 防回马枪铁律：6 注中必须至少有 1 注的生肖是「{last_zodiac}」"
            f"（但不能是上期原号 {latest_number}）。"
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
                )
                return {
                    "picks": picks,
                    "market_judgment": str(parsed.get("盘面判定", "")),
                    "logic_brief": str(parsed.get("逆向逻辑简述", "")),
                    "attempts": attempt,
                    "violations": violations,
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
    def _sort_key(n: int) -> tuple[int, int]:
        d = missing_days.get(n, 999)
        return (abs(d - sort_base), n)

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

    # --- 4. 铁律校验 AI 输出 ---
    # 主攻阵营：变盘时（杀热切冷/杀冷切热）与大盘相反，2/3 档始终跟大盘
    primary_camp_eff = primary_camp if primary_camp in ("heat", "cold") else camp_direction

    def _check_violations(n: int, slot: str) -> list[str]:
        v: list[str] = []
        if slot in ("primary", "secondary") and n in extreme_cold_numbers:
            v.append("极寒号禁1/2档")
        if slot == "primary":
            if previous_number is not None and n == previous_number:
                v.append("明牌号禁主攻")
            if n in recent_cold_break:
                v.append("刚破冷号禁主攻")
        # 阵营绝对过滤：1 档按主攻阵营（可能变盘反向），2/3 档按大盘阵营
        if z6_set:
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
        return v

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

    def _zodiac_ok(nums: list[int], new_n: int) -> bool:
        if not zodiac_map:
            return True
        counts: Counter[str] = Counter(zodiac_map.get(x, "") for x in [*nums, new_n] if zodiac_map.get(x))
        return all(c <= 2 for c in counts.values())

    # --- 5. 档位分配（AI 输出有违规则替换，否则按 AI 输出）---
    slot_types = ["primary", "secondary", "secondary", "defense", "defense", "defense"]
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

        # AI 候选不行 → 从排序池取
        if candidate is None:
            while pool_idx < len(sorted_pool):
                n = sorted_pool[pool_idx]
                pool_idx += 1
                if n in used or n == latest_number:
                    continue
                v = _check_violations(n, slot_type)
                if v:
                    continue
                if not _zodiac_ok(list(used), n):
                    continue
                candidate = n
                break

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

    # --- 6. 最终保底校验 ---
    all_nums = [final_primary, *final_secondary, *final_defense]

    # 6.1 防回马枪：至少 1 注与上期生肖同肖（不能是上期原号）
    # 先尝试用该生肖的合法号自动替换 3 档最后一注；确实无解才退回重做
    if zodiac_map:
        last_zodiac = zodiac_map.get(int(latest_number), "")
        if last_zodiac and not any(zodiac_map.get(n, "") == last_zodiac for n in all_nums):
            horse_pool = [
                n for n in sorted_pool
                if zodiac_map.get(n, "") == last_zodiac
                and n not in all_nums
                and n != latest_number
            ]
            replaced = False
            for hn in horse_pool:
                if _check_violations(hn, "defense"):
                    continue
                for idx in range(len(final_defense) - 1, -1, -1):
                    trial = [
                        final_primary,
                        *final_secondary,
                        *(final_defense[:idx] + [hn] + final_defense[idx + 1:]),
                    ]
                    cnt = Counter(zodiac_map.get(x, "") for x in trial if zodiac_map.get(x))
                    if any(c > 2 for c in cnt.values()):
                        continue
                    final_defense[idx] = hn
                    all_nums = trial
                    replaced = True
                    break
                if replaced:
                    break
            if not replaced:
                raise ValueError(
                    f"缺少防回马枪号：6 注中至少有 1 注必须是上期生肖「{last_zodiac}」"
                    f"的同肖号（不能是上期原号 {latest_number}）"
                )

    # 6.2 同肖深水防雷（补丁1）：同肖 2 注时，第 2 注必须遗漏>50 天
    # 自动修复优先：从该生肖深水号（>50天）替换；无深水号才拆双肖；
    # 若 fix 号是防回马枪唯一来源则保留（回马枪优先于深水）。
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
                if missing_days.get(fix_n, 0) > 50:
                    continue  # 已合规（一温一深）
                # fix 号是回马枪唯一来源 → 保留，回马枪优先
                if dz == last_zodiac_dw and not any(
                    zodiac_map.get(x, "") == last_zodiac_dw
                    for x in all_nums
                    if x != fix_n
                ):
                    continue
                slot_of_fix = (
                    "primary"
                    if fix_n == final_primary
                    else "secondary" if fix_n in final_secondary else "defense"
                )
                # 深水候选：同生肖、>50天、未占用、非上期原号、通过该档位铁律
                deep_cands = [
                    n for n in sorted_pool
                    if zodiac_map.get(n, "") == dz
                    and n not in all_nums
                    and n != latest_number
                    and missing_days.get(n, 0) > 50
                    and not _check_violations(n, slot_of_fix)
                ]
                # 优先 50-80 天（非极寒，任何档可放）；极寒>80 只能进 3 档
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

    if len(set(all_nums)) != 6:
        raise ValueError(f"号码重复：{all_nums}")
    if zodiac_map:
        counts = Counter(zodiac_map[n] for n in all_nums if zodiac_map.get(n))
        bad = {z: c for z, c in counts.items() if c > 2}
        if bad:
            raise ValueError(f"生肖超2：{bad}")
    if final_primary in extreme_cold_numbers:
        raise ValueError(f"极寒号 {final_primary} 溜进主攻")
    if previous_number is not None and final_primary == previous_number:
        raise ValueError(f"明牌号 {previous_number} 溜进主攻")
    if final_primary in recent_cold_break:
        raise ValueError(f"刚破冷号 {final_primary} 溜进主攻")
    # 阵营最终校验：1 档按主攻阵营（变盘时反向），2/3 档按大盘阵营
    if z6_set and zodiac_map:
        for idx, num in enumerate(all_nums):
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
