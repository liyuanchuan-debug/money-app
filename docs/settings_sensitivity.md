# 设置敏感度审计：这 30 个旋钮到底改变了什么

## 近期走势加权去掉（2026-10-10）

> 本节记录 **2026-10-10「近期走势加权去掉」**：与三类软降权同一模式——设置键保留、选号侧恒惰性。

| 项 | 口径 |
|---|---|
| 停用键 | `trend_bias` / `trend_window` / `trend_bias_explicit` |
| 选号影响 | **无**。`trend_sampling_weight` 恒 `1.0`；`order_pool` 不吃走势前缀；取号恒走池首 / 配额内点阵抽样；`effective_trend_bias` / `resolve_trend_bias` 恒回退 `neutral`（写 `explicit=True` 也无法恢复） |
| 仍产出（仅展示） | `trend_distributions`（波动×角色频次带）、每注 `trend_count` / `trend_note`、`number_frequency`、`trend_bias_label` |
| EV | **不变**（任意 K 个不同号命中概率仍为 K/49；单注期望仍 −2.0408 元/期） |
| 默认 | 新用户 / `clamp_settings` → `trend_bias=neutral`，`trend_window=20`（窗口只改展示） |
| 仍生效的权重 | 避冷加权（默认关）、角色金额配额（3:2:1）、波动桶配额、点阵抽样概率 |

旧敏感度表里把 `trend_bias` / `trend_window` 标成 `ACTIVE` 的行**已作废**；需要刷新表格请重跑 `--markdown`。

## 种子随机 + 点阵分布：去降权后的新选号口径（2026-10-09）

> 本节记录 **2026-10-09「降权去除 + 点阵分布化 + 期号种子随机」** 这一次选号口径变更；
> 标题以下的表格是 **2026-10-07 旧口径** 的实测快照，用于对照。旧口径里被判为
> `ACTIVE` 的 `repeat_number_weight` / `repeat_zodiac_weight` / `stale_weight`
> 三行**已经作废**（现在三者恒为惰性），`lattice_window` 也不再单独换号（见下文）。
> **2026-10-10 起** `trend_bias` / `trend_window` 同样恒为惰性（见上一节）。
> 需要刷新表格请重跑 `--markdown`（注意：脚本只生成表格段，文末的手写分析需保留）。

### 1. 三处改动

| # | 改动 | 具体口径 |
|---|---|---|
| 1 | **降权去除** | `repeat_number_weight` / `repeat_zodiac_weight` / `stale_weight` **不再参与任何排序、抽样与金额**。键仍被 `clamp_settings` 接受并原样回显（向后兼容），`soft_weight` 恒 `1.0`、`soft_penalized` 恒 `false`；`is_repeat_number` / `is_repeat_zodiac` / `is_stale` / `soft_reasons` / `periods_since_last` 与前端徽章**照旧如实产出**。保留的权重（当时）：走势加权、避冷加权、角色金额配额（3:2:1）与波动桶配额。—— **2026-10-10 起走势加权亦已停用**。 |
| 2 | **点阵只定义抽样概率** | `lattice_enabled` 默认重新开启（`DEFAULT_LATTICE_ENABLED = True`）。带内（`low ≤ \|n − latest\| ≤ high`）权重 `1.0`，带外 `1 / (1 + 距离/6)`。点阵**不再是排序键**：`wave_pass_order` 里以 `lattice_primary` 打头的门控路径已从 `recommend` 移除，`lattice_primary` 只剩展示用途。 |
| 3 | **期号种子随机抽样** | 新增设置 `pick_sampling`（英文枚举）：`seeded_random`（默认，标签「种子随机（推荐：期号确定性抽样）」）｜ `ranked`（旧口径，标签「按名次（旧：确定性排序取号）」）。`wave_alloc="balanced"` 仍是形状骨架（非空桶按最大余数均分，10 注 → 4/3/3），随机只发生在**每个桶的配额之内**。`wave_alloc="drain"` 保留为历史审计复现路径。 |

### 2. 种子公式（逐字节可复现）

```python
# backend/services/lottery.py::sampling_seed_key
payload = {
    "domain": "lottery.pick_sampling.v1",
    "latest": <最新特码>,
    "period": <期号>,                # 缺失时用 "period_fallback": "latest=..|previous=.."
    "previous": <上期特码>,
    "settings": {<SAMPLING_SEED_SETTING_KEYS 白名单键>: <生效值>, ...},  # json sort_keys=True
}
seed = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")).hexdigest()
rng = random.Random(seed)          # 只用局部 Random，绝不碰全局 random / time / os.urandom
```

白名单（只放**决定候选池与配额结构**的键）：
`pick_strategy` · `pick_sampling` · `wave_alloc` · `pick_count` · `mode` · `small_max` ·
`normal_max` · `exclude_repeat_zodiac` · `include_repeat_number`。

刻意**不入种子**的是两类：金额类（`total_amount` / `amount_unit` / `odds` / `role_w_*`）
与**所有加权旋钮**（`lattice_enabled` / `lattice_window` / `trend_bias` / `trend_window` /
`avoid_cold_*` / `score_w_*`）。理由：它们已经通过 `bucket_sampling_weight`
（点阵 × 避冷 × 走势）直接改变每个号的抽样概率；若再把它们塞进种子，就会出现
「权重其实没变、只因为窗口参数被重摇一遍」的**假敏感性**——
例如平带序列（每期 +1）上波动带恒为 `[1,1]`，`lattice_window` 无论怎么改都不该换号，
测试 `test_flat_band_series_makes_wave_window_inert` 就是这条护栏。

