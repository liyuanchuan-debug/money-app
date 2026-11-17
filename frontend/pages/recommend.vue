<script setup lang="ts">
import type { DrawItem } from '~/composables/useApi'
import {
  CHIP_MODE_OPTIONS,
  MIN_BET_AMOUNT,
  SOFT_REASON_LABELS,
  TOTAL_AMOUNT_MAX,
  TREND_WINDOW_DEFAULT,
  TREND_WINDOW_OPTIONS,
  avoidColdDaysText,
  avoidColdWeightText,
  periodsSinceLastText,
  type ChipMode,
  type Pick,
  type RecommendResult,
  type TrendNumberStat,
  type TrendWaveRoles,
} from '~/composables/useApi'
import { normalizeDraws, periodText, scopeLabel, zodiacText } from '~/composables/useDraws'
import { STATS_SAMPLE_LIMIT_OPTIONS } from '~/composables/useStats'
import {
  formatBandHitRate,
  latticeBandHitStats,
  maxReplayOffset,
  replayWaveBandAt,
} from '~/utils/predictWaveBand'

definePageMeta({ role: 'VIP' })

/**
 * 波浪买入法（财富密码）—— 只读消费 POST /api/recommend 的结果，展示层归一化。
 *
 * 写入语义（重要）：
 *   - 默认筹码模式 / 默认注数**只在设置页持久化**；
 *   - 本页的筹码模式、注数切换是**临时预览**：只影响本页这一次查看，绝不写回设置
 *     （本页从不调用 PUT /api/settings）。
 *   - 走势偏好加权已停用：本页不再提供热号/冷号旋钮；走势取样窗只改「走势分布参考」展示。
 *
 * 口径：本页所有数字都只描述「本池已导入」的开奖记录，样本不足就说「数据不足」。
 * 走势分布 = 近窗经验频率，不是真实概率，也不承诺提高命中率。
 * 采用快照当前为**模拟买入**：按生成号码与设定赔率试算，非真实投注记录。
 */
const api = useApi()

/** 本页一次读取的期数上限（与其他页共用同一口径）；只用于展示样本口径文案 */
const SAMPLE_LIMIT = 200
/** 临场注数范围（与后端 PICK_COUNT_MIN/MAX 一致） */
const BET_COUNT_MIN = 1
const BET_COUNT_MAX = 10

/** 临时预览用的筹码模式：初值仍来自设置页保存的默认值 */
const mode = ref<ChipMode>('ladder')
/** 临时预览：注数（档位 / 逆向 2-3-4 默认 9，可改；其它模式同样跟本页控件） */
const betCount = ref(9)
/** 档位尖刀：10 / 15 / 20，只影响第一注主攻（默认 10） */
const stakeTop = ref<10 | 15 | 20>(10)
const stakeTopOptions = [10, 15, 20] as const
/** 走势分布参考取样窗（仅展示；不改选号；本页切换不写库） */
const trendWindow = ref(TREND_WINDOW_DEFAULT)
const activeWaveTab = ref<'small' | 'normal' | 'big'>('small')
/** 下方「走势分布参考」大区块默认折叠，避免与卡片内同角色参考号重复刷屏 */
const trendDistExpanded = ref(false)
const copied = ref(false)
/** 最近一次复制来源，用于对应按钮显示「已复制」 */
const copiedSource = ref<'ticket' | ''>('')
const copyError = ref('')
const generateHit = useHitPulse()
const copyHit = useHitPulse()

function clampBetCount(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return BET_COUNT_MIN
  return Math.min(BET_COUNT_MAX, Math.max(BET_COUNT_MIN, Math.round(n)))
}

function nudgeBetCount(delta: number) {
  betCount.value = clampBetCount(betCount.value + delta)
}

function onBetCountInput(event: Event) {
  const raw = (event.target as HTMLInputElement).value
  betCount.value = clampBetCount(raw === '' ? BET_COUNT_MIN : Number(raw))
}

/**
 * 随机分配的重掷计数：0 表示用「期」+ 参数派生的确定性种子（同一期重算结果一致）。
 * 点「重新随机」后 +1，作为显式的 amount_seed 发给后端换取另一份分配。
 */
const rerollCount = ref(0)
const amountSeed = computed(() =>
  mode.value === 'random' && rerollCount.value > 0 ? rerollCount.value : undefined)

/**
 * 号码换批计数：0 = 同期默认样本（首屏自动生成）；点「生成财富密码」后 +1 换一批号。
 * 改注数 / 模式时归零，回到该结构下的默认样本。
 */
const pickRerollCount = ref(0)
const generating = ref(false)

/**
 * 设置与样本口径互不依赖，并行拉取；推荐结果依赖 mode（来自设置），必须等设置回来。
 * 远端 DB 下串行会把本页 SSR 拉到 settings+draws+recommend 三趟 RTT 之和。
 */
const [{ data: settings }, { data: rawDraws }] = await Promise.all([
  useAsyncData('settings', () => api.getSettings()),
  useAsyncData<DrawItem[]>('draws-sample', () => api.listDraws(SAMPLE_LIMIT, 0)),
])
// 临场尖刀初值跟设置页已保存的 stake_top；缺省 / 非法回退产品默认 10
if (settings.value?.stake_top === 10 || settings.value?.stake_top === 15 || settings.value?.stake_top === 20) {
  stakeTop.value = settings.value.stake_top
} else {
  stakeTop.value = 10
}
// 临场注数：其它模式跟设置；档位 / 逆向 2-3-4 强制 9（覆盖库内旧默认 6/8）
if (typeof settings.value?.pick_count === 'number') {
  betCount.value = clampBetCount(settings.value.pick_count)
}
if (mode.value === 'ladder') {
  betCount.value = 9
}
if (typeof settings.value?.trend_window === 'number') {
  trendWindow.value = settings.value.trend_window
}

/** 热度网格生肖：农历年映射与本池最新一期开奖日同源（只读表，不写库） */
const zodiacTableDate = computed(
  () => normalizeDraws(rawDraws.value)[0]?.draw_date ?? '',
)
const { data: heatZodiacTable } = await useAsyncData(
  () => `recommend-heat-zodiac-${zodiacTableDate.value}`,
  () =>
    zodiacTableDate.value
      ? api.zodiacTable(zodiacTableDate.value)
      : Promise.resolve(null),
  { watch: [zodiacTableDate] },
)

function parseDrawDayUtc(iso: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso).trim())
  if (!match) return null
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

/** 本池已加载样本内：各号距最近一次出现的自然日（最新一期开奖日为锚点） */
const heatDaysSinceLastByNumber = computed(() => {
  const newestFirst = normalizeDraws(rawDraws.value)
  const map = new Map<number, number | null>()
  for (let n = 1; n <= 49; n += 1) map.set(n, null)
  const anchorMs = newestFirst[0]?.draw_date
    ? parseDrawDayUtc(newestFirst[0].draw_date)
    : null
  if (anchorMs == null) return map
  for (let i = 0; i < newestFirst.length; i += 1) {
    const num = newestFirst[i].special_number
    if (map.get(num) !== null) continue
    const dayMs = parseDrawDayUtc(newestFirst[i].draw_date ?? '')
    if (dayMs == null) continue
    map.set(num, Math.max(0, Math.round((anchorMs - dayMs) / 86_400_000)))
  }
  return map
})

function heatDaysSinceShort(days: number | null | undefined): string {
  if (days == null || !Number.isFinite(days)) return '未出'
  if (days <= 0) return '今日'
  return `${Math.trunc(days)} 天`
}

const heatZodiacLabelByNumber = computed(() => {
  const map = new Map<number, string>()
  for (const row of heatZodiacTable.value?.numbers ?? []) {
    const label = row.zodiac_label?.trim()
    if (label) map.set(row.number, label)
  }
  return map
})

const sampleSize = computed(() => normalizeDraws(rawDraws.value).length)
const scope = computed(() => scopeLabel(sampleSize.value))
const truncated = computed(() => sampleSize.value >= SAMPLE_LIMIT)

/* ---------------- 01 - 49 出现热度（与开奖页同一口径：只数本池已导入样本） ---------------- */

/**
 * 数据源复用本页已抓取的样本（`draws-sample` = GET /api/draws），**不新增请求**。
 * 口径与开奖页一致：只统计「本池已导入」记录里特码的出现次数，不涉及任何全市场 / 预测语义。
 *
 * 「统计期数」窗口只是本页展示状态：默认取最大档（= 本页一次能加载的全部样本），
 * 切换只重算热度网格，不写库、不进任何请求体（api.recommend 的参数完全不受影响）。
 */
const heatWindowOptions = STATS_SAMPLE_LIMIT_OPTIONS.filter(
  option => Number.isFinite(option) && option <= SAMPLE_LIMIT,
)
/** 默认档 = 最大可选档（= 单次加载上限）：首屏等同「统计全部已加载样本」，行为与改动前一致 */
const HEAT_WINDOW_DEFAULT = heatWindowOptions.includes(100)
  ? 100
  : (heatWindowOptions[heatWindowOptions.length - 1] ?? SAMPLE_LIMIT)
const heatWindow = ref(HEAT_WINDOW_DEFAULT)

/** 实际参与统计的期数：本池更少时以全部期数为准（min 保证任何 slice 都不越界） */
const heatWindowEffective = computed(() => Math.min(heatWindow.value, sampleSize.value))

/**
 * 窗口内开奖记录。listDraws 返回**新→旧**（同类页面一致约定：本池最新一期在 [0]），
 * 所以「最近 N 期」= slice(0, N)；本池不足 N 期时切片天然返回全部，无需特判。
 */
const heatWindowDraws = computed(() => {
  const newestFirst = normalizeDraws(rawDraws.value)
  return newestFirst.slice(0, heatWindowEffective.value)
})

const heatCounts = computed(() => {
  const map = new Map<number, number>()
  for (const draw of heatWindowDraws.value) {
    map.set(draw.special_number, (map.get(draw.special_number) ?? 0) + 1)
  }
  return map
})

