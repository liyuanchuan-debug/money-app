# 波浪买入法：引擎实际规则（从代码推导）

> 口径声明：本文描述的是**本池（本地已导入的 `data/draws_70_279.json`，210 期）样本内**
> 的选号 / 分配规则，全部引用 `backend/services/lottery.py`（下称 `lottery.py`）的真实行号。
> **没有任何一节声称这些规则能提高中奖概率**：49 个号等概率，单注命中概率恒为 `注数 / 49`，
> 赔率 47 时每 100 元期望盈亏恒为 `47 / 49 − 1 = −4.0816%`（见 `services/pick_ticket.py:1-21`）。
> 文中的「偏好 / 权重 / 打分」都是**样本内排序启发式**，不是概率模型。

> **⚠️ 2026-10-09 机制变更后的阅读须知**：以下章节记录的是**变更前**的机制。自 2026-10-09 起：
> ① 三类软降权（重号 0.5 / 同肖 0.8 / 冷号 0.3）**不再参与任何排序、抽样与金额**（设置键保留但恒等失效，
> `soft_weight` 恒 `1.0`、`soft_penalized` 恒 `false`，标签字段照旧如实产出）；
> ② 点阵**不再是排序键**，只定义 49 个号的**抽样概率分布**（带内 `1.0`，带外 `1/(1+距离/6)`）；
> ③ 取号改为**期号种子随机**（`pick_sampling="seeded_random"` 默认，`"ranked"` 为旧的按名次路径）。
> 因此本文 §0 一句话总结、§3.5「点阵与走势的排序优先级」、§4「三类软降权」、
> `apply_soft_weights` 相关段落以及「一坨」复盘里「带内优先排序 / 软降权靠后」的描述**均已过时**。
> 现行口径以 `docs/settings_sensitivity.md` 的
> 「种子随机 + 点阵分布：去降权后的新选号口径（2026-10-09）」一节为准。

---

## 0. 一句话总结

```text
一句话（<200 字）
波浪买入法的规则是：把 1..49 每个号相对「上期特码」的差值分成小波动/常规波动/大跳三桶；
用最近 30 对相邻差值的 P25~P75 估一条「预测波动带」，带内优先取号；
再用重号/同肖/冷号三类软降权把特定号排到后面；
最后按主推:次选:防守 = 3:2:1 把 50 元预算（5 元为一个注码单位）分给 10 注。
全套规则只决定「花多少钱买哪些号」，不改变单期命中概率 10/49 与期望值 −4.0816%。
```

---

## 1. 参数与取值范围（`lottery.py:17-271`）

**白话小结：** 号码 1~49；注数 1~10；单次最大投注 5~100 元、且必须是 5 元的整数倍；
赔率默认 47；「大跳」的下限不是独立设置，恒等于 `normal_max + 1`。

