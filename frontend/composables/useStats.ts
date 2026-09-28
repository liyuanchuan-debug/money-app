/* ==========================================================================
 * /api/stats/* 统计接口的展示层封装
 * --------------------------------------------------------------------------
 * 四个端点全部是**只读 / 纯计算**，绝不写库：
 *   GET  /api/stats/frequency     冷热统计（出现次数 / 出现率 / 期望值 / 偏差）
 *   GET  /api/stats/trend         特码走势 + 相邻波动分布
 *   GET  /api/stats/zodiac-trend  生肖走势（连出 / 最近窗口覆盖 / 轮转）
 *   POST /api/stats/backtest      候选生成引擎走步回测（严格无未来函数）
 *
 * 口径铁律（与 backend/routers/stats.py、backend/services/analytics.py 同源）：
 *   - 所有数字都只描述「本池已导入的 N 期数据」这个**样本**，
 *     响应体首字段就是 scope / sample_size，页面必须原样展示 scope；
 *   - 样本太薄时后端返回 data_status = INSUFFICIENT / data_status_label = 数据不足，
 *     页面必须如实渲染「数据不足」+ 原始计数，禁止自己编结论、禁止把样本内观察说成结论；
 *   - 后端给的解释性文案（note / notes / insufficient_reason /
 *     random_baseline_note / actual_in_pool_note）一律优先原样展示，UI 不重写口径。
 *
 * 类型全部按**实测**响应体（真实 200 期数据）声明；可能缺失 / 为 null 的字段一律标可选，
 * 页面不能因为某个字段缺席就白屏。
 * ========================================================================== */

import { withApiCache } from '~/utils/apiCache'

/* -------------------------------------------------------------------------- */
/* 公共信封 + 样本控制                                                         */
/* -------------------------------------------------------------------------- */

/** 四个端点共有的返回字段（scope / sample_size / data_status / data_status_label） */
export interface StatsEnvelope {
  /** 口径文案，例：「本池已导入 200 期数据内」——必须原样展示 */
  scope: string
  /** 实际参与统计的期数（可能小于请求的 limit，因为本池总共只有这么多期） */
  sample_size: number
  /** 'OK' | 'INSUFFICIENT'（后端目前只产出这两个值） */
  data_status: string
  /** 中文状态标签：'样本可用' | '数据不足' */
  data_status_label: string
  notes?: string[]
}

/** 后端 DATA_STATUS 常量（英文枚举落库 / 传输，汉字只出现在 label 里） */
export const STATS_STATUS_OK = 'OK'
export const STATS_STATUS_INSUFFICIENT = 'INSUFFICIENT'

/**
 * 「样本期数」控件可选项。
 * 本池目前共 200 期，所以默认取 200（= 全量样本）；500 留作本池继续导入后的余量。
 * 注意：后端 GET /openapi.json 实测 limit 上限是 1000（routers/stats.py 的 LIMIT_MAX），
 * 不是 500 —— 这里给到 500 只是控件粒度，不构成上限声明。
 */
export const STATS_SAMPLE_LIMIT_OPTIONS: number[] = [50, 100, 200, 500]
/** 默认样本期数（与全站 convention 一致：一次取最近 200 期） */
export const STATS_DEFAULT_SAMPLE_LIMIT = 200

/** 是否处于「数据不足」态（响应体缺失时按数据不足处理，宁可不给结论） */
export function isInsufficient(envelope: StatsEnvelope | null | undefined): boolean {
  if (!envelope) return false
  return envelope.data_status === STATS_STATUS_INSUFFICIENT
}

/** 从 notes 里挑出后端显式标注「数据不足」的提示（用于薄样本警示横幅） */
export function insufficientNotes(envelope: StatsEnvelope | null | undefined): string[] {
  return (envelope?.notes ?? []).filter(note => note.startsWith('数据不足'))
}

/* -------------------------------------------------------------------------- */
/* 展示格式化（页面必须复用，避免各页各写一套）                                */
/* -------------------------------------------------------------------------- */

/** 0 - 1 的比率 → 0 - 100 的百分比数值（RingGauge 用）；null 透传 */
export function pctOf(rate: number | null | undefined): number | null {
  if (rate === null || rate === undefined) return null
  const value = Number(rate)
  return Number.isFinite(value) ? value * 100 : null
}