/** 01 - 49 固定 49 格；格内附带生肖、距上次出现；本期推荐号码由 HeatGrid marked 角标「预」标识 */
const heatCells = computed(() => {
  const lat = result.value?.lattice
  const bandSet = new Set(
    lat?.enabled && lat.band
      ? (lat.numbers ?? []).filter(row => row.in_band).map(row => pad(row.number))
      : [],
  )
  return Array.from({ length: 49 }, (_, index) => {
    const number = index + 1
    const padded = pad(number)
    const count = heatCounts.value.get(number) ?? 0
    const zodiac = heatZodiacLabelByNumber.value.get(number) ?? ''
    const days = heatDaysSinceLastByNumber.value.get(number)
    const omitShort = heatDaysSinceShort(days)
    const inBand = bandSet.has(padded)
    let hint = `所选期数内特码出现 ${count} 次`
    if (zodiac) hint += ` · 生肖 ${zodiac}`
    hint += ` · 距上次 ${avoidColdDaysText(days)}`
    if (inBand) hint += ' · 预测波动带内'
    return {
      number: padded,
      value: count,
      label: zodiac || undefined,
      sublabel: omitShort,
      hint,
    }
  })
})

/** 本期推荐号码（热度网格角标「预」；与 result.picks 一致，约 bet_count 注） */
const heatPickMarkedNumbers = computed(() =>
  (result.value?.picks ?? []).map(pick => pad(pick.number)),
)

/** 本期预测号里落在预测带外的（点阵 / 热力勾选时用 bloom 红，与改造前一致） */
const heatPickOutBandNumbers = computed(() =>
  (result.value?.picks ?? [])
    .filter(pick => pick.in_lattice_band !== true)
    .map(pick => pad(pick.number)),
)

/** 图例口径随所选窗口走，保证图例与格子永不打架（只描述样本内计数，无预测含义） */
const heatScope = computed(() => scopeLabel(heatWindowEffective.value))

/** 本池最新一期特码（仅用于在热度网格上高亮这一格，样本为空时 null） */
const latestSampleNumber = computed(
  () => normalizeDraws(rawDraws.value)[0]?.special_number ?? null,
)

function recommendPayload() {
  return {
    mode: mode.value,
    bet_count: betCount.value,
    amount_seed: amountSeed.value,
    // 必须带整数：undefined 会被 JSON 丢掉，后端永远当成第 0 批
    pick_seed: pickRerollCount.value,
    trend_bias: 'neutral' as const,
    trend_window: trendWindow.value,
    stake_top: mode.value === 'ladder' ? stakeTop.value : undefined,
  }
}

const {
  data: rawResult,
  pending,
  error,
} = await useAsyncData<RecommendResult>(
  () =>
    `recommend:${mode.value}:${betCount.value}:${trendWindow.value}:${stakeTop.value}:${pickRerollCount.value}:${rerollCount.value}`,
  () => api.recommend(recommendPayload()),
  {
    // 禁止用 SSR/静态 payload 顶掉换批结果（固定 key 时 refresh 会一直吐首屏那批号）
    getCachedData: () => undefined,
  },
)

const generateBusy = computed(() => pending.value || generating.value)

/**
 * 展示层归一化：把 recommend payload 里「模板按数组用」的字段补齐为数组。
 *
 * 为什么必须做：`RecommendResult` 的字段类型只是**编译期**约定，运行时不校验 ——
 * 只要响应里缺 `notes` / `picks` 等数组字段（旧版 / 部分 / 被代理截断的 payload），
 * 模板里 `result.notes.length`、`result.picks.length` 就会在渲染期抛
 * `Cannot read properties of undefined (reading 'length')`（本页历史崩溃点即
 * `result.notes.length`）。这里只在拿到对象时补齐缺失数组（缺字段按「无内容」展示，
 * 不编造），拿不到数据（undefined / null / 非对象）时原样返回 undefined，
 * 因此 `!result` 与 error 分支的语义完全不变。
 */
function normalizeRecommendResult(raw: unknown): RecommendResult | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const source = raw as Partial<RecommendResult>
  const asArray = <T>(value: T[] | null | undefined): T[] =>
    Array.isArray(value) ? value : []
  const soft = source.soft_weights
  const pivots = source.price_pivots
  return {
    ...(source as RecommendResult),
    picks: asArray(source.picks),
    notes: asArray(source.notes),
    missing_waves: asArray(source.missing_waves),
    focus_order: asArray(source.focus_order),
    latest_zodiac: asArray(source.latest_zodiac),
    ...(pivots
      ? {
          price_pivots: {
            ...pivots,
            support: asArray(pivots.support),
            resistance: asArray(pivots.resistance),
            preferred_numbers: asArray(pivots.preferred_numbers),
          },
        }
      : {}),
    ...(soft
      ? {
          soft_weights: {
            ...soft,
            repeat_number_picks: asArray(soft.repeat_number_picks),
            repeat_zodiac_picks: asArray(soft.repeat_zodiac_picks),
            stale_picks: asArray(soft.stale_picks),
          },
        }
      : {}),
  }
}

/** 模板统一读这个归一化后的对象（`result.value` 语义不变，只是数组字段一定存在） */
const result = computed(() => normalizeRecommendResult(rawResult.value))

/** 点阵 / 热力共用：当前选中会连线的号码（默认 = 本期预测 picks） */
const latticeSelected = ref<number[]>([])

const latticePickSet = computed(() => {
  const set = new Set<number>()
  for (const pick of result.value?.picks ?? []) {
    const n = Number(pick.number)
    if (Number.isFinite(n) && n >= 1 && n <= 49) set.add(Math.trunc(n))
  }
  return set
})

function syncLatticeSelectedFromPicks() {
  latticeSelected.value = [...latticePickSet.value].sort((a, b) => a - b)
}

watch(
  () => (result.value?.picks ?? []).map(pick => Number(pick.number)).join(','),
  () => {
    syncLatticeSelectedFromPicks()
  },
  { immediate: true },
)

function toggleLatticeNumber(raw: number | string) {
  const n = typeof raw === 'number' ? raw : Number(String(raw).replace(/^0+/, '') || raw)
  if (!Number.isFinite(n)) return
  const num = Math.trunc(n)
  if (num < 1 || num > 49) return
  const set = new Set(latticeSelected.value)
  if (set.has(num)) set.delete(num)
  else set.add(num)
  latticeSelected.value = [...set].sort((a, b) => a - b)
}

const heatSelectedNumbers = computed(() => latticeSelected.value.map(pad))

const latticeSelectedCount = computed(() => latticeSelected.value.length)

/**
 * 勾选号排序：仍在本期 picks 里的保持引擎名次，其余自选号按号码升序接在后面。
 * 再按当前模式摊金额，生成投注串（与热力格勾选同步）。
 */
const STAKE_ATTACK_SECOND = 10
const STAKE_REGULAR = 5
const STAKE_DEFENSE = 3
/** 与后端 stake_ladder_tier_counts 同口径 */
const STAKE_TIP_MAX = 2
const STAKE_ATTACK_SECOND_MAX = 3
const STAKE_REGULAR_MAX = 5
/** 尖刀 10：5 元档最多 3 注（2 个 10 → 3 个 5 → 其余 3） */
const STAKE_TIP10_FIVE_MAX = 3
/** 注数达到该值才把一档升到 2；未满时保持 1 注尖刀（如 16 注 → 1+3+5+其余） */
const STAKE_TIP_TWO_MIN_COUNT = 20

function stakeLadderTierCounts(
  count: number,
  tip: number,
): [number, number, number, number] {
  const target = Math.max(0, Math.trunc(count))
  if (target <= 0) return [0, 0, 0, 0]
  // 尖刀 10：2 个 10 元 → 最多 3 个 5 元 → 其余 3 元
  if (tip === 10) {
    const tipN = Math.min(STAKE_TIP_MAX, target)
    let rem = target - tipN
    const fiveN = Math.min(STAKE_TIP10_FIVE_MAX, rem)
    return [tipN, 0, fiveN, rem - fiveN]
  }
  const tipN = Math.min(
    STAKE_TIP_MAX,
    target >= STAKE_TIP_TWO_MIN_COUNT ? 2 : 1,
    target,
  )
  let rem = target - tipN
  const secondN = Math.min(STAKE_ATTACK_SECOND_MAX, rem)
  rem -= secondN
  const regularN = Math.min(STAKE_REGULAR_MAX, rem)
  const defenseN = rem - regularN
  return [tipN, secondN, regularN, defenseN]
}

function stakeLadderAmounts(top: number, count: number): number[] {
  const tip = top === 15 || top === 20 ? top : 10
  const n = Math.max(0, Math.min(49, Math.trunc(count)))
  const [tipN, secondN, regularN, defenseN] = stakeLadderTierCounts(n, tip)
  const rows: number[] = []
  for (let i = 0; i < tipN; i += 1) rows.push(tip)
  for (let i = 0; i < secondN; i += 1) rows.push(STAKE_ATTACK_SECOND)
  for (let i = 0; i < regularN; i += 1) rows.push(STAKE_REGULAR)
  for (let i = 0; i < defenseN; i += 1) rows.push(STAKE_DEFENSE)
  return rows
}

/** 均注：余数补给前几注（与后端均注口径一致的展示版） */
function evenAmounts(total: number, count: number, unit: number): number[] {
  const n = Math.max(0, Math.trunc(count))
  if (n <= 0) return []
  const u = Math.max(1, Math.trunc(unit) || 1)
  let budget = Math.max(0, Math.trunc(total))
  budget = Math.floor(budget / u) * u
  if (budget < u) return Array.from({ length: n }, () => 0)
  const base = Math.floor(budget / n / u) * u
  let rest = budget - base * n
  return Array.from({ length: n }, () => {
    const extra = rest >= u ? u : 0
    rest -= extra
    return base + extra
  })
}

function buildSelectionCopyText(picks: Array<{ number: number; amount: number }>): string {
  if (!picks.length) return ''
  const groups: Array<{ amount: number; numbers: number[] }> = []
  const amountIndex = new Map<number, number>()
  for (const pick of picks) {
    const amount = Math.trunc(pick.amount)
    const number = Math.trunc(pick.number)
    const idx = amountIndex.get(amount)
    if (idx === undefined) {
      amountIndex.set(amount, groups.length)
      groups.push({ amount, numbers: [number] })
    } else {
      groups[idx].numbers.push(number)
    }
  }
  // 格式与用户粘贴样例一致：独额「号：N元；」/ 同额「号、号：各N元；」，无合计行
  const lines: string[] = []
  for (const group of groups) {
    const joined = group.numbers.map(pad).join('、')
    if (group.numbers.length === 1) lines.push(`${joined}：${group.amount}元；`)
    else lines.push(`${joined}：各${group.amount}元；`)
  }
  return lines.join('\n')
}