| 参数 | 代码位置 | 取值 / 规则 |
|---|---|---|
| 号码范围 | `lottery.py:17-18` | `NUMBER_MIN = 1`，`NUMBER_MAX = 49`（共 49 个号） |
| 同肖步长 | `lottery.py:19` | `ZODIAC_STEP = 12`；同肖组 = `(n − 1) % 12`（`lottery.py:294-296`） |
| 波动三类 | `lottery.py:20-29` | `small` / `normal` / `big`，`WAVE_ORDER = [small, normal, big]` |
| 角色三类 | `lottery.py:31-39` | `primary`（主推）/ `secondary`（次选）/ `defense`（防守） |
| 注数 | `lottery.py:42-43` | `PICK_COUNT_MIN = 1`，`PICK_COUNT_MAX = 10` |
| 金额模式 | `lottery.py:45-58` | `even`（均注）/ `weighted`（侧重）/ `single`（单挑）/ `random`（随机分配） |
| 注码粒度 | `lottery.py:61-66` | `AMOUNT_UNIT_MIN = 5`，`step = 5`，默认 5 元 |
| 每注最低金额 | `lottery.py:69` | `MIN_BET_AMOUNT = 5`（1 个注码单位） |
| 最大投注 | `lottery.py:72-75` | `5 ≤ total_amount ≤ 100`，默认 50 |
| 赔率 | `lottery.py:77-79` | `1 ≤ odds ≤ 999`，默认 47（含本金的兑付倍数） |
| 避冷（按自然日） | `lottery.py:90-93` | `avoid_cold_enabled` 默认 `False`；`avoid_cold_days` 默认 60 |
| 三类软降权 | `lottery.py:104-113` | 重号 0.5、同肖 0.8、冷号（按期数）0.3，`stale_periods` 默认 60 |
| 点阵 | `lottery.py:150-160` | `lattice_enabled` 默认 `True`，`lattice_window` 默认 30，带宽 P25~P75 |
| 走势加权 | `lottery.py:162-192` | `neutral`（不加权）/ `hot` / `cold` / `mid`；`trend_window` 默认 20 |
| 选号策略 | `lottery.py:195-206` | `wave_round`（默认，波动桶轮取）/ `score_top`（打分 Top-N） |
| 打分权重 | `lottery.py:208-215` | `score_w_focus = 1.0`、`score_w_mid = 2.0`、`score_w_omit = 0.0`、`score_w_diff = 0.5`，钳到 `−5..5` |
| 默认配置字典 | `lottery.py:225-271` | `DEFAULT_SETTINGS`（上面所有键的唯一权威默认值） |

**`clamp_settings`（`lottery.py:345-488`）是所有设置的唯一入口**，逐项钳制：

- `normal_max ≥ small_max + 1`（`lottery.py:361`）——**这条约束让「大跳」不可能与「常规波动」重叠**；
- `amount_unit` 先钳到 `5..10000`，再**向下取整到 5 的倍数**（`lottery.py:364-369`）；
- 旧行缺 `total_amount` 时回退为 `bet_unit × pick_count`（`lottery.py:376-382`）——
  `bet_unit` 已**降级为派生展示值**，不再是可写设置项（`lottery.py:346-347`、`545-560`）；
- 布尔/枚举脏值一律回退默认，**不报错**（`lottery.py:390-470`）。
- 未知键（如 `big_min`）被整条忽略：循环只接收 `DEFAULT_SETTINGS` 里已有的键（`lottery.py:355-358`）。

**大/小、常规/大跳的定义**（`lottery.py:335-341`）：

```python
def classify_wave(diff, small_max=10, normal_max=30):
    diff = abs(diff)
    if diff <= small_max:      return WAVE_SMALL    # 小波动
    if diff <= normal_max:     return WAVE_NORMAL   # 常规波动
    return WAVE_BIG                                 # 大跳
```

其中 `diff = |本期号 − 最新特码|`。`big_min = normal_max + 1` 是**派生只读值**（`lottery.py:535-537`），
不落库、也不接受写入（`merge_settings_patch` 只接受 `DEFAULT_SETTINGS` 的键，`lottery.py:512-533`）。

现场配置下：`small_max = 15`、`normal_max = 20`，所以
**小波动 = diff 0~15，常规波动 = diff 16~20，大跳 = diff ≥ 21（big_min = 21）**。

---

## 2. 候选池：谁能进、谁被排除（`lottery.py:586-612`）

**白话小结：** 池子就是 1~49 里去掉「被排除」的号。两个排除开关：`include_repeat_number=False`
会踢掉上期特码本身；`exclude_repeat_zodiac=True` 会踢掉上期特码的**整个同肖组**（最多 5 个号）。
被踢掉的号**永远不可能被选中**——这会直接压低「最多可能命中几期」。

```python
for number in 1..49:
    if number == latest and not include_repeat_number:   continue   # 排除重号
    if exclude_repeat_zodiac and zodiac_group(number) == zodiac_group(latest):
        continue                                                     # 排除整组同肖
    pools[classify_wave(number - latest, small_max, normal_max)].append(number)
```

