# 设置敏感度审计：这 30 个旋钮到底改变了什么

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

## 复现

```powershell
cd backend
.\\.venv\\Scripts\\python.exe scripts\\settings_sensitivity.py `
    --draws-json data\\draws_70_279.json --out ..\\.tmp-wave\\settings_sensitivity.json
```