const selectionTicketPicks = computed(() => {
  const selected = new Set(latticeSelected.value)
  if (!selected.size) return [] as Array<{ number: number; amount: number }>
  /** 红色 = 本期预测且落在预测带外 → 投注串一律 3 元，不占尖刀 / 10 / 5 档 */
  const redSet = new Set<number>()
  for (const pick of result.value?.picks ?? []) {
    if (pick.in_lattice_band === true) continue
    const n = Number(pick.number)
    if (!Number.isFinite(n)) continue
    const num = Math.trunc(n)
    if (selected.has(num)) redSet.add(num)
  }
  const ordered: number[] = []
  for (const pick of result.value?.picks ?? []) {
    const n = Number(pick.number)
    if (!Number.isFinite(n)) continue
    const num = Math.trunc(n)
    if (selected.has(num) && !ordered.includes(num)) ordered.push(num)
  }
  for (const n of [...selected].sort((a, b) => a - b)) {
    if (!ordered.includes(n)) ordered.push(n)
  }
  const core = ordered.filter(n => !redSet.has(n))
  const reds = ordered.filter(n => redSet.has(n))
  const activeMode = result.value?.mode ?? mode.value
  let coreAmounts: number[]
  if (activeMode === 'ladder') {
    coreAmounts = stakeLadderAmounts(stakeTop.value, core.length)
  } else if (activeMode === 'single') {
    const unit = Math.max(1, Number(result.value?.amount_unit) || 5)
    const total = Math.max(0, Number(result.value?.total_amount) || 0)
    const tip = Math.floor(total / unit) * unit
    coreAmounts = core.map((_, i) => (i === 0 ? tip : 0))
  } else {
    const unit = Math.max(1, Number(result.value?.amount_unit) || 5)
    const total = Math.max(0, Number(result.value?.total_amount) || 0)
    coreAmounts = evenAmounts(total, core.length, unit)
  }
  const picks: Array<{ number: number; amount: number }> = []
  for (let i = 0; i < core.length; i += 1) {
    const amount = coreAmounts[i] ?? 0
    if (amount > 0) picks.push({ number: core[i], amount })
  }
  for (const number of reds) {
    picks.push({ number, amount: STAKE_DEFENSE })
  }
  return picks
})

/** 投注串：始终按热力格当前勾选重排（空选则空串） */
const displayCopyText = computed(() => buildSelectionCopyText(selectionTicketPicks.value))

const selectionTicketTotal = computed(() =>
  selectionTicketPicks.value.reduce((sum, pick) => sum + pick.amount, 0),
)

/** 各注金额（随机分配下通常不等，如实展示，不做任何美化） */
const amounts = computed(() => result.value?.picks.map(pick => pick.amount) ?? [])
const amountsEqual = computed(() =>
  amounts.value.length > 0 && new Set(amounts.value).size === 1)

/** 避冷加权本次是否确实压低了金额（旧后端缺字段 → 视为未生效） */
const avoidColdActive = computed(() =>
  result.value?.avoid_cold?.enabled === true
  && (result.value?.avoid_cold?.penalized_picks ?? 0) > 0)

/** 实际分配到各注的合计（避冷加权压低后可能 < 最大投注金额） */
const stakedTotal = computed(() =>
  result.value?.staked_total ?? result.value?.total_amount ?? 0)

/**
 * 零注（预算连 1 个注码单位都覆盖不了）：后端返回 `status = 'no_ticket'` +
 * 英文原因码 + 中文说明，此时 `picks` 为空。
 *
 * **不能把空列表当成「正常但没选到号」**：必须把原因显示出来。
 * 老后端不返回该字段 → 按 `ok` 处理（保持原行为）。
 */
const NO_TICKET_FALLBACK_REASON = '预算不足以购买 1 注，请提高总金额或降低每注金额。'

const noTicket = computed(() => result.value?.status === 'no_ticket')

const noTicketReason = computed(() => {
  if (result.value?.status !== 'no_ticket') return ''
  return result.value.reason_message?.trim() || NO_TICKET_FALLBACK_REASON
})

const waveTabs = [
  { type: 'small' as const, label: '小波动' },
  { type: 'normal' as const, label: '常规波动' },
  { type: 'big' as const, label: '大跳' },
]

const roleBlocks: Array<{ key: 'primary' | 'secondary' | 'defense'; label: string; tone: 'aqua' | 'neutral' | 'nebula' }> = [
  { key: 'primary', label: '主推区', tone: 'aqua' },
  { key: 'secondary', label: '次选区', tone: 'neutral' },
  { key: 'defense', label: '防守区', tone: 'nebula' },
]

const activeWaveDist = computed<TrendWaveRoles | null>(() => {
  const waves = result.value?.trend_distributions?.waves
  if (!waves) return null
  return waves[activeWaveTab.value] ?? null
})

const trendWindowLabel = computed(() => {
  const used = result.value?.trend_distributions?.used_window
  const win = result.value?.trend_window ?? trendWindow.value
  if (used != null) {
    return win === 0 ? `本池全部 ${used} 期` : `本池近 ${used} 期`
  }
  return win === 0 ? '本池全部样本' : `本池近 ${win} 期`
})

function roleNumbers(role: 'primary' | 'secondary' | 'defense'): TrendNumberStat[] {
  return activeWaveDist.value?.[role] ?? []
}

/**
 * 卡片内「走势参考」：取该 pick 所属波动档 × 同角色列表
 * （wave_type → waves[wave].primary|secondary|defense）。
 */
function pickTrendRefs(pick: { wave_type: string; role: 'primary' | 'secondary' | 'defense' }): TrendNumberStat[] {
  const waves = result.value?.trend_distributions?.waves
  if (!waves) return []
  const band = waves[pick.wave_type]
  if (!band) return []
  return band[pick.role] ?? []
}