- 位置：`lottery.py:605-611`；
- 现场配置 `include_repeat_number = True`（保留重号）、`exclude_repeat_zodiac = True`（排除整组同肖）；
- 同肖组大小：`(n − 1) % 12` 相同的号。49 个号分 12 组，`1/13/25/37/49` 同组，因此一组最多 5 个号
  （`lottery.py:294-308`）。
- **没有被映射进池的号在回测里永远不可能命中**：`services/analytics.py` 的 `actual_in_pool_rate`
  就是这一条的可测指标（池外期数 = 命中数上界，而不是概率）。

现场配置下候选池 = 49 − 同肖组大小（通常 4~5 个号）。

---

## 3.「波浪」怎么算：预测波动带（`lottery.py:752-851`）

**白话小结：** 用最近 30 对相邻差值的**中位数**当「预测波动中心」，用 **P25~P75** 当「带宽」。
离最新特码的差值落在这条带里的号 = 满分权重 1.0；带外的号按 `1 / (1 + 距离/6)` 衰减。
`lattice_enabled=False` 时整块不参与（权重恒为 1.0）。

### 3.1 计算预测波动带 `predict_wave_band`（`lottery.py:766-811`）

```python
series = history[:window]                      # window = lattice_window（0 = 全池）
diffs  = sorted(|series[i] - series[i-1]|)     # 近 window 期的相邻差值
center = quantile(diffs, 0.50)                 # 中位数
low    = quantile(diffs, 0.25)                 # P25
high   = quantile(diffs, 0.75)                 # P75
```

- 分位数用线性插值（`_quantile`，`lottery.py:752-763`）；
- 样本不足 2 期返回 `None`（明确「无预测」，不编造，`lottery.py:785-786`）；
- `trend_window` **完全不参与**这条线——用的是 `lattice_window`（`lottery.py:1972-1977`）。

### 3.2 点阵权重 `lattice_weight`（`lottery.py:814-827`）

```python
if low <= diff <= high:  return 1.0                        # 带内满分
distance = (low - diff) if diff < low else (diff - high)
return 1.0 / (1.0 + distance / 6.0)                        # 带外衰减，LATTICE_DECAY_SCALE = 6
```

`band is None` → 恒为 `1.0`（关掉点阵与旧行为逐元素一致，`lottery.py:819`）。

### 3.3 预测波动桶 `lattice_primary_wave`（`lottery.py:830-851`）

把 `[floor(low), ceil(high)]` 区间内每个整数差值按 `classify_wave` 分桶，**带内差值最多的那一桶**
就是「预测波动桶」。取号时先在它里面取满（`lottery.py:2182-2196`）。

### 3.4 「趋势」/`trend_bias` 到底做什么（`lottery.py:162-192`、`1108-1123`、`926-932`）

**这是全套规则里最容易误解的一处：`trend_bias` 与「预测波动带」是两套独立机制。**

- `trend_bias = neutral`：排序回退为「本池全历史遗漏优先」的旧行为（`_trend_sort_prefix` 返回空元组，`lottery.py:1123`）；
- `trend_bias = hot / cold / mid`：在每个波动桶内，按近 `trend_window` 期出现次数排序
  （`hot` → `−count` 降序、`cold` → `count` 升序、`mid` → `|count − mid_target|` 升序，`lottery.py:1113-1122`），
  并把该桶切成 主推 / 次选 / 防守 **三段频次带**（`split_pool_into_role_bands`，`lottery.py:956+`；
  段大小 `(n+2)//3, (n+1)//3, n//3`，`lottery.py:916-923`）。
- `mid_target` = 近窗内「出现过的号码」的平均次数（`lottery.py:2013-2018`）。

**关键门闩：`trend_bias` 存了值不代表生效。** `effective_trend_bias`（`lottery.py:490-505`）规定：