### 3. 确定性保证（可复现 → 前向账本才敢冻结预测）

| 场景 | 结果 |
|---|---|
| 同期 + 同设置，重算两次 | 号码 / 金额 / 角色逐字节一致（SQLite 无关、无全局状态） |
| 三个**独立进程**、`PYTHONHASHSEED=0/1/12345` | 逐字节一致（不依赖 dict/set 迭代顺序、不含对象地址与时间） |
| 只换期号 | 换样本（期号是种子主键） |
| 换**结构设置**（抽样模式 / 配额口径 / 池规模 / 重复号开关 / 注数 / 策略） | 换样本 |
| 换**加权旋钮**且权重不变（如平带序列上的 `lattice_window`） | **不换样本**（诚实：它本来就没用） |

### 4. 形状对照（208 期真实池，before = drain 旧口径，after = balanced + 种子随机）

| 指标 | `drain`（before） | `balanced + seeded_random`（after） |
|---|---:|---:|
| 每期各桶均值 小 / 常 / 大 | 8.269 / 1.000 / 0.731 | 4.346 / 3.562 / 2.091 |
| 单桶最大计数（各期最大值的均值） | 8.269 | 4.346 |
| **「单桶供给 ≥7 注」的期数** | **208 / 208 = 100.0%** | **0 / 208 = 0.0%** |
| 「单桶供给 10 注」的期数 | 0 | 0 |
| 最长连号（均值 / 最大） | 4.24 / 8 | 2.08 / 4 |
| 最大内部间隔（均值 / 最大） | 15.78 / 29 | 11.72 / 22 |
| 每期带内入选注数（均值） | 2.885 | 4.582 |
| 命中（208 期窗口） | 51 | 41 |

结论：`balanced` 配额把旧的「一个波动桶包办 8~9 注」结构彻底消掉（100% → 0%），
连号与内部间隔同时收窄；代价只是每期仍然恒 10 注、互不相同，命中概率不变。

### 5. 带内 vs 带外抽样率（点阵是不是装饰）

**口径警告**：直接比较「带内入选率 vs 全 49 号的带内占比（47.86%）」是**被配额结构混淆**的
—— 预测带在 208 期里有 182 期同时覆盖 `{normal, small}`、其余 26 期只覆盖 `{normal}`，
而 `balanced` 只给 normal 桶约 3.6 注，所以带内入选率天然低于全空间占比。
正确的对照是「**同桶内均匀**」期望：`Σ_b 配额_b × 桶 b 的带内占比`。

| 口径 | `balanced + seeded_random`（新） | `balanced + ranked`（旧） |
|---|---:|---:|
| 带内号码占全 49 号比例 | 47.86%（朴素均匀期望 4.786 注/期） | 同左 |
| **同桶均匀期望**（配额感知） | 4.123 注/期 | 4.123 注/期 |
| **实测带内入选** | **4.582 注/期（+0.459，+11.1%）** | **3.962 注/期（−0.162，−3.9%）** |
| 同桶条件概率：带内入选/可入选 | 953 / 4878 = 0.1954 | 824 / 4878 = 0.1689 |
| 同桶条件概率：带外入选/可入选 | 580 / 3312 = 0.1751 | 709 / 3312 = 0.2141 |
| 带内/带外比值 | **1.116×**（带内更容易被抽到） | 0.789×（旧口径反而避开带内） |
| `weighted_sample_distinct` 单元测量（4000 次 × 10 注） | 带内占比 0.5336 vs 均匀 0.3673 = **1.453×** | — |

**受控对照（同一份 208 期走步、同一份设置，只动点阵开关与抽样模式）** —— 这张表是
「点阵到底有没有用」最干净的答案，四个格子用的是同一个池、同一份配额：

| 口径 | 命中（208 期窗口） | 每期带内入选 | 同桶均匀期望 | 差值 |
|---|---:|---:|---:|---:|
| `wave_alloc=drain`（旧形状，用于基线对照） | 51 | 2.885 | 4.123 | **−1.238** |
| `balanced + seeded_random + 点阵 ON`（**新默认**） | 41 | **4.582** | 4.123 | **+0.459** |
| `balanced + seeded_random + 点阵 OFF`（消融） | 38 | 4.058 | 4.123 | −0.065 |
| `balanced + ranked + 点阵 ON`（旧取号） | 48 | 3.962 | 4.123 | −0.162 |

读法：**只有「点阵 ON + 种子随机」这一格把带内入选推到同桶均匀期望之上**（+0.459）；
把点阵关掉（4.058）或换成旧的 `ranked` 取号（3.962），实测立刻回落到均匀期望
（4.123）附近甚至之下。也就是说点阵偏置**不是装饰**：它确实改变了 208 期里被抽中的
是哪些号。四个格子的命中数（51 / 41 / 38 / 48）全部落在 `±2 SE ≈ ±11.6 期` 的抽样噪声内
（0 优势期望 42.45、sd 5.81），差异只是同一份固定路径上的不同随机样本，**不构成任何
边际优势**。

结论：点阵**不是装饰**。在结构完全相同的两条路径里，开启点阵加权使带内入选
比「同桶均匀」高约 `+11%`、比旧 `ranked` 口径高约 `+16%`（4.582 vs 3.962 注/期）；
`ranked` 路径反而**低于**均匀期望，因为确定性「取最分散」系统性避开预测带。

### 6. 期望值：完全没变

