# 前瞻（期外）验证账本 —— `forward_ledger/`

本目录是**唯一**无法被「后见之明」污染的验证口径：预测在开奖**之前**冻结，
开奖之后只允许「读」。它专门回答两个问题：

> ① 线上推荐引擎在**没见过的期**上，到底能不能比随机基线更好？
> ② 波动法预登记变体那条 26.67%（样本内）会不会向 20.41% 的随机基线回归？

## 三种验证口径，别混

| 口径 | 做法 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| 样本内（回看 / 反推） | 目标期自己也进入拟合 | 模型能把历史「解释」到多完美 | **什么也证明不了**（答案已经在那了） |
| 样本外回测（walk-forward） | 只用目标期之前的数据逐期回测 | 在同一份历史上的相对表现 | 历史可以被反复挑选口径，结论仍可被调参「雕」出来 |
| **前瞻冻结（本目录）** | 开奖前把预测写进账本并提交 | 唯一诚实的「未来会不会应验」 | 需要**时间**（期数）才能攒出统计功效 |

## 协议

1. **一次冻结 = 一条记录。** 记录里含该期的策略输出：线上引擎 `PRODUCTION_ENGINE`、
   均匀对照 `UNIFORM`、可选 `FIT_ONLY` 演示行，以及波动法预登记主变体
   `WAVE_PREREGISTERED`（可用 `freeze --strategies wave,...` 只写入其中几条）。
2. **append-only，判重口径是 `(期号, 策略)`。**
   - 同一 `(期号, 策略)` 再冻结 → **硬报错**（`ImmutabilityError`），绝不覆盖；
   - 向**已冻结但尚未开奖**的期**追加一条新策略**是允许的 —— 波动法预登记变体就是
     这样入账的：历史记录一个字节不改，只在链尾 append 一条只含新策略的新记录；
   - 目标期若**已经开奖** → **硬报错**（`LookAheadError`）：禁止事后补冻，
     那等于编造一条当时并不存在的预测。
3. **一个开奖周期只冻结一期**（详见下一节）——一次冻结多期只会把同一注重复登记。
4. **无前视凭证。** 每条记录都写死 `available_length = period_index = 策略实际看到的
   期数`，并且 `available_first_period … available_last_period` 明确列出可用前缀。
   目标期**从不出现在**输入里 —— `verify` 会重算并再次确认这一点。
5. **哈希链。** `record_hash = sha256(canonical(记录全部字段 + 上一条的 record_hash))`，
   首条以 `genesis_hash` 起链。`verify` 会逐条重算，任何事后改写都会暴露。
6. **数据摘要。** `data_digest = sha256(可用开奖前缀 + 生效配置 + 策略集合)`，
   `available_digest = sha256(可用开奖前缀)`。改掉历史开奖或配置，两个摘要立刻对不上。
7. **预测摘要 + 独立性标记。** 每个策略条目写死
   `prediction_digest = sha256(策略 + 选号 + 注码 + 产出它的配置)`（**不含期号**）、
   `independent`（英文 `true`/`false`）与 `duplicate_of_period`（重复的是哪一期）。
   同一策略下摘要重复 = 同一个预测被登记了两次。

## 为什么一次只冻结一期

线上引擎是**确定性**的：给定同一份已开奖前缀，它永远给出同一注。所以开奖前一次性
冻结 280/281/282，三期在 `PRODUCTION_ENGINE` 上会是**逐字相同**的选号 —— 那不是
3 条证据，而是**同一注下了 3 次**。把「已计分行数」当样本量会凭空放大证据强度。

因此：

- `score` / `status` 会报告**三个**样本量：`frozen_rows`（冻结条目）、`scored_rows`
  （已计分行数，含重复）、`effective_independent_rows`（`prediction_digest` 去重后的
  **有效独立样本**）；
- **二项检验 / 命中率 delta / 最小可检测 Δ / 判定只按有效独立样本计算**；重复登记的行
  逐条列在 `excluded_duplicates`（理由 `DUPLICATE_PREDICTION`），可见、有理由，不静默折叠；