```python
if not raw[TREND_BIAS_EXPLICIT_KEY]:   # 库里的内部元数据 trend_bias_explicit
    return "neutral"                   # 一律不加权，无论存的是 hot/cold/mid
```

`trend_bias_explicit` 只在用户**显式提交过** `trend_bias` 时才置 True（`merge_settings_patch`，`lottery.py:529-531`），
它是内部标记，**不出现在 `GET /api/settings` 的返回体里**（`SettingsOut` 不含该键）。
`Store.get_settings` 读出来就套了这一层（`repository.py:1013-1018` 内存实现 / `:1013` 与 `:1489` 两个实现同口径）。

因此「`trend_bias=mid` 是不是空操作」的答案是：**不是空操作，但只在显式标记为 True 时才生效**。
现场库里的标记是 True（`GET /api/settings` 返回 `mid`，且实测改 `trend_bias` 会改变号码），
所以 `mid` 真的在改号码。反过来，若某条写入路径没能保住这个内部键
（`merge_settings_patch` 是从 `current` 拷贝的，正常 patch 不会丢），
`trend_bias` 会**静默退回 `neutral`** —— 同一组设置会因为「标记在不在」表现完全不同，
这是排查「开关时灵时不灵」时要先看的一处。

### 3.5 点阵与走势的排序优先级（`order_pool`，`lottery.py:1126-1192`）

排序键从前往后（`lottery.py:1162-1190`）：

```text
1. 点阵权重      (-lattice_weight)        # 带内优先（lattice_enabled 才传入）
2. 软降权        (-soft_weight)           # 重号/同肖/冷号靠后（永远传入）
3. 避冷          冷=1 / 非冷=0            # 仅 avoid_cold_enabled=True 时参与
4. 趋势偏好      hot/cold/mid 的前缀      # 仅 trend_bias != neutral 时参与
5. 回落键        (出现次数, diff, 号码)   # 旧行为：遗漏优先
```

注意代码注释里明说的一条**反直觉后果**（`lottery.py:2409-2412`）：
「带外的新号排在带内的冷号之后」——点阵优先级高于软降权，所以点阵开着时，
降权几乎只在**同一个点阵权重档位内部**起作用。

---

## 4. 三类软降权（`lottery.py:711-749`）

**白话小结：** 上期特码本身打 0.5 折，与它同肖的号打 0.8 折，连续 60 期以上没出现的号打 0.3 折。
**都不排除，只降权**（排序靠后 + 金额打折），权重相乘。

```python
if number == latest:                       weight *= repeat_number_weight   # 0.5
elif zodiac_group(number) == zodiac_group(latest):
                                           weight *= repeat_zodiac_weight   # 0.8
stale = sample_size >= stale_periods and (periods_since_last is None or
                                          periods_since_last >= stale_periods)
if stale:                                  weight *= stale_weight           # 0.3
```

- 位置：`lottery.py:737-748`；返回 `(权重, 原因列表)`，原因英文枚举 `repeat_number` / `repeat_zodiac` / `stale`；
- `sample_size < stale_periods` 时**一律不判冷号**（`lottery.py:731-734`）——避免短样本把「从未出现」误判成冷；
- 重号与同肖互斥（重号必然同肖，按重号计，`lottery.py:737-742`）；
- **金额侧同步打折**：`apply_soft_weights`（`lottery.py:881-914`）把金额乘以权重，
  下限为 `MIN_BET_AMOUNT = 5` 元，**省下的预算不补给其它注**（`recommend` 调用点 `lottery.py:2242-2258`）。

**为什么它在现场配置下几乎不生效：** 现场 `exclude_repeat_zodiac = True` 已经把整组同肖（含重号）
踢出候选池（第 2 节），所以 `repeat_number_weight` / `repeat_zodiac_weight` 在**号码选择**上无事可做，
只剩「池内残留的同肖号」——而池内已无同肖号。实测见第 7 节。

---

## 5. 选 10 注：`wave_round` 与角色（`lottery.py:2102-2226`）

