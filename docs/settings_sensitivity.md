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