选号口径怎么改都改不动定价。任意 `N` 个不同号的单期命中概率恒为 `N/49`，
期望值恒为 `50 × (47/49 − 1) = -2.0408 元/期`（`-4.0816%`）。
**唯一能改真实盈亏的是投入金额**，不是选号方法、权重、点阵或种子。
本节所有命中数字都只是**同一份固定路径上的一个随机样本**，不构成任何边际优势声明。

---

口径：本池已导入 210 期数据内（样本仅 210 期，禁止外推）。本报告只统计**样本内**走步回测，禁止升格为全量 / 市场结论。

- 样本期数：210；可评估期数：208
- 命中数变化 **不等于** 命中概率变化；任何权重都改不动 `有效注数 / 49`。
- 期望值恒为 `赔率 / 49 - 1`，与选号方法、权重、注数、注码无关。

## 现场配置的样本内命中率与统计功效

- 命中 **38 / 208** = `18.2692%`；随机基线 `20.4082%`（10 注 / 49）
- 差 `-2.14pp`；引擎判定 `noise`（within_noise = True）
- 精确二项双侧 p = `0.4916`；单侧(greater) p = `0.8015`
- 标准误 `0.0279`（2SE = `0.0559`）；MDD(80% 功效) = `0.0783`
- 80% 功效所需期数：+1% → 12750，+2% → 3188，+5% → 510，+10% → 128，盈亏平衡(50/235, +0.87pp) → 16905
- 期望值：`ev_per_100 = 100 * (47 / 49 - 1) = -4.0816`

## 各配置下的逐项分类

### 配置 `live`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 38→0、8、19 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 203 | 38→42 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 185 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 38→56 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 160 | 38→36、38、39、40 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 38→3、38 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 110 | 38→41、43、51 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 38→19、21、31、35 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 38→43 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 208 | 38→35、36、43、47、49 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 141 | 38→39、40、41 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 117 | 38→38、40 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 38→3、19、35、38 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 207 | 38→37、38 | 号码/注数改变（经 number_selection） |
| `trend_bias_explicit` | ACTIVE | 207 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `trend_window` | ACTIVE | 169 | 38→34、36、37、38 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 38→38（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 38→38（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `repeat_zodiac_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 snapshot 下改变号码（127 期） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |

### 配置 `snapshot`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 46→0、8、19 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 58 | 46→46（不变） | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 57 | 46→40 | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 46→48 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 152 | 46→35、40、45、46 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 46→5、46 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 160 | 46→42、46、47 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 46→19、24、31、40 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 46→42 | 号码/注数改变（经 number_selection） |
| `repeat_zodiac_weight` | ACTIVE | 127 | 46→41、43 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 202 | 46→39、42、43 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 189 | 46→37、39、42 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 143 | 46→41、46 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 46→5、19、40、46 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 208 | 46→46、47、48 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 46→46（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 46→46（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `trend_bias_explicit` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 live 下改变号码（207 期） |
| `trend_window` | INERT_UNDER_CURRENT_CONFIG | 0 | 46→46（不变） | 当前配置下无效；在配置 live 下改变号码（169 期） |

### 配置 `naked`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 50→0、5、20 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 208 | 50→43 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 208 | 50→51 | 号码/注数改变（经 number_selection） |
| `include_repeat_number` | ACTIVE | 208 | 50→51 | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 50→37 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 50→2、50 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 208 | 50→49、51 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 50→20、24、37、40 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 50→37 | 号码/注数改变（经 number_selection） |
| `repeat_number_weight` | ACTIVE | 208 | 50→51 | 号码/注数改变（经 number_selection） |
| `repeat_zodiac_weight` | ACTIVE | 79 | 50→50（不变） | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 208 | 50→46、48、49、52、54 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 150 | 50→45 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 50→2、20、40、50 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 208 | 50→43、45、47 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 50→50（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `lattice_window` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 live 下改变号码（160 期） |
| `odds` | COSMETIC | 0 | 50→50（不变） | 号码不变（payout_only） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `stale_periods` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 live 下改变号码（141 期） |
| `trend_bias_explicit` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 live 下改变号码（207 期） |
| `trend_window` | INERT_UNDER_CURRENT_CONFIG | 0 | 50→50（不变） | 当前配置下无效；在配置 live 下改变号码（169 期） |

### 配置 `score_top`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 43→10、27 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 198 | 43→43（不变） | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 171 | 43→42 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 129 | 43→42、43、44 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 43→4、43 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 134 | 43→44、46 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 43→27、29、40 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 43→38 | 号码/注数改变（经 number_selection） |
| `score_w_diff` | ACTIVE | 183 | 43→45、46 | 号码/注数改变（经 number_selection） |
| `score_w_focus` | ACTIVE | 199 | 43→42、46 | 号码/注数改变（经 number_selection） |
| `score_w_mid` | ACTIVE | 206 | 43→47、50 | 号码/注数改变（经 number_selection） |
| `score_w_omit` | ACTIVE | 199 | 43→43、54 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 195 | 43→43、44、46、47 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 198 | 43→44、45 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 149 | 43→40、42、45 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 43→4、27、40、43 | 号码/注数改变（经 number_selection） |
| `trend_window` | ACTIVE | 200 | 43→39、41、49、51 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `avoid_cold_enabled` | COSMETIC | 0 | 43→43（不变） | 号码不变（amount_allocation） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 43→43（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 43→43（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `repeat_zodiac_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 snapshot 下改变号码（127 期） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `trend_bias` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 live 下改变号码（207 期） |
| `trend_bias_explicit` | INERT_UNDER_CURRENT_CONFIG | 0 | 43→43（不变） | 当前配置下无效；在配置 live 下改变号码（207 期） |

