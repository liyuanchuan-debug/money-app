"""六合彩（Mark Six）开奖记录领域模块 —— 纯函数实现，不访问数据库。

数据模型（2026 清理后的口径）：
- 每期只保留**特码**（special number）与由农历年推导出的**生肖**；6 个正码丢弃；
- **波色（color）、五行（element）、河合码** 已彻底移除：输入文本里若仍带
  ``19(红)鼠/火``、``+22(绿)鸡/水`` 这类旧装饰，解析器**静默忽略**，只取号码与生肖；
- 落库枚举一律英文码（生肖 RAT…PIG、号码 1-49），中文只出现在展示标签与提示文案里；
- 生肖随农历年轮转（每年 01 号所属生肖不同），边界按农历年起始日（春节）判定，
  年份数据集中在 ``ZODIAC_YEARS``，不使用散落的 if 分支；
- 49 号码 → 生肖 的映射完全由 ``(农历年, animal_of_01)`` 决定，
  见 ``zodiac_table_for_year()``（物化到 ``zodiac_numbers`` 表时也用同一函数）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

NUMBER_MIN = 1
NUMBER_MAX = 49

# --------------------------------------------------------------------------- #
# 应用时区（用户都在国内）：快捷录入的「今天」一律按 UTC+8 判定，
# 避免服务器跑在 UTC 时把凌晨录的一期算到前一天。
# --------------------------------------------------------------------------- #
APP_TIMEZONE_NAME = "Asia/Shanghai"
APP_TIMEZONE = timezone(timedelta(hours=8), APP_TIMEZONE_NAME)


def today_local() -> date:
    """App 口径的「今天」（UTC+8）。"""
    return datetime.now(APP_TIMEZONE).date()

# --------------------------------------------------------------------------- #
# 生肖（12 生肖环固定，号码归属随农历年轮转）
# --------------------------------------------------------------------------- #
ZODIAC_RAT = "RAT"
ZODIAC_OX = "OX"
ZODIAC_TIGER = "TIGER"
ZODIAC_RABBIT = "RABBIT"
ZODIAC_DRAGON = "DRAGON"
ZODIAC_SNAKE = "SNAKE"
ZODIAC_HORSE = "HORSE"
ZODIAC_GOAT = "GOAT"
ZODIAC_MONKEY = "MONKEY"
ZODIAC_ROOSTER = "ROOSTER"
ZODIAC_DOG = "DOG"
ZODIAC_PIG = "PIG"

# 生肖环：号码递增时生肖逆序回退（01 号归属当年生肖）
ZODIAC_ORDER: tuple[str, ...] = (
    ZODIAC_RAT,
    ZODIAC_OX,
    ZODIAC_TIGER,
    ZODIAC_RABBIT,
    ZODIAC_DRAGON,
    ZODIAC_SNAKE,
    ZODIAC_HORSE,
    ZODIAC_GOAT,
    ZODIAC_MONKEY,
    ZODIAC_ROOSTER,
    ZODIAC_DOG,
    ZODIAC_PIG,
)
ZODIAC_LABELS: dict[str, str] = {
    ZODIAC_RAT: "鼠",
    ZODIAC_OX: "牛",
    ZODIAC_TIGER: "虎",
    ZODIAC_RABBIT: "兔",
    ZODIAC_DRAGON: "龙",
    ZODIAC_SNAKE: "蛇",
    ZODIAC_HORSE: "马",
    ZODIAC_GOAT: "羊",
    ZODIAC_MONKEY: "猴",
    ZODIAC_ROOSTER: "鸡",
    ZODIAC_DOG: "狗",
    ZODIAC_PIG: "猪",
}

# 农历年边界表：(农历年, 春节当日, 号码 01 所属生肖)
# 春节日期已与万年历核对：2025-01-29（乙巳蛇年）、2026-02-17（丙午马年）。
ZODIAC_YEARS: tuple[tuple[int, date, str], ...] = (
    (2025, date(2025, 1, 29), ZODIAC_SNAKE),
    (2026, date(2026, 2, 17), ZODIAC_HORSE),
)

_LABEL_TO_ZODIAC: dict[str, str] = {v: k for k, v in ZODIAC_LABELS.items()}


# --------------------------------------------------------------------------- #
# 基础查表
# --------------------------------------------------------------------------- #
def lunar_year_for(draw_date: date) -> int | None:
    """开奖日 → 农历年；早于已知年份起点（春节）时返回 None。"""
    for lunar_year, starts_on, _animal in reversed(ZODIAC_YEARS):
        if draw_date >= starts_on:
            return lunar_year
    return None


def year_starts_on(lunar_year: int | None) -> date | None:
    """农历年 → 春节当日；年份未知时返回 None。"""
    if lunar_year is None:
        return None
    for year, starts_on, _animal in ZODIAC_YEARS:
        if year == lunar_year:
            return starts_on
    return None


def animal_of_01(lunar_year: int | None) -> str | None:
    """农历年 → 号码 01 的所属生肖英文码。"""
    if lunar_year is None:
        return None
    for year, _starts_on, animal in ZODIAC_YEARS:
        if year == lunar_year:
            return animal
    return None


def zodiac_of(number: int, draw_date: date) -> str | None:
    """号码 + 开奖日 → 生肖英文码；年份未知时返回 None（不抛异常）。"""
    animal = animal_of_01(lunar_year_for(draw_date))
    if animal is None:
        return None
    try:
        base = ZODIAC_ORDER.index(animal)
    except ValueError:  # 表里写错的生肖码，退化为未知
        return None
    offset = (number - 1) % len(ZODIAC_ORDER)
    return ZODIAC_ORDER[(base - offset) % len(ZODIAC_ORDER)]


def zodiac_numbers(number: int, draw_date: date) -> list[int]:
    """与 number 同肖的全部号码（按开奖日所属农历年）。"""
    animal = zodiac_of(number, draw_date)
    if animal is None:
        return []
    return [n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if zodiac_of(n, draw_date) == animal]


def zodiac_table(draw_date: date) -> dict[str, list[int]] | None:
    """开奖日所属农历年的完整生肖表；年份未知时返回 None。"""
    if animal_of_01(lunar_year_for(draw_date)) is None:
        return None
    table: dict[str, list[int]] = {code: [] for code in ZODIAC_ORDER}
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        code = zodiac_of(number, draw_date)
        if code is not None:
            table[code].append(number)
    return table


def zodiac_table_for_year(lunar_year: int | None) -> dict[str, list[int]] | None:
    """农历年 → 完整生肖表；年份未知时返回 None。（``zodiac_numbers`` 表的唯一来源）"""
    starts_on = year_starts_on(lunar_year)
    if starts_on is None:
        return None
    return zodiac_table(starts_on)


def known_zodiac_years() -> list[dict]:
    """参考接口用：已知农历年边界列表。"""
    return [
        {
            "lunar_year": year,
            "starts_on": starts_on,
            "animal_of_01": animal,
            "animal_of_01_label": ZODIAC_LABELS.get(animal),
        }
        for year, starts_on, animal in ZODIAC_YEARS
    ]


def zodiac_label(code: str | None) -> str | None:
    return ZODIAC_LABELS.get(code) if code else None


# --------------------------------------------------------------------------- #
# 文本解析
# --------------------------------------------------------------------------- #
@dataclass
class ParsedNumber:
    """解析出的一枚号码（只保留号码本身与生肖；旧装饰信息一律丢弃）。"""

    number: int
    position: int
    is_special: bool
    zodiac: str | None = None


@dataclass
class ParsedDraw:
    """解析出的一期开奖记录（落库只取特码）。"""

    draw_date: date
    period: int
    numbers: list[ParsedNumber] = field(default_factory=list)
    line_number: int = 0

    @property
    def special_number(self) -> int | None:
        for item in self.numbers:
            if item.is_special:
                return item.number
        return None


@dataclass
class ParseError:
    """一行无法解析时的错误记录。"""

    line: int
    text: str
    message: str


# 行首头部两种顺序：
#   1) 日期在前：2026-03-11\t70期\t…
#   2) 期号在前：269期 2026-09-26：…（用户真实数据）
# 日期与期号之间允许任意空白，日期后允许 `:` / `：`，期号允许 `期` 后缀。
_HEADER_DATE_FIRST_RE = re.compile(
    r"^(?P<date>\d{4}-\d{1,2}-\d{1,2})\s+"
    r"(?P<period>\d+)\s*期?\s*[:：]?\s*(?P<rest>.*)$"
)
_HEADER_PERIOD_FIRST_RE = re.compile(
    r"^(?P<period>\d+)\s*期\s*"
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2})\s*[:：]?\s*(?P<rest>.*)$"
)
# 单枚号码：可选 + 前缀、两位数、可选 (色) 装饰（整段忽略）、其余为生肖/五行
_TOKEN_RE = re.compile(
    r"^[+＋]?\s*(?P<number>\d{1,2})\s*"
    r"(?:[（(][^）)]*[）)])?\s*"
    r"(?P<rest>.*)$"
)
_NUMBER_START_RE = re.compile(r"^[+＋]?\s*\d{1,2}")
_SPECIAL_MARKER_RE = re.compile(r"[+＋]\s*\d{1,2}")
# 号码分隔符：顿号 / 逗号 / 分号，以及任意空白（兼容 TAB 分隔）
_SEPARATOR_RE = re.compile(r"[、,，;；\s]+")
_SLASH_RE = re.compile(r"[/／]")


def _match_header(text: str) -> re.Match[str] | None:
    """两种头部顺序都试一遍（按日期样式区分）。"""
    for regex in (_HEADER_DATE_FIRST_RE, _HEADER_PERIOD_FIRST_RE):
        match = regex.match(text)
        if match:
            return match
    return None


def _zodiac_code(token: str | None) -> str | None:
    if not token:
        return None
    text = token.strip()
    if not text:
        return None
    upper = text.upper()
    if upper in ZODIAC_LABELS:
        return upper
    return _LABEL_TO_ZODIAC.get(text)


def _zodiac_from_rest(rest: str) -> str | None:
    """从号码尾部的 ``鼠/火``、``鼠``、``/火`` 等文本里挑出生肖；其余（五行）忽略。"""
    for part in _SLASH_RE.split(rest):
        code = _zodiac_code(part)
        if code is not None:
            return code
    return None


def _split_fragments(text: str) -> list[str]:
    """按 `、` `,` `，` `;` `；` 或空白切分号码；不以数字开头的碎片并入上一枚（处理折行）。"""
    merged: list[str] = []
    for fragment in _SEPARATOR_RE.split(text):
        piece = fragment.strip()
        if not piece:
            continue
        if merged and not _NUMBER_START_RE.match(piece):
            merged[-1] = merged[-1] + piece
            continue
        merged.append(piece)
    return merged


def parse_number_fragment(fragment: str, position: int = 1) -> ParsedNumber:
    """解析单枚号码片段。

    号码是唯一必需的部分；``(波色)``、``/五行`` 等旧装饰一律忽略，
    尾部若出现生肖（中文标签或英文码）则记为申报生肖。
    """
    text = fragment.strip()
    match = _TOKEN_RE.match(text)
    if not match:
        raise ValueError(f"无法识别的号码片段: {text}")
    number = int(match.group("number"))
    if not NUMBER_MIN <= number <= NUMBER_MAX:
        raise ValueError(f"号码越界: {number}")
    return ParsedNumber(
        number=number,
        position=position,
        is_special=text.startswith(("+", "＋")),
        zodiac=_zodiac_from_rest(match.group("rest")),
    )


def _is_complete(group_text: str) -> bool:
    """判断累积文本是否已是一整期（已出现 + 特码标记）。"""
    match = _match_header(group_text.strip())
    if not match:
        return False
    return bool(_SPECIAL_MARKER_RE.search(match.group("rest")))


def _parse_group(group_text: str, line_number: int) -> ParsedDraw:
    text = group_text.strip()
    match = _match_header(text)
    if not match:
        raise ValueError("行首缺少「日期 + 期号」")
    year, month, day = (int(part) for part in match.group("date").split("-"))
    try:
        draw_date = date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"非法日期: {match.group('date')}") from exc

    fragments = _split_fragments(match.group("rest"))
    if not fragments:
        raise ValueError("缺少号码（至少需要一个特码）")

    numbers = [
        parse_number_fragment(fragment, index + 1)
        for index, fragment in enumerate(fragments)
    ]
    specials = [item for item in numbers if item.is_special]
    if len(specials) > 1:
        raise ValueError(f"一行出现多个特码（+）标记：{len(specials)} 个")
    if not specials:
        # 容错：没有 + 前缀时按位置取最后一枚为特码
        numbers[-1].is_special = True

    return ParsedDraw(
        draw_date=draw_date,
        period=int(match.group("period")),
        numbers=numbers,
        line_number=line_number,
    )


def parse_draw_lines(text: str) -> tuple[list[ParsedDraw], list[ParseError]]:
    """解析粘贴文本 → (成功期数, 错误列表)。

    容错规则：
    - 前后空白与空行忽略；行首不是头部的非空行视为上一期的折行续写；
    - 头部兼容「日期在前」与「期号在前」两种顺序（按日期样式区分）；
    - 号码分隔符兼容 `、` / `,` / `，` / `;` / `；` 与任意空白（TAB / 空格）；
    - 括号兼容全角 `（）` 与半角 `()`，括号内容（旧波色装饰）整体忽略；
    - 号码尾部允许 `生肖/五行`，解析只取生肖，五行与波色永不落库；
    - ``+`` 前缀标记特码；缺失时按位置取最后一枚；
    - 单行解析失败只记入 errors（带行号与原文），不中断整批。
    """
    parsed: list[ParsedDraw] = []
    errors: list[ParseError] = []

    pending: str | None = None
    pending_line = 0

    def flush() -> None:
        nonlocal pending, pending_line
        if pending is None:
            return
        try:
            parsed.append(_parse_group(pending, pending_line))
        except ValueError as exc:
            errors.append(ParseError(line=pending_line, text=pending, message=str(exc)))
        pending = None
        pending_line = 0

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue  # 空行忽略，不打断折行分组
        if _match_header(line):
            flush()
            pending = line
            pending_line = index
            continue
        if pending is not None and not _is_complete(pending):
            pending = f"{pending} {line}"  # 折行续写
            continue
        errors.append(
            ParseError(
                line=index,
                text=line,
                message="行首缺少「日期 + 期号」，且无法并入上一期",
            )
        )

    flush()
    return parsed, errors


# --------------------------------------------------------------------------- #
# 校验 / 落库载荷
# --------------------------------------------------------------------------- #
def validate_draw(parsed: ParsedDraw) -> list[str]:
    """交叉校验申报生肖与推导生肖、期号与年积日，返回警告文案（永不抛异常）。"""
    warnings: list[str] = []
    if lunar_year_for(parsed.draw_date) is None:
        warnings.append(
            f"第 {parsed.line_number} 行 {parsed.draw_date} 不在已知农历年表内，"
            "生肖按申报值原样记录。"
        )

    special = next((item for item in parsed.numbers if item.is_special), None)
    if special is not None:
        derived_zodiac = zodiac_of(special.number, parsed.draw_date)
        if (
            derived_zodiac is not None
            and special.zodiac is not None
            and special.zodiac != derived_zodiac
        ):
            warnings.append(
                f"第 {parsed.line_number} 行 特码 {special.number:02d}：申报生肖"
                f"{ZODIAC_LABELS.get(special.zodiac, special.zodiac)}与推导"
                f"{ZODIAC_LABELS[derived_zodiac]}不一致，已按权威表记录。"
            )

    # 期号 vs 日期的年积日：软提示，仅供参考，绝不阻断导入
    expected = parsed.draw_date.timetuple().tm_yday
    if parsed.period != expected:
        warnings.append(
            f"第 {parsed.line_number} 行：期号 {parsed.period} 与日期 "
            f"{parsed.draw_date} 的年积日 {expected} 不一致（仅供参考，不影响导入）。"
        )
    return warnings


def resolve_draw(parsed: ParsedDraw) -> dict:
    """把解析结果整理为落库载荷：只有日期 / 期号 / 特码 / 生肖。"""
    special = parsed.special_number
    zodiac = zodiac_of(special, parsed.draw_date) if special is not None else None
    if zodiac is None:
        # 农历年未知时退回申报值（仍不落库任何波色 / 五行）
        declared = next((item.zodiac for item in parsed.numbers if item.is_special), None)
        zodiac = declared
    return {
        "draw_date": parsed.draw_date,
        "period": parsed.period,
        "special_number": special,
        "zodiac": zodiac,
        "zodiac_label": zodiac_label(zodiac),
    }


def resolve_draws(parsed_draws: list[ParsedDraw]) -> tuple[list[dict], list[str]]:
    """批量整理落库载荷，并汇总全部警告。"""
    payloads: list[dict] = []
    warnings: list[str] = []
    for parsed in parsed_draws:
        warnings.extend(validate_draw(parsed))
        payloads.append(resolve_draw(parsed))
    return payloads, warnings


def decorate_draw(draw: dict | None) -> dict | None:
    """给落库的一期记录补上生肖与中文标签（落库仍只有特码号码）。"""
    if not draw:
        return draw
    special = draw.get("special_number")
    zodiac = zodiac_of(int(special), draw["draw_date"]) if special is not None else None
    return {**draw, "zodiac": zodiac, "zodiac_label": zodiac_label(zodiac)}