/** 0 - 100 的百分比数值 → '7.58%'；拿不到时 '—'（绝不用 0% 冒充） */
export function formatPercentValue(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return '—'
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return `${numeric.toFixed(decimals)}%`
}

/** 0 - 1 的比率 → '7.58%' */
export function formatRate(rate: number | null | undefined, decimals = 2): string {
  return formatPercentValue(pctOf(rate), decimals)
}

/** 0 - 1 的比率差 → 百分点文案（'+1.45pp' / '-6.12pp'） */
export function formatSignedPp(diff: number | null | undefined, decimals = 2): string {
  const value = pctOf(diff)
  if (value === null) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(decimals)}pp`
}

/** 0 - 1 的比率 → 无符号百分点文案（'1.70pp'），用于标准误这类非差值量 */
export function formatPp(value: number | null | undefined, decimals = 2): string {
  const pp = pctOf(value)
  return pp === null ? '—' : `${pp.toFixed(decimals)}pp`
}

/** 计数展示；拿不到时 '—' */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const numeric = Number(value)
  return Number.isFinite(numeric) ? String(Math.trunc(numeric)) : '—'
}

/** 号码 / 期号补零（01 - 49），非法值退化成占位符（与 useDraws.padNumber 同语义） */
export function padStatNumber(value: number | string | null | undefined, width = 2): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  return String(Math.trunc(Math.abs(numeric))).padStart(width, '0')
}

/* -------------------------------------------------------------------------- */
/* 2. 冷热统计                                                                 */
/* -------------------------------------------------------------------------- */

export interface FrequencyNumberRow {
  number: number
  zodiac: string
  zodiac_label: string
  appearances: number
  /** 0 - 1 */
  rate: number
  /** 1 / 49 ≈ 0.0204 */
  expected_rate: number
  expected_count: number
  deviation: number
}

export interface FrequencyZodiacRow {
  zodiac: string
  zodiac_label: string
  appearances: number
  rate: number
  /** 按各期农历年生肖表加权后的期望率；生肖表不可用时为 null */
  expected_rate: number | null
  deviation: number | null
}

/** 样本功效：说明「这点样本量能支撑什么结论」（本池 200 期 → 不足以支持强结论） */
export interface SamplePower {
  draws: number
  expected_appearances_per_number: number
  expected_appearances_per_zodiac: number
  /** 单号码期望出现次数 >= 10 次所需的期数（490） */
  strong_conclusion_min_draws: number
  can_support_strong_conclusion: boolean
  /** 生肖表可用（能映射农历年）的期数 */
  mapped_draws: number
}

export interface FrequencyStats extends StatsEnvelope {
  numbers: FrequencyNumberRow[]
  zodiacs: FrequencyZodiacRow[]
  expected_number_rate: number
  sample_power: SamplePower
  /** 主提示（与 notes[0] 同文） */
  note: string
  notes: string[]
}

/* -------------------------------------------------------------------------- */
/* 3. 特码走势                                                                 */
/* -------------------------------------------------------------------------- */

export interface TrendPoint {
  period: number
  draw_date: string
  special_number: number
  zodiac: string
  zodiac_label: string
  /** 与上一期的差值；序列第一期恒为 null */
  diff: number | null
  /** 'small' | 'normal' | 'big'；序列第一期恒为 null */
  wave_type: string | null
  wave_label: string | null
}

export interface WaveDistributionItem {
  type: string
  label: string
  count: number
  /** count / total_pairs；无相邻对时为 null */
  rate: number | null
}

export interface TrendStats extends StatsEnvelope {
  /** 按期号升序（旧 → 新） */
  series: TrendPoint[]
  wave_distribution: {
    total_pairs: number
    items: WaveDistributionItem[]
    dominant: string | null
    dominant_label: string | null
  }
  /** 波动阈值口径（与 /api/settings 一致） */
  settings: { small_max: number; normal_max: number; big_min: number }
  notes: string[]
}

/* -------------------------------------------------------------------------- */
/* 4. 生肖走势                                                                 */
/* -------------------------------------------------------------------------- */

export interface ZodiacTrendPoint {
  period: number
  draw_date: string
  zodiac: string
  zodiac_label: string
}

export interface ZodiacTrendRow {
  zodiac: string
  zodiac_label: string
  appearances: number
  /** 样本内最长连续出现期数 */
  max_streak: number
  /** 最长连出结束在哪一期 */
  max_streak_end_period: number | null
  /** 仅在最新一期结尾处仍在延续时 > 0 */
  current_streak: number
}

export interface ZodiacTrendStats extends StatsEnvelope {
  series: ZodiacTrendPoint[]
  zodiacs: ZodiacTrendRow[]
  /** 最长连出前 5 名 */
  max_streak_leaders: ZodiacTrendRow[]
  recent_window: {
    requested: number
    /** 实际取到的窗口长度（可能不足 requested，因为样本更短） */
    used: number
    /** 最近 used 期的生肖英文码，按时间升序 */
    sequence: string[]
    present: string[]
    present_labels: string[]
    missing: string[]
    missing_labels: string[]
    distinct_count: number
    available: number
  }
  rotation: { distinct_in_recent: number; available: number; note: string }
  notes: string[]
}

/* -------------------------------------------------------------------------- */
/* 6. 策略回测                                                                 */
/* -------------------------------------------------------------------------- */

export interface BacktestResultRow {
  period: number
  draw_date: string
  /** 该期预测时已知的上一期特码 */
  latest_used: number
  previous_used: number | null
  predicted: number[]
  actual: number
  hit: boolean
  /** 预测当时已知的上一期波动 */
  prev_wave_type: string | null
  prev_wave_label: string | null
  /** 事后归因：当期实际与上一期的差值分类（不参与预测） */
  realized_wave_type: string
  realized_wave_label: string
  realized_diff: number
  /** 实际特码是否落在候选池内（落在池外则引擎不可能命中） */
  actual_in_candidate_pool: boolean
}

export interface BacktestWaveBucket {
  type: string
  label: string
  evaluated: number
  hits: number
  /** hits / evaluated；evaluated = 0 时为 null */
  hit_rate: number | null
}

export interface BacktestSettings {
  small_max: number
  normal_max: number
  mode: string
  pick_count: number
  big_min: number
  /** 实际生效注数（单挑模式恒为 1） */
  effective_pick_count: number
  /** 避开重肖是否生效（与 /api/settings 同源；老后端可能缺此字段） */
  exclude_repeat_zodiac?: boolean
  /** 走势加权偏好 / 近窗（扫描与单次回测均回报；老后端可能缺） */
  trend_bias?: string
  trend_bias_label?: string
  trend_window?: number
  avoid_cold_enabled?: boolean
  avoid_cold_days?: number
}

export interface BacktestVerdict {
  kind: 'insufficient' | 'noise' | 'beyond'
  label: string
  text: string
  /** 后端字段 within_noise */
  within_noise?: boolean
}

export interface BacktestStats extends StatsEnvelope {
  /** 本次运行**实际生效**的参数（后端以「已保存的设置」为基线 + 显式覆盖推导，用于复现） */
  settings: BacktestSettings
  /**
   * 逐字段来源（后端 additive 字段）：request_override / saved_settings / default。
   * 老版本后端没有该字段，故为可选；页面缺失时显示「—」，禁止自行推断来源。
   */
  parameter_sources?: Record<'small_max' | 'normal_max' | 'mode' | 'pick_count', string>
  evaluation_window: {
    draws_used: number
    min_prior_draws: number
    first_evaluated_period: number | null
    last_evaluated_period: number | null
    evaluated: number
    skipped: number
    /** 后端生成的口径描述（可直接展示） */
    description: string
  }
  evaluated: number
  hits: number
  /** 命中率 0 - 1；无可评估期数时为 null */
  hit_rate: number | null
  /** 随机参考值 = 有效注数 / 49 */
  random_baseline_hit_rate: number
  random_baseline_note: string
  /** 命中率 - 随机参考值（0 - 1）；无法计算时为 null */
  hit_rate_minus_baseline: number | null
  /** 命中率的抽样标准误（0 - 1）；无法计算时为 null */
  hit_rate_standard_error: number | null
  /** 与前端 backtestVerdict 同口径的判定（后端 additive；老后端可能缺） */
  verdict?: BacktestVerdict
  average_available_numbers: number | null
  /** 实际特码落在候选池内的比例 —— 命中率的理论上限 */
  actual_in_pool_rate: number | null
  actual_in_pool_note: string
  /** 按**当期实际**波动分类（事后归因）；扫描接口不返回 */
  wave_breakdown?: { items: BacktestWaveBucket[] }
  /** 按**预测当时已知**的上一期波动分类；扫描接口不返回 */
  wave_breakdown_by_prev?: { items: BacktestWaveBucket[] }
  /** 逐期明细；扫描接口不返回 */
  results?: BacktestResultRow[]
  notes: string[]
}

/* -------------------------------------------------------------------------- */
/* 参数扫描 POST /api/stats/backtest/sweep                                       */
/* -------------------------------------------------------------------------- */

export interface BacktestSweepRow {
  trend_bias: string
  trend_bias_label: string
  trend_window: number
  trend_window_label: string
  avoid_cold_enabled: boolean
  avoid_cold_days: number
  mode: string
  pick_count: number
  effective_pick_count: number
  evaluated: number
  hits: number
  hit_rate: number | null
  random_baseline_hit_rate: number
  hit_rate_minus_baseline: number | null
  hit_rate_standard_error: number | null
  actual_in_pool_rate: number | null
  verdict: BacktestVerdict
  /** 是否与扫描时的已保存设置（held）在三轴上一致 */
  matches_baseline: boolean
}

export interface BacktestSweepStats extends StatsEnvelope {
  axes: {
    trend_bias: string[]
    trend_window: number[]
    avoid_cold_enabled: boolean[]
  }
  held_settings: {
    mode: string
    pick_count: number
    small_max: number
    normal_max: number
    big_min: number
    exclude_repeat_zodiac: boolean
    avoid_cold_days: number
    trend_bias: string
    trend_window: number
    avoid_cold_enabled: boolean
  }
  combo_count: number
  beyond_count: number
  noise_count: number
  insufficient_count: number
  rows: BacktestSweepRow[]
  notes: string[]
}

/* -------------------------------------------------------------------------- */
/* 收益仪表 GET /api/stats/pnl                                                  */
/* -------------------------------------------------------------------------- */

export interface PnlPick {
  number: number
  amount: number
  role?: string
  role_label?: string
  hit?: boolean | null
  payout?: number
}

export interface PnlRound {
  id?: number
  period: number
  base_period?: number | null
  draw_date?: string | null
  special_number?: number | null
  odds: number
  cost: number
  payout: number
  profit: number | null
  hit: boolean | null
  mode?: string
  picks: PnlPick[]
  status: string
  status_label: string
  /** 英文枚举：SIMULATED | REAL | SKIPPED；展示用 stake_mode_label */
  stake_mode?: string
  stake_mode_label?: string
  created_at?: string | null
  settled_at?: string | null
}

export interface PnlSeriesPoint {
  period: number
  profit: number
  cumulative_profit: number
  hit: boolean
}

export interface PnlSummary {
  total_cost: number
  total_payout: number
  total_profit: number
  settled_rounds: number
  hit_rounds: number
  miss_rounds: number
  hit_rate: number | null
  pending_rounds: number
}

export interface PnlStats extends StatsEnvelope {
  odds: number
  odds_note?: string
  summary: PnlSummary
  series: PnlSeriesPoint[]
  recent: PnlRound[]
}

/**
 * POST /api/stats/backtest 的请求体：只传显式覆盖项。
 * 未传的字段由后端取**当前调用者已保存的设置**作基线（响应里 parameter_sources 标明来源）。
 */
export interface BacktestPayload {
  /** 'even' | 'weighted' | 'single'；不传 = 沿用已保存设置 */
  mode?: string
  /** 1 - 10；不传 = 沿用已保存设置（注意：mode=single 时后端 effective_pick_count 恒为 1） */
  pick_count?: number
  small_max?: number
  normal_max?: number
  limit?: number
}

/**
 * POST /api/stats/backtest/sweep 的请求体。
 * 轴列表省略 = 后端默认（bias×window×avoid_cold ≈ 32 组）；空列表由后端回退默认。
 * 只读，不写库。
 */
export interface BacktestSweepPayload {
  mode?: string
  pick_count?: number
  small_max?: number
  normal_max?: number
  limit?: number
  trend_biases?: string[]
  trend_windows?: number[]
  avoid_cold_values?: boolean[]
}

/* -------------------------------------------------------------------------- */
/* 回测判定（**本文件是唯一来源**，页面必须原样渲染 text）                      */
/* -------------------------------------------------------------------------- */

export type StatsVerdictKind = 'insufficient' | 'noise' | 'beyond'

export interface StatsVerdict {
  kind: StatsVerdictKind
  /** 短标签（chip / 标题） */
  label: string
  /** 完整判定语句：必须原样展示给用户，禁止改写或弱化 */
  text: string
  /** true = 差值落在抽样噪声内（= 无法证明策略优于随机） */
  withinNoise: boolean
}

const VERDICT_INSUFFICIENT: StatsVerdict = {
  kind: 'insufficient',
  label: '数据不足',
  text: '数据不足：可评估期数不足，无法把引擎命中率与随机参考值做比较。',
  withinNoise: true,
}

/**
 * 回测结论判定：严格按照 |命中率 - 随机参考值| 是否落在 2 倍抽样标准误内。
 * 落在噪声内时必须坦白说「无法证明该策略优于随机」，不得包装成正面结论。
 */
export function backtestVerdict(result: BacktestStats | null | undefined): StatsVerdict {
  if (!result) return VERDICT_INSUFFICIENT

  const {
    evaluated,
    hit_rate: hitRate,
    random_baseline_hit_rate: baseline,
    hit_rate_minus_baseline: diff,
    hit_rate_standard_error: se,
  } = result

  if (
    !evaluated
    || !Number.isFinite(evaluated)
    || hitRate === null || hitRate === undefined
    || diff === null || diff === undefined
    || se === null || se === undefined
    || !Number.isFinite(hitRate)
    || !Number.isFinite(diff)
    || !Number.isFinite(se)
  ) {
    return VERDICT_INSUFFICIENT
  }

  const hitText = formatRate(hitRate)
  const baseText = formatRate(baseline)
  const diffText = formatSignedPp(diff)
  const seText = formatPp(se)
  const doubleSeText = formatPp(2 * se)

  if (Math.abs(diff) <= 2 * se) {
    return {
      kind: 'noise',
      withinNoise: true,
      label: '差值落在抽样噪声内 · 不能证明优于随机',
      text:
        `命中率 ${hitText} 与随机参考值 ${baseText} 相差 ${diffText}，`
        + `在 ${evaluated} 期样本下抽样标准误约 ${seText}；`
        + `差值落在抽样噪声内（未超过 2 倍标准误 ≈ ${doubleSeText}），`
        + '目前无法证明该策略优于随机。',
    }
  }

  return {
    kind: 'beyond',
    withinNoise: false,
    label: '差值超过 2 倍标准误 · 仍不构成承诺',
    text:
      `命中率 ${hitText} 与随机参考值 ${baseText} 相差 ${diffText}，`
      + `在 ${evaluated} 期样本下标准差约 ${seText}；`
      + `差值超过 2 倍标准误（≈ ${doubleSeText}），但单一小样本内的偏离仍可能由其他因素造成，`
      + '不构成对未来命中能力的任何承诺。',
  }
}

/* -------------------------------------------------------------------------- */
/* 接口封装                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * /api/stats/* 的取数封装。
 * 与 useApi.ts / useAuth.ts 同一套传输约定：
 *   - 浏览器：`$fetch` + `credentials: 'include'`（会话是 HttpOnly Cookie）
 *   - SSR：在 `useStats()` 调用时捕获一次 `useRequestFetch()`，把浏览器送来的
 *     Cookie 原样转发给后端（服务端裸 `$fetch` 不会自动带 Cookie）
 * 错误交给调用方（useAsyncData 的 error / 页面 catch）处理，不吞异常。
 */
export function useStats() {
  const config = useRuntimeConfig()
  const baseURL = config.public.apiBase as string

  /**
   * SSR 专用的「带请求上下文的 fetch」。
   *
   * 为什么在这里（`useStats()` 调用时）捕获一次，而不是在 `statsFetch` 里现取：
   * `useRequestFetch()` 依赖当前 Nuxt 实例的 `ssrContext.event`，**只能在 setup /
   * 请求上下文里调用**。`statsFetch` 完全可能在 setup 之外被调用（`useAsyncData`
   * 的 handler、`watch` 回调等），那时再调就取不到了。在 `useStats()` 时把实例钉住，
   * 之后无论从哪个回调里调 `statsFetch` 都能用上。
   *
   * 拿不到请求上下文时静默回退到普通 `$fetch`：只是这一次没有登录态，绝不抛错。
   */
  let requestFetch: typeof $fetch | null = null
  if (!import.meta.client) {
    try {
      requestFetch = useRequestFetch() as unknown as typeof $fetch
    } catch {
      requestFetch = null
    }
  }

  async function statsFetch<T>(path: string, options: Parameters<typeof $fetch>[1] = {}) {
    // 与 useApi.apiFetch 同一套传输层缓存：GET 短 TTL + 全方法 in-flight 去重。
    return withApiCache(path, options, () => {
      const fetcher = requestFetch ?? $fetch
      return fetcher<T>(path, {
        baseURL,
        // 会话是后端下发的 HttpOnly Cookie：不带 credentials 的话登录「看起来成功」，
        // 但后续每个请求仍是匿名。放在 ...options 之前，调用方显式传值时仍可覆盖。
        credentials: 'include',
        ...options,
      })
    })
  }

  /** limit 为空时交给后端取全部（本池当前 200 期） */
  function limitQuery(limit?: number | null) {
    return limit && Number.isFinite(limit) ? { limit: Math.trunc(limit) } : undefined
  }

  return {
    baseURL,

    /** 2. 冷热统计：出现次数 / 出现率 / 期望值 / 偏差 + 样本功效 */
    frequency: (limit?: number | null) =>
      statsFetch<FrequencyStats>('/api/stats/frequency', { query: limitQuery(limit) }),

    /**
     * 3. 特码走势：升序序列 + 相邻波动分类。
     * small_max / normal_max 可覆盖波动阈值（不传则用 /api/settings 的当前配置）。
     */
    trend: (limit?: number | null, thresholds?: { small_max?: number; normal_max?: number }) =>
      statsFetch<TrendStats>('/api/stats/trend', {
        query: {
          ...limitQuery(limit),
          ...(thresholds?.small_max !== undefined ? { small_max: thresholds.small_max } : {}),
          ...(thresholds?.normal_max !== undefined ? { normal_max: thresholds.normal_max } : {}),
        },
      }),

    /** 4. 生肖走势：连出统计 + 最近 recent 期窗口覆盖 / 轮转（recent 上限 49） */
    zodiacTrend: (limit?: number | null, recent?: number | null) =>
      statsFetch<ZodiacTrendStats>('/api/stats/zodiac-trend', {
        query: {
          ...limitQuery(limit),
          ...(recent !== undefined && recent !== null && Number.isFinite(recent)
            ? { recent: Math.trunc(recent) }
            : {}),
        },
      }),

    /**
     * 6. 策略回测（walk-forward，严格无未来函数）。
     * 只传显式覆盖项：`{}` = 完全沿用当前用户**已保存的设置**（后端 GET /api/settings），
     * 请求体里显式给出的 mode / pick_count / small_max / normal_max 才是本次覆盖项。
     * limit 既可放在 query 也可放在 body，这里统一放 body（后端以 body 优先）。
     */
    backtest: (payload: BacktestPayload = {}) =>
      statsFetch<BacktestStats>('/api/stats/backtest', {
        method: 'POST',
        body: payload,
      }),

    /**
     * 6b. 参数扫描（样本内对照，只读）。
     * 默认网格 trend_bias × trend_window × avoid_cold；不写库、不改设置。
     * 排序按命中率降序仅便于浏览，不是最优策略。
     */
    backtestSweep: (payload: BacktestSweepPayload = {}) =>
      statsFetch<BacktestSweepStats>('/api/stats/backtest/sweep', {
        method: 'POST',
        body: payload,
      }),

    /** 模拟收益仪表：采用快照的累计模拟成本 / 兑付 / 盈亏 */
    pnl: (recent = 20) =>
      statsFetch<PnlStats>('/api/stats/pnl', {
        query: { recent },
      }),
  }
}