### 配置 `avoid_cold_on`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 42→0、7、16 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | ACTIVE | 194 | 42→37、39、42 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 203 | 42→38 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 164 | 42→36 | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 42→48 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 144 | 42→37、38、41、42、43 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 42→3、42 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 110 | 42→40、44、47、50 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 42→16、22、29、34 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 42→43 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 208 | 42→35、36、42、44、45 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 122 | 42→40、42 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 98 | 42→41、42 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 42→3、16、34、42 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 207 | 42→35、40、41 | 号码/注数改变（经 number_selection） |
| `trend_bias_explicit` | ACTIVE | 207 | 42→35 | 号码/注数改变（经 number_selection） |
| `trend_window` | ACTIVE | 160 | 42→34、35、39 | 号码/注数改变（经 number_selection） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 42→42（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 42→42（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `repeat_zodiac_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 snapshot 下改变号码（127 期） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 42→42（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |

### 配置 `legacy_no_total`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 38→0、8、19 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 203 | 38→42 | 号码/注数改变（经 number_selection） |
| `bet_unit` | ACTIVE | 208 | 38→8、21、38 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 185 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 38→56 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 160 | 38→36、38、39、40 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 38→3、38 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 110 | 38→41、43、51 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 38→19、21、31、35 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 38→43 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 208 | 38→35、36、43、47、49 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 141 | 38→39、40、41 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 117 | 38→38、40 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 38→3、19、35、38 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 207 | 38→37、38 | 号码/注数改变（经 number_selection） |
| `trend_bias_explicit` | ACTIVE | 207 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `trend_window` | ACTIVE | 169 | 38→34、36、37、38 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `big_min` | DEAD | 0 | 38→38（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 38→38（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `repeat_zodiac_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 snapshot 下改变号码（127 期） |
| `role_w_defense` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_primary` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `role_w_secondary` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下金额也不变（无余量 / 无预算差异）；在配置 slack_budget 下改变金额（不影响号码） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |

### 配置 `slack_budget`

| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |
|---|---|---:|---|---|
| `amount_unit` | ACTIVE | 208 | 38→3、16、38 | 号码/注数改变（经 number_selection） |
| `avoid_cold_enabled` | ACTIVE | 203 | 38→42 | 号码/注数改变（经 number_selection） |
| `exclude_repeat_zodiac` | ACTIVE | 185 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `lattice_enabled` | ACTIVE | 208 | 38→56 | 号码/注数改变（经 number_selection） |
| `lattice_window` | ACTIVE | 160 | 38→36、38、39、40 | 号码/注数改变（经 number_selection） |
| `mode` | ACTIVE | 208 | 38→3、38 | 号码/注数改变（经 number_selection） |
| `normal_max` | ACTIVE | 110 | 38→41、43、51 | 号码/注数改变（经 number_selection） |
| `pick_count` | ACTIVE | 208 | 38→19、21、31、35 | 号码/注数改变（经 number_selection） |
| `pick_strategy` | ACTIVE | 208 | 38→43 | 号码/注数改变（经 number_selection） |
| `small_max` | ACTIVE | 208 | 38→35、36、43、47、49 | 号码/注数改变（经 number_selection） |
| `stale_periods` | ACTIVE | 141 | 38→39、40、41 | 号码/注数改变（经 number_selection） |
| `stale_weight` | ACTIVE | 117 | 38→38、40 | 号码/注数改变（经 number_selection） |
| `total_amount` | ACTIVE | 208 | 38→3、19、35、38 | 号码/注数改变（经 number_selection） |
| `trend_bias` | ACTIVE | 207 | 38→37、38 | 号码/注数改变（经 number_selection） |
| `trend_bias_explicit` | ACTIVE | 207 | 38→38（不变） | 号码/注数改变（经 number_selection） |
| `trend_window` | ACTIVE | 169 | 38→34、36、37、38 | 号码/注数改变（经 number_selection） |
| `avoid_cold_days` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 avoid_cold_on 下改变号码（194 期） |
| `bet_unit` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 legacy_no_total 下改变号码（208 期） |
| `big_min` | DEAD | 0 | 38→38（不变） | 不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明 |
| `include_repeat_number` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `odds` | COSMETIC | 0 | 38→38（不变） | 号码不变（payout_only） |
| `repeat_number_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 naked 下改变号码（208 期） |
| `repeat_zodiac_weight` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 snapshot 下改变号码（127 期） |
| `role_w_defense` | COSMETIC | 0 | 38→38（不变） | 号码不变（amount_allocation） |
| `role_w_primary` | COSMETIC | 0 | 38→38（不变） | 号码不变（amount_allocation） |
| `role_w_secondary` | COSMETIC | 0 | 38→38（不变） | 号码不变（amount_allocation） |
| `score_w_diff` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（183 期） |
| `score_w_focus` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |
| `score_w_mid` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（206 期） |
| `score_w_omit` | INERT_UNDER_CURRENT_CONFIG | 0 | 38→38（不变） | 当前配置下无效；在配置 score_top 下改变号码（199 期） |

## 分类口径

- `ACTIVE`：有效：扰动即改变号码或有效注数
- `COSMETIC`：仅展示/筹码：号码不变，只改金额或兑付
- `INERT_UNDER_CURRENT_CONFIG`：当前配置下失效：被开关挡住，换配置才会生效
- `DEAD`：死键：任何配置都无法影响输出

## 默认配置该不该改？（真金白银视角）

口径：本节只算**本池已导入的 210 期样本**（可评估 208 期）里的真金白银账，不做任何外推；
也不构成任何「能提高中奖概率」的说法。