**白话小结：** 有两条互斥的选号路径。默认 `wave_round` = 「按波动桶轮取」；
`score_top` = 「全候选池打分取前 N」。取完之后，**按侧重顺序**把 10 注分成
主推 1 注、次选/防守交替（3:2:1 是**金额**权重，不是注数配额）。

### 5.1 侧重顺序 `FOCUS_BY_PREV_WAVE`（`lottery.py:217-223`）

```python
small  → [normal, big,    small]
normal → [small,  big,    normal]
big    → [small,  normal, big]
```

上期波动类型决定本期三类波动的侧重次序，第一个即主推方向（`lottery.py:2030-2043`）。

### 5.2 路径 A：`wave_round`（默认，`lottery.py:2197-2212`）

1. **点阵开启时**（`lattice_primary is not None`）：先在「预测波动桶」里取满，
   再按 `[primary] + [其余桶]` 的顺序向其它桶扩散（`lottery.py:2182-2196`）；
2. **点阵关闭时**（旧行为）：先按 `WAVE_ORDER`（小 → 常 → 大）**每个桶各取 1 注**（`lottery.py:2199-2204`）；
   不够 10 注再从仍有候选的桶里补齐，允许同类多取（`lottery.py:2206-2211`）；
3. 桶内取号：`trend_bias=neutral` 直接取池首；否则先在该注对应角色的**频次带**里取，
   带内没有才回退池首（`_take_one`，`lottery.py:2155-2179`）。

角色与取号的对应：第 `i` 注的偏好角色 = `role_for_focus_rank(focus.index(wave))`
（第 1 侧重→主推、第 2→次选、其余→防守，`lottery.py:926-932`）。

### 5.3 路径 B：`score_top`（`lottery.py:2135-2153`）

跨三个波动桶统一打分，取分数最高的 N 个（`select_score_top_candidates`，`lottery.py:1787-1845`）：

```python
score = (−w_focus × focus_rank
         − w_mid   × |count − mid_target|
         − w_omit  × omit_feat              # omit_feat = 距上次出现天数 / 60，从未出现取 4.0
         − w_diff  × diff / 49)             # 分越高越优先
score *= lattice_weight                    # 点阵作乘数
score *= soft_penalty_weight                # 软降权作乘数
```

- 位置：`score_candidate` `lottery.py:1743-1784`；乘数在 `lottery.py:1820-1823`；
- 平手按 `diff` 升序、再按号码升序，保证可复现（`lottery.py:1825-1827`）；
- **`score_w_omit` 现场值为 0.0**：该项**恒被乘 0**，因此该权重在现场配置下对结果没有任何影响
  （除非把 `pick_strategy` 改成 `score_top` 并且改 `score_w_omit`——实测见第 7 节）。

### 5.4 角色分配与注数（`assign_roles`，`lottery.py:1222-1235`）

```python
index 0        → primary     # 只有第 1 注是主推
index % 2 == 1 → secondary   # 次选
else           → defense     # 防守
```

10 注 → `[主推, 次选, 防守, 次选, 防守, …]`。取号完成后先按侧重顺序排序
（`picks.sort(key=focus.index(wave_type))`，`lottery.py:2215`），**以保证主推落在最侧重的波动上**。

---

## 6. 金额怎么算（`lottery.py:1336-1513`、`1627-1672`、`2230-2258`）

**白话小结：** 先把预算向下取整到 5 元的整数倍，单位数 = `total_amount // amount_unit`；
均注模式先给每注保底 1 单位，余量按 **主推:次选:防守 = 3:2:1** 分给三组，组内再均分；
最后再按软降权/避冷打一次折，省下的钱**不补给别注**。

### 6.1 预算规范化 `prepare_budget`（`lottery.py:1627-1672`）

```python
units_total = total // unit
if units_total <= 0:            return pick_count = 0     # 预算买不起 1 个单位 → 无法分配
if mode == single:              return pick_count = 1
if units_total < pick_count:    return pick_count = units_total   # 只分配能覆盖的注数
```