- 有效独立样本 < 2 条时**不给 p 值、不给判定**（`evidence_status` 会说明是
  `NO_SCORED_ROWS` 还是 `SINGLE_INDEPENDENT_ROW`）—— 单条观测无法区分随机与优势；
- `freeze --next N`（N>1）**不会报错**，但会把重复行标记成 `independent=false` 并打印
  醒目警告。规范做法永远是 **`freeze --next 1`**：冻结下一期 → 等它开奖 →
  `score` 计分 → 再冻结下一期。这样每期的前缀都包含上期开奖，**每期都是独立预测**。
  （`--next` 会跳过账本里已冻结的期号，所以开奖后再跑 `freeze --next 1` 依然顺畅。）

**向后兼容 / 追加记录**：新字段只加在**新冻结**的记录上。历史三条记录（期号
280 / 281 / 282，各含线上引擎 / 均匀对照 / FIT_ONLY）一个字节都不改 —— 改写历史会
破坏哈希链与 append-only 保证。波动法预登记变体是**追加**的第 4 条记录（期号 280、
`prev_hash` = 原链根 `91af45bf…`）：原链根因此仍可单独验证，新链根变成追加记录的
`record_hash`。缺字段的老记录由 `score` / `verify` 按**完全相同的规则现算**
（`digest_source=DERIVED`），所以老记录的完整性一模一样可查。

## 波动法预登记变体 `WAVE_PREREGISTERED`（回归追踪）

`services/wave_study.py` 的预登记主变体 `engine_component|w30|neutral`（30 期窗口、
无走势偏好；波段阈值固定 `small_max=10` / `normal_max=30`，不作为变体轴）在样本内评估
窗（210 期里跳过最早 30 期 warmup、在最后 180 期上评估）拿到 **48/180 = 26.67%**；
同网格 max 零分布冠军是 **51/180 = 28.33%**，合法 p = 0.1493 —— **既没显著、也没被
证伪**。把它放进前瞻账本，就是为了看这条数字会不会向 20.41%（10/49）回归。

- 注码与线上引擎**完全一致**（预算 50 元、10 注、5 元/注、`even` 均分），所以命中率
  可以和 `PRODUCTION_ENGINE` 逐期直接对照，不需要任何换算。
- 冻结第 280 期时可用前缀是第 70…279 期共 **210** 期（`available_length = 210`）——
  恰好是研究评估窗之后的**第一个真正期外位置**；目标期从未出现在输入里。
- 入账的**只有预登记那一条**（`services.wave_study.PRIMARY_VARIANT_ID`），不是事后在
  网格里挑冠军。波动 / 点阵数学直接调用 `services.wave_study.wave_step`，一行都不重写。
- 该变体**禁止进入线上推荐路径**，只用于「这条 26.67% 会不会向 20.41% 回归」的对照。

`regression` 子命令专门回答「回归还是稳住」，输出：

- 每策略的 `frozen_rows` / `scored_rows` / `effective_independent_rows` / 累计命中 /
  命中率 / **Wilson 置信区间**（Wald 在 0% / 100% 时会塌成 `[0,0]`，所以表里列 Wilson；
  两条区间都在 JSON 产物里）；
- 基线 `10/49 = 20.4082%`，以及观测与基线的**差（pp）及其标准误**、差有几倍 SE；
- 波动法专属：被测的样本内数字（26.67% 与 28.33%）、**噪声投影**（若只是噪声，观测
  偏离会被稀释回基线：`cumulative_rate_if_noise = (hits + M·p0) / (n + M)`）、以及
  **所需期数**（含逐项算术）；
- `p(>随机)` 与 **Holm 校正后 p**（复用 `services.analytics`）—— 多策略同时检验时必须
  看校正后的 p，而不是单条原始 p。

「什么才算证据」的台阶（80% 功效、α=0.05，复用 `wave_study.power_and_verdict`）：