function formatRate(rate: number | undefined) {
  if (rate == null || !Number.isFinite(rate)) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

/** 近窗 0 次号的附注：有自然日差写「距上次 N 天」，否则「样本内未出现」。 */
function formatZeroCountNote(item: TrendNumberStat): string {
  if (item.days_since_last == null) return '样本内未出现'
  return `距上次 ${item.days_since_last} 天`
}

/** 走势列表 / tooltip 文案。 */
function trendCountLabel(item: TrendNumberStat): string {
  if ((item.count ?? 0) > 0) {
    return `${item.count} 次 · ${formatRate(item.rate)}`
  }
  return `0 次 · ${formatZeroCountNote(item)}`
}

/* ---------------------------------------------------------------------- */
/* 号码标签徽章：重号 / 同肖 / 冷号（只打标，不降权）                        */
/* ---------------------------------------------------------------------- */

/** 徽章色调：英文码 → StatChip tone（重号=暖红、同肖=琥珀、冷号=冷色） */
const SOFT_REASON_TONES: Record<string, 'bloom' | 'amber' | 'aqua'> = {
  repeat_number: 'bloom',
  repeat_zodiac: 'amber',
  stale: 'aqua',
}

/**
 * 本注命中的号码标签。
 * 口径由后端判定（repeat_number / repeat_zodiac / stale），这里只做展示过滤。
 */
function pickSoftReasons(pick: Pick): string[] {
  const reasons = pick.soft_reasons ?? []
  return reasons.filter(reason => reason in SOFT_REASON_LABELS)
}

/** 号码标签摘要（旧后端缺字段 → null，照实不展示） */
const softSummary = computed(() => result.value?.soft_weights ?? null)

const lattice = computed(() => result.value?.lattice ?? null)
const latticeBand = computed(() => lattice.value?.band ?? null)

/** 最新 6 肖展示条数（不重复生肖） */
const LATEST_ZODIAC_DISPLAY_COUNT = 6

/** 单期生肖键：英文码优先，其次中文标签 / 号码表（与最新 6 肖同口径） */
function drawZodiacKey(draw: {
  special_number: number
  zodiac?: string
  zodiac_label?: string
}): string {
  const code = typeof draw.zodiac === 'string' ? draw.zodiac.trim() : ''
  if (code) return code
  const fromDraw = zodiacText(draw)
  if (fromDraw) return fromDraw
  return heatZodiacLabelByNumber.value.get(draw.special_number) ?? ''
}

/**
 * 特码生肖组：与后端 ``zodiac_group`` 同口径 ``(n-1) % 12``。
 * 重肖统计必须用组号，不能只靠 API 生肖字符串（缺字段 / 中英不一致会漏计）。
 * 例：34 与 46 同组 → 相邻开出即重肖。
 */
function zodiacGroupOf(number: number): number {
  return (Math.trunc(number) - 1) % 12
}

function drawZodiacLabel(draw: {
  special_number: number
  zodiac?: string
  zodiac_label?: string
}): string {
  return zodiacText(draw) || drawZodiacKey(draw) || heatZodiacLabelByNumber.value.get(draw.special_number) || ''
}

/**
 * 本池最近一次「相邻两期」重号 / 重肖距最新隔了几期（纯展示）。
 * newest-first：draws[i] 与 draws[i+1]（更早一期）比较；periodsAgo=i（0=最新一期即重号/重肖）。
 * 重肖 = 相邻两期特码不同号、但 ``(n-1)%12`` 同组（与选号 ``is_repeat_zodiac`` 同口径；纯重号不算重肖）。
 */
const latestRepeatGaps = computed(() => {
  const draws = normalizeDraws(rawDraws.value)
  let numberAgo: number | null = null
  let zodiacAgo: number | null = null
  let numberPair: {
    number: number
    label: string
    period: number | null
  } | null = null
  let zodiacPair: {
    newer: number
    older: number
    label: string
    period: number | null
  } | null = null
  for (let index = 0; index < draws.length - 1; index += 1) {
    const newer = draws[index]
    const older = draws[index + 1]
    if (numberAgo == null && newer.special_number === older.special_number) {
      numberAgo = index
      numberPair = {
        number: newer.special_number,
        label: drawZodiacLabel(newer),
        period: newer.period,
      }
    }
    // 重肖：不同号 + 同生肖组（34 后接 46 算；46 后再 46 只算重号）
    if (
      zodiacAgo == null
      && newer.special_number !== older.special_number
      && zodiacGroupOf(newer.special_number) === zodiacGroupOf(older.special_number)
    ) {
      zodiacAgo = index
      zodiacPair = {
        newer: newer.special_number,
        older: older.special_number,
        label: drawZodiacLabel(newer) || drawZodiacLabel(older),
        period: newer.period,
      }
    }
    if (numberAgo != null && zodiacAgo != null) break
  }
  return { numberAgo, zodiacAgo, numberPair, zodiacPair }
})

function periodsAgoLabel(ago: number): string {
  return ago === 0 ? '最新' : `${ago}期前`
}

const WAVE_TREND_WINDOW_DEFAULT = 100
const WAVE_TREND_WINDOW_OPTIONS = [30, 60, 100, 0]
const waveTrendWindow = ref(WAVE_TREND_WINDOW_DEFAULT)
/**
 * 预测带往前步数：0 = 本期（最新日后的前瞻）；
 * 1 = 最近已开奖那一期当时的预测；越大越早，可一直往前翻。
 */
const latticeHistoryOffset = ref(0)

/**
 * 当前预测锚点特码历史上「出现过后的下一期」号码列表（纯展示，不参与选号）。
 * 开奖序列约定最新在前；跟 latticeHistoryOffset 同步：
 *   offset=0（本期）→ 锚点 draws[0]；offset>0（更早）→ 锚点 draws[offset]
 *   （与点阵回放的历史锚点同号）。
 * 更早的同号出现在更大下标，其下一期实开 = draws[i-1]。
 * 本期尚无下一期，不把 draws[0] 自己算进去；回放期已有实开，会计入。
 * 顺序 = 最近一次转移在前；同号可重复出现（每次历史都列）。
 */
const latestNumberNextDraws = computed(() => {
  const draws = normalizeDraws(rawDraws.value)
  const empty = {
    anchor: null as number | null,
    successors: [] as Array<{
      number: number
      fromPeriod: number | null
      toPeriod: number | null
      /** 该次「下一期实开」距本池最新一期隔了几期（0 = 就是最新一期） */
      periodsAgo: number
    }>,
  }
  if (!draws.length) return empty
  const offset = Math.max(
    0,
    Math.min(latticeHistoryOffset.value, draws.length - 1),
  )
  const anchor = draws[offset]?.special_number
  if (anchor == null) return empty
  const successors: Array<{
    number: number
    fromPeriod: number | null
    toPeriod: number | null
    periodsAgo: number
  }> = []
  for (let index = 1; index < draws.length; index += 1) {
    if (draws[index].special_number !== anchor) continue
    const next = draws[index - 1]
    successors.push({
      number: next.special_number,
      fromPeriod: draws[index].period,
      toPeriod: next.period,
      // 下一期实开在 newest-first 下标 index-1 → 距最新隔了 index-1 期
      periodsAgo: index - 1,
    })
  }
  return { anchor, successors }
})

/**
 * 预测点阵旁：从当前回放锚点起，特码历史上最新开出的 6 个不重复生肖（新→旧）。
 * 跟 latticeHistoryOffset 同步（本期 / 更早），避免「今日也是同肖时把前天同号吞掉却看不出号码」。
 * 每项带上首次入列的特码与距最新隔了几期；纯展示，不参与选号。
 */
const latestSixZodiacs = computed(() => {
  const draws = normalizeDraws(rawDraws.value)
  const out: Array<{
    code: string
    label: string
    number: number
    period: number | null
    periodsAgo: number
  }> = []
  if (!draws.length) return out
  const start = Math.max(
    0,
    Math.min(latticeHistoryOffset.value, draws.length - 1),
  )
  const seen = new Set<string>()
  for (let index = start; index < draws.length; index += 1) {
    const draw = draws[index]
    const code = typeof draw.zodiac === 'string' ? draw.zodiac.trim() : ''
    const fromDraw = zodiacText(draw)
    const fromTable = heatZodiacLabelByNumber.value.get(draw.special_number) ?? ''
    const label = fromDraw || fromTable || code
    if (!label) continue
    const key = code || label
    if (seen.has(key)) continue
    seen.add(key)
    out.push({
      code,
      label,
      number: draw.special_number,
      period: draw.period,
      periodsAgo: index,
    })
    if (out.length >= LATEST_ZODIAC_DISPLAY_COUNT) break
  }
  return out
})

const latticeReplayWindow = computed(() => {
  const newestFirst = normalizeDraws(rawDraws.value)
  const CHART_HISTORY_CAP = 500
  const sample = newestFirst.length > CHART_HISTORY_CAP
    ? newestFirst.slice(0, CHART_HISTORY_CAP)
    : newestFirst
  const window = typeof settings.value?.lattice_window === 'number'
    ? settings.value.lattice_window
    : 30
  return { sample, window, maxOffset: maxReplayOffset(sample.length) }
})

const historyReplay = computed(() => {
  const offset = latticeHistoryOffset.value
  if (offset <= 0) return null
  const { sample, window, maxOffset } = latticeReplayWindow.value
  if (offset > maxOffset) return null
  return replayWaveBandAt(
    sample.map(item => item.special_number),
    sample.map(item => Number(item.period) || 0),
    offset - 1,
    window,
  )
})

/** 本池预测带命中率：优先用已加载开奖即时重算（录入后 refresh draws-sample 即更新） */
const latticeHitStats = computed(() => {
  const fromApi = result.value?.lattice?.hit_stats
  const { sample, window } = latticeReplayWindow.value
  const local = latticeBandHitStats(
    sample.map(item => item.special_number),
    sample.map(item => item.period),
    window,
    12,
  )
  // 样本与后端一致时优先本地，保证录入刷新后不依赖再跑一次 recommend
  if (local.dataStatus === 'OK') return local
  if (fromApi && fromApi.data_status === 'OK') {
    return {
      dataStatus: 'OK' as const,
      sampleSize: fromApi.sample_size,
      evaluated: fromApi.evaluated,
      hits: fromApi.hits,
      misses: fromApi.misses,
      hitRate: fromApi.hit_rate,
      latticeWindow: fromApi.lattice_window,
      latest: fromApi.latest
        ? {
            targetIndex: fromApi.latest.target_index,
            period: fromApi.latest.period,
            anchor: fromApi.latest.anchor,
            actual: fromApi.latest.actual,
            diff: fromApi.latest.diff,
            hit: fromApi.latest.hit,
            low: fromApi.latest.low,
            high: fromApi.latest.high,
          }
        : null,
      recent: [],
    }
  }
  return local
})

function goLivePrediction() {
  latticeHistoryOffset.value = 0
}

function stepHistoryEarlier() {
  const { maxOffset } = latticeReplayWindow.value
  if (maxOffset <= 0) return
  latticeHistoryOffset.value = Math.min(maxOffset, latticeHistoryOffset.value + 1)
}

function stepHistoryLater() {
  latticeHistoryOffset.value = Math.max(0, latticeHistoryOffset.value - 1)
}

/** 点阵图左右键：delta>0 更早，delta<0 更新 */
function onLatticePeriodStep(payload: { delta: number }) {
  if (payload.delta > 0) stepHistoryEarlier()
  else if (payload.delta < 0) stepHistoryLater()
}

/** 竖列点选 / 回车：仅「本期」可改选中；回放只浏览 */
function onLatticeCandidateClick(payload: { value: number }) {
  if (latticeHistoryOffset.value > 0) return
  toggleLatticeNumber(payload.value)
}

watch(
  () => latticeReplayWindow.value.maxOffset,
  (maxOffset) => {
    if (latticeHistoryOffset.value > maxOffset) {
      latticeHistoryOffset.value = maxOffset
    }
  },
)

const waveTrendChart = computed(() => {
  type ColumnCandidate = {
    value: number
    label: string
    zodiac?: string
    tone: 'emerald' | 'bloom' | 'nebula' | 'slate'
    selected: boolean
    pinLabel?: boolean
  }
  type Annotation = {
    index: number
    value: number
    label: string
    tone: 'slate' | 'emerald' | 'bloom'
  }
  type ChartBand = {
    from: number
    to: number
    label: string
    tone?: 'aqua' | 'amber' | 'emerald' | 'bloom'
  }
  const empty = {
    series: [] as Array<{ name: string, values: Array<number | null>, tone?: 'aqua' }>,
    labels: [] as string[],
    bands: [] as ChartBand[],
    candidateColumn: [] as ColumnCandidate[],
    candidateColumnFromIndex: null as number | null,
    annotations: [] as Annotation[],
    pointCount: 0,
    historySummary: null as null | {
      offset: number
      maxOffset: number
      anchorPeriod: number
      targetPeriod: number
      anchor: number
      actual: number
      diff: number
      hit: boolean
      low: number
      high: number
    },
  }
  const meta = lattice.value
  const band = latticeBand.value
  const offset = latticeHistoryOffset.value
  const replay = historyReplay.value
  const useHistory = offset > 0
  if (!meta?.enabled) return empty
  if (!useHistory && !band) return empty
  if (useHistory && !replay) return empty

  const { sample, maxOffset } = latticeReplayWindow.value
  // offset>0：切到更早开奖日，走势去掉更新的 offset 期，末端 = 当时锚点
  let forChart = useHistory ? sample.slice(offset) : sample
  const ordered = forChart.slice().reverse()
  if (ordered.length < 2) return empty

  const specialValues: number[] = []
  const labels: string[] = []
  for (let i = 0; i < ordered.length; i += 1) {
    specialValues.push(ordered[i].special_number)
    labels.push(periodText(ordered[i]) || ordered[i].draw_date || `第${i + 1}点`)
  }
  const clampToLane = (value: number) => Math.min(49, Math.max(1, Math.round(value)))
  /** 与 pushDiffBands 画出来的整数色带同一口径（避免浮点带宽 vs 圆整色带打架） */
  const numberInPaintedBand = (num: number, anchor: number, low: number, high: number) => {
    if (!Number.isFinite(anchor) || !Number.isFinite(low) || !Number.isFinite(high)) return false
    const lowerFrom = clampToLane(anchor - high)
    const lowerTo = clampToLane(anchor - low)
    const upperFrom = clampToLane(anchor + low)
    const upperTo = clampToLane(anchor + high)
    const inLower = num >= Math.min(lowerFrom, lowerTo) && num <= Math.max(lowerFrom, lowerTo)
    const inUpper = num >= Math.min(upperFrom, upperTo) && num <= Math.max(upperFrom, upperTo)
    return inLower || inUpper
  }
  const pushDiffBands = (
    list: ChartBand[],
    anchor: number,
    low: number,
    high: number,
    label: string,
    tone: ChartBand['tone'],
  ) => {
    if (!Number.isFinite(anchor) || !Number.isFinite(low) || !Number.isFinite(high)) return
    const lowerFrom = clampToLane(anchor - high)
    const lowerTo = clampToLane(anchor - low)
    list.push({
      from: Math.min(lowerFrom, lowerTo),
      to: Math.max(lowerFrom, lowerTo),
      label,
      tone,
    })
    const upperFrom = clampToLane(anchor + low)
    const upperTo = clampToLane(anchor + high)
    list.push({
      from: Math.min(upperFrom, upperTo),
      to: Math.max(upperFrom, upperTo),
      label: '',
      tone,
    })
  }

  const endDraw = ordered[ordered.length - 1]
  const endValue = endDraw.special_number
  const annotations: Annotation[] = []
  const bands: ChartBand[] = []
  let candidateColumn: ColumnCandidate[] = []
  let candidateColumnFromIndex: number | null = null
  /** 回放文案 / 点色用的命中：跟画出来的色带一致 */
  let displayHit: boolean | null = replay ? replay.hit : null

  if (useHistory && replay) {
    const low = replay.band.low
    const high = replay.band.high
    // 点色跟「画出来的色带」走：带外实开必须是 bloom（红），不能因浮点命中仍亮绿
    const actualInBand = numberInPaintedBand(replay.actual, replay.anchor, low, high)
    displayHit = actualInBand
    pushDiffBands(
      bands,
      replay.anchor,
      low,
      high,
      `第${replay.anchorPeriod}期后的预测带`,
      actualInBand ? 'emerald' : 'amber',
    )
    candidateColumn = Array.from({ length: 49 }, (_, index) => {
      const num = index + 1
      const inBand = numberInPaintedBand(num, replay.anchor, low, high)
      const isActual = num === replay.actual
      let tone: ColumnCandidate['tone'] = 'slate'
      if (isActual) tone = actualInBand ? 'emerald' : 'bloom'
      else if (inBand) tone = 'amber'
      return {
        value: num,
        label: pad(num),
        zodiac: heatZodiacLabelByNumber.value.get(num) || undefined,
        tone,
        selected: inBand || isActual,
        // 实开号强制出标签并置顶，避免 01/02/03 等边缘号被邻点盖住
        pinLabel: isActual,
      }
    })
    candidateColumnFromIndex = specialValues.length - 1
    annotations.push({
      index: specialValues.length - 1,
      value: replay.anchor,
      label: `第 ${replay.anchorPeriod} 期 · 特码 ${pad(replay.anchor)}`,
      tone: 'slate',
    })
  } else if (band) {
    pushDiffBands(bands, endValue, Number(band.low), Number(band.high), '本期预测带', 'aqua')
    const selectedSet = new Set(latticeSelected.value)
    const pickSet = latticePickSet.value
    const pickInBand = new Map<number, boolean>()
    for (const pick of result.value?.picks ?? []) {
      const n = Number(pick.number)
      if (!Number.isFinite(n)) continue
      pickInBand.set(Math.trunc(n), pick.in_lattice_band === true)
    }
    candidateColumn = Array.from({ length: 49 }, (_, index) => {
      const num = index + 1
      const selected = selectedSet.has(num)
      const isPick = pickSet.has(num)
      let tone: ColumnCandidate['tone'] = 'slate'
      if (selected && isPick) tone = pickInBand.get(num) ? 'emerald' : 'bloom'
      else if (selected) tone = 'nebula'
      return {
        value: num,
        label: pad(num),
        zodiac: heatZodiacLabelByNumber.value.get(num) || undefined,
        tone,
        selected,
      }
    })
    if (candidateColumn.some(item => item.selected)) {
      candidateColumnFromIndex = specialValues.length - 1
    }
    annotations.push({
      index: specialValues.length - 1,
      value: endValue,
      label: `最新一期 ${periodText(endDraw)} · 特码 ${pad(endValue)}`,
      tone: 'slate',
    })
  }

  labels.push('')
  return {
    series: [{ name: '特码走势', values: specialValues, tone: 'aqua' as const }],
    labels,
    bands,
    candidateColumn,
    candidateColumnFromIndex,
    annotations,
    pointCount: specialValues.length,
    historySummary: replay
      ? {
          offset,
          maxOffset,
          anchorPeriod: replay.anchorPeriod,
          targetPeriod: replay.targetPeriod,
          anchor: replay.anchor,
          actual: replay.actual,
          diff: replay.diff,
          hit: displayHit === true,
          low: replay.band.low,
          high: replay.band.high,
        }
      : null,
  }
})

/**
 * 切换筹码模式 / 注数 / 走势取样窗 = 只重算本页（临时预览）。
 * ⚠️ 这里刻意**不调用** api.updateSettings：默认值只在设置页改。
 */
function clearCopyFeedback() {
  copied.value = false
  copiedSource.value = ''
  copyError.value = ''
}

watch(mode, (next) => {
  // 切回档位时对齐 2-3-4（9 注）；其它模式不改用户临场注数
  if (next === 'ladder' && (betCount.value === 6 || betCount.value === 8)) {
    betCount.value = 9
  }
})

watch([mode, betCount, trendWindow, stakeTop], () => {
  clearCopyFeedback()
  rerollCount.value = 0
  pickRerollCount.value = 0
})

/** 显式重新生成：换一批号码。直接 POST，不走 useAsyncData 缓存。 */
async function generate() {
  if (generateBusy.value) return
  generating.value = true
  clearCopyFeedback()
  pickRerollCount.value += 1
  try {
    rawResult.value = await api.recommend(recommendPayload())
    generateHit.fire()
  } finally {
    generating.value = false
  }
}

/** 重新随机：换一个显式种子，得到另一份金额分配（仅随机分配模式可点） */
async function reroll() {
  if (generateBusy.value) return
  generating.value = true
  rerollCount.value += 1
  clearCopyFeedback()
  try {
    rawResult.value = await api.recommend(recommendPayload())
    generateHit.fire()
  } finally {
    generating.value = false
  }
}

function pad(n: number) {
  return String(n).padStart(2, '0')
}

/**
 * 各注号码的生肖文案（汉字标签，缺失时退到英文码）。
 *
 * 口径：生肖是「号码 → 生肖」的**固定映射**，随农历年（春节）轮转；
 * 农历年由后端按本池最新一期开奖日推导，与上方「最新同肖」号码组同源。
 * 拿不到农历年时后端返回 null —— 这里照实留空，绝不拿前端本地日期猜。
 */
function pickZodiac(pick: { zodiac?: string | null; zodiac_label?: string | null }): string {
  return zodiacText(pick)
}

/** 波动 → StatChip 色调（小波动 / 常规 / 大跳） */
const waveTones: Record<string, 'emerald' | 'amber' | 'bloom'> = {
  small: 'emerald',
  normal: 'amber',
  big: 'bloom',
}

/** 角色 → StatChip 色调（主推 / 次选 / 防守） */
const roleTones: Record<string, 'aqua' | 'neutral' | 'nebula'> = {
  primary: 'aqua',
  secondary: 'neutral',
  defense: 'nebula',
}

/** AI 阵型：按 role 分组 */
const primaryPicks = computed(() => (result.value?.picks ?? []).filter(p => p.role === 'primary'))
const secondaryPicks = computed(() => (result.value?.picks ?? []).filter(p => p.role === 'secondary'))
const defensePicks = computed(() => (result.value?.picks ?? []).filter(p => p.role === 'defense'))

/** 盘面判定 → 色标 tone */
function marketTone(judgment: string): 'emerald' | 'amber' | 'bloom' {
  const t = (judgment || '').toLowerCase()
  if (t.includes('变盘') || t.includes('透支')) return 'bloom'
  if (t.includes('极寒')) return 'amber'
  return 'emerald' // 顺热期
}

/** 取值表与设置页共用同一份（composables/useApi.ts） */
const chipModes = CHIP_MODE_OPTIONS
const trendWindowOptions = TREND_WINDOW_OPTIONS

async function writeClipboard(text: string, source: 'ticket') {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    copiedSource.value = source
    copyError.value = ''
    copyHit.fire()
    setTimeout(() => {
      copied.value = false
      copiedSource.value = ''
    }, 2000)
  } catch {
    copied.value = false
    copiedSource.value = source
    copyError.value = '复制失败，请手动选中下方文本'
  }
}