**这条「降级注数」是 `total_amount` / `amount_unit` 会改变号码的原因**：
有效注数一变，实际选出的号数就变（实测：`normal_max` 组现场配置下 `amount_unit=25` → 命中数 8/208）。

### 6.2 单位分配（`allocate_amounts`，`lottery.py:1474-1513`）

| 模式 | 算法 | 位置 |
|---|---|---|
| `even` | `distribute_units_by_role`：每注保底 1 单位，余量按角色权重 3:2:1 分给三组，组内均分；权重全相同则退回严格均分 | `lottery.py:1403-1471` |
| `weighted` | 主推约占 4/6，其余均分（并保证每注 ≥ 1 单位） | `lottery.py:1351-1373` |
| `single` | 全部预算押在第 1 注 | `lottery.py:1504-1507` |
| `random` | 按 `amount_seed_key(period, latest, previous, total, count, unit)` 派生的确定性种子随机拆分（同参数必然复现，不用时间戳） | `lottery.py:1239-1259`、`1302-1334` |

余量分配用**最大余数法**（`lottery.py:1453-1462`）；组内用 `_distribute_units_even`
（`units // count`，余数补给前 rem 注，`lottery.py:1336-1348`）。

**现场配置下的实际分配**（`total=50`、`unit=5` → 10 个单位、10 注、权重 3:2:1）：
保底 10 个单位刚好用完，**余量为 0** → 权重不产生任何差异，10 注各 5 元。
代码里对这种情况有明确提示（`lottery.py:2387-2391`）：
「本次预算 50 元刚好等于『每注最低 5 元 × 10 注』，没有余量可分配，角色配额未产生任何金额差异」。

### 6.3 金额折扣（`lottery.py:2230-2258`）

1. **避冷**（仅 `avoid_cold_enabled=True`）：`apply_avoid_cold_amounts`（`lottery.py:1593-1624`）
   —— 权重随天数递减，金额最多给到**保本金额 = 1 个注码单位**（5 元），不足 1 单位记 0（号码仍列出）；
2. **三类软降权**：`apply_soft_weights`（`lottery.py:881-914`）
   —— 金额 × 权重（0.5 / 0.8 / 0.3），下限 `MIN_BET_AMOUNT = 5` 元；
3. 两步都是**只降不升**，省下的预算计入 `unspent`，**绝不静默改配到其它注**。

---

## 7. 实测：这些规则在回测里到底做了什么

全部数据来自 `backend/scripts/settings_sensitivity.py`（离线纯函数，样本内 208 期）。
完整表格见 `.tmp-wave/settings_sensitivity.json`（本仓库 gitignore 的运行产物）；
结论表格见 `docs/settings_sensitivity.md`。

- 现场配置：`hits = 38 / 208 = 18.27%`，随机基线 `10/49 = 20.41%`，判定 `noise`；
- **关掉 `lattice_enabled` 就变成 56 / 208 = 26.92%**（引擎自己的判定翻转成 `beyond`）。
  同一份数据、同一套号码池，只去掉一个开关就「从落后随机变成超越随机」——
  这本身就是「208 期样本不足以判定任何优势」的最直观证据（第 8 节给 p 值与功效）。
  现场实测与离线复算完全一致（`38 → 56`），两者都记为 `ACTIVE`。

### 7.1 现场配置下 10 注到底落在哪（实测，208 期）

按现场阈值（`small_max = 15`、`normal_max = 20`）逐期统计这 10 注的波动桶：

| 统计 | 数值 |
|---|---|
| 「10 注全部落在小波动」的期数 | **186 / 208（89.4%）** |
| 「10 注全部落在大跳」的期数 | 12 / 208 |
| 「10 注全部落在常规波动」的期数 | 0 / 208 |
| 逐注口径：小波动 | **1903 / 2080 注 = 91.5%** |
| 逐注口径：大跳 / 常规 | 141 / 36 注 |