| 目标 Δ | 共需已计分（有效独立）期数 | 还差 | 说明 |
|---|---|---|---|
| +6.26pp | 345 | 165（≈5.4 个月） | 样本内 26.67% 的偏离要坐实 |
| +5.00pp | 535 | 355 | 常用的「最低值得追的优势」 |
| +0.87pp | 17,063 | 16,883 | 只求打平赔率（break-even） |

**在追平这些期数之前，无论命中率高低，都只能报 `noise`。**

## 文件

| 文件 | 说明 |
|---|---|
| `ledger.json` | **已提交**的账本本体（记录数组 + 创世哈希）。改它一定会被 `verify` 抓到 |
| `production_settings.json` | 冻结时使用的「线上生效配置」快照（来自 `Store.get_settings(None)` = 全局模板） |

落盘字段的枚举一律**英文码或数字**（`PRODUCTION_ENGINE` / `UNIFORM` /
`FIT_ONLY_MARKOV_1` / `WAVE_PREREGISTERED` / `LIVE` / `FIT_ONLY` / `even` /
`true` / `false`）；汉字只出现在人类可读的 `label` / `notes` / `disclaimer` 里。

## 命令（规范工作流：一次开奖只冻结一期）

```powershell
cd D:\myproject\wave-money\backend
$py = ".\.venv\Scripts\python.exe"

# ① 先把最新一期开奖写进数据文件（backend/data/draws_70_279.json；被 .gitignore 忽略）
#    数据没更新就冻结 = 策略看不到新信息 → 只会重复上一注（CLI 会提示你）。

# ② 开奖前冻结下一个尚未开奖的期（线上引擎 + 均匀对照 + 可选 FIT_ONLY + 波动法预登记）
& $py scripts\forward_validate.py freeze --next 1

# ③ 立刻校验完整性（哈希链 + 数据摘要 + available_length 无前视）
& $py scripts\forward_validate.py verify

# ④ 提交账本 —— 这一步不是形式，它是「预测早于开奖」的证据
git -C D:\myproject\wave-money add -- backend/forward_ledger/ledger.json
git -C D:\myproject\wave-money commit -m "chore(forward-ledger): freeze period <N> before draw"

# ⑤ 等该期开奖 → 把新开奖写进数据文件 → 计分（未开奖的期一律 pending，绝不提前计分）
& $py scripts\forward_validate.py score

# ⑥ 回答「波动法在向 20.41% 基线回归，还是稳住？」
& $py scripts\forward_validate.py regression

# ⑦ 进度总览（冻结 / 待开奖 / 已计分 + 有效独立样本）+ 当前判定 + 完整性
& $py scripts\forward_validate.py status

# ⑧ 回到 ①，冻结再下一期

# 只写波动法预登记变体（向已冻结但未开奖的期追加新策略时用；老记录一个字节不动）
& $py scripts\forward_validate.py freeze --period 280 --strategies wave

# 只写线上引擎 + 均匀对照（不写波动法 / FIT_ONLY 演示行）
& $py scripts\forward_validate.py freeze --next 1 --strategies production,uniform

# 用线上库当前生效配置冻结（而不是快照文件）
& $py scripts\forward_validate.py freeze --next 1 --from-store
```

一次冻结多期（`--next 3`）不会报错，但会打印醒目警告并把重复行标成
`independent=false` —— 它们不会增加有效独立样本。

`--next N` 只取「尚未开奖**且尚未冻结**」的期号：账本里已冻结的期会被自动跳过并打印
一行说明，所以开奖后再跑一次 `freeze --next 1` 不会撞上已冻结的期号。想显式指定期号
请用 `--period`（append-only 仍然会按 `(期号, 策略)` 拒绝重复）。

## 为什么必须「开奖前冻结」、为什么 commit 就是证据

**开奖后写下来的预测一文不值。** 只要目标期已经开奖，就存在无穷多种「我当时就想选这
几个号」的说法；事后挑一个漂亮的选号再声称早想好了，无法被证伪，也就无法当证据。
所以本账本在代码层面直接拒绝：目标期已开奖 → `LookAheadError`，连试都不让试。