async function copy() {
  const text = result.value?.copy_text || displayCopyText.value
  if (!text) return
  await writeClipboard(text, 'ticket')
}

useHead({ title: '波浪买入法 · 四叶沙盘' })
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <!-- PC 加宽：点阵 100 期一屏时内容区太窄会挤；手机仍用 3xl -->
    <div class="mx-auto flex w-full max-w-3xl flex-col gap-7 lg:max-w-6xl">
      <!-- 头部 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            波浪买入法
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            按本池已导入的开奖记录推算波动，给出当期的财富密码候选号码；所有数字只描述本池样本，
            样本不足时只输出「数据不足」，不做越界推断。
          </p>
          <div class="flex flex-wrap items-center gap-2">
            <StatChip tone="neutral" size="sm" dot>{{ scope }}</StatChip>
            <StatChip v-if="truncated" tone="amber" size="sm">
              本次只读取最近 {{ SAMPLE_LIMIT }} 期
            </StatChip>
            <NuxtLink to="/stats/pnl" class="text-xs text-aqua-300 underline-offset-2 hover:underline">
              模拟收益仪表
            </NuxtLink>
          </div>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 本期基线 + 显式重新生成 -->
      <MotionReveal :index="1">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
          <!-- 加载中（首屏还没有结果时） -->
          <div v-if="pending && !result" class="flex flex-col items-center gap-3 py-3">
            <SpecialBall :number="null" size="xl" tone="aqua" rolling label="计算中" />
            <p class="text-sm text-slate-400">正在按当前筹码模式推算…</p>
          </div>

          <!-- 接口不可用：如实报错（重试就走下方那颗「生成财富密码」） -->
          <p
            v-else-if="error"
            class="glass-panel rounded-2xl border border-bloom-400/40 px-4 py-3 text-sm text-bloom-200"
          >
            {{ (error as any)?.data?.detail || error.message || '生成失败' }}
          </p>

          <!-- 无结果：数据不足就说数据不足 -->
          <p v-else-if="!result" class="text-center text-sm text-slate-500">
            数据不足：本池还没有可用于推算的开奖记录。
          </p>

          <div v-else class="space-y-5">
            <div
              class="flex flex-col items-center gap-5 text-center sm:flex-row sm:items-center sm:justify-between sm:text-left"
            >
              <div class="space-y-3">
                <StatChip tone="aqua" size="sm" dot>最新开奖号</StatChip>
                <p class="num text-2xl font-semibold text-white">{{ pad(result.latest) }}</p>
                <div class="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                  <StatChip tone="neutral" size="sm">
                    上期号码 {{ result.previous === null ? '—' : pad(result.previous) }}
                  </StatChip>
                  <StatChip
                    v-if="result.prev_wave"
                    :tone="waveTones[result.prev_wave.type] ?? 'neutral'"
                    size="sm"
                  >
                    上期波动 {{ result.prev_wave.label }} · 差值 {{ result.prev_wave.diff }}
                  </StatChip>
                  <StatChip v-else tone="neutral" size="sm">上期波动：数据不足</StatChip>
                </div>
                <div class="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                  <span class="text-xs text-slate-500">本期侧重</span>
                  <StatChip
                    v-for="item in result.focus_order"
                    :key="item.type"
                    :tone="waveTones[item.type] ?? 'neutral'"
                    size="xs"
                  >
                    {{ item.label }}
                  </StatChip>
                </div>
                <div
                  v-if="result.adopted_round"
                  class="flex flex-wrap items-center justify-center gap-2 sm:justify-start"
                >
                  <StatChip tone="emerald" size="sm" dot>
                    模拟买入 · 第{{ result.adopted_round.period }}期 · 模拟成本
                    {{ result.adopted_round.cost }}元 · 赔率 {{ result.adopted_round.odds }}
                  </StatChip>
                  <p class="w-full text-[11px] leading-relaxed text-slate-500 sm:w-auto">
                    按当期生成号码与设定赔率试算；非真实投注记录。
                  </p>
                </div>
                <div class="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                  <span class="text-xs text-slate-500">
                    {{ result.settings?.exclude_repeat_zodiac ? '重肖（需避开）' : '最新同肖' }}
                  </span>
                  <!-- 这一组号码对应的生肖（与卡片上的生肖同一张农历年表，不会互相打架） -->
                  <StatChip v-if="result.latest_zodiac_label" tone="nebula" size="xs">
                    {{ result.latest_zodiac_label }}
                  </StatChip>
                  <StatChip
                    v-for="number in result.latest_zodiac"
                    :key="number"
                    :tone="result.settings?.exclude_repeat_zodiac ? 'bloom' : 'neutral'"
                    size="xs"
                  >
                    {{ pad(number) }}
                  </StatChip>
                  <span
                    v-if="!result.settings?.exclude_repeat_zodiac"
                    class="text-[11px] text-slate-500"
                  >
                    （当前未避开，同肖号可入选）
                  </span>
                </div>

                <!-- 最新 6 肖（AI 输入数据） -->
                <div
                  v-if="result.zodiac_6_recent?.length"
                  class="flex flex-wrap items-center gap-1.5"
                >
                  <span class="text-xs text-slate-500">最新 6 肖</span>
                  <div class="flex flex-wrap gap-1">
                    <StatChip
                      v-for="item in result.zodiac_6_recent"
                      :key="item.code"
                      tone="nebula"
                      size="xs"
                    >
                      {{ item.animal }}
                    </StatChip>
                  </div>
                </div>

                <!-- 极寒号（AI 输入数据） -->
                <div
                  v-if="result.extreme_cold?.length"
                  class="flex flex-wrap items-center gap-1.5"
                >
                  <span class="text-xs text-slate-500">极寒号</span>
                  <div class="flex flex-wrap gap-1">
                    <StatChip
                      v-for="item in result.extreme_cold.slice(0, 8)"
                      :key="item.number"
                      tone="amber"
                      size="xs"
                    >
                      {{ pad(item.number) }} · {{ item.days }}天
                    </StatChip>
                    <span
                      v-if="result.extreme_cold.length > 8"
                      class="text-[11px] text-slate-500"
                    >
                      +{{ result.extreme_cold.length - 8 }}
                    </span>
                  </div>
                </div>

                <!-- 最近重号 / 最近重肖 / X 后曾开 -->
                <div
                  v-if="latestRepeatGaps.numberPair || latestNumberNextDraws.anchor != null"
                  class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500"
                >
                  <span v-if="latestRepeatGaps.numberPair" class="inline-flex items-center gap-1.5">
                    <span>最近重号</span>
                    <StatChip tone="bloom" size="xs">{{ pad(latestRepeatGaps.numberPair.number) }}</StatChip>
                    <span class="num rounded-md border border-bloom-400/35 bg-bloom-400/10 px-1.5 py-0.5 text-[10px] text-bloom-200/90">{{ periodsAgoLabel(latestRepeatGaps.numberAgo) }}</span>
                  </span>
                  <span v-if="latestRepeatGaps.zodiacPair" class="inline-flex items-center gap-1.5">
                    <span>最近重肖</span>
                    <StatChip tone="amber" size="xs">{{ pad(latestRepeatGaps.zodiacPair.newer) }}</StatChip>
                    <span class="text-[10px] text-slate-500">/</span>
                    <StatChip tone="amber" size="xs">{{ pad(latestRepeatGaps.zodiacPair.older) }}</StatChip>
                    <span class="num rounded-md border border-amber-400/35 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-200/90">{{ periodsAgoLabel(latestRepeatGaps.zodiacAgo) }}</span>
                  </span>
                  <template v-if="latestNumberNextDraws.anchor != null">
                    <span v-if="latestRepeatGaps.numberPair || latestRepeatGaps.zodiacPair" class="mx-1 text-slate-600">·</span>
                    <span class="text-[11px] text-slate-500">
                      <span class="num text-slate-300">{{ pad(latestNumberNextDraws.anchor) }}</span>
                      后曾开 <template v-if="latestNumberNextDraws.successors.length">· {{ latestNumberNextDraws.successors.length }} 次</template>
                    </span>
                    <span v-if="latestNumberNextDraws.successors.length" class="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span
                        v-for="(item, i) in latestNumberNextDraws.successors"
                        :key="`${item.fromPeriod}-${item.toPeriod}-${item.number}-${i}`"
                        class="inline-flex items-center gap-1"
                      >
                        <StatChip tone="aqua" size="xs">{{ pad(item.number) }}</StatChip>
                        <span class="num text-[10px] text-slate-500">{{ item.periodsAgo === 0 ? '最新' : `${item.periodsAgo}期前` }}</span>
                      </span>
                    </span>
                    <span v-else class="text-[11px] text-amber-200/90">本池此前未开过该号</span>
                  </template>
                </div>
              </div>

              <div class="shrink-0">
                <SpecialBall
                  :number="result.latest"
                  size="xl"
                  tone="aqua"
                  label="最新一期"
                  :glow="true"
                />
              </div>
            </div>

          </div>

          <hr class="glass-hairline my-5">

          <!-- 主行动：显式重新生成（首屏自动生成一次的行为保留；按钮常驻，任何状态下都能点） -->
          <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p class="text-xs leading-relaxed text-slate-500">
              打开本页会自动生成一期默认样本；再点右侧按钮会换一批号码（同期可复现的换批种子）。
              这里不会改动设置页保存的默认值。
            </p>
            <div class="relative inline-flex shrink-0 rounded-[14px]">
              <GlassButton
                variant="primary"
                size="lg"
                class="min-h-[48px] w-full sm:w-auto"
                :loading="generateBusy"
                :disabled="generateBusy"
                @click="generate"
              >
                {{ generateBusy ? '生成中…' : (pickRerollCount > 0 ? `换一批（第 ${pickRerollCount} 批）` : '生成财富密码') }}
              </GlassButton>
              <PulseRing :trigger="generateHit.key" tone="aqua" :rings="2" />
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>

      <template v-if="result && !error">
        <!-- 无解波动提示 -->
        <MotionReveal v-if="result.missing_waves.length" :index="2">
          <div class="glass-panel space-y-1.5 rounded-2xl border border-amber-400/50 px-4 py-3">
            <p
              v-for="item in result.missing_waves"
              :key="item.type"
              class="text-sm font-medium text-amber-200"
            >
              ⚠ {{ item.note }}
            </p>
          </div>
        </MotionReveal>

        <!-- 财富密码（AI 阵型 · 紧凑投注票据） -->
        <MotionReveal :index="5" class="space-y-1">
          <GlassPanel padding="md" rounded="3xl" class="overflow-hidden">
            <!-- 标题行 -->
            <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-base font-medium text-white">财富密码</h2>
              <div class="flex flex-wrap items-center gap-1.5">
                <StatChip
                  v-if="result.market_judgment"
                  :tone="marketTone(result.market_judgment)"
                  size="xs"
                  dot
                >
                  {{ result.market_judgment }}
                </StatChip>
                <StatChip tone="neutral" size="xs">
                  ¥{{ result.staked_total }} · {{ result.picks.length }}注
                </StatChip>
              </div>
            </div>
            <p v-if="result.logic_brief" class="mb-1.5 text-[11px] leading-relaxed text-slate-400">
              {{ result.logic_brief }}
            </p>

            <!-- 同肖优选对比（后端代码生成，非 AI 编造） -->
            <div
              v-if="result.same_zodiac_comparison?.length"
              class="mb-2 rounded-xl border border-white/10 bg-white/[0.03] px-3.5 py-2.5"
            >
              <p class="mb-1.5 text-xs font-semibold tracking-wide text-slate-300">同肖优选对比</p>
              <ul class="space-y-1">
                <li
                  v-for="(line, i) in result.same_zodiac_comparison"
                  :key="i"
                  class="text-xs leading-relaxed text-slate-400"
                >
                  · {{ line }}
                </li>
              </ul>
            </div>
            <p
              v-if="result.ai_violations?.length"
              class="mb-1.5 text-[10px] leading-relaxed text-amber-300/80"
            >
              AI 第 {{ result.ai_attempts }} 次生成通过校验 · 拦截：{{ result.ai_violations.join('；') }}
            </p>

            <!-- 零注兜底 -->
            <p v-if="noTicket" class="mb-2 rounded-lg border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-200">
              <strong>本次不出票</strong>：{{ noTicketReason }}
            </p>

            <!-- 三档票据 -->
            <div
              v-if="!noTicket"
              class="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.02]"
            >
              <!-- 1档 · 20元 主攻 -->
              <div v-if="primaryPicks.length" class="relative bg-gradient-to-r from-amber-400/[0.22] via-amber-400/[0.08] to-transparent px-3.5 py-3">
                <span class="absolute inset-y-0 left-0 w-[3px] bg-gradient-to-b from-amber-300 via-amber-400/60 to-transparent" />
                <div class="mb-2.5 flex items-center gap-2">
                  <span class="rounded-md bg-amber-400/15 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-amber-300 ring-1 ring-amber-400/25">
                    1档 · 主攻
                  </span>
                  <span class="num text-[13px] font-bold text-amber-300">¥{{ primaryPicks[0].amount }}</span>
                  <span class="text-[10px] text-slate-500">× 1 注</span>
                </div>
                <div class="flex items-center gap-3">
                  <SpecialBall :number="primaryPicks[0].number" size="lg" tone="aqua" glow />
                  <div class="min-w-0 flex-1">
                    <div class="mb-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
                      <span class="font-medium text-slate-200">{{ pickZodiac(primaryPicks[0]) || '—' }}</span>
                      <span class="text-slate-600">|</span>
                      <span class="text-slate-400">差值 {{ primaryPicks[0].diff }}</span>
                      <span class="text-slate-600">|</span>
                      <span class="text-slate-400">{{ primaryPicks[0].wave_label }}</span>
                    </div>
                    <p v-if="primaryPicks[0].reason" class="text-[11px] leading-relaxed text-slate-400">
                      {{ primaryPicks[0].reason }}
                    </p>
                  </div>
                </div>
              </div>

              <!-- 2档 · 10元 中坚 -->
              <div v-if="secondaryPicks.length" class="relative border-t border-white/[0.06] bg-gradient-to-r from-sky-400/[0.18] via-sky-400/[0.06] to-transparent px-3.5 py-3">
                <span class="absolute inset-y-0 left-0 w-[3px] bg-gradient-to-b from-sky-300 via-sky-400/60 to-transparent" />
                <div class="mb-2.5 flex items-center gap-2">
                  <span class="rounded-md bg-sky-400/15 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-sky-300 ring-1 ring-sky-400/25">
                    2档 · 中坚
                  </span>
                  <span class="num text-[13px] font-bold text-sky-300">¥{{ secondaryPicks[0].amount }}</span>
                  <span class="text-[10px] text-slate-500">× {{ secondaryPicks.length }} 注</span>
                </div>
                <div class="grid gap-1.5 sm:grid-cols-2">
                  <div
                    v-for="pick in secondaryPicks"
                    :key="pick.number"
                    class="flex items-center gap-2 rounded-xl bg-white/[0.06] px-2 py-1.5 ring-1 ring-white/10"
                  >
                    <SpecialBall :number="pick.number" size="sm" tone="nebula" :glow="false" />
                    <div class="min-w-0 flex-1">
                      <div class="text-[11px] font-medium text-slate-200">{{ pickZodiac(pick) || '—' }}</div>
                      <div
                        v-if="pick.reason"
                        class="text-[10px] leading-snug text-slate-500 line-clamp-2"
                        :title="pick.reason"
                      >
                        {{ pick.reason }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 3档 · 5元 防守 -->
              <div v-if="defensePicks.length" class="relative border-t border-white/[0.06] bg-gradient-to-r from-emerald-400/[0.16] via-emerald-400/[0.05] to-transparent px-3.5 py-3">
                <span class="absolute inset-y-0 left-0 w-[3px] bg-gradient-to-b from-emerald-300 via-emerald-400/60 to-transparent" />
                <div class="mb-2.5 flex items-center gap-2">
                  <span class="rounded-md bg-emerald-400/15 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-emerald-300 ring-1 ring-emerald-400/25">
                    3档 · 防守
                  </span>
                  <span class="num text-[13px] font-bold text-emerald-300">¥{{ defensePicks[0].amount }}</span>
                  <span class="text-[10px] text-slate-500">× {{ defensePicks.length }} 注</span>
                </div>
                <div class="grid gap-1.5 sm:grid-cols-3">
                  <div
                    v-for="pick in defensePicks"
                    :key="pick.number"
                    class="flex items-center gap-2 rounded-xl bg-white/[0.06] px-2 py-1.5 ring-1 ring-white/10"
                  >
                    <SpecialBall :number="pick.number" size="xs" tone="nebula" :glow="false" />
                    <div class="min-w-0 flex-1">
                      <div class="text-[11px] font-medium text-slate-200">{{ pickZodiac(pick) || '—' }}</div>
                      <div
                        v-if="pick.reason"
                        class="text-[10px] leading-snug text-slate-500 line-clamp-2"
                        :title="pick.reason"
                      >
                        {{ pick.reason }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 投注串（合并到底部，直接用后端 copy_text） -->
            <div
              v-if="!noTicket && result.copy_text"
              class="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-white/5 pt-2"
            >
              <code
                class="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-300"
                :title="result.copy_text"
              >
                {{ result.copy_text }}
              </code>
              <GlassButton size="sm" variant="primary" @click="copy" class="shrink-0">
                {{ copied && copiedSource === 'ticket' ? '已复制 ✓' : '复制' }}
              </GlassButton>
            </div>
            <p v-if="copyError && copiedSource === 'ticket'" class="mt-1 text-[10px] text-bloom-300">
              {{ copyError }}
            </p>
            <p v-else-if="copied && copiedSource === 'ticket'" class="mt-1 text-[10px] text-emerald-300">
              已复制到剪贴板
            </p>
          </GlassPanel>
        </MotionReveal>

        <MotionReveal v-if="lattice?.enabled && latticeBand && waveTrendChart.pointCount" :index="6" class="min-w-0">
          <GlassPanel padding="lg" rounded="3xl" class="min-w-0 space-y-3 overflow-hidden">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="space-y-1">
                <h2 class="text-lg font-medium text-white">预测点阵</h2>
                <p class="text-xs leading-relaxed text-slate-500">
                  「本期」= 最新开奖日之后的预测；「更早 / 更新」可一期一期往前或往后翻，按当天重画预测带与右侧号码连线，实开号会高亮。
                  点进图后：↑↓ 移动竖列号码，←→ 切换更早 / 更新，回车切换选中（仅本期）。
                  下方「N 期」是一屏显示多少期。命中率 = 实开差值落在当时预测带内的本池样本对照，不是概率。
                </p>
                <p
                  v-if="latticeHitStats.dataStatus === 'OK'"
                  class="text-sm text-slate-200"
                >
                  带内命中
                  <span class="font-medium text-aqua-200">
                    {{ latticeHitStats.hits }}/{{ latticeHitStats.evaluated }}
                  </span>
                  （{{ formatBandHitRate(latticeHitStats.hitRate) }}）
                  <span
                    v-if="latticeHitStats.latest"
                    class="ml-1 text-xs"
                    :class="latticeHitStats.latest.hit ? 'text-emerald-300/90' : 'text-rose-300/90'"
                  >
                    · 最近第 {{ latticeHitStats.latest.period ?? '—' }} 期
                    {{ latticeHitStats.latest.hit ? '落在带内' : '落在带外' }}
                  </span>
                </p>
                <p v-else class="text-xs text-amber-200/90">
                  本池至少要 3 期开奖记录，才能统计预测带命中率。
                </p>
              </div>
              <div class="flex flex-col items-stretch gap-2 sm:items-end">
                <div class="flex flex-wrap items-center gap-1" role="group" aria-label="预测带日期">
                  <button
                    type="button"
                    class="inline-flex min-h-[32px] items-center justify-center rounded-lg border px-2.5 text-xs font-medium transition-colors"
                    :class="latticeHistoryOffset === 0
                      ? 'border-aqua-400/60 bg-aqua-400/15 text-aqua-100'
                      : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                    :aria-pressed="latticeHistoryOffset === 0"
                    @click="goLivePrediction"
                  >
                    本期
                  </button>
                  <button
                    type="button"
                    class="inline-flex min-h-[32px] items-center justify-center rounded-lg border border-white/10 bg-white/5 px-2.5 text-xs font-medium text-slate-300 transition-colors active:bg-white/15 disabled:cursor-not-allowed disabled:opacity-40"
                    :disabled="latticeHistoryOffset <= 0"
                    @click="stepHistoryLater"
                  >
                    更新
                  </button>
                  <button
                    type="button"
                    class="inline-flex min-h-[32px] items-center justify-center rounded-lg border px-2.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                    :class="latticeHistoryOffset > 0
                      ? 'border-amber-400/60 bg-amber-400/15 text-amber-100'
                      : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                    :disabled="latticeHistoryOffset >= latticeReplayWindow.maxOffset"
                    @click="stepHistoryEarlier"
                  >
                    更早
                  </button>
                </div>
              </div>
            </div>
            <p
              v-if="waveTrendChart.historySummary"
              class="text-xs leading-relaxed"
              :class="waveTrendChart.historySummary.hit ? 'text-emerald-300/90' : 'text-rose-300/90'"
            >
              已往前 {{ waveTrendChart.historySummary.offset }} 期
              （可再往前 {{ waveTrendChart.historySummary.maxOffset - waveTrendChart.historySummary.offset }} 期）：
              锚点第 {{ waveTrendChart.historySummary.anchorPeriod }} 期特码 {{ waveTrendChart.historySummary.anchor }}，
              下一期（第 {{ waveTrendChart.historySummary.targetPeriod }} 期）实开
              {{ waveTrendChart.historySummary.actual }}（差 {{ waveTrendChart.historySummary.diff }}），
              {{ waveTrendChart.historySummary.hit ? '落在带内' : '落在带外' }}。
              右侧连线高亮 = 当时带宽 {{ waveTrendChart.historySummary.low }}–{{ waveTrendChart.historySummary.high }} 内的号码；
              「实开 {{ pad(waveTrendChart.historySummary.actual) }}」单独标出。
            </p>
            <p v-else-if="latticeReplayWindow.maxOffset <= 0" class="text-xs text-amber-200/90">
              本池至少要 3 期开奖记录，才能往前翻看历史预测带。
            </p>
            <LineChart
              :series="waveTrendChart.series"
              :labels="waveTrendChart.labels"
              :bands="waveTrendChart.bands"
              :candidate-column="waveTrendChart.candidateColumn"
              :candidate-column-from-index="waveTrendChart.candidateColumnFromIndex"
              :candidate-column-by-value="true"
              :annotations="waveTrendChart.annotations"
              :height="220"
              :y-min="1"
              :y-max="49"
              scrollable
              keyboard-mode="column-period"
              v-model:window-size="waveTrendWindow"
              :window-options="WAVE_TREND_WINDOW_OPTIONS"
              empty-text="数据不足"
              empty-hint="本池至少要 2 期开奖记录才能连成预测波动线"
              @period-step="onLatticePeriodStep"
              @candidate-click="onLatticeCandidateClick"
            />
            <div
              v-if="result.price_pivots"
              class="space-y-1.5 border-t border-white/5 pt-2"
            >
              <p class="text-[11px] leading-relaxed text-slate-500">
                特码拐点（左右各 {{ result.price_pivots.span }} 期，近
                {{ result.price_pivots.lookback ?? 60 }} 期）
                <template v-if="result.price_pivots.enabled">· 近窗抬权</template>
                <template v-else>· 偏好已关闭</template>
                · K 线转向：低点=支撑、高点=压力；只优先近窗；不硬塞。
              </p>
              <div
                v-if="(result.price_pivots.preferred_numbers ?? []).length"
                class="flex flex-wrap gap-x-3 gap-y-1 text-[11px]"
              >
                <span class="inline-flex flex-wrap items-center gap-1 text-emerald-300/90">
                  支撑
                  <template v-if="(result.price_pivots.support ?? []).length">
                    <StatChip
                      v-for="row in result.price_pivots.support"
                      :key="`sup-${row.number}`"
                      tone="emerald"
                      size="xs"
                    >
                      {{ pad(row.number) }}
                      <span v-if="row.count > 1" class="opacity-70">×{{ row.count }}</span>
                    </StatChip>
                  </template>
                  <span v-else class="text-slate-500">—</span>
                </span>
                <span class="inline-flex flex-wrap items-center gap-1 text-bloom-300/90">
                  压力
                  <template v-if="(result.price_pivots.resistance ?? []).length">
                    <StatChip
                      v-for="row in result.price_pivots.resistance"
                      :key="`res-${row.number}`"
                      tone="bloom"
                      size="xs"
                    >
                      {{ pad(row.number) }}
                      <span v-if="row.count > 1" class="opacity-70">×{{ row.count }}</span>
                    </StatChip>
                  </template>
                  <span v-else class="text-slate-500">—</span>
                </span>
              </div>
              <p
                v-else
                class="text-[11px] text-amber-200/90"
                role="status"
              >
                样本不足以标出局部高低点，本期不抬权。
              </p>
            </div>
          </GlassPanel>
        </MotionReveal>

        <!-- 01 - 49 出现热度：点选主入口，与上方预测点阵连线同步 -->
        <MotionReveal :index="7">
          <GlassPanel padding="md" rounded="3xl">
            <div class="mb-2 flex flex-wrap items-end justify-between gap-x-3 gap-y-1.5">
              <div class="space-y-0.5">
                <h2 class="text-base font-medium text-white">01 - 49 出现热度</h2>
                <p class="text-[11px] leading-relaxed text-slate-500">
                  点格勾选后在上方点阵连线。「预」=本期预测；绿=带内预测，红=带外预测，紫=自选。已选 {{ latticeSelectedCount }}。
                </p>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  class="inline-flex min-h-[32px] items-center justify-center rounded-lg border border-white/10 bg-white/5 px-2.5 text-xs text-slate-300 transition-colors active:bg-white/15"
                  @click="syncLatticeSelectedFromPicks"
                >
                  恢复预测号
                </button>
                <div class="flex flex-wrap gap-1" role="group" aria-label="热度统计期数">
                  <button
                    v-for="option in heatWindowOptions"
                    :key="option"
                    type="button"
                    class="num inline-flex min-h-[32px] min-w-[44px] items-center justify-center rounded-lg border px-2.5 text-xs font-medium transition-colors duration-200 select-none active:scale-[0.98]"
                    :class="option === heatWindow
                      ? 'border-aqua-400/60 bg-aqua-400/15 text-aqua-100'
                      : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                    :aria-pressed="option === heatWindow"
                    @click="heatWindow = option"
                  >
                    {{ option }}
                  </button>
                </div>
              </div>
            </div>

            <HeatGrid
              :cells="heatCells"
              :columns="7"
              :row-scale="0.58"
              :gap="3"
              tone="aqua"
              show-labels
              selectable
              :highlight="latestSampleNumber === null ? [] : [pad(latestSampleNumber)]"
              :marked="heatPickMarkedNumbers"
              :marked-out-band="heatPickOutBandNumbers"
              :selected="heatSelectedNumbers"
              legend
              value-unit="次"
              :legend-scope="heatScope"
              empty-text="数据不足"
              @toggle="toggleLatticeNumber"
            />
          </GlassPanel>
        </MotionReveal>

        <!-- 走势分布参考：默认折叠，完整三档仍可展开查阅（与卡片内点阵互补） -->
        <MotionReveal v-if="result.trend_distributions" :index="8">
          <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
            <button
              type="button"
              class="flex w-full flex-wrap items-center justify-between gap-2 text-left"
              :aria-expanded="trendDistExpanded"
              aria-controls="trend-dist-panel"
              @click="trendDistExpanded = !trendDistExpanded"
            >
              <div class="space-y-1">
                <h2 class="text-lg font-medium text-white">走势分布参考</h2>
                <p class="text-xs leading-relaxed text-slate-500">
                  完整三档明细（默认收起）；卡片内已挂同角色点阵。口径：经验频率，非概率 / 非预测。
                </p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <StatChip tone="aqua" size="sm" dot>{{ trendWindowLabel }}</StatChip>
                <span
                  class="inline-flex min-h-[40px] items-center rounded-xl bg-white/5 px-3 text-sm text-slate-200 ring-1 ring-white/10"
                >
                  {{ trendDistExpanded ? '收起' : '展开' }}
                </span>
              </div>
            </button>

            <div
              v-show="trendDistExpanded"
              id="trend-dist-panel"
              class="space-y-4 border-t border-white/5 pt-3"
            >
              <div class="space-y-2">
                <p class="text-[11px] leading-relaxed text-slate-500">
                  取样窗只改本块统计展示，不改上方财富密码号码或金额。热号 / 冷号偏好加权已停用。
                </p>
                <div
                  role="radiogroup"
                  aria-label="走势分布取样窗"
                  class="flex flex-wrap gap-2"
                >
                  <GlassButton
                    v-for="option in trendWindowOptions"
                    :key="option.value"
                    role="radio"
                    :aria-checked="trendWindow === option.value"
                    :variant="trendWindow === option.value ? 'primary' : 'glass'"
                    class="min-h-[40px] px-3 text-sm"
                    @click="trendWindow = option.value"
                  >
                    {{ option.label }}
                  </GlassButton>
                </div>
              </div>
              <p class="text-[11px] leading-relaxed text-slate-500">
                {{ result.trend_distributions.rule }}
              </p>

              <div
                role="tablist"
                aria-label="波动档"
                class="flex flex-wrap gap-2"
              >
                <GlassButton
                  v-for="tab in waveTabs"
                  :key="tab.type"
                  role="tab"
                  :aria-selected="activeWaveTab === tab.type"
                  :variant="activeWaveTab === tab.type ? 'primary' : 'glass'"
                  class="min-h-[40px] px-4 text-sm"
                  @click.stop="activeWaveTab = tab.type"
                >
                  {{ tab.label }}
                </GlassButton>
              </div>

              <div class="grid gap-3 sm:grid-cols-3">
                <div
                  v-for="block in roleBlocks"
                  :key="block.key"
                  class="glass-panel-soft space-y-2 rounded-2xl p-3"
                >
                  <StatChip :tone="block.tone" size="sm" dot>{{ block.label }}</StatChip>
                  <p
                    v-if="!roleNumbers(block.key).length"
                    class="text-xs text-slate-500"
                  >
                    数据不足：本档暂无可列号码
                  </p>
                  <ul v-else class="space-y-1.5">
                    <li
                      v-for="item in roleNumbers(block.key).slice(0, 8)"
                      :key="`${block.key}-${item.number}`"
                      class="flex items-center justify-between gap-2 text-sm"
                    >
                      <span class="num font-medium text-slate-100">{{ pad(item.number) }}</span>
                      <span class="num text-xs text-slate-400">
                        {{ trendCountLabel(item) }}
                      </span>
                    </li>
                  </ul>
                  <p
                    v-if="roleNumbers(block.key).length > 8"
                    class="text-[11px] text-slate-500"
                  >
                    另有 {{ roleNumbers(block.key).length - 8 }} 个号码未展开
                  </p>
                </div>
              </div>
            </div>
          </GlassPanel>
        </MotionReveal>

        <!-- 说明 -->
        <MotionReveal v-if="(result.notes ?? []).length" :index="11">
          <GlassPanel variant="soft" padding="lg" rounded="3xl" class="space-y-1.5">
            <p v-for="note in (result.notes ?? [])" :key="note" class="text-xs leading-relaxed text-slate-400">
              · {{ note }}
            </p>
          </GlassPanel>
        </MotionReveal>
      </template>
    </div>
  </main>
</template>
