"""前瞻（期外）验证账本 —— 开奖前冻结预测，开奖后诚实计分。

本模块是**唯一**能证明「策略到底有没有用」的验证口径：预测在开奖之前冻结，
之后只允许「读」，不允许改；改一条冻结记录，哈希链立刻能查出来。它与
``scripts/max_fit_analysis.py`` 那类**样本内 / 回看**分析互补 —— 后者永远可以
事后挑选，前者不能。

口径铁律（违反即为缺陷）：

- **落库 / 存储的枚举一律英文码**（``PRODUCTION_ENGINE`` / ``UNIFORM`` /
  ``FIT_ONLY`` / ``LIVE``）；汉字只出现在人类可读的 ``*_label`` 与提示文案。
- **只看已开奖的前缀**：冻结第 ``P`` 期时，策略只允许看到
  ``period < P`` 的开奖；``available_length`` 就是它实际看到的期数
  （= 前缀长度 = ``period_index``），目标期**从不出现在**输入里。这是
  「无前视 / 无未来函数」的可核对凭证。
- **不做事后补冻**：目标期一旦已开奖，冻结一律硬报错 —— 宁可拒绝，也不编造
  一条「其实当时没预测过」的记录。
- **只写英文的存储字段**，且**不改动线上推荐路径**：``services.lottery`` /
  ``routers/*`` / ``main.py`` 一律不得 import 本模块（有回归测试守着）。
  依赖方向是单向的：本模块 → ``services.lottery``，绝不反向。
- **数据不足就说数据不足**：未开奖的期只报 ``pending``，绝不提前计分；
  引擎只输出 top-k，就明说 ``mean_rank`` / ``log_loss`` / ``brier`` 不适用
  （``null``），不拿 0 或编造的排序冒充。
- 统计只针对「本账本已冻结的 N 期」，不升格为任何全量 / 市场结论。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services import analytics as A
from services.lottery import (
    NUMBER_MAX,
    NUMBER_MIN,
    ROLE_ORDER,
    allocate_amounts,
    assign_roles,
    clamp_settings,
    effective_pick_count,
    recommend,
)

# --------------------------------------------------------------------------- #
# 常量 / 路径
# --------------------------------------------------------------------------- #
BACKEND_ROOT = Path(__file__).resolve().parents[1]
NUMBER_COUNT = NUMBER_MAX - NUMBER_MIN + 1  # 49

LEDGER_VERSION = 1
# 链的起点（第一条记录的 prev_hash）；与仓库 / 版本绑定，改了就是另一个协议
GENESIS_HASH = hashlib.sha256(b"wave-money/forward-ledger/v1/genesis").hexdigest()

DEFAULT_LEDGER_PATH = BACKEND_ROOT / "forward_ledger" / "ledger.json"
DEFAULT_SETTINGS_SNAPSHOT_PATH = BACKEND_ROOT / "forward_ledger" / "production_settings.json"

# 策略 id（英文枚举，落库 / 传输用英文码）
STRATEGY_PRODUCTION = "PRODUCTION_ENGINE"
STRATEGY_UNIFORM = "UNIFORM"
STRATEGY_FIT_DEMO = "FIT_ONLY_MARKOV_1"
STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_PRODUCTION: "线上推荐引擎（仓库默认 / 生产已存配置）",
    STRATEGY_UNIFORM: "均匀随机对照（定义随机基线本身）",
    STRATEGY_FIT_DEMO: "仅拟合演示（不是预测）",
}
ALL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_PRODUCTION,
    STRATEGY_UNIFORM,
    STRATEGY_FIT_DEMO,
)

# 诚实标记：LIVE = 线上真实路径；FIT_ONLY = 样本内拟合演示（与 max_fit 同码）
FIT_ROLE_LIVE = "LIVE"
FIT_ROLE_FIT_ONLY = "FIT_ONLY"

# 均匀对照的抽样种子盐：只与期号有关，与开奖数据无关（可复现且不可事后调参）
UNIFORM_SEED_SALT = "wave-money/forward-ledger/uniform/v1"

# 均匀参考值（1..49）
UNIFORM_MEAN_RANK = (NUMBER_COUNT + 1) / 2.0  # 25.0
UNIFORM_LOG_LOSS = math.log(NUMBER_COUNT)  # ln 49
UNIFORM_BRIER = (1.0 / NUMBER_COUNT) * (1.0 - 1.0 / NUMBER_COUNT)  # (1/49)(1−1/49)

# 默认计分显著性 / 功效（与 services.analytics 同一套口径）
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.8


class LedgerError(Exception):
    """账本层错误（数据不足 / 期号非法 / 文件损坏……）。"""


class ImmutabilityError(LedgerError):
    """试图覆盖 / 追加一条已存在期号的记录 —— 账本是 append-only，禁止改写。"""


class LookAheadError(LedgerError):
    """试图冻结一个**已经开奖**的期 —— 那是事后补冻，等同于编造预测。"""


# --------------------------------------------------------------------------- #
# 规范化 / 摘要 / 哈希
# --------------------------------------------------------------------------- #
def _stringify_keys(value: Any) -> Any:
    """递归把所有字典键转成字符串。

    必须做这一步：``json.dumps`` 会把 int 键写成字符串，而 ``sort_keys`` 对
    int 键按数值排、对读回后的字符串键按字典序排 —— 两者顺序不同，会让
    「同一份记录写盘再读回」算出**不同的 record_hash**（真实踩过的坑）。
    先统一成字符串键，序列化就对「内存对象 / JSON 往返」幂等。
    """
    if isinstance(value, Mapping):
        return {str(key): _stringify_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_keys(item) for item in value]
    return value


def canonical_json(payload: Any) -> str:
    """稳定序列化：键统一为字符串、键排序、无多余空白 —— 保证同一份数据恒得同一个摘要。"""
    return json.dumps(
        _stringify_keys(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_draws(path: str | Path) -> list[dict[str, Any]]:
    """读开奖数据并按 ``(draw_date, period)`` **升序**返回规范行。

    与 ``scripts/fit_capacity_analysis.load_draws`` 同源口径（同一份数据 → 同一序列）。
    非法特码 / 重复期号直接报错，不静默跳过（账本的输入必须唯一确定）。
    """
    file = Path(path)
    if not file.exists():
        raise LedgerError(
            f"开奖数据文件不存在：{file}（backend/data/* 被 .gitignore 忽略，需本地准备）"
        )
    raw = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise LedgerError(f"开奖数据顶层必须是数组：{file}")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in raw:
        try:
            period = int(row["period"])
            number = int(row["special_number"])
            draw_date = str(row["draw_date"])
        except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - 防脏数据
            raise LedgerError(f"开奖行缺少 period / draw_date / special_number：{row!r}") from exc
        if not NUMBER_MIN <= number <= NUMBER_MAX:
            raise LedgerError(f"特码越界（第 {period} 期）：{number}")
        if period in seen:
            raise LedgerError(f"期号重复：{period}")
        seen.add(period)
        rows.append(
            {"period": period, "draw_date": draw_date, "special_number": number}
        )
    rows.sort(key=lambda item: (item["draw_date"], item["period"]))
    return rows


def load_ledger(path: str | Path = DEFAULT_LEDGER_PATH) -> dict[str, Any]:
    """读账本；文件不存在时返回一个空账本（首次冻结会创建它）。"""
    file = Path(path)
    if not file.exists():
        return {"version": LEDGER_VERSION, "genesis_hash": GENESIS_HASH, "records": []}
    ledger = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(ledger, dict):
        raise LedgerError(f"账本顶层必须是对象：{file}")
    if int(ledger.get("version", 0)) != LEDGER_VERSION:
        raise LedgerError(
            f"账本版本不支持：{ledger.get('version')!r}（本工具只认识 {LEDGER_VERSION}）"
        )
    if ledger.get("genesis_hash") != GENESIS_HASH:
        raise LedgerError("账本 genesis_hash 与本协议不一致（可能来自另一条链）")
    if not isinstance(ledger.get("records"), list):
        raise LedgerError("账本缺少 records 数组")
    return ledger


def dump_ledger(ledger: Mapping[str, Any], path: str | Path = DEFAULT_LEDGER_PATH) -> Path:
    """原子写账本（先写 ``.tmp`` 再 replace），避免半截文件。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(dict(ledger), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(file)
    return file


def load_settings_snapshot(path: str | Path = DEFAULT_SETTINGS_SNAPSHOT_PATH) -> dict[str, Any]:
    """读已提交的「生产生效配置」快照（``{"settings": {...}}``）。"""
    file = Path(path)
    if not file.exists():
        raise LedgerError(
            f"生产配置快照不存在：{file}（可用 --settings-json 显式指定，"
            "或用 --from-store 从线上库读取当前生效配置）"
        )
    payload = json.loads(file.read_text(encoding="utf-8"))
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        raise LedgerError(f"配置快照缺少 settings 对象：{file}")
    return settings


def available_rows(
    available_last_period: int, draws: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """按「期号 <= 截止期」取可用前缀（与冻结时的口径一致，不依赖文件位置）。

    用「期号截止」而不是「取前 N 行」：这样即使之后把更早的历史补进文件，
    也不会让已冻结记录的摘要失效（补更早期号会让「前 N 行」整体错位）。
    """
    cutoff = int(available_last_period)
    rows = [
        {
            "period": int(draw["period"]),
            "draw_date": str(draw["draw_date"]),
            "special_number": int(draw["special_number"]),
        }
        for draw in draws
        if int(draw["period"]) <= cutoff
    ]
    rows.sort(key=lambda item: (item["draw_date"], item["period"]))
    return rows


def available_digest(available: Sequence[Mapping[str, Any]]) -> str:
    """可用前缀的摘要：只覆盖「策略看到的开奖」，不含配置。"""
    payload = [
        [int(row["period"]), str(row["draw_date"]), int(row["special_number"])]
        for row in available
    ]
    return sha256_hex(canonical_json(payload))


def data_digest(
    available: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    strategy_ids: Iterable[str],
) -> str:
    """``data_digest``：预测所依据的**全部输入**的摘要（开奖前缀 + 生效配置 + 策略集合）。"""
    payload = {
        "available_draws": [
            [int(row["period"]), str(row["draw_date"]), int(row["special_number"])]
            for row in available
        ],
        "settings": dict(settings),
        "strategy_ids": sorted(str(value) for value in strategy_ids),
    }
    return sha256_hex(canonical_json(payload))


def compute_record_hash(record: Mapping[str, Any], prev_hash: str) -> str:
    """重算一条记录的 ``record_hash``：覆盖除自身外的**全部**字段 + 上一条的哈希。"""
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    payload["prev_hash"] = str(prev_hash)
    return sha256_hex(canonical_json(payload))


# --------------------------------------------------------------------------- #
# 策略：线上引擎 / 均匀对照 / 仅拟合演示
# --------------------------------------------------------------------------- #
def _uniform_deck(period: int, salt: str = UNIFORM_SEED_SALT) -> list[int]:
    """确定性「均匀随机」的 1..49 全排列：种子只来自期号，与开奖结果无关。"""
    seed = int(sha256_hex(f"{salt}|{int(period)}")[:16], 16)
    deck = list(range(NUMBER_MIN, NUMBER_MAX + 1))
    random.Random(seed).shuffle(deck)
    return deck


def _matched_amounts(
    cfg: Mapping[str, Any], count: int, seed: Any = None
) -> list[int]:
    """对照策略的注码：与线上引擎**同一份预算口径**，但角色权重退化为等权。

    这样线上引擎与对照策略的 ``budget`` / ``amount_unit`` / 注数完全一致，
    差别只来自「选号」本身，而不是预算规模。
    """
    roles = assign_roles(max(1, int(count)))
    return [
        int(value)
        for value in allocate_amounts(
            str(cfg["mode"]),
            roles,
            int(cfg["total_amount"]),
            amount_unit=int(cfg["amount_unit"]),
            seed=seed,
            role_weights={role: 1.0 for role in ROLE_ORDER},
        )
    ]


def production_strategy(
    available: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    period: int,
) -> dict[str, Any]:
    """冻结**线上推荐引擎**在给定前缀下的实际输出（与 ``/api/recommend`` 同参数同口径）。"""
    cfg = clamp_settings(dict(settings))
    specials = [int(row["special_number"]) for row in available]
    dates = [str(row["draw_date"]) for row in available]
    if len(specials) < 2:
        raise LedgerError("历史不足两期，无法生成线上引擎推荐（无上一期波动可算）")

    outcome = recommend(
        latest=specials[-1],
        previous=specials[-2],
        # ``recommend`` 的契约是「最新在前」（见 services.lottery 文档）；
        # 本模块内部一律按升序前缀，所以这里显式翻转。
        history_numbers=list(reversed(specials)),
        history_dates=list(reversed(dates)),
        settings=cfg,
        mode=str(cfg["mode"]),
        period=int(period),
    )
    picks = [int(pick["number"]) for pick in outcome["picks"]]
    amounts = [int(pick["amount"]) for pick in outcome["picks"]]
    return {
        "strategy": STRATEGY_PRODUCTION,
        "label": STRATEGY_LABELS[STRATEGY_PRODUCTION],
        "fit_role": FIT_ROLE_LIVE,
        "available_length": len(specials),
        "pick_count": len(picks),
        "picks": picks,
        "amounts": amounts,
        "staked_total": int(outcome["staked_total"]),
        "odds": float(cfg["odds"]),
        "budget": int(cfg["total_amount"]),
        "mode": str(cfg["mode"]),
        # 引擎只输出 top-k，没有 1..49 完整排序 / 概率向量 → 密度指标不适用
        "ranking": None,
        "probabilities": None,
        "notes": [
            "线上引擎只输出 top-k 选号，不提供 1..49 完整排序 / 概率向量；"
            "本行的 mean_rank / log_loss / brier 记为 null（不适用），"
            "只用 hit_rate 与盈亏同均匀基线对照。",
        ],
    }


def uniform_strategy(
    period: int, settings: Mapping[str, Any], pick_count: int
) -> dict[str, Any]:
    """均匀随机对照（定义随机基线本身）：确定性抽样，与线上引擎同一份预算。"""
    cfg = clamp_settings(dict(settings))
    count = max(1, min(NUMBER_COUNT, int(pick_count)))
    deck = _uniform_deck(int(period))
    picks = deck[:count]
    amounts = _matched_amounts(cfg, count, seed=f"{UNIFORM_SEED_SALT}|amounts|{int(period)}")
    notes: list[str] = []
    if len(amounts) < len(picks):
        notes.append(
            f"预算 {cfg['total_amount']} 元 / 单位 {cfg['amount_unit']} 元不足以覆盖 "
            f"{len(picks)} 注，本对照只按可覆盖的 {len(amounts)} 注入账。"
        )
        picks = picks[: len(amounts)]
    probabilities = {number: 1.0 / NUMBER_COUNT for number in range(NUMBER_MIN, NUMBER_MAX + 1)}
    return {
        "strategy": STRATEGY_UNIFORM,
        "label": STRATEGY_LABELS[STRATEGY_UNIFORM],
        "fit_role": FIT_ROLE_LIVE,
        "available_length": None,  # 回填：均匀对照不读任何开奖数据
        "pick_count": len(picks),
        "picks": picks,
        "amounts": amounts,
        "staked_total": int(sum(amounts)),
        "odds": float(cfg["odds"]),
        "budget": int(cfg["total_amount"]),
        "mode": str(cfg["mode"]),
        "ranking": list(deck),
        "probabilities": probabilities,
        "notes": [
            "均匀随机对照：1..49 上的确定性全排列（种子只来自期号，与开奖结果无关），"
            "取前 k 个作为选号。它是「随机基线」的定义本身 —— 与它比较才算匹配口径，"
            "与教科书常数比较则可能因 k / 预算不同而不可比。",
        ],
    }


def fit_only_strategy(
    available: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    pick_count: int,
) -> dict[str, Any]:
    """仅拟合演示控制行（``FIT_ONLY``）：严格 ``series[:index]``，目标期从不进入拟合。"""
    from services import max_fit as MF  # 局部导入：只有这一条演示行需要它

    cfg = clamp_settings(dict(settings))
    series = [int(row["special_number"]) for row in available]
    prediction = MF.predict_walk_forward(
        MF.MODEL_MARKOV_1, series, len(series), max(1, int(pick_count))
    )
    picks = [int(number) for number in prediction["picks"]]
    amounts = _matched_amounts(cfg, len(picks))
    return {
        "strategy": STRATEGY_FIT_DEMO,
        "label": STRATEGY_LABELS[STRATEGY_FIT_DEMO],
        "fit_role": MF.FIT_ROLE,  # "FIT_ONLY"
        "available_length": len(series),
        "pick_count": len(picks),
        "picks": picks,
        "amounts": amounts,
        "staked_total": int(sum(amounts)),
        "odds": float(cfg["odds"]),
        "budget": int(cfg["total_amount"]),
        "mode": str(cfg["mode"]),
        "ranking": [int(number) for number in prediction["ranking"]],
        "probabilities": {
            int(number): float(value)
            for number, value in prediction["probabilities"].items()
        },
        "disclaimer": MF.DISCLAIMER,
        "notes": [
            "这是 max_fit 样本内拟合演示模型（MARKOV_1）的严格样本外输出，"
            "标为 FIT_ONLY：它的样本外表现预计与均匀基线不可区分，绝不允许进入线上推荐路径。",
        ],
    }


# --------------------------------------------------------------------------- #
# 冻结（append-only）
# --------------------------------------------------------------------------- #
def _strategy_entries(
    available: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    period: int,
    pick_count: int,
    include_fit_demo: bool,
) -> list[dict[str, Any]]:
    cfg = clamp_settings(dict(settings))
    count = effective_pick_count(str(cfg["mode"]), int(pick_count))
    entries = [
        production_strategy(available, cfg, period),
        uniform_strategy(period, cfg, count),
    ]
    # 均匀对照不读开奖，available_length 用前缀长度补齐（与其它策略同口径可比）
    entries[1]["available_length"] = len(available)
    if include_fit_demo:
        entries.append(fit_only_strategy(available, cfg, count))
    return entries


def build_record(
    *,
    period: int,
    draws: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    pick_count: int,
    include_fit_demo: bool = True,
    prev_hash: str = GENESIS_HASH,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """构造一条冻结记录（不改账本）。目标期必须晚于全部已开奖期。"""
    target = int(period)
    available = sorted(
        (
            {
                "period": int(draw["period"]),
                "draw_date": str(draw["draw_date"]),
                "special_number": int(draw["special_number"]),
            }
            for draw in draws
        ),
        key=lambda item: (item["draw_date"], item["period"]),
    )
    if not available:
        raise LedgerError("开奖数据为空，无法冻结任何期（策略无历史可用）")
    last_available = max(int(row["period"]) for row in available)
    if target <= last_available:
        raise LookAheadError(
            f"第 {target} 期已开奖（可用前缀已到第 {last_available} 期）："
            "禁止事后补冻 —— 那等于编造一条当时并不存在的预测。"
        )
    cfg = clamp_settings(dict(settings))
    entries = _strategy_entries(
        available, cfg, target, int(pick_count), bool(include_fit_demo)
    )
    available_length = len(available)
    strategy_ids = [str(entry["strategy"]) for entry in entries]
    record: dict[str, Any] = {
        "period": target,
        "frozen_at": frozen_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # period_index = 该期在「按时间升序的序列」里的下标 = 策略实际看到的期数
        "period_index": available_length,
        "available_length": available_length,
        "available_first_period": int(min(row["period"] for row in available)),
        "available_last_period": last_available,
        "available_digest": available_digest(available),
        "data_digest": data_digest(available, cfg, strategy_ids),
        "settings": cfg,
        "strategies": entries,
        "prev_hash": str(prev_hash),
        "notes": [
            "本记录在开奖前冻结：available_length = period_index = 策略实际看到的期数，"
            "目标期从不出现在输入里（无前视）。",
            f"可用前缀：第 {int(min(row['period'] for row in available))}…{last_available} 期，"
            f"共 {available_length} 期。",
            "冻结后不可改写；任何事后改动都会被 hash 链与 data_digest 查出。",
        ],
    }
    record["record_hash"] = compute_record_hash(record, record["prev_hash"])
    return record


def freeze_periods(
    ledger: Mapping[str, Any],
    draws: Sequence[Mapping[str, Any]],
    periods: Iterable[int],
    *,
    settings: Mapping[str, Any],
    pick_count: int | None = None,
    include_fit_demo: bool = True,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """冻结若干**尚未开奖**的期，返回追加后的新账本（不改原对象）。

    硬性拒绝（抛错，绝不静默覆盖）：

    - 目标期已开奖 → :class:`LookAheadError`（禁止事后补冻）；
    - 目标期已有记录 → :class:`ImmutabilityError`（append-only）。
    """
    if not draws:
        raise LedgerError("开奖数据为空，无法冻结")
    drawn_periods = {int(draw["period"]) for draw in draws}
    latest_drawn = max(drawn_periods)
    existing = {int(record["period"]) for record in ledger.get("records") or []}
    targets = sorted({int(value) for value in periods})
    if not targets:
        raise LedgerError("未指定任何要冻结的期号")

    for target in targets:
        if target <= latest_drawn:
            raise LookAheadError(
                f"第 {target} 期已开奖（最新已开奖第 {latest_drawn} 期）："
                "禁止事后补冻，只能冻结尚未开奖的期。"
            )
        if target in existing:
            raise ImmutabilityError(
                f"第 {target} 期在本账本中已有冻结记录：账本是 append-only，"
                "禁止覆盖或追加第二条。"
            )

    cfg = clamp_settings(dict(settings))
    count = int(pick_count) if pick_count is not None else int(cfg["pick_count"])
    records = list(ledger.get("records") or [])
    prev_hash = str(records[-1]["record_hash"]) if records else GENESIS_HASH
    for target in targets:
        record = build_record(
            period=target,
            draws=draws,
            settings=cfg,
            pick_count=count,
            include_fit_demo=include_fit_demo,
            prev_hash=prev_hash,
            frozen_at=frozen_at,
        )
        records.append(record)
        prev_hash = str(record["record_hash"])
    return {**dict(ledger), "version": LEDGER_VERSION, "records": records}


def next_undrawn_periods(draws: Sequence[Mapping[str, Any]], count: int) -> list[int]:
    """最新已开奖期之后的 ``count`` 个期号（按连续期号推进）。"""
    if not draws:
        raise LedgerError("开奖数据为空")
    latest = max(int(draw["period"]) for draw in draws)
    return [latest + step for step in range(1, max(1, int(count)) + 1)]


# --------------------------------------------------------------------------- #
# 校验（哈希链 + 数据摘要）
# --------------------------------------------------------------------------- #
def verify_ledger(
    ledger: Mapping[str, Any], draws: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """逐条重算 ``record_hash`` 与 ``data_digest``，返回结构化校验结果。"""
    problems: list[str] = []
    if int(ledger.get("version", 0)) != LEDGER_VERSION:
        problems.append(f"账本版本不是 {LEDGER_VERSION}：{ledger.get('version')!r}")
    if ledger.get("genesis_hash") != GENESIS_HASH:
        problems.append("genesis_hash 与本协议不一致")

    records = list(ledger.get("records") or [])
    expected_prev = GENESIS_HASH
    details: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        record_problems: list[str] = []
        period = int(record.get("period", -1))
        if str(record.get("prev_hash")) != expected_prev:
            record_problems.append("prev_hash 与上一条 record_hash 不一致（链断裂）")
        recomputed = compute_record_hash(record, str(record.get("prev_hash")))
        if str(record.get("record_hash")) != recomputed:
            record_problems.append("record_hash 重算不一致（记录内容被改写）")
        try:
            available = available_rows(int(record["available_last_period"]), draws)
            if len(available) != int(record["available_length"]):
                record_problems.append(
                    f"可用前缀期数不符：账本记 {record['available_length']}，"
                    f"按截止期重算得 {len(available)}"
                )
            if int(record["available_length"]) != int(record["period_index"]):
                record_problems.append("available_length != period_index（无前视凭证被破坏）")
            if available_digest(available) != str(record["available_digest"]):
                record_problems.append("available_digest 重算不一致（开奖前缀被改写）")
            if data_digest(
                available,
                record["settings"],
                [str(entry["strategy"]) for entry in record["strategies"]],
            ) != str(record["data_digest"]):
                record_problems.append("data_digest 重算不一致（输入数据或配置被改写）")
            leaked = [int(row["period"]) for row in available if int(row["period"]) >= period]
            if leaked:
                record_problems.append(
                    f"可用前缀包含目标期或更晚的期：{leaked}（前视 / 无未来函数被破坏）"
                )
            for entry in record["strategies"]:
                if entry.get("available_length") not in (None, int(record["available_length"])):
                    record_problems.append(
                        f"策略 {entry.get('strategy')} 的 available_length 与记录不一致"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            record_problems.append(f"重算失败：{exc!r}")

        details.append(
            {
                "period": period,
                "record_hash": record.get("record_hash"),
                "ok": not record_problems,
                "problems": record_problems,
            }
        )
        problems.extend(f"第 {period} 期：{text}" for text in record_problems)
        expected_prev = str(record.get("record_hash"))

    return {
        "ok": not problems,
        "version": ledger.get("version"),
        "genesis_hash": GENESIS_HASH,
        "records": len(records),
        "chain_root": expected_prev,
        "problems": problems,
        "details": details,
        "note": (
            "verify 只重算哈希与摘要：链根 / 记录内容 / 开奖前缀 / 配置，任何一处"
            "被事后改动都会在此暴露。"
        ),
    }


# --------------------------------------------------------------------------- #
# 计分
# --------------------------------------------------------------------------- #
def log_loss(probabilities: Mapping[Any, float] | None, actual: int) -> float | None:
    """多分类对数损失 ``−ln p(actual)``（均匀参考 = ln 49）。无概率向量时返回 None。"""
    if not probabilities:
        return None
    value = float(probabilities.get(int(actual), probabilities.get(str(int(actual)), 0.0)))
    if value <= 0.0:
        return None
    return -math.log(value)


def brier_score(probabilities: Mapping[Any, float] | None, actual: int) -> float | None:
    """逐类平均口径的多分类 Brier（均匀参考 = (1/49)(1−1/49)）。"""
    if not probabilities:
        return None
    target = int(actual)
    total = 0.0
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        value = float(
            probabilities.get(number, probabilities.get(str(number), 0.0))
        )
        expected = 1.0 if number == target else 0.0
        total += (value - expected) ** 2
    return total / NUMBER_COUNT


def score_entry(entry: Mapping[str, Any], actual: int) -> dict[str, Any]:
    """把一条冻结策略输出与真实开奖比对，产出逐期明细。"""
    picks = [int(number) for number in entry["picks"]]
    amounts = [int(value) for value in entry.get("amounts") or []]
    target = int(actual)
    hit = target in picks
    payout = 0.0
    if hit:
        position = picks.index(target)
        amount = amounts[position] if position < len(amounts) else 0
        payout = float(amount) * float(entry["odds"])
    ranking = entry.get("ranking")
    rank: int | None = None
    if ranking:
        order = [int(number) for number in ranking]
        if target in order:
            rank = order.index(target) + 1
    probabilities = entry.get("probabilities")
    spend = int(entry.get("staked_total") or sum(amounts))
    return {
        "strategy": str(entry["strategy"]),
        "pick_count": int(entry.get("pick_count") or len(picks)),
        "hit": bool(hit),
        "rank": rank,
        "log_loss": log_loss(probabilities, target),
        "brier": brier_score(probabilities, target),
        "spend": spend,
        "return": payout,
        "pnl": payout - spend,
    }


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _strategy_baseline(rows: Sequence[Mapping[str, Any]]) -> float:
    """该策略的均匀基线命中率 = k/49（k 取该策略自己的注数）。"""
    if not rows:
        return 0.0
    return sum(int(row["pick_count"]) / NUMBER_COUNT for row in rows) / len(rows)


def summarize_entries(
    rows: Sequence[Mapping[str, Any]],
    pending: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> dict[str, Any]:
    """把一个策略的逐期明细汇成指标块（含均匀参考、二项检验、样本量需求）。"""
    evaluated = len(rows)
    hits = sum(1 for row in rows if row["hit"])
    hit_rate = (hits / evaluated) if evaluated else None
    baseline = _strategy_baseline(rows) if rows else None
    ranks = [float(row["rank"]) for row in rows if row["rank"] is not None]
    losses = [float(row["log_loss"]) for row in rows if row["log_loss"] is not None]
    briers = [float(row["brier"]) for row in rows if row["brier"] is not None]
    total_spend = float(sum(int(row["spend"]) for row in rows))
    total_return = float(sum(float(row["return"]) for row in rows))

    p_greater: float | None = None
    p_two_sided: float | None = None
    power_block: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    if evaluated and baseline is not None:
        p_greater = A.binomial_tail_p(hits, evaluated, baseline, alternative="greater")
        p_two_sided = A.binomial_tail_p(hits, evaluated, baseline, alternative="two-sided")
        power_block = A.minimum_detectable_delta(
            evaluated, baseline, alpha=alpha, power=power
        )
        difference = (hit_rate - baseline) if hit_rate is not None else None
        standard_error = (
            math.sqrt(baseline * (1.0 - baseline) / evaluated) if evaluated else None
        )
        verdict = A.backtest_verdict_payload(
            evaluated=evaluated,
            hit_rate=hit_rate,
            baseline=baseline,
            difference=difference,
            standard_error=standard_error,
        )

    return {
        "evaluated": evaluated,
        "pending": int(pending),
        "hits": hits,
        "hit_rate": hit_rate,
        "mean_rank": _mean(ranks),
        "log_loss": _mean(losses),
        "brier": _mean(briers),
        "total_spend": total_spend,
        "total_return": total_return,
        "net_pnl": total_return - total_spend,
        "baseline_hit_rate": baseline,
        "uniform_reference": {
            "hit_rate": (baseline if baseline is not None else None),
            "mean_rank": UNIFORM_MEAN_RANK,
            "log_loss": UNIFORM_LOG_LOSS,
            "brier": UNIFORM_BRIER,
        },
        "binomial_p_greater": p_greater,
        "binomial_p_two_sided": p_two_sided,
        "power": power_block,
        "verdict": verdict,
        "rows": [dict(row) for row in rows],
    }


def score_ledger(
    ledger: Mapping[str, Any],
    draws: Sequence[Mapping[str, Any]],
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> dict[str, Any]:
    """对**已开奖**的冻结记录计分；未开奖的期一律记 ``pending``，绝不提前计分。"""
    drawn = {int(draw["period"]): int(draw["special_number"]) for draw in draws}
    records = list(ledger.get("records") or [])
    buckets: dict[str, list[dict[str, Any]]] = {}
    pending: dict[str, int] = {}
    scored_periods: list[int] = []
    pending_periods: list[int] = []

    for record in records:
        period = int(record["period"])
        actual = drawn.get(period)
        for entry in record["strategies"]:
            sid = str(entry["strategy"])
            buckets.setdefault(sid, [])
            pending.setdefault(sid, 0)
        if actual is None:
            pending_periods.append(period)
            for entry in record["strategies"]:
                pending[str(entry["strategy"])] += 1
            continue
        scored_periods.append(period)
        for entry in record["strategies"]:
            row = score_entry(entry, actual)
            row["period"] = period
            row["actual"] = actual
            buckets[str(entry["strategy"])].append(row)

    strategies: dict[str, Any] = {}
    for sid, rows in buckets.items():
        block = summarize_entries(rows, pending.get(sid, 0), alpha=alpha, power=power)
        block["label"] = STRATEGY_LABELS.get(sid, sid)
        strategies[sid] = block

    return {
        "scope": f"本账本已冻结 {len(records)} 期",
        "records": len(records),
        "scored_periods": sorted(scored_periods),
        "pending_periods": sorted(pending_periods),
        "drawn_periods": sorted(drawn),
        "strategies": strategies,
        "data_status": A.DATA_STATUS_OK if records else A.DATA_STATUS_INSUFFICIENT,
        "data_status_label": (
            A.DATA_STATUS_LABELS[A.DATA_STATUS_OK]
            if records
            else A.DATA_STATUS_LABELS[A.DATA_STATUS_INSUFFICIENT]
        ),
        "notes": [
            "只对已开奖的冻结期计分；未开奖的期记为 pending，绝不用「还没发生」的结果计分。",
            "均匀参考：命中 k/49、平均排名 25.0、log-loss ln 49、Brier (1/49)(1−1/49)；"
            "k 取该策略自己的注数。",
            "统计只针对本账本已冻结的期，不升格为任何全量 / 市场结论。",
        ],
    }


def status_payload(
    ledger: Mapping[str, Any], draws: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """进度 + 当前判定 + 完整性：冻结 / 待开奖 / 已计分各多少期。"""
    verification = verify_ledger(ledger, draws)
    scoring = score_ledger(ledger, draws)
    primary = scoring["strategies"].get(STRATEGY_PRODUCTION) or {}
    return {
        **scoring,
        "integrity_ok": verification["ok"],
        "chain_root": verification["chain_root"],
        "integrity_problems": verification["problems"],
        "verdict": primary.get("verdict"),
        "minimum_detectable_delta": (primary.get("power") or {}).get(
            "minimum_detectable_delta"
        ),
        "required_draws": (primary.get("power") or {}).get("required_draws"),
    }