**开奖前一次性冻结多期同样不值钱。** 线上引擎是确定性的：同一份已开奖前缀只产出一个
预测。开奖前冻结 280 / 281 / 282，三期的线上引擎选号逐字相同 —— 那是同一注登记了三
次，不是三条证据。所以规则是**一个开奖周期只冻结一期**：`freeze --next 1` → 等开奖 →
`score` → 再冻结下一期。这样每期都看到了上一期的新开奖，每期才是独立预测。

**为什么 Git 提交时间是关键。** 账本文件本身可以被任意改写，哈希链只能证明「这堆记录
自洽」，证不了「它是什么时候写下的」。Git 提交由仓库之外的时间线锚定：`git log` 里的
提交时间戳独立于账本文件存在。先 `freeze` 再 `git commit`，等于给「这条预测早于第 N 期
开奖」留下了一份不可事后伪造的旁证 —— 要伪造就得改写已推送的历史，代价极高且会留下
痕迹。**不 commit，冻结就只是一份本地草稿。**

**`pending` 永远不参与统计。** 目标期还没开奖，就没有命中可言。`score` / `regression`
只统计已开奖的期；未开奖的期只报 `pending`。任何「先算个大概」的写法都是自欺。

## 计分口径

- 只对**已开奖**的冻结期计分；未开奖的期只报 `pending`，绝不用「还没发生」的结果计分。
- **样本量三件套**：`frozen_rows`（冻结条目）/ `scored_rows`（已计分行数）/
  `effective_independent_rows`（去重后的有效独立样本）。推断只用最后那个。
- `excluded_duplicates` 逐条列出被排除的重复预测（`reason=DUPLICATE_PREDICTION`，
  并指出它重复的是哪一期），**绝不静默折叠进统计**。
- 有效独立样本 < 2 条：`evidence_status` = `NO_SCORED_ROWS` 或
  `SINGLE_INDEPENDENT_ROW`，`binomial_p_*` / `power` / `verdict` 全部为 `null`
  —— 宁可说「数据不足」，也不给一个用错样本量算出来的 p 值。
- 均匀参考：命中率 `k/49`（k 取该策略自己的注数）、平均排名 `25.0`、
  log-loss `ln 49 ≈ 3.8918`、Brier `(1/49)(1−1/49) ≈ 0.0200`。
- 二项检验 / 最小可检测 Δ / 所需期数全部复用 `services.analytics`
  （`binomial_tail_p` / `minimum_detectable_delta`），不另起一套算法。
- 收支（`total_spend` / `total_return` / `net_pnl`）记**全部已计分行**的真实金额
  （重复登记也是真金白银下注）；去重口径的收支另记在
  `independent_spend` / `independent_return` / `independent_net_pnl`。
- 线上引擎只输出 top-k 选号，**没有** 1..49 完整排序 / 概率向量，因此它的
  `mean_rank` / `log_loss` / `brier` 记为 `null`（不适用）——只用命中率与盈亏
  同均匀基线对照。这不是数据缺失，而是口径本身不成立，绝不编造排序。

## 人数不够时的实话

即使攒到 208 期（= 现在全部历史），随机基线 20.41% 下的抽样标准误也有约 2.79%；
要检出「+5% 绝对优势」（20.41% → 25.41%）在 80% 功效下约需 **510 期**已计分样本，
检出 +2% 约需 **3188 期**。所以前几十期无论命中多高多低，都只能报 `noise` ——
本账本的价值在于**从今天开始不再被后见之明污染**，而不是马上给出结论。

这里的「期数」一律指 **`effective_independent_rows`**（有效独立样本），不是冻结了几条
记录。一次冻结 20 期不会得到 20 期证据：线上引擎在开奖前只能产出一注，重复登记不增加
统计功效。**一个开奖周期只冻结一期**，才能让每一期都算数。