### 保本线是 21.2766%，不是 20.4082%

现场票面：10 注、`total_amount=50`、`amount_unit=5`、`mode=even` → 每注 5 元；`odds=47` 是**含本金**的兑付倍数。
一中：`47 × 5 = 235` 元，扣掉 50 元成本净 `+185`；未中净 `-50`。所以命中率必须跨过
`50 / 235 = 21.2766%` 才不亏（随机基线 `10 / 49 = 20.4082%`，比保本线低 0.8684pp）。
期望值恒为 `50 × (47/49 − 1) = −2.0408` 元/期 = 投注额的 `−4.0816%` ——
**与选号方法、权重、点阵、配注全都无关**（见 `services/pick_ticket.py` 顶部声明）。

### 三个配置的比分都落在噪声带里

零边（完全随机）下 208 期命中数 ≈ `Binomial(208, 10/49)`：均值 `42.45`、**sd = 5.81**。

| 配置 | 命中 | 命中率 | 208 期净盈亏（10 注 × 5 元 × odds 47） |
|---|---:|---:|---:|
| 线上配置（点阵开：`small_max=15` / `normal_max=20` / `trend_bias=mid` / `exclude_repeat_zodiac=true`） | 38/208 | 18.2692% | −1470 |
| 代码默认旋钮（点阵开；`small_max=10` / `normal_max=30` / `trend_bias=neutral` / `exclude_repeat_zodiac=false`） | 46/208 | 22.1154% | +410 |
| 代码默认旋钮 + 点阵关（**同上**配置，仅 `lattice_enabled=false`） | 48/208 | 23.0769% | +880 |
| 线上配置 + 点阵关（`small_max=15` / `normal_max=20` / `trend_bias=mid` / `exclude_repeat_zodiac=true`，仅 `lattice_enabled=false`） | 56/208 | 26.9231% | +2760 |
| 波动预登记变体（180 期） | 48/180 | 26.6667% | +2280 |
| 波动网格冠军（180 期，事后挑出） | 51/180 | 28.3333% | +2985 |
| 均匀随机基线（理论） | 42.45/208 | 20.4082% | −424.5 |

线上配置的 −1470 离随机基线只有 3.0074pp（`z = −0.77`、`p = 0.44`），**不显著**。

零边下 208 期净盈亏本身就是一条很宽的分布：均值 −424.5、sd 1366.0 → **95% 区间 [−3156, +2307]**；
而且**一个完全没用的策略有 35.66% 的概率在 208 期里是赚钱的**（P(命中 ≥ 45)）。
也就是说，「回测赚了还是亏了」在这里分不出真优势和运气。

两条校正注意，专门用来给「看起来赢」的格子降温：

- 「线上配置 + 点阵关」的 +2760 原始 p = 1.44%，但这个格子是从 **30 个设置的扫描**里事后挑出来的，
  不能按单次检验读；而且它属于 `small_max=15 / normal_max=20 / trend_bias=mid / exclude_repeat_zodiac=true`
  这套**旧配置**，不是与「代码默认点阵开」的干净对照 —— 同配置内 ON/OFF 只差 2 期（见下文
  「点阵为什么会把 10 注押成一个号」）。**禁止把 56 与 46 并排当作「点阵开→关」的效果。**
- 波动变体的原始 p = 2.59%，在 **87 条变体**上做 Holm 校正后 = 0.149，一条都活不下来。

### 结论：恢复默认是「卫生 / 先验」决定，不是「赚钱」决定

期望值恒为 `−4.0816%`，**任何配置都不会改变长期盈亏**。把引擎旋钮恢复成代码默认
（`small_max=10` / `normal_max=30` / `trend_bias=neutral` / `exclude_repeat_zodiac=false`）的理由是：

- **卫生**：回到有测试断言、可复现的基准，而不是某次调参残留；
- **先验**：没有任何证据支持「线上那套 15 / 20 / mid / 排除重肖」更好。

它**不是**一个收益决定。把它讲成「换回默认就更能赚」，就是用噪声自我欺骗。
（`pick_count=10` 是用户显式偏好，保持不动，不回落代码默认的 6 注。）

### 一个确凿的真 bug：开了重肖排除，两个软降权权重直接失效

`exclude_repeat_zodiac=true` 时，`build_candidate_pools`（`lottery.py:708`）会把**上期特码的整个同肖组**
（`zodiac_group` 同组，最多 5 个号，包含上期特码本身）从候选池里直接剔除：

```python
if exclude_repeat_zodiac and zodiac_group(number) == latest_group:
    continue
```

而 `repeat_number_weight` / `repeat_zodiac_weight` 只在 `soft_penalty_weight` 里对**池内**的这些号生效
（`lottery.py:838` 重号、`lottery.py:841` 同肖）——号已经被踢出池子，权重永远没有机会应用：

- `repeat_number_weight`（重号软降权）：**失效**；
- `repeat_zodiac_weight`（同肖软降权）：**失效**；
- 连带 `include_repeat_number`（是否保留重号，`lottery.py:706`）：被这条排除整体盖过。

换句话说，线上配置下的取号结果与「把这两个权重设成 0 / 0.8 / 1.0 中的任何值」完全无关；
只有关掉 `exclude_repeat_zodiac`（回到代码默认），这两个旋钮才重新参与排序与金额分配。
上文逐项分类里 `repeat_number_weight` / `repeat_zodiac_weight` 标为
`INERT_UNDER_CURRENT_CONFIG`，机制就是这条。

## 点阵为什么会把 10 注押成一个号