**机制：** 点阵开启时先在「预测波动桶」取满（`lottery.py:2182-2196`），
而现场 `small_max = 15` 把小波动桶撑得很大（差值 0~15），
近 30 对差值的中位数与 P25~P75 几乎总落在 0~15 内 → 预测波动桶几乎恒为 `small`，
于是 10 注全被压在「离上期特码 15 以内」这个约 31 个号的窗口里。

**这改变了什么、没改变什么（必须说清）：**

- **没改变**单期命中概率：仍然是 `10 / 49 = 20.41%`（10 个号从 49 个等概率号里选，与它们的分布无关）；
- **没改变**期望值：仍然是 `47 / 49 − 1 = −4.0816%`；
- **改变了**的是选号覆盖的形态：引擎实际上只在一个约 31 个号的窗口里选号，
  其余 18 个号在 186/208 期里完全没有被覆盖。这是「偏好」，不是优势，也不是劣势。
- 因此 38/208 与 42.4（= 208 × 20.41%）之间的差距只是抽样波动：`(42.4 − 38) / 5.81 ≈ 0.76σ`。

---

## 8. 为什么不可能是「规则不够好」

**白话小结：** 规则决定「买哪些号、各买多少钱」，但每个号等概率，所以**命中概率只取决于买几个号**；
期望值只取决于赔率。208 期的样本量根本看不出 ±8pp 以内的差别。

1. **命中概率不取决于选号**：单期命中概率 = `有效注数 / 49`。现场 10 注 → `10/49 = 20.4082%`。
   任何权重、点阵、降权都只是在 49 个等概率号之间**重新分配**「是哪 10 个」，
   不改变这一期命中概率。
2. **期望值恒定**：`EV = odds / 49 − 1 = 47 / 49 − 1 = −4.0816%`（每 100 元）。
   `services/pnl.py` 把赔率当「含本金的兑付倍数」。**没有任何设置能改动这个数**。
3. **观测 38/208 = 18.27% vs 基线 20.41%**：差距 **−2.14pp**，
   精确二项双侧 `p ≈ 0.49` —— 与随机**在统计上无法区分**。
4. **最小可检出差异（MDD）**：`n = 208`，`α = 0.05`，功效 80% 时
   `MDD ≈ 7.83pp`。也就是说 208 期只能排除「真实优势大于约 8 个百分点」这种量级的假设，
   对 ±2pp 级别的差异**没有任何分辨力**。
5. **所需的样本量**（80% 功效）：`+0.87pp`（约 50/235 的盈亏平衡点）≈ 数万期；
   `+5pp` ≈ 上千期；`+10.2pp` ≈ 数百期。**远超过本池 210 期**。

**结论（不修饰）：** 现场这套旋钮**不会、也不可能**改变命中概率或期望值。
它们交付的是花费控制（每注金额、合计不超预算）、号码卫生偏好（重号/同肖/冷号打标）
和可复现性（同参数必然复现）。回测数字低不是「规则还没调好」，而是**规则本来就不影响那个数字**，
18.27% 与 20.41% 的差别就是抽样噪声。

---

## 9. 诚实清单：哪些规则没有预测依据

| 规则 | 性质 | 依据 |
|---|---|---|
| 预测波动带（P25~P75） | 经验分布启发式 | 只是「近 30 期差值的中位数与四分位」；号码等概率，上期差值不含下期信息 |
| `lattice_enabled=True` 时「带内优先」 | 集中度偏好 | 会把 10 注大量压在「离上期特码近」的号上；实测 186/208 期 10 注全落在同一个小波动桶 |
| `trend_bias` hot/cold/mid | 近期频次偏好 | 近 20 期频次是历史噪声；`neutral` 与 `mid` 的命中差异在噪声内 |
| 重号 / 同肖 / 冷号降权 | 号码卫生偏好 | 无预测依据；仅表达「不想买这些号」的偏好，并如实打标 |
| 角色配额 3:2:1 | 筹码分配偏好 | 只改金额分布，不改号码；预算刚好等于最低注时**完全不生效** |
| 避冷（自然日） | 号码卫生偏好 | 默认关闭；开启后只在选号排后 + 金额封顶 |
| `score_top` 打分权重 | 样本内排序键 | 代码注释已自述「这是样本内排序键，不是概率，也不承诺提高命中率」（`lottery.py:1761`） |

