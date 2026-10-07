# 前瞻（期外）验证账本 —— `forward_ledger/`

本目录是**唯一**无法被「后见之明」污染的验证口径：预测在开奖**之前**冻结，
开奖之后只允许「读」。它专门回答一个问题：

> 线上推荐引擎在**没见过的期**上，到底能不能比随机基线更好？

## 三种验证口径，别混

| 口径 | 做法 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| 样本内（回看 / 反推） | 目标期自己也进入拟合 | 模型能把历史「解释」到多完美 | **什么也证明不了**（答案已经在那了） |
| 样本外回测（walk-forward） | 只用目标期之前的数据逐期回测 | 在同一份历史上的相对表现 | 历史可以被反复挑选口径，结论仍可被调参「雕」出来 |
| **前瞻冻结（本目录）** | 开奖前把预测写进账本并提交 | 唯一诚实的「未来会不会应验」 | 需要**时间**（期数）才能攒出统计功效 |

## 协议

1. **一次冻结 = 一条记录（按期号）。** 记录里含该期的全部策略输出
   （线上引擎 / 均匀对照 / 可选 `FIT_ONLY` 演示行）。
2. **append-only。** 某期一旦有记录：
   - 再冻结同一期 → **硬报错**（`ImmutabilityError`），绝不覆盖、绝不追加第二条；
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

**向后兼容**：新字段只加在**新冻结**的记录上。历史记录（第 280/281/282 期）一个字节
都不改 —— 改写历史会破坏哈希链与 append-only 保证。缺字段的老记录由 `score`/`verify`
按**完全相同的规则现算**（`digest_source=DERIVED`），所以链根 `91af45bf…` 依然可验。

## 文件

| 文件 | 说明 |
|---|---|
| `ledger.json` | **已提交**的账本本体（记录数组 + 创世哈希）。改它一定会被 `verify` 抓到 |
| `production_settings.json` | 冻结时使用的「线上生效配置」快照（来自 `Store.get_settings(None)` = 全局模板） |

落盘字段的枚举一律**英文码或数字**（`PRODUCTION_ENGINE` / `UNIFORM` /
`FIT_ONLY_MARKOV_1` / `LIVE` / `FIT_ONLY` / `even` / `true` / `false`）；汉字只出现在
人类可读的 `label` / `notes` / `disclaimer` 里。

## 命令（规范工作流：一次开奖只冻结一期）

```powershell
cd D:\myproject\wave-money\backend
$py = ".\.venv\Scripts\python.exe"

# ① 开奖前：冻结下一个尚未开奖的期（线上引擎 + 均匀对照 + FIT_ONLY 演示行）
& $py scripts\forward_validate.py freeze --next 1

# ② 立刻校验完整性并提交账本（提交时间 = 「预测早于开奖」的旁证）
& $py scripts\forward_validate.py verify
git add backend/forward_ledger/ledger.json
git commit -m "chore(forward-ledger): freeze period <N> before its draw"

# ③ 等该期开奖后把新开奖写进数据文件，再计分
& $py scripts\forward_validate.py score

# ④ 进度（冻结 / 待开奖 / 已计分 + 有效独立样本）+ 当前判定 + 完整性
& $py scripts\forward_validate.py status

# ⑤ 再冻结下一期（回到 ①）
& $py scripts\forward_validate.py freeze --next 1

# 用线上库当前生效配置冻结（而不是快照文件）
& $py scripts\forward_validate.py freeze --next 1 --from-store
```

一次冻结多期（`--next 3`）不会报错，但会打印醒目警告并把重复行标成
`independent=false` —— 它们不会增加有效独立样本。

`--next N` 只取「尚未开奖**且尚未冻结**」的期号：账本里已冻结的期会被自动跳过并打印
一行说明，所以开奖后再跑一次 `freeze --next 1` 不会撞上已冻结的期号。想显式指定期号
请用 `--period`（append-only 仍然会拒绝重复期）。

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