口径：本节只解释机制与 208 期样本内的确定性算术，不构成任何「提高命中率」的说法。

### 机制：绝对差值 + 「先取满一个波动桶」

- 基准是**绝对差**（不是环形差）：`diff = abs(number - latest)`
  （`lottery.py:716` 候选池、`lottery.py:970` 点阵行）。
- 带内判定 = `low <= diff <= high`（`lottery.py:972`）；带内权重 1.0，带外按离带边缘的距离衰减
  `1 / (1 + distance / LATTICE_DECAY_SCALE)`，`LATTICE_DECAY_SCALE = 6.0`
  （`lottery.py:920-934`、`:166`）。注意：**带外号码没有被排除**，只是权重变小。
- 真正「押一个桶」的是 `lattice_primary_wave()`（`lottery.py:936-957`）：它数出预测带里整数差值
  落在哪一类波动桶最多并返回该桶；取号循环（`lottery.py:2344` 的 `wave_pass_order`、
  `:2352-2357` 的点阵路径）先在**这个桶里取满 10 注**，不够才向其余桶扩散。
- 结果：208 期里**每一期**的 10 注都取自同一个波动桶，等于整张票押在一次
  「下期差值是否落在预测带内」。

### 后果：双峰——约 51.4% 的期数被结构性判负

条件在下期 `|next − latest|` 是否落进预测带：

| 下期差值是否落在预测带内 | 命中 | 期数 | 命中率 |
|---|---:|---:|---:|
| 带内 | 46 | 99 | 46.46% |
| 带外 | 0 | 109 | 0.00% |

即 `107/208 = 51.4%` 的期数，开奖号一出带就已经注定 0 命中 —— 不是选得差，是整张票压在了带外。
反过来也解释了「看着像赢」的格子：命中时往往全票命中。

### 号码跨度：被压缩到 21–38 的窄区间

| 口径 | 号码跨度均值 | 号码跨度中位数 | 平均带内注数（满分 10） |
|---|---:|---:|---:|
| 点阵 ON | 25.11 | 19.5 | 9.885 |
| 点阵 OFF | 33.84 | 36.5 | 3.269 |
| 均匀随机 10-of-49（理论） | 40.93 | 42.0 | — |

点阵 ON 时平均近 10 注全在带内，号码自然挤在预测线附近的 21–38 区间；这正是用户看到
「号码不对 / 都挤在一起」的原因。

### 校正：同配置内 ON/OFF 只差 2 期

- **代码默认配置**（`small_max=10` / `normal_max=30` / `trend_bias=neutral` /
  `exclude_repeat_zodiac=false`）：点阵 ON `46/208`、点阵 OFF `48/208` —— 只差 **2 期**，
  远在噪声带内（sd 5.81）。
- `56/208` 属于**另一套旧配置**（`small_max=15` / `normal_max=20` / `trend_bias=mid` /
  `exclude_repeat_zodiac=true`）：同配置内 ON `38/208`、OFF `56/208`。它**不是**与代码默认的
  对照，**禁止**把「56 vs 46」并排当作点阵开关的效果。

### 结论：开关不动钱，动钱的是投注额

期望值恒为 `−4.0816%`（`−2.0408 元/期`），与点阵开关、走势、权重全都无关。点阵只改变下注的
**形状**：ON = 单桶集中、双峰分布；OFF = 10 个号摊得更开、更接近均匀覆盖。这是关于
「下注分布形状」的偏好，**不是**优势。真正改变长期盈亏的是**总投注额**（`total_amount`）与
买了多少期——投入翻倍，期望亏损线性翻倍。

**2026-10-07：点阵已按用户偏好关闭（线上 `lattice_enabled=false`，代码默认同步改为 `False`）。**

## 点阵关掉后为什么还是一坨（波动桶「逐桶取满」）

用户第二次反馈：点阵关了，出号仍然「全都集中在一人堆」。当期的线上票面（第 280 期，最新 = 10）是

```text
05 08 11 12 14 18 19 20 | 38 | 42
```

即 8 注落在 `[5,20]`、`[21,37]` 整段空档、只有 2 个高位离群号。**这不是点阵问题，是波动桶的分配问题。**

### 机制：波动桶是以「上一期特码」为圆心的连续区段

分桶用的是**绝对差**（`classify_wave`，`services/lottery.py` 的 `diff = abs(number - latest)`
候选池入口与 `classify_wave(diff, small_max, normal_max)`）：

| 桶 | 判据 | `latest=10` 的号段 | `latest=25` 的号段 | `latest=45` 的号段 |
|---|---|---|---|---|
| 小波动 `small` | `\|n − latest\| ≤ 10` | `1..20`（20 个） | `15..35`（21 个） | `35..49`（15 个） |
| 常规 `normal` | `11 ≤ \|n − latest\| ≤ 30` | `21..40`（20 个） | `1..14 ∪ 36..49`（28 个） | `15..34`（20 个） |
| 大跳 `big` | `\|n − latest\| > 30` | `41..49`（9 个） | 空（最远只有 24） | `1..14`（14 个） |

关键点：**桶边界随上一期特码移动，且每个桶都是一段连续区间。** `latest` 靠近低位时小波动桶压在低位
（最新 = 10 → `1..20`），靠近高位时压在**高位**（最新 = 45 → 小波动桶变成 `35..49`）。所以「一坨」出现在
哪一端，完全由上一期特码的位置决定——不是算法偏爱低位。

### 机制：旧口径会「先把第一个桶抽干」

旧取号循环（`wave_alloc="drain"`，`services/lottery.py` 的 `recommend()` 非点阵分支）分两步：