---

## 10. 一处已发现的引擎缺陷（未修，如实记录）

`score_top` + 「预算不足 1 个注码单位」时会产出一注**没有金额**的号：

- `prepare_budget` 把 `pick_count` 降级为 `0`（`lottery.py:1651-1657`）；
- 但 `select_score_top_candidates` 用 `max(1, int(pick_count))` 仍返回 **1 注**（`lottery.py:1842`）；
- `allocate_amounts` 因 `units_total <= 0` 返回**空金额列表**（`lottery.py:1501-1502`）；
- `zip(picks, amounts)` 迭代 0 次，`pick["amount"]` 从未被赋值（`lottery.py:2260-2266`），
  `build_copy_text` 取该键 → `KeyError: 'amount'`（`lottery.py:1703`）。

复现：`pick_strategy=score_top`、`total_amount=50`、`amount_unit=100`。
`backend/scripts/settings_sensitivity.py` 会把这种格子记录为 `engine_error`，不静默吞掉。
本文只记录，不修改 `services/lottery.py`（按任务约束）。

---

## 附：代码位置速查

| 主题 | 函数 / 常量 | 行号 |
|---|---|---|
| 波动分类 | `classify_wave` | `lottery.py:335-341` |
| 设置钳制（唯一入口） | `clamp_settings` | `lottery.py:345-488` |
| 走势加权读取门闩 | `effective_trend_bias` / `resolve_trend_bias` | `lottery.py:490-509` |
| 设置写入合并 | `merge_settings_patch` | `lottery.py:512-533` |
| 大跳下限 | `derive_big_min` | `lottery.py:535-537` |
| 有效注数 | `effective_pick_count` | `lottery.py:540-542` |
| 候选池 | `build_candidate_pools` | `lottery.py:586-612` |
| 近窗频次 | `recent_number_frequency` | `lottery.py:617-636` |
| 距上次出现（自然日 / 期数） | `compute_days_since_last` / `compute_periods_since_last` | `lottery.py:646-708` |
| 三类软降权 | `soft_penalty_weight` | `lottery.py:711-749` |
| 预测波动带 | `predict_wave_band` | `lottery.py:766-811` |
| 点阵权重 | `lattice_weight` | `lottery.py:814-827` |
| 预测波动桶 | `lattice_primary_wave` | `lottery.py:830-851` |
| 池内排序 | `order_pool` | `lottery.py:1126-1192` |
| 角色带取号 | `take_from_role_band` | `lottery.py:1195-1219` |
| 角色分配 | `assign_roles` | `lottery.py:1222-1235` |
| 均分 / 侧重 | `_distribute_units_even` / `_distribute_units_weighted` | `lottery.py:1336-1373` |
| 角色配额分配 | `distribute_units_by_role` | `lottery.py:1403-1471` |
| 金额分配主入口 | `allocate_amounts` | `lottery.py:1474-1513` |
| 金额打折 | `apply_soft_weights` / `apply_avoid_cold_amounts` | `lottery.py:881-914`、`1593-1624` |
| 预算规范化 | `prepare_budget` | `lottery.py:1627-1672` |
| 打分 / Top-N | `score_candidate` / `select_score_top_candidates` | `lottery.py:1743-1845` |
| 主入口 | `recommend` | `lottery.py:1850-2531` |
| 出票单（复用引擎） | `build_ticket` | `services/pick_ticket.py:845+` |