1. **决策 1**：按 `WAVE_ORDER = [small, normal, big]` 每个桶取 1 注；
2. **决策 2**：再按同一个 `WAVE_ORDER`，**从第一个仍有候选的桶里连续取满**，直到凑够注数。

因为小波动桶恒定有 ≥11 个候选号（`small_max=10` 时最少 11 个），决策 2 每次都从 `small` 开始抽，
于是 10 注固定切成 **8 / 1 / 1**（`big` 为空时是 **9 / 1 / 0**）。当期票面 8 个小波动号、1 个常规号
（38，差值 28）、1 个大跳号（42，差值 32），正是这个分配的结果；
`WAVE_ORDER` 的桶顺序——而不是角色配额（主推/次选/防守 3:2:1）或桶内遗漏排序——决定了每桶注数。
角色配额只管**金额**，桶内遗漏排序只管**取哪几个号**。

### 208 期实测：100% 的期数都会出现「一个桶抽走 ≥ 8 注」

口径：`data/draws_70_279.json`（210 期，逐期走步回测 208 期），线上设置
（`small_max=10` / `normal_max=30` / `trend_bias=neutral` / `lattice_enabled=false` / `pick_count=10`）。

| 形状指标（208 期） | 旧口径 `drain`（均值 / 中位数） | 新口径 `balanced`（均值 / 中位数） |
|---|---:|---:|
| 单个桶的最大注数 | **8.27 / 8** | 4.35 / 4 |
| 最大连续号段长度 | 3.51 / 3 | 1.43 / 1 |
| 票内最大空档 | 15.81 / 17 | 9.12 / 10 |
| 号码跨度 `max − min` | 33.84 / 36.5 | 47.23 / 48 |
| `[1,20]` 注数 | 4.33 / 4 | 4.26 / 4 |
| `[21,40]` 注数 | 4.00 / 4 | 3.41 / 3 |
| `[41,49]` 注数 | 1.67 / 1 | 2.33 / 2 |

| 频率（208 期） | 旧口径 `drain` | 新口径 `balanced` |
|---|---:|---:|
| **一个桶抽走 ≥ 7 注** | **100.00%** | **0.00%** |
| 一个桶抽走 ≥ 6 注 | 100.00% | 0.00% |
| 最大连续号段 ≥ 5 | 18.75% | 0.00% |
| 最大连续号段 ≥ 4 | 39.42% | 5.29% |
| 票内最大空档 ≥ 12 | 73.56% | 1.92% |
| 三个号段（低/中/高）都有号 | 56.73% | 100.00% |

旧口径下「一个桶抽走 8/9 注」的直方图是 `8 注:152 期、9 注:56 期`（合计 208），即**每一期**都退化成
「一坨 + 离群点」；新口径的直方图是 `4 注:136 期、5 注:72 期`，最多相差 1 注。

### 修正：`wave_alloc`（均衡分散，默认）

新增设置 `wave_alloc`（英文枚举 `balanced` / `drain`，代码默认 `balanced`；汉字标签见设置页）：

- **配额**：按「非空波动桶均分 + 最大余额法」分注数（10 注 → `4/3/3`；某桶装不下时余量摊给其它桶；
  空桶不参与分配），配额之和恒等于请求注数，**绝不因此少出号**；
- **轮转取号**：按 `WAVE_ORDER` 轮转，每个桶轮到就取 1 注，桶内改挑**「离已选号码最远」**的号
  （`pick_most_spread`，并列时保持池内既有顺序：点阵 → 软降权 → 遗漏）；
- **确定性**：整个过程无随机数，同一组入参 → 逐字节一致（前向台账要求）；
- **金额口径不变**：角色顺位、主推/次选/防守 3:2:1 配额、避冷/软降权打折全部原样保留；
- **只作用于点阵关闭的波动轮取路径**；点阵开启时的「预测波动桶取满」路径一字未改。

需要回退旧行为时，把 `wave_alloc` 设回 `drain` 即可（旧路径保留、未删除）。

### 修正后 208 期命中：48 → 52，仍在噪声带内（**不是**变强）

同一份 208 期、同一套设置，只换 `wave_alloc`：

| 口径 | 命中 / 评估 | 命中率 | 判决 |
|---|---:|---:|---|
| `drain`（旧） | 48 / 208 | 23.0769% | noise |
| `balanced`（新） | 52 / 208 | 25.0000% | noise |

**必须如实说明**：命中数移动了 +4 期，看似超过「±2 期」的经验阈值，但这是**换了一批号码**之后
在同一条历史路径上的另一次抽样，不是优势：

- 208 期里两个口径命中/不命中的**分歧期有 72 期**（旧口径独中 34 期、新口径独中 38 期）——两个
  号码集合在大多数期上命中的根本不是同一批期，+4 只是这 34 与 38 的净差；
- 同一条历史路径上「随便买 10 个不同号」的命中数，蒙特卡洛 4000 次的分布是
  **均值 42.37、标准差 5.78**（5%~95% 分位 `33..52`）。48 落在第 81 百分位、52 落在第 94 百分位，
  两者相差 4 期不到 0.5 个标准差；
- 同一条历史里「下期实际落在哪个桶」的频率与小/常/大桶本身的大小几乎一致
  （实际 37.50% / 48.08% / 14.42% vs 池内占比 36.37% / 48.37% / 15.26%）——
  **没有任何桶在这段历史里被系统性高估或低估**，集中押桶既不占便宜也不吃亏。
- 判决字段也是 `kind=noise`（+4.59pp，样本标准误 2.79pp，未超 2 个标准误）。

因此**没有**为了命中数去调参数：形状修正按形状本身评价（`≥7 注同桶` 从 100% 降到 0%），
命中率仍按噪声处理，真正的检验是前向台账，不是这 208 期的回看。

### 期望值不变：唯一真金白银的杠杆仍是投注额

任意 10 个**不同**号码的命中概率恒为 `10/49`，每期期望值恒为
`50 × (47/49 − 1) = −2.0408 元`。`balanced` 只是换了一批等价的 10 个号，**没有也不能**改变
P(hit) 或 EV——这一点与点阵开关、走势加权、避冷、角色配额完全一样。真正决定长期盈亏的仍然只有
**总投注额**（`total_amount` / `amount_unit`）与买了多少期。

## 命中率是覆盖率的函数，不是本事

口径：本节只做确定性算术，不引用任何样本数据；表内每个数都能用 `N/49`、`N/47`、`N·u` 当场复算，
禁止当成任何「优势」或「外推」结论。

### 随机命中率恒为 N/49，保本命中率恒为 N/47

公平 49 号彩：每期买 N 注、每注 u 元（现场即 N=10、u=5）。

- **公平（随机）命中率 = N/49**：它只是「你覆盖了 49 个号里的几个」的算术结果——与选号方法、权重、
  点阵、走势、运气全都无关。买 5 个号就是 10.2041%，买 49 个号就是 100.0000%，不需要任何本事。
- **保本命中率 = N·u / (47·u) = N/47**：赔率 47 含本金，中一注返 `47·u`，未中净亏当期成本 `N·u`，
  所以必须跨过 `N·u / (47·u) = N/47`。
- 两者之比恒为 `(N/47) / (N/49) = 49/47`，即保本线永远高出 `49/47 − 1 = 4.2553%`（相对值）。
  **这是把房子的抽水换算成「命中率要求」，任何注数都改不动它。** 换成百分点是 `N/47 − N/49 = 2N/2303`，
  随 N 线性放大——看起来「差距越来越大」，其实相对差一直是那 4.2553%。
- **每期期望值 = 投注额 × (47/49 − 1) = 投注额 × −4.0816%**，与 N、u、选号方法统统无关。

单位 u = 5 元、赔率 47（中一注返 235 元）的完整对照：

| 注数 N | 每期投入 | 公平命中率 N/49 | 保本命中率 N/47 | 差距（百分点） | 每期期望盈亏 | 期望年盈亏（365 期） |
|---:|---:|---:|---:|---:|---:|---:|
| 5 | 25 | 10.2041% | 10.6383% | 0.43 | −1.020 | −372 |
| 10 | 50 | 20.4082% | 21.2766% | 0.87 | −2.041 | −745 |
| 15 | 75 | 30.6122% | 31.9149% | 1.30 | −3.061 | −1117 |
| 20 | 100 | 40.8163% | 42.5532% | 1.74 | −4.082 | −1490 |
| 25 | 125 | 51.0204% | 53.1915% | 2.17 | −5.102 | −1862 |
| 30 | 150 | 61.2245% | 63.8298% | 2.61 | −6.122 | −2235 |
| 40 | 200 | 81.6327% | 85.1064% | 3.47 | −8.163 | −2980 |
| 49 | 245 | 100.0000% | 104.2553% | 4.26 | −10.000 | −3650 |

### 买满 49 个号：命中率 100%，却每期稳亏 10 元

买下全部 49 个号，命中率是铁定的 100.0000%——每期都中。但每期投入 `49 × 5 = 245` 元，中一注只返
`47 × 5 = 235` 元，**每期净亏 245 − 235 = −10 元**，一年（365 期）稳亏 **−3650 元**。

要想不亏，保本命中率得到 `49/47 = 104.2553%`——命中率不可能超过 100%，所以这个洞永远补不上。
这正是把「命中率」当本事看的荒谬之处：**百分百命中率照样每期亏钱**，因为命中率只反映覆盖率，不反映定价。

### 当前 10 注位置：观测 22.1154%，只比 20.4082% 的覆盖率线高一口气

N=10、208 期：公平线 `10/49 = 20.4082%`，保本线 `10/47 = 21.2766%`，观测命中率 `46/208 = 22.1154%`。

- 比公平线高 `+1.71pp`，比保本线只高 `+0.84pp`；
- 命中率自身的标准差 `√(0.204082 × 0.795918 / 208) = 2.79pp`，所以这 `+1.71pp` 折合 `z = 0.61`、
  `p = 0.54`——**纯噪声**，连「比随机好」都说不上。

### 波动法标的 26.67% 只是同一把尺子上的另一个刻度

波动法自报 `48/180 = 26.6667%`，看着比 22.1154% 高不少，但它**用的是同一个 10 注口径**：保本线同样是
`10/47 = 21.2766%`，它只比保本线高 `+5.39pp`。换个注数（比如 20 注），公平线和保本线会一起抬到
40.8163% / 42.5532%，同一个 26.67% 立刻变成「低于公平线」。

**命中率离开注数就没有意义。** 跨不同注数比命中率，等于拿 5 个号的 10% 去比 49 个号的 10%——分母根本
不是一回事。要比就比同一注数下的「命中率 − N/49」，或者干脆比「每期期望盈亏」（永远是 −4.0816% × 投入）。

## 复现

```powershell
cd backend
.\\.venv\\Scripts\\python.exe scripts\\settings_sensitivity.py `
    --draws-json data\\draws_70_279.json --out ..\\.tmp-wave\\settings_sensitivity.json
```
