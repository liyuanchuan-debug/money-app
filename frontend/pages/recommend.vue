<script setup lang="ts">
import type { DrawItem } from '~/composables/useApi'
import {
  CHIP_MODE_OPTIONS,
  MIN_BET_AMOUNT,
  SOFT_REASON_LABELS,
  STALE_WEIGHT_DEFAULT,
  TOTAL_AMOUNT_MAX,
  TREND_BIAS_OPTIONS,
  TREND_WINDOW_DEFAULT,
  TREND_WINDOW_OPTIONS,
  avoidColdDaysText,
  avoidColdWeightText,
  periodsSinceLastText,
  softWeightText,
  type ChipMode,
  type FreezeResult,
  type LedgerStatus,
  type Pick,
  type PickTicket,
  type PickTicketPayload,
  type RecommendResult,
  type TrendBias,
  type TrendNumberStat,
  type TrendWaveRoles,
} from '~/composables/useApi'
import { normalizeDraws, periodText, scopeLabel, zodiacText } from '~/composables/useDraws'
import { STATS_SAMPLE_LIMIT_OPTIONS } from '~/composables/useStats'

definePageMeta({ role: 'VIP' })

/**
 * 波浪买入法（财富密码）—— 只读消费 POST /api/recommend 的结果，展示层归一化。
 *
 * 写入语义（重要）：
 *   - 默认筹码模式 / 默认注数 / 走势加权**只在设置页持久化**；
 *   - 本页的筹码模式、注数、走势加权切换是**临时预览**：只影响本页这一次查看，绝不写回设置
 *     （本页从不调用 PUT /api/settings）。
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
const mode = ref<ChipMode>('even')
/** 临时预览：注数（默认跟设置 pick_count；本页切换不写库，经 bet_count 传给 recommend） */
const betCount = ref(10)
/**
 * 临时预览：走势加权（默认跟设置走；本页切换不写库）。
 * 设置页读取口径：没手动设置过就是「不加权」（neutral），所以这里默认也是 neutral。
 */
const trendBias = ref<TrendBias>('neutral')
const trendWindow = ref(TREND_WINDOW_DEFAULT)
const activeWaveTab = ref<'small' | 'normal' | 'big'>('small')
/** 下方「走势分布参考」大区块默认折叠，避免与卡片内同角色参考号重复刷屏 */
const trendDistExpanded = ref(false)
const copied = ref(false)
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
 * 设置与样本口径互不依赖，并行拉取；推荐结果依赖 mode（来自设置），必须等设置回来。
 * 远端 DB 下串行会把本页 SSR 拉到 settings+draws+recommend 三趟 RTT 之和。
 */
const [{ data: settings }, { data: rawDraws }] = await Promise.all([
  useAsyncData('settings', () => api.getSettings()),
  useAsyncData<DrawItem[]>('draws-sample', () => api.listDraws(SAMPLE_LIMIT, 0)),
])
if (settings.value?.mode) {
  mode.value = settings.value.mode as ChipMode
}
if (typeof settings.value?.pick_count === 'number') {
  betCount.value = clampBetCount(settings.value.pick_count)
}
if (settings.value?.trend_bias) {
  trendBias.value = settings.value.trend_bias as TrendBias
}
if (typeof settings.value?.trend_window === 'number') {
  trendWindow.value = settings.value.trend_window
}

/* ---------------------------------------------------------------------- */
/* 出票单（选号工具）的状态                                              */
/*                                                                        */
/* 五件事：① 花费控制 ② 号码卫生 ③ 覆盖透明 ④ 留痕复现 ⑤ 诚实披露。      */
/* 它**不**改变中奖概率，也**不**改变期望值 —— 页面文案必须与此一致。      */
/* ---------------------------------------------------------------------- */

/** 预算档位（元）：与后端 TOTAL_AMOUNT_MIN / MAX 和注码粒度（5 元）对齐 */
const TICKET_BUDGET_MIN = 5
const TICKET_BUDGET_STEP = 5
/** 「换一批」的起始批号必须 ≥ 1：批号 0 等于「不给种子」= 生产引擎既定名次 */
const TICKET_SEED_START = 1

const ticket = ref<PickTicket | null>(null)
const ticketPending = ref(false)
const ticketError = ref('')
/** null = 生产引擎既定名次；≥1 = 在引擎候选排序里滑动的窗口（「换一批」） */
const ticketSeed = ref<number | null>(null)
const ticketCopied = ref(false)
const ticketCopyError = ref('')
const ticketHit = useHitPulse()
const ticketCopyHit = useHitPulse()

/** 出票形状（初值取设置页保存的口径，本页改动不写库） */
const ticketBudget = ref(50)
const ticketPickCount = ref(10)
const ticketMode = ref<ChipMode>('even')

/** 号码卫生开关（初值 = 设置页口径；本页改动只作用于这次出票） */
const ticketRepeatNumber = ref(true)
const ticketExcludeZodiac = ref(false)
const ticketStale = ref(true)
const ticketLattice = ref(false)
/** 冷号开关开启时用的降权系数（沿用设置页的值，不在这里另造数字） */
const ticketStaleWeight = ref(STALE_WEIGHT_DEFAULT)

/** 前瞻验证账本（开奖前冻结、开奖后诚实计分）—— 可选能力 */
const ledger = ref<LedgerStatus | null>(null)
const freezePending = ref(false)
const freezeNotice = ref('')
const freezeError = ref('')

if (settings.value) {
  const stored = settings.value
  if (typeof stored.total_amount === 'number') {
    ticketBudget.value = clampTicketBudget(stored.total_amount)
  }
  if (typeof stored.pick_count === 'number') {
    ticketPickCount.value = clampTicketCount(stored.pick_count)
  }
  if (stored.mode) ticketMode.value = stored.mode as ChipMode
  ticketRepeatNumber.value = stored.include_repeat_number !== false
  ticketExcludeZodiac.value = stored.exclude_repeat_zodiac === true
  ticketLattice.value = stored.lattice_enabled === true
  if (typeof stored.stale_weight === 'number') {
    ticketStaleWeight.value = stored.stale_weight
  }
  ticketStale.value = ticketStaleWeight.value < 1
}

function clampTicketBudget(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return TICKET_BUDGET_MIN
  const stepped = Math.round(Math.round(n / TICKET_BUDGET_STEP) * TICKET_BUDGET_STEP)
  return Math.min(TOTAL_AMOUNT_MAX, Math.max(TICKET_BUDGET_MIN, stepped))
}

function nudgeTicketBudget(delta: number) {
  ticketBudget.value = clampTicketBudget(ticketBudget.value + delta)
}

function onTicketBudgetInput(event: Event) {
  const raw = (event.target as HTMLInputElement).value
  ticketBudget.value = clampTicketBudget(raw === '' ? TICKET_BUDGET_MIN : raw)
}

function clampTicketCount(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return BET_COUNT_MIN
  return Math.min(BET_COUNT_MAX, Math.max(BET_COUNT_MIN, Math.round(n)))
}

function nudgeTicketCount(delta: number) {
  ticketPickCount.value = clampTicketCount(ticketPickCount.value + delta)
}

function onTicketCountInput(event: Event) {
  const raw = (event.target as HTMLInputElement).value
  ticketPickCount.value = clampTicketCount(raw === '' ? BET_COUNT_MIN : raw)
}

/** 出票请求体：开关恒为显式值（所见即所得，不依赖后端存储设置的隐式默认） */
function ticketPayload(seed: number | null): PickTicketPayload {
  return {
    budget: ticketBudget.value,
    pick_count: ticketPickCount.value,
    mode: ticketMode.value,
    ...(seed === null ? {} : { seed }),
    include_repeat_number: ticketRepeatNumber.value,
    exclude_repeat_zodiac: ticketExcludeZodiac.value,
    stale_weight: ticketStale.value ? ticketStaleWeight.value : 1,
    lattice_enabled: ticketLattice.value,
  }
}

async function requestTicket(seed: number | null) {
  if (ticketPending.value) return
  ticketPending.value = true
  ticketError.value = ''
  freezeNotice.value = ''
  freezeError.value = ''
  ticketCopied.value = false
  ticketCopyError.value = ''
  try {
    ticket.value = await api.pickTicket(ticketPayload(seed))
    ticketSeed.value = seed
    ticketHit.fire()
  } catch (err: any) {
    ticket.value = null
    ticketError.value = err?.data?.detail || err?.message || '生成出票单失败'
  } finally {
    ticketPending.value = false
  }
}

/** 生成：不给种子 = 生产引擎的既定名次（同一期重算结果完全一致） */
async function generateTicket() {
  await requestTicket(null)
}

/** 换一批：换一个批号，在引擎自己的候选排序里滑动窗口；期望值与上面那一张完全相同 */
async function reshuffleTicket() {
  const next = (ticketSeed.value ?? TICKET_SEED_START - 1) + 1
  await requestTicket(Math.max(TICKET_SEED_START, next))
}

/** 纯文本票据（可直接粘贴）；UTF-8 无 BOM */
function downloadText(text: string, filename: string) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

async function copyTicket() {
  if (!ticket.value?.ticket_text) return
  try {
    await navigator.clipboard.writeText(ticket.value.ticket_text)
    ticketCopied.value = true
    ticketCopyError.value = ''
    ticketCopyHit.fire()
    setTimeout(() => (ticketCopied.value = false), 2000)
  } catch {
    ticketCopied.value = false
    ticketCopyError.value = '复制失败，请手动选中下方文本'
  }
}

/** 导出：文本 + JSON 各一份；文件名含目标期与内容摘要，便于事后对账 */
function exportTicket() {
  const value = ticket.value
  if (!value) return
  const stem = `ticket_${value.data.target_period}_${value.ticket_id}`
  downloadText(value.ticket_text, `${stem}.txt`)
  downloadJson(value, `${stem}.json`)
}

/** 读账本状态（可选能力：模块不在时后端返回 available=false，页面照常可用） */
async function refreshLedger() {
  try {
    ledger.value = await api.pickLedger()
  } catch {
    ledger.value = null
  }
}

/** 冻结到台账：开奖前冻结、冻结后不可改写；失败一律如实报出，不假装成功 */
async function freezeTicket() {
  const value = ticket.value
  if (!value || freezePending.value) return
  freezePending.value = true
  freezeNotice.value = ''
  freezeError.value = ''
  try {
    const result: FreezeResult = await api.pickFreeze(value)
    freezeNotice.value = `已冻结第 ${result.period} 期（账本共 ${result.records_total} 条 · ${result.record_hash.slice(0, 12)}…）`
    await refreshLedger()
  } catch (err: any) {
    const status = err?.status ?? err?.response?.status
    const detail = err?.data?.detail || err?.message || '冻结失败'
    freezeError.value = status === 501 ? `台账暂不可用：${detail}` : detail
  } finally {
    freezePending.value = false
  }
}

onMounted(refreshLedger)

/** 出票单里的软降权标记（与推荐卡片共用同一份展示映射，避免两处口径漂移） */
function ticketSoftReasons(row: { soft_reasons?: string[] }): string[] {
  return (row.soft_reasons ?? []).filter(reason => reason in SOFT_REASON_LABELS)
}

/** 百分比文案（null 照实写「—」，不编造） */
function pct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** 金额文案（保留两位：期望值这类小数不该被四舍五入成整数） */
function yuan(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

/** 账本是否可用（不可用时「冻结到台账」按钮降级为不可点 + 显示原因） */
const freezeAvailable = computed(() => ledger.value?.available === true)
const freezeUnavailableReason = computed(
  () => ledger.value?.reason || '前瞻验证账本模块当前未接入',
)


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
const HEAT_WINDOW_DEFAULT = heatWindowOptions[heatWindowOptions.length - 1] ?? SAMPLE_LIMIT
/** 最小档：本池比它还短时，说明文案要如实交代「全部统计」 */
const heatMinOption = heatWindowOptions[0] ?? SAMPLE_LIMIT
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

/** 01 - 49 固定 49 格；一次都没出现过的号码给 0（组件按零值档渲染中性灰） */
const heatCells = computed(() =>
  Array.from({ length: 49 }, (_, index) => {
    const number = index + 1
    return {
      number: pad(number),
      value: heatCounts.value.get(number) ?? 0,
      hint: '所选期数内特码出现次数',
    }
  }),
)

/** 图例口径随所选窗口走，保证图例与格子永不打架（只描述样本内计数，无预测含义） */
const heatScope = computed(() => scopeLabel(heatWindowEffective.value))

/** 诚实说明：只统计本池样本内的计数；本池更少时如实说「全部统计」 */
const heatWindowCaption = computed(() => {
  if (sampleSize.value === 0) {
    return '本池暂无已导入期数；补录开奖后再按所选期数统计。'
  }
  if (sampleSize.value < heatMinOption) {
    return `本池仅 ${sampleSize.value} 期，全部统计。`
  }
  return `只统计本池最近 ${heatWindow.value} 期；若本池期数更少，则以全部期数为准。`
})

/** 本池最新一期特码（仅用于在热度网格上高亮这一格，样本为空时 null） */
const latestSampleNumber = computed(
  () => normalizeDraws(rawDraws.value)[0]?.special_number ?? null,
)

const {
  data: rawResult,
  refresh,
  pending,
  error,
} = await useAsyncData<RecommendResult>('recommend', () =>
  api.recommend({
    mode: mode.value,
    bet_count: betCount.value,
    amount_seed: amountSeed.value,
    trend_bias: trendBias.value,
    trend_window: trendWindow.value,
  }),
)

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
  return {
    ...(source as RecommendResult),
    picks: asArray(source.picks),
    notes: asArray(source.notes),
    missing_waves: asArray(source.missing_waves),
    focus_order: asArray(source.focus_order),
    latest_zodiac: asArray(source.latest_zodiac),
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

/** 出票单的零注原因（同一口径；零注时不会产生任何金额，也不会冻结到台账） */
const ticketNoTicket = computed(() => ticket.value?.status === 'no_ticket')

const ticketNoTicketReason = computed(() => {
  if (ticket.value?.status !== 'no_ticket') return ''
  return ticket.value.reason_message?.trim() || NO_TICKET_FALLBACK_REASON
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

/**
 * 筹码点阵散落：用 number + index 做确定性伪随机偏移，刷新不乱跳。
 * 频次高 → 略大略实；0 次 → 更淡更小；当前 pick 号另加高亮。
 * 另用 margin 制造不规则间距，避免整齐表格感。
 */
function scatterChipStyle(
  item: TrendNumberStat,
  index: number,
  activeNumber: number,
): Record<string, string> {
  const n = item.number
  const seed = (n * 37 + index * 53) % 97
  const dx = ((seed % 13) - 6) * 1.35
  const dy = (((seed * 3) % 11) - 5) * 1.15
  const rot = ((seed % 9) - 4) * 1.6
  const mRight = 2 + (seed % 7)
  const mBottom = 2 + ((seed * 5) % 6)
  const count = item.count ?? 0
  const isActive = n === activeNumber
  let scale = 0.78
  let opacity = 0.38
  if (count >= 5) {
    scale = 1.16
    opacity = 0.96
  } else if (count >= 3) {
    scale = 1.06
    opacity = 0.9
  } else if (count >= 1) {
    scale = 0.95
    opacity = 0.74
  }
  if (isActive) {
    scale = Math.max(scale, 1.18)
    opacity = 1
  }
  return {
    transform: `translate(${dx.toFixed(1)}px, ${dy.toFixed(1)}px) rotate(${rot.toFixed(1)}deg) scale(${scale.toFixed(2)})`,
    opacity: String(opacity),
    marginRight: `${mRight}px`,
    marginBottom: `${mBottom}px`,
  }
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

/** 点阵筹码副文案（空间紧，用短写）。 */
function trendChipSub(item: TrendNumberStat): string {
  if ((item.count ?? 0) > 0) return `${item.count}次`
  if (item.days_since_last == null) return '未出'
  return `0·${item.days_since_last}天`
}

/* ---------------------------------------------------------------------- */
/* 三类软降权徽章：重号 / 同肖 / 冷号（不排除号码，只降权 + 打标）           */
/* ---------------------------------------------------------------------- */

/** 徽章色调：英文码 → StatChip tone（重号=暖红、同肖=琥珀、冷号=冷色） */
const SOFT_REASON_TONES: Record<string, 'bloom' | 'amber' | 'aqua'> = {
  repeat_number: 'bloom',
  repeat_zodiac: 'amber',
  stale: 'aqua',
}

/**
 * 本注命中的软降权标记。
 * 口径由后端判定（repeat_number / repeat_zodiac / stale），这里只做展示过滤，
 * 前端不重复判定，避免与后端口径打架。
 */
function pickSoftReasons(pick: Pick): string[] {
  const reasons = pick.soft_reasons ?? []
  return reasons.filter(reason => reason in SOFT_REASON_LABELS)
}

/** 三类软降权本次生效摘要（旧后端缺字段 → null，照实不展示） */
const softSummary = computed(() => result.value?.soft_weights ?? null)

/* ---------------------------------------------------------------------- */
/* 预测波动线 · 折线（同首页「最近特码走势」）+ 1~49 号码点阵                 */
/* ---------------------------------------------------------------------- */

/** 点阵块（仅展示；enabled=false 时只画不带内优先） */
const lattice = computed(() => result.value?.lattice ?? null)
/** 预测波动线：中心=中位数、带宽=P25~P75；样本不足 → null（不编造） */
const latticeBand = computed(() => lattice.value?.band ?? null)
/** 1~49 全号点阵（后端按号码升序返回，照原顺序渲染） */
const latticeNumbers = computed(() => lattice.value?.numbers ?? [])
/** 落在预测带内的号数（带内 / 合计分开计数，如实展示） */
const inBandCount = computed(() => latticeNumbers.value.filter(cell => cell.in_band).length)
/** 本期推荐号码集合（点阵上打标用；不参与任何选号判定） */
const pickedNumbers = computed(
  () => new Set((result.value?.picks ?? []).map(pick => pick.number)))
/** 本期推荐里落在预测带内的注数 */
const pickedInBandCount = computed(
  () => (result.value?.picks ?? []).filter(pick => pick.in_lattice_band).length)

/** 预测波动折线一屏期数（与首页特码走势同一档位习惯） */
const WAVE_TREND_WINDOW_DEFAULT = 30
const WAVE_TREND_WINDOW_OPTIONS = [30, 60, 100, 0]
const waveTrendWindow = ref(WAVE_TREND_WINDOW_DEFAULT)

/**
 * 预测波动折线数据（前端用本池开奖样本按**特码原值 1–49** 画一条线，带宽取 recommend.lattice.band）。
 * - 未开启：不编造曲线
 * - 已开启但 band 为空：数据不足
 * - 有 band：实线只连本池各期真实特码点，不做任何外推（不画预测点 / 虚线尾段）。
 * - 纵轴口径：阴影带（预测带宽）本身在「相邻差值 |Δ|」空间取 P25~P75（low=P25 / high=P75），
 *   这里按基准 = 最新特码换算到特码轴：from = clamp(最新 − high)、to = clamp(最新 − low)，
 *   超出 1–49 按边界截断。它是**差值区间的换算**，不是独立的特码分布（页面文案必须写清）。
 * - 有 picks：把候选号码从「最新一期真实特码点」拉出**多条虚线扇形**，末端按
 *   **真实特码值 1–49** 落在同一竖列 x 上（LineChart 的 candidateColumn + candidateColumnByValue）。
 *   每条虚线一个号、末端一个点；因为纵轴本身就是特码 1–49，末端自然排成一列
 *   **点阵分布**（哪几段号码密、哪几段空，一眼可见）。号码升序排列（确定性），纵向位置即真实取值。
 *   不是未来路径，也非真实开奖。
 * - 标注：把本池**最新一期的特码原值**（1–49）写在最右的真实点上，供核对「最新一期 = 特码 N」。
 */
const waveTrendChart = computed(() => {
  type ColumnCandidate = {
    /** 候选号码原值（1–49）：纵轴即特码原值，末端按真实值定位 → 形成一列取值分布 */
    value: number
    label: string
    tone: 'emerald' | 'bloom'
  }
  /** 图上数据点标注：把最新一期的特码原值写在最右真实点上（纵轴已是特码原值） */
  type Annotation = {
    index: number
    value: number
    label: string
    tone: 'slate'
  }
  const empty = {
    series: [] as Array<{
      name: string
      values: Array<number | null>
      tone?: 'aqua' | 'amber'
    }>,
    labels: [] as string[],
    bands: [] as Array<{ from: number; to: number; label: string }>,
    candidateColumn: [] as ColumnCandidate[],
    /** 扇形起点 = 最新真实特码点下标；无候选 / 无数据时 null（只画列或不画） */
    candidateColumnFromIndex: null as number | null,
    annotations: [] as Annotation[],
    pointCount: 0,
  }
  const meta = lattice.value
  const band = latticeBand.value
  if (!meta?.enabled || !band) return empty
  // listDraws 新→旧；与后端 predict_wave_band(history[:window]) 同一窗口
  const newestFirst = normalizeDraws(rawDraws.value)
  const window = typeof meta.window === 'number' ? meta.window : 0
  const windowed = window > 0 ? newestFirst.slice(0, window) : newestFirst
  const ordered = windowed.slice().reverse()
  if (ordered.length < 2) return empty
  // 主线 = 每期特码原值（1–49），一个点一期
  const specialValues: number[] = []
  const labels: string[] = []
  for (let i = 0; i < ordered.length; i += 1) {
    specialValues.push(ordered[i].special_number)
    labels.push(periodText(ordered[i]) || ordered[i].draw_date || `第${i + 1}点`)
  }
  const low = Number(band.low)
  const high = Number(band.high)
  // 差值带 → 特码轴换算：基准 = 最新一期真实特码，from = clamp(最新 − high)、to = clamp(最新 − low)，夹在 1–49。
  // 只取「下降分支」（最新 − Δ），口径在页面文案里写清；它仍是差值区间的换算，不是独立特码分布。
  const clampToLane = (value: number) => Math.min(49, Math.max(1, Math.round(value)))
  const newestDraw = ordered[ordered.length - 1]
  // 基准 = 最新一期真实特码：既是图上最右真实点，也是阴影带的锚点
  const newestValue = newestDraw.special_number
  const newestValid = Number.isFinite(newestValue)
  // 主线只画真实开奖点（specialValues），不做外推，也不画虚线尾段。
  const bandValid = newestValid && Number.isFinite(low) && Number.isFinite(high)
  const bandFrom = bandValid ? clampToLane(newestValue - high) : null
  const bandTo = bandValid ? clampToLane(newestValue - low) : null
  // 财富密码 = 下期候选集合：从「最新一期真实特码点」拉出多条虚线扇形，
  // 末端按**真实特码值 1–49** 落在同一竖列 x 上（LineChart 的 candidateColumn + candidateColumnByValue）。
  // 因为纵轴就是特码原值，末端自然排成一列**点阵分布**：哪几段号码密、哪几段空，一眼可见。
  // 号码**升序**排列（确定性），纵向位置由真实取值决定（大号在上、小号在下，与纵轴同向）。
  const candidateColumn: ColumnCandidate[] = []
  for (const pick of result.value?.picks ?? []) {
    const num = Number(pick.number)
    if (!Number.isFinite(num)) continue
    candidateColumn.push({
      value: num,
      label: pad(num),
      tone: pick.in_lattice_band ? 'emerald' : 'bloom',
    })
  }
  candidateColumn.sort((a, b) => Number(a.label) - Number(b.label))
  // 候选竖列整列只占**一个** x 槽位（排在所有序列点之后）：labels 补一个空串，
  // 否则 x 刻度回落到「序号」会在右端多出一道无名刻度。
  labels.push('')

  // 标注：把本池最新一期的特码原值挂在最右真实点上，方便核对「最新一期 = 特码 N」。
  // 纵轴已是特码原值，这里不再承担「另一个单位」的消歧作用，只把「最新一期」点出来。
  const annotations: Annotation[] = []
  if (newestDraw && typeof newestValue === 'number' && Number.isFinite(newestValue)) {
    annotations.push({
      index: specialValues.length - 1,
      value: newestValue,
      label: `最新一期 ${periodText(newestDraw)} · 特码 ${pad(newestValue)}`,
      tone: 'slate',
    })
  }

  return {
    series: [
      {
        name: '特码走势',
        values: specialValues,
        tone: 'aqua' as const,
      },
    ],
    labels,
    bands: bandFrom !== null && bandTo !== null
      ? [{ from: bandFrom, to: bandTo, label: '预测带（最新 − 差值区间）' }]
      : [],
    candidateColumn,
    // 扇形起点 = 最新一期真实特码点（specialValues 的最后一个）
    candidateColumnFromIndex: candidateColumn.length
      ? specialValues.length - 1
      : null,
    annotations,
    pointCount: specialValues.length,
  }
})

/**
 * 点阵单元格透明度：带内 lattice_weight=1.0 最实，带外按距离衰减变淡。
 * 只影响观感，不改变任何口径。
 */
function latticeCellStyle(cell: { lattice_weight?: number }): Record<string, string> {
  const weight = cell.lattice_weight ?? 1
  const opacity = 0.4 + Math.max(0, Math.min(1, weight)) * 0.6
  return { opacity: opacity.toFixed(2) }
}

/**
 * 切换筹码模式 / 注数 / 走势加权 = 只重算本页（临时预览）。
 * ⚠️ 这里刻意**不调用** api.updateSettings：默认值只在设置页改。
 */
watch([mode, betCount, trendBias, trendWindow], async () => {
  copied.value = false
  copyError.value = ''
  rerollCount.value = 0
  await refresh()
})

/** 显式重新生成（首屏仍会自动生成一次，这颗按钮只是「再算一遍」） */
async function generate() {
  if (pending.value) return
  copied.value = false
  copyError.value = ''
  await refresh()
  generateHit.fire()
}

/** 重新随机：换一个显式种子，得到另一份金额分配（仅随机分配模式可点） */
async function reroll() {
  if (pending.value) return
  rerollCount.value += 1
  copied.value = false
  copyError.value = ''
  await refresh()
  generateHit.fire()
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

/** 取值表与设置页共用同一份（composables/useApi.ts） */
const chipModes = CHIP_MODE_OPTIONS
const trendBiasOptions = TREND_BIAS_OPTIONS
const trendWindowOptions = TREND_WINDOW_OPTIONS

async function copy() {
  if (!result.value?.copy_text) return
  try {
    await navigator.clipboard.writeText(result.value.copy_text)
    copied.value = true
    copyError.value = ''
    copyHit.fire()
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    copied.value = false
    copyError.value = '复制失败，请手动选中下方文本'
  }
}

useHead({ title: '波浪买入法 · 四叶沙盘' })
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-3xl flex-col gap-7">
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
              打开本页会自动生成一次；点右侧按钮即按当前筹码模式与走势加权重新生成。
              这里不会改动设置页保存的默认值。
            </p>
            <div class="relative inline-flex shrink-0 rounded-[14px]">
              <GlassButton
                variant="primary"
                size="lg"
                class="min-h-[48px] w-full sm:w-auto"
                :loading="pending"
                :disabled="pending"
                @click="generate"
              >
                {{ pending ? '生成中…' : '生成财富密码' }}
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

        <!-- 筹码模式：临时预览，不写默认值 -->
        <MotionReveal :index="3">
          <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 id="chip-mode-label" class="text-lg font-medium text-white">筹码模式</h2>
              <StatChip tone="neutral" size="sm">临时预览 · 不写入默认值</StatChip>
            </div>

            <div
              role="radiogroup"
              aria-labelledby="chip-mode-label"
              class="flex flex-wrap gap-2"
            >
              <GlassButton
                v-for="option in chipModes"
                :key="option.value"
                role="radio"
                :aria-checked="mode === option.value"
                :variant="mode === option.value ? 'primary' : 'glass'"
                class="min-h-[44px] px-4 text-base"
                @click="mode = option.value"
              >
                {{ option.label }}
              </GlassButton>
            </div>

            <p class="text-xs leading-relaxed text-slate-500">
              临时预览：此处切换只影响本次查看，不作为默认值；本页打开时仍以设置页保存的模式与注数启动。
              随机分配会按「金额最小单位」把最大投注金额随机拆给各注，各注金额可以不等；
              要改默认请到
              <NuxtLink
                to="/settings"
                class="text-aqua-200 underline underline-offset-2 active:text-aqua-100"
              >
                设置页
              </NuxtLink>
              保存。
            </p>

            <!-- 临场注数：增减 / 输入，走 recommend bet_count（不落库） -->
            <div class="space-y-2">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <label id="bet-count-label" class="text-sm font-medium text-slate-200">
                  注数
                </label>
                <StatChip tone="neutral" size="sm">临时预览 · 不写入默认值</StatChip>
              </div>
              <div
                class="flex flex-wrap items-center gap-2"
                role="group"
                aria-labelledby="bet-count-label"
              >
                <GlassButton
                  variant="glass"
                  class="min-h-[44px] min-w-[44px] px-0 text-lg"
                  :disabled="pending || betCount <= BET_COUNT_MIN || mode === 'single'"
                  aria-label="减少注数"
                  @click="nudgeBetCount(-1)"
                >
                  −
                </GlassButton>
                <input
                  id="bet-count"
                  :value="betCount"
                  type="number"
                  :min="BET_COUNT_MIN"
                  :max="BET_COUNT_MAX"
                  inputmode="numeric"
                  class="num h-11 w-20 rounded-xl border border-white/15 bg-white/5 px-3 text-center text-base text-slate-100 outline-none focus:border-aqua-400/50"
                  :disabled="pending || mode === 'single'"
                  aria-labelledby="bet-count-label"
                  @change="onBetCountInput"
                >
                <GlassButton
                  variant="glass"
                  class="min-h-[44px] min-w-[44px] px-0 text-lg"
                  :disabled="pending || betCount >= BET_COUNT_MAX || mode === 'single'"
                  aria-label="增加注数"
                  @click="nudgeBetCount(1)"
                >
                  +
                </GlassButton>
                <StatChip tone="aqua" size="sm">
                  {{ mode === 'single' ? '单挑恒为 1 注' : `当前 ${betCount} 注` }}
                </StatChip>
              </div>
              <p class="text-xs leading-relaxed text-slate-500">
                改注数会立刻按当前最大投注金额与金额最小单位重新出码、重摊金额（最大投注与单位倍数约束不变）。
                单挑模式注数固定为 1；默认注数仍在设置页保存。
              </p>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <StatChip tone="aqua" size="sm">模式：{{ result.mode_label }}</StatChip>
              <StatChip tone="nebula" size="sm">最大投注金额 {{ result.total_amount }} 元</StatChip>
              <StatChip tone="neutral" size="sm">金额最小单位 {{ result.amount_unit }} 元</StatChip>
              <StatChip tone="neutral" size="sm">
                每注最低 {{ MIN_BET_AMOUNT }} 元 · 上限 {{ TOTAL_AMOUNT_MAX }} 元 · 最多 {{ BET_COUNT_MAX }} 注
              </StatChip>
              <StatChip v-if="avoidColdActive" tone="amber" size="sm">
                避冷加权后实投 {{ stakedTotal }} 元（省下 {{ result.avoid_cold?.reduced_total ?? 0 }} 元未再分配）
              </StatChip>
              <StatChip tone="neutral" size="sm">
                实际 {{ result.picks.length }} 注
              </StatChip>
              <StatChip v-if="amountsEqual" tone="neutral" size="sm">
                各注 {{ result.picks[0]?.amount ?? result.bet_unit }} 元（最小单位 {{ result.amount_unit }} 元的倍数）
              </StatChip>
              <StatChip v-else tone="amber" size="sm">各注金额不等（均为最小单位倍数）</StatChip>
            </div>

            <!-- 避冷加权（冷号排后 + 金额封顶）：如实说明，绝不宣称提高命中率或收益 -->
            <p
              v-if="result.avoid_cold?.enabled"
              class="text-xs leading-relaxed text-slate-500"
            >
              避冷加权已开启（阈值 {{ result.avoid_cold.days }} 天，
              保本金额 {{ result.avoid_cold.cap_amount }} 元）：距上次出现超过阈值的号码已排到候选队列末尾
              （非冷号优先），
              <template v-if="avoidColdActive">
                本次 {{ result.avoid_cold.penalized_picks }} 注因间隔过久被压低金额，合计少投
                {{ result.avoid_cold.reduced_total }} 元（不补给其它注）。
              </template>
              <template v-else>本次没有距上次出现超过阈值的注，金额未受影响。</template>
              这是样本内偏好，不是概率，也不承诺提高命中率或收益。
            </p>
            <p
              v-else-if="result.avoid_cold"
              class="text-xs leading-relaxed text-slate-500"
            >
              避冷加权已关闭：距上次出现多久都不影响选号与金额（可在设置页开启）。
            </p>

            <!-- 三类软降权（重号 / 同肖 / 冷号）：不排除号码，只降排序与金额，如实说明 -->
            <p v-if="softSummary" class="text-xs leading-relaxed text-slate-500">
              软降权已生效（<strong class="font-semibold text-slate-400">不排除任何号码</strong>，只降排序与金额）：
              重号 ×{{ softSummary.repeat_number_weight }}、同肖 ×{{ softSummary.repeat_zodiac_weight }}、
              冷号（{{ softSummary.stale_periods }} 期未出现）×{{ softSummary.stale_weight }}；
              降权后每注最低仍为 {{ softSummary.min_bet_amount }} 元。
              <template
                v-if="softSummary.repeat_number_picks.length
                  || softSummary.repeat_zodiac_picks.length
                  || softSummary.stale_picks.length"
              >
                本次命中：重号 {{ softSummary.repeat_number_picks.length }} 注、
                同肖 {{ softSummary.repeat_zodiac_picks.length }} 注、
                冷号 {{ softSummary.stale_picks.length }} 注。
              </template>
              <template v-if="softSummary.penalized_picks > 0">
                本次 {{ softSummary.penalized_picks }} 注被降权压低金额，合计少投
                {{ softSummary.reduced_total }} 元（不补给其它注）。
              </template>
              <template v-else>本次命中的注金额未受影响。</template>
              这是样本内偏好，不是概率，也不承诺提高命中率或收益。
            </p>

            <!-- 各注金额一览（随机分配下通常不等，如实列出） -->
            <div class="space-y-1.5">
              <p class="text-xs text-slate-500">
                各注金额（共 {{ amounts.length }} 注，合计 {{ stakedTotal }} 元）：
              </p>
              <div class="flex flex-wrap gap-2">
                <span
                  v-for="pick in result.picks"
                  :key="pick.number"
                  class="rounded-lg bg-white/5 px-2.5 py-1 text-sm text-slate-200"
                >
                  <span class="num">{{ pad(pick.number) }}</span>
                  <span
                    v-if="pickZodiac(pick)"
                    class="ml-1 text-xs text-nebula-200"
                  >{{ pickZodiac(pick) }}</span>
                  <span class="mx-1 text-slate-500">→</span>
                  <span class="num">{{ pick.amount }} 元</span>
                </span>
              </div>
            </div>

            <!-- 重新随机：只有随机分配模式可用；对应后端 amount_seed（显式重掷） -->
            <div
              v-if="mode === 'random'"
              class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <p class="text-xs leading-relaxed text-slate-500">
                同一期不重掷时结果稳定（可复现）；点一下即换一份金额分配，最大投注与最小单位约束不变。
              </p>
              <GlassButton
                variant="glass"
                size="lg"
                class="min-h-[44px] w-full shrink-0 sm:w-auto"
                :loading="pending"
                :disabled="pending"
                @click="reroll"
              >
                重新随机
              </GlassButton>
            </div>
          </GlassPanel>
        </MotionReveal>

        <!-- 走势加权：已停用；切换仅改走势分布参考展示 -->
        <MotionReveal :index="4">
          <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 id="trend-bias-label" class="text-lg font-medium text-white">近期走势加权（已停用）</h2>
              <StatChip tone="neutral" size="sm">仅展示 · 不改号码</StatChip>
            </div>

            <div
              role="radiogroup"
              aria-labelledby="trend-bias-label"
              class="flex flex-wrap gap-2"
            >
              <GlassButton
                v-for="option in trendBiasOptions"
                :key="option.value"
                role="radio"
                :aria-checked="trendBias === option.value"
                :variant="trendBias === option.value ? 'primary' : 'glass'"
                class="min-h-[44px] px-4 text-base"
                @click="trendBias = option.value"
              >
                {{ option.label }}
              </GlassButton>
            </div>

            <div class="flex flex-wrap gap-2">
              <GlassButton
                v-for="option in trendWindowOptions"
                :key="option.value"
                :variant="trendWindow === option.value ? 'primary' : 'glass'"
                class="min-h-[40px] px-3 text-sm"
                @click="trendWindow = option.value"
              >
                {{ option.label }}
              </GlassButton>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <StatChip tone="aqua" size="sm">
                {{ result.trend_bias_label || '走势加权已停用' }}
              </StatChip>
              <StatChip tone="neutral" size="sm">{{ trendWindowLabel }}</StatChip>
            </div>

            <p
              class="rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-200"
            >
              近期走势加权已停用：切换热号 / 中频 / 冷号或近窗档位
              <strong class="font-semibold">不会改变推荐号码或金额</strong>，
              只影响下方走势分布参考的展示。
            </p>
          </GlassPanel>
        </MotionReveal>

        <!-- 财富密码 -->
        <section class="space-y-3">
          <MotionReveal :index="5" class="space-y-1">
            <h2 class="text-lg font-medium text-white">财富密码</h2>
            <p class="text-xs text-slate-500">
              按 {{ result.mode_label }} 模式生成 {{ result.picks.length }} 注候选号码；口径：{{ scope }}。
              生肖是「号码 → 生肖」的固定映射（随农历年轮转），不是命中概率，也不代表这注更有可能开出。
            </p>
          </MotionReveal>

          <!-- 零注：预算连 1 个注码单位都覆盖不了 → 说明本次不出票，不静默给空列表 -->
          <MotionReveal v-if="noTicket" :index="5">
            <p
              class="rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-200"
              role="status"
            >
              <strong class="font-semibold">本次不出票</strong>：{{ noTicketReason }}
            </p>
          </MotionReveal>

          <div v-if="!noTicket" class="grid gap-4 sm:grid-cols-3">
            <MotionReveal
              v-for="(pick, index) in result.picks"
              :key="pick.number"
              :index="index"
              :stagger="60"
            >
              <GlassCard
                as="article"
                padding="md"
                :selected="pick.role === 'primary'"
                class="flex h-full flex-col items-center gap-3"
              >
                <div class="flex w-full items-center justify-between gap-2">
                  <StatChip :tone="roleTones[pick.role] ?? 'neutral'" size="sm" dot>
                    {{ pick.role_label }}
                  </StatChip>
                  <span class="num text-sm font-medium text-slate-200">{{ pick.amount }} 元</span>
                </div>

                <SpecialBall
                  :number="pick.number"
                  size="lg"
                  :tone="pick.role === 'primary' ? 'aqua' : 'nebula'"
                  :glow="pick.role === 'primary'"
                />

                <!-- 生肖：号码的固定映射（随农历年轮转），不是命中概率 / 预测 -->
                <StatChip tone="nebula" size="xs">
                  生肖 {{ pickZodiac(pick) || '—' }}
                </StatChip>

                <!-- 软降权标记：重号 / 同肖 / 冷号 + 是否落在预测波动带内（不排除，只打标） -->
                <div
                  v-if="pickSoftReasons(pick).length || pick.in_lattice_band"
                  class="flex flex-wrap items-center justify-center gap-1.5"
                >
                  <StatChip
                    v-for="reason in pickSoftReasons(pick)"
                    :key="`${pick.number}-${reason}`"
                    :tone="SOFT_REASON_TONES[reason] ?? 'neutral'"
                    size="xs"
                    dot
                  >
                    {{ SOFT_REASON_LABELS[reason] }}
                  </StatChip>
                  <StatChip v-if="pick.in_lattice_band" tone="aqua" size="xs" outline>
                    预测带内
                  </StatChip>
                </div>

                <div class="w-full space-y-1.5 text-sm">
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">差值</span>
                    <AnimatedNumber :value="pick.diff" :pad="0" text-class="text-slate-200" />
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">波动</span>
                    <StatChip :tone="waveTones[pick.wave_type] ?? 'neutral'" size="xs">
                      {{ pick.wave_label }}
                    </StatChip>
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">近窗出现</span>
                    <span class="num text-xs text-slate-200">
                      {{ pick.trend_count ?? 0 }} 次
                    </span>
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">距上次出现</span>
                    <span
                      class="text-xs"
                      :class="pick.avoid_cold_penalized ? 'text-amber-300' : 'text-slate-200'"
                    >
                      {{ avoidColdDaysText(pick.days_since_last) }}
                    </span>
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">重号</span>
                    <span
                      class="text-xs font-medium"
                      :class="pick.is_repeat_number ? 'text-bloom-300' : 'text-emerald-300'"
                    >
                      {{ pick.is_repeat_number ? '是' : '否' }}
                    </span>
                  </div>
                  <div class="flex items-center justify-between">
                    <span class="text-slate-500">重肖</span>
                    <span
                      class="text-xs font-medium"
                      :class="pick.is_repeat_zodiac ? 'text-bloom-300' : 'text-emerald-300'"
                    >
                      {{ pick.is_repeat_zodiac ? '是' : '否' }}
                    </span>
                  </div>
                  <div
                    v-if="pick.is_stale"
                    class="flex items-center justify-between"
                  >
                    <span class="text-slate-500">冷号</span>
                    <StatChip tone="aqua" size="xs">
                      {{ periodsSinceLastText(pick.periods_since_last) }}
                    </StatChip>
                  </div>
                  <div
                    v-if="pick.soft_penalized"
                    class="flex items-center justify-between"
                  >
                    <span class="text-slate-500">降权</span>
                    <StatChip tone="amber" size="xs">
                      {{ softWeightText(pick.soft_weight) }}
                    </StatChip>
                  </div>
                  <div
                    v-if="pick.avoid_cold_penalized"
                    class="flex items-center justify-between"
                  >
                    <span class="text-slate-500">避冷加权</span>
                    <StatChip tone="amber" size="xs">
                      {{ avoidColdWeightText(pick.avoid_cold_weight) }}
                    </StatChip>
                  </div>
                </div>

                <!-- 同角色走势参考：pick.wave_type × pick.role，筹码点阵散落 -->
                <div
                  v-if="result.trend_distributions"
                  class="w-full border-t border-white/5 pt-3"
                >
                  <div class="mb-2 flex flex-wrap items-center justify-between gap-1">
                    <span class="text-[11px] font-medium text-slate-400">
                      走势参考 · {{ pick.wave_label }}
                    </span>
                    <span class="text-[10px] text-slate-500">经验频率 · 非概率</span>
                  </div>
                  <p
                    v-if="!pickTrendRefs(pick).length"
                    class="text-[11px] leading-relaxed text-slate-500"
                  >
                    数据不足：本档同角色暂无可列号码
                  </p>
                  <div
                    v-else
                    class="trend-scatter flex flex-wrap content-start items-center overflow-visible px-1 py-2"
                    role="list"
                    :aria-label="`${pick.role_label}走势参考`"
                  >
                    <span
                      v-for="(item, chipIndex) in pickTrendRefs(pick).slice(0, 12)"
                      :key="`${pick.role}-${pick.wave_type}-${item.number}`"
                      role="listitem"
                      class="trend-chip num inline-flex min-h-[34px] min-w-[38px] select-none flex-col items-center justify-center rounded-full px-1.5 py-0.5 text-center leading-none will-change-transform"
                      :class="item.number === pick.number
                        ? 'z-[1] bg-aqua-400/25 text-aqua-50 ring-1 ring-aqua-300/60 shadow-[0_0_12px_rgba(34,211,238,0.35)]'
                        : 'bg-white/[0.06] text-slate-200 ring-1 ring-white/10'"
                      :style="scatterChipStyle(item, chipIndex, pick.number)"
                      :title="`${pad(item.number)} · ${trendCountLabel(item)}`"
                    >
                      <span class="text-[13px] font-semibold tabular-nums">{{ pad(item.number) }}</span>
                      <span class="mt-0.5 text-[9px] tabular-nums text-slate-400">
                        {{ trendChipSub(item) }}
                      </span>
                    </span>
                  </div>
                  <p
                    v-if="pickTrendRefs(pick).length > 12"
                    class="mt-1 text-[10px] text-slate-500"
                  >
                    另有 {{ pickTrendRefs(pick).length - 12 }} 个未展开
                  </p>
                </div>
              </GlassCard>
            </MotionReveal>
          </div>
        </section>

        <!-- 预测波动线 · 折线 + 1~49 号码点阵：带内优先参与选号（口径：经验分布，非概率） -->
        <MotionReveal v-if="lattice" :index="6">
          <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
            <div class="flex flex-wrap items-start justify-between gap-2">
              <div class="space-y-1">
                <h2 class="text-lg font-medium text-white">预测波动线 · 号码点阵</h2>
                <p class="text-xs leading-relaxed text-slate-500">
                  折线按本池各期特码原值（1–49）绘制；阴影带由相邻差值波动区间 P25~P75 换算到特码轴
                  （基准 = 最新特码，即 [最新−P75, 最新−P25]，超出 1–49 按边界截断）—— 是差值区间
                  的换算，不是独立的特码分布。下方点阵标出带内号码。口径：样本内经验分布，非概率。
                </p>
              </div>
              <StatChip :tone="lattice.enabled ? 'aqua' : 'neutral'" size="sm">
                {{ lattice.enabled ? '已参与选号' : '未开启' }}
              </StatChip>
            </div>

            <!-- 未开启：诚实空态，不画假线 -->
            <p
              v-if="!lattice.enabled"
              class="text-xs leading-relaxed text-slate-500"
            >
              预测波动线未开启：当前设置未启用点阵取样，本页不绘制预测线路。可在设置页打开「预测波动线 · 号码点阵」。
            </p>

            <template v-else-if="latticeBand">
              <div class="flex flex-wrap items-center gap-2">
                <StatChip tone="aqua" size="sm" dot>
                  带内 {{ inBandCount }} / {{ latticeNumbers.length }} 号
                </StatChip>
                <StatChip :tone="waveTones[latticeBand.wave_type] ?? 'neutral'" size="sm">
                  差值中心 |{{ latticeBand.center_number }}| · {{ latticeBand.wave_label }}
                </StatChip>
                <StatChip tone="neutral" size="sm">
                  差值带 |{{ latticeBand.low }}| ~ |{{ latticeBand.high }}| → 换算到特码轴
                </StatChip>
                <StatChip tone="neutral" size="sm">
                  样本 {{ latticeBand.samples }} 对差值 · 近 {{ latticeBand.used_window }} 对
                </StatChip>
                <StatChip v-if="lattice.primary_wave_label" tone="amber" size="sm">
                  优先 {{ lattice.primary_wave_label }}
                </StatChip>
                <StatChip v-if="result.picks.length" tone="bloom" size="sm">
                  本次推荐落带内 {{ pickedInBandCount }} / {{ result.picks.length }} 注
                </StatChip>
              </div>

              <!-- 预测波动折线：与首页「最近特码走势」同组件 / 同交互（号码光标） -->
              <div class="space-y-2">
                <div class="flex flex-wrap items-end justify-between gap-2">
                  <div>
                    <h3 class="text-sm font-medium text-slate-100">预测波动线</h3>
                    <p class="text-[11px] text-slate-500">
                      纵轴为特码原值（1–49）；实线 = 本池各期特码（只画真实开奖点，不做外推）；
                      阴影带 = 由相邻差值波动区间 P25~P75 换算到特码轴（基准 = 最新特码，即 [最新−P75, 最新−P25]，
                      超出 1–49 按边界截断）—— 是差值区间的换算，不是独立的特码分布。
                      本期候选：从最新一期拉出的多条虚线，末端按真实特码值（1–49）排成一列 ——
                      看点就是这几注在号码轴上的分布（疏 / 密），点色：绿 = 预测带内 / 粉 = 带外；
                      非真实开奖。左右滑动移动光标。
                    </p>
                  </div>
                  <span class="num text-[11px] text-slate-500">
                    {{ waveTrendChart.pointCount }} 期
                  </span>
                </div>
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
                  v-model:window-size="waveTrendWindow"
                  :window-options="WAVE_TREND_WINDOW_OPTIONS"
                  axis-hint="纵轴为特码原值 1–49；实线只画本池各期真实特码；阴影带由相邻差值区间换算到特码轴（基准 = 最新特码，即 [最新−P75, 最新−P25]）。"
                  empty-text="数据不足"
                  empty-hint="本池至少要 2 期开奖记录才能连成预测波动线"
                />
                <!-- 图例：实线=各期特码；灰点=本池最新一期特码；虚线扇形+竖列点=本期候选（末端按真实特码值排布，非真实开奖） -->
                <p class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
                  <span class="inline-flex items-center gap-1.5">
                    <span class="h-0.5 w-4 rounded-full bg-aqua-300/80" aria-hidden="true" />
                    实线 · 各期特码（1–49）
                  </span>
                  <span
                    v-if="waveTrendChart.annotations.length"
                    class="inline-flex items-center gap-1.5"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full bg-slate-300/90 ring-2 ring-slate-300/20"
                      aria-hidden="true"
                    />
                    灰点 · 本池最新一期特码（纵轴即特码原值）
                  </span>
                  <span
                    v-if="waveTrendChart.candidateColumn.length"
                    class="inline-flex items-center gap-1.5"
                  >
                    <span
                      class="h-0 w-4 border-t-2 border-dashed border-emerald-400/80"
                      aria-hidden="true"
                    />
                    虚线扇形 · 本期候选（从最新一期拉出，末端按真实特码值 1–49 竖列排布；点色：绿=带内 / 粉=带外；非真实开奖）
                  </span>
                  <span v-else class="text-slate-600">
                    生成财富密码后，候选号码会从最新一期拉出多条虚线，末端按真实特码值（1–49）竖列排成一列，看点就是分布（绿=带内 / 粉=带外）
                  </span>
                </p>
              </div>

              <div
                class="grid grid-cols-7 gap-1.5 sm:gap-2"
                role="list"
                aria-label="1 到 49 号码点阵"
              >
                <div
                  v-for="cell in latticeNumbers"
                  :key="cell.number"
                  role="listitem"
                  class="relative flex flex-col items-center justify-center rounded-xl px-1 py-1.5 text-center ring-1"
                  :class="cell.in_band
                    ? 'bg-aqua-400/15 text-aqua-100 ring-aqua-300/50'
                    : 'bg-white/[0.04] text-slate-300 ring-white/10'"
                  :style="latticeCellStyle(cell)"
                  :title="`${pad(cell.number)} · 差值 |${cell.diff}| · ${cell.wave_label}${cell.in_band ? ' · 预测带内' : ' · 预测带外'}`"
                >
                  <span class="num text-[13px] font-semibold tabular-nums">
                    {{ pad(cell.number) }}
                  </span>
                  <span class="num text-[9px] tabular-nums opacity-70">|{{ cell.diff }}|</span>
                  <!-- 右上角：本期推荐号码（不参与任何判定，只是打标） -->
                  <span
                    v-if="pickedNumbers.has(cell.number)"
                    class="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-bloom-400 ring-2 ring-slate-950"
                    aria-hidden="true"
                  />
                  <!-- 左上角：本池最新一期号码 -->
                  <span
                    v-if="cell.is_latest"
                    class="absolute -left-1 -top-1 h-2 w-2 rounded-full bg-amber-300 ring-2 ring-slate-950"
                    aria-hidden="true"
                  />
                </div>
              </div>

              <p class="text-[11px] leading-relaxed text-slate-500">
                {{ latticeBand.rule }}
                高亮 = 带内；右上红点 = 本期推荐；左上金点 = 本池最新一期号码。
                带外号码不会被排除，只是抽样概率更低。
              </p>
            </template>

            <p v-else class="text-xs leading-relaxed text-slate-500">
              数据不足：本池不足两期，无法估算预测波动线（按「无预测」处理，不编造带宽）。
            </p>
          </GlassPanel>
        </MotionReveal>

        <!-- 01 - 49 出现热度：本页为单列手机版式，直接整幅铺开（不用开奖页的 lg:grid-cols-3 / col-span-2 栅格） -->
        <MotionReveal :index="7">
          <GlassPanel padding="lg" rounded="3xl">
            <div class="mb-4">
              <h2 class="text-lg font-medium text-white">01 - 49 出现热度</h2>
              <p class="text-xs text-slate-500">
                同一个青色系里，颜色越深表示在所选期数里出现越多；
                中性灰格表示暂时没有出现过。下方色阶标出每一档对应的出现次数。
              </p>
            </div>

            <!-- 统计期数：仅本页展示状态，切换只重算下方网格，不写库、不改任何请求 -->
            <div class="mb-4 space-y-2">
              <div class="flex flex-wrap gap-2" role="group" aria-label="热度统计期数">
                <button
                  v-for="option in heatWindowOptions"
                  :key="option"
                  type="button"
                  class="num min-h-[44px] min-w-[64px] rounded-xl border px-4 text-base font-medium transition-colors duration-200 select-none active:scale-[0.98]"
                  :class="option === heatWindow
                    ? 'border-aqua-400/60 bg-aqua-400/15 text-aqua-100'
                    : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                  :aria-pressed="option === heatWindow"
                  @click="heatWindow = option"
                >
                  {{ option }}
                </button>
              </div>
              <p class="text-[11px] leading-relaxed text-slate-500">{{ heatWindowCaption }}</p>
            </div>

            <HeatGrid
              :cells="heatCells"
              :columns="7"
              tone="aqua"
              :highlight="latestSampleNumber === null ? [] : [pad(latestSampleNumber)]"
              legend
              value-unit="次"
              :legend-scope="heatScope"
              empty-text="数据不足"
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

        <!-- 复制投注串 -->
        <MotionReveal :index="9">
          <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-lg font-medium text-white">投注串</h2>
              <StatChip tone="neutral" size="sm">复制到剪贴板</StatChip>
            </div>

            <div class="relative">
              <GlassButton
                variant="primary"
                size="lg"
                block
                class="min-h-[48px]"
                @click="copy"
              >
                {{ copied ? '已复制 ✓' : '一键复制投注串' }}
              </GlassButton>
              <PulseRing :trigger="copyHit.key" tone="aqua" :rings="2" />
            </div>

            <p class="whitespace-pre-line break-all text-center font-mono text-sm text-slate-300">
              {{ result.copy_text }}
            </p>
            <p v-if="copyError" class="text-center text-xs text-bloom-300" role="alert">
              {{ copyError }}
            </p>
            <p v-else-if="copied" class="text-center text-xs text-emerald-300" role="status">
              已复制到剪贴板
            </p>
          </GlassPanel>
        </MotionReveal>

        <!-- 出票单（选号工具）：花费控制 · 号码卫生 · 覆盖透明 · 留痕复现 · 诚实披露 -->
        <MotionReveal :index="10">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" class="space-y-4">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-lg font-medium text-white">出票单</h2>
              <StatChip tone="aqua" size="sm" dot>生成 · 换一批 · 复制 · 导出</StatChip>
            </div>

            <p class="text-xs leading-relaxed text-slate-500">
              把上面这份候选号码落成一张可直接拿去用的票：预算精确拆到每一注、每注不低于
              {{ MIN_BET_AMOUNT }} 元、被降权省下的钱如实列成「未投」而<strong class="font-semibold text-slate-400">不补给</strong>其它注。
              它只管<b class="font-semibold text-slate-400">纪律、号码卫生、覆盖透明和留痕</b>，
              不改变中奖概率，也不改变期望值。
            </p>

            <!-- 预算 / 注数 -->
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <label id="ticket-budget-label" class="text-sm font-medium text-slate-200">预算（元）</label>
                  <StatChip tone="neutral" size="sm">{{ TICKET_BUDGET_MIN }}–{{ TOTAL_AMOUNT_MAX }} · {{ TICKET_BUDGET_STEP }} 元一档</StatChip>
                </div>
                <div class="flex flex-wrap items-center gap-2" role="group" aria-labelledby="ticket-budget-label">
                  <GlassButton
                    variant="glass"
                    class="min-h-[44px] min-w-[44px] px-0 text-lg"
                    :disabled="ticketPending || ticketBudget <= TICKET_BUDGET_MIN"
                    aria-label="减少预算"
                    @click="nudgeTicketBudget(-TICKET_BUDGET_STEP)"
                  >
                    −
                  </GlassButton>
                  <input
                    :value="ticketBudget"
                    type="number"
                    :min="TICKET_BUDGET_MIN"
                    :max="TOTAL_AMOUNT_MAX"
                    :step="TICKET_BUDGET_STEP"
                    inputmode="numeric"
                    class="num h-11 w-24 rounded-xl border border-white/15 bg-white/5 px-3 text-center text-base text-slate-100 outline-none focus:border-aqua-400/50"
                    :disabled="ticketPending"
                    aria-labelledby="ticket-budget-label"
                    @change="onTicketBudgetInput"
                  >
                  <GlassButton
                    variant="glass"
                    class="min-h-[44px] min-w-[44px] px-0 text-lg"
                    :disabled="ticketPending || ticketBudget >= TOTAL_AMOUNT_MAX"
                    aria-label="增加预算"
                    @click="nudgeTicketBudget(TICKET_BUDGET_STEP)"
                  >
                    +
                  </GlassButton>
                </div>
              </div>

              <div class="space-y-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <label id="ticket-count-label" class="text-sm font-medium text-slate-200">注数</label>
                  <StatChip tone="neutral" size="sm">{{ BET_COUNT_MIN }}–{{ BET_COUNT_MAX }}</StatChip>
                </div>
                <div class="flex flex-wrap items-center gap-2" role="group" aria-labelledby="ticket-count-label">
                  <GlassButton
                    variant="glass"
                    class="min-h-[44px] min-w-[44px] px-0 text-lg"
                    :disabled="ticketPending || ticketPickCount <= BET_COUNT_MIN"
                    aria-label="减少注数"
                    @click="nudgeTicketCount(-1)"
                  >
                    −
                  </GlassButton>
                  <input
                    :value="ticketPickCount"
                    type="number"
                    :min="BET_COUNT_MIN"
                    :max="BET_COUNT_MAX"
                    inputmode="numeric"
                    class="num h-11 w-24 rounded-xl border border-white/15 bg-white/5 px-3 text-center text-base text-slate-100 outline-none focus:border-aqua-400/50"
                    :disabled="ticketPending"
                    aria-labelledby="ticket-count-label"
                    @change="onTicketCountInput"
                  >
                  <GlassButton
                    variant="glass"
                    class="min-h-[44px] min-w-[44px] px-0 text-lg"
                    :disabled="ticketPending || ticketPickCount >= BET_COUNT_MAX"
                    aria-label="增加注数"
                    @click="nudgeTicketCount(1)"
                  >
                    +
                  </GlassButton>
                </div>
              </div>
            </div>

            <!-- 筹码模式（出票用，不写默认值） -->
            <div class="space-y-2">
              <p class="text-sm font-medium text-slate-200">筹码模式</p>
              <div class="flex flex-wrap gap-2">
                <GlassButton
                  v-for="option in chipModes"
                  :key="`ticket-mode-${option.value}`"
                  :variant="ticketMode === option.value ? 'primary' : 'glass'"
                  class="min-h-[44px] px-4 text-base"
                  :disabled="ticketPending"
                  @click="ticketMode = option.value"
                >
                  {{ option.label }}
                </GlassButton>
              </div>
            </div>

            <!-- 号码卫生开关：沿用设置页口径，仅本次出票生效 -->
            <div class="space-y-2">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm font-medium text-slate-200">号码卫生</p>
                <StatChip tone="neutral" size="sm">只影响排序与注码分配</StatChip>
              </div>
              <div class="flex flex-wrap gap-2">
                <GlassButton
                  :variant="ticketRepeatNumber ? 'primary' : 'glass'"
                  class="min-h-[40px] px-3 text-sm"
                  :disabled="ticketPending"
                  @click="ticketRepeatNumber = !ticketRepeatNumber"
                >
                  {{ ticketRepeatNumber ? '✓ ' : '' }}重号保留（只降权）
                </GlassButton>
                <GlassButton
                  :variant="ticketExcludeZodiac ? 'primary' : 'glass'"
                  class="min-h-[40px] px-3 text-sm"
                  :disabled="ticketPending"
                  @click="ticketExcludeZodiac = !ticketExcludeZodiac"
                >
                  {{ ticketExcludeZodiac ? '✓ ' : '' }}避开上期同肖
                </GlassButton>
                <GlassButton
                  :variant="ticketStale ? 'primary' : 'glass'"
                  class="min-h-[40px] px-3 text-sm"
                  :disabled="ticketPending"
                  @click="ticketStale = !ticketStale"
                >
                  {{ ticketStale ? '✓ ' : '' }}冷号降权
                </GlassButton>
                <GlassButton
                  :variant="ticketLattice ? 'primary' : 'glass'"
                  class="min-h-[40px] px-3 text-sm"
                  :disabled="ticketPending"
                  @click="ticketLattice = !ticketLattice"
                >
                  {{ ticketLattice ? '✓ ' : '' }}预测波动带优先
                </GlassButton>
              </div>
            </div>

            <!-- 行动 -->
            <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
              <GlassButton
                variant="primary"
                size="lg"
                block
                :loading="ticketPending"
                :disabled="ticketPending"
                @click="generateTicket"
              >
                {{ ticket ? '重新生成出票单' : '生成出票单' }}
              </GlassButton>
              <GlassButton
                variant="glass"
                size="lg"
                block
                class="shrink-0 sm:w-auto"
                :disabled="ticketPending || !ticket"
                @click="reshuffleTicket"
              >
                换一批
              </GlassButton>
              <PulseRing :trigger="ticketHit.key" tone="aqua" :rings="2" />
            </div>

            <p v-if="ticketError" class="glass-panel rounded-2xl border border-bloom-400/40 px-4 py-3 text-sm text-bloom-200" role="alert">
              {{ ticketError }}
            </p>

            <template v-if="ticket">
              <!-- 口径与期号 -->
              <div class="flex flex-wrap items-center gap-2">
                <StatChip tone="aqua" size="sm" dot>目标第 {{ ticket.data.target_period }} 期</StatChip>
                <StatChip tone="neutral" size="sm">
                  上期 #{{ pad(ticket.data.latest_number) }}
                  {{ ticket.data.previous_number === null ? '' : `· 前一期 #${pad(ticket.data.previous_number)}` }}
                </StatChip>
                <StatChip tone="neutral" size="sm">{{ ticket.scope }}</StatChip>
                <StatChip tone="nebula" size="sm">{{ ticket.mode_label }}</StatChip>
                <StatChip tone="neutral" size="sm">{{ ticket.selection_label }}</StatChip>
                <StatChip tone="neutral" size="sm">票号 {{ ticket.ticket_id.slice(0, 12) }}…</StatChip>
              </div>

              <!-- 零注：预算连 1 个注码单位都覆盖不了 → 说明本次不出票（票面文本里也有一条） -->
              <p
                v-if="ticketNoTicket"
                class="rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-200"
                role="status"
              >
                <strong class="font-semibold">本次不出票</strong>：{{ ticketNoTicketReason }}
              </p>

              <!-- 逐注：号码 / 金额 / 标记 -->
              <div class="space-y-2">
                <div class="flex flex-wrap items-baseline justify-between gap-2">
                  <p class="text-sm font-medium text-slate-200">
                    {{ ticket.picks.length }} 注 · 合计 {{ ticket.budget.staked }} 元
                  </p>
                  <p class="text-xs text-slate-500">
                    预算 {{ ticket.budget.requested }} 元
                    <template v-if="ticket.budget.unspent > 0">
                      · 未投 {{ ticket.budget.unspent }} 元（降权省下，不补给其它注）
                    </template>
                  </p>
                </div>
                <p class="text-[11px] leading-relaxed text-slate-500">
                  角色（主推 / 次选 / 防守）、波动、带内、冷号都只是既有设置里的打标口径：
                  只影响筹码分配与号码卫生偏好，<span class="text-slate-400">不代表哪一注更可能开出</span>。
                </p>
                <div class="grid gap-2 sm:grid-cols-2">
                  <div
                    v-for="row in ticket.picks"
                    :key="`ticket-pick-${row.number}`"
                    class="flex flex-wrap items-center gap-2 rounded-xl bg-white/5 px-3 py-2"
                  >
                    <span class="num text-base font-semibold text-white">{{ pad(row.number) }}</span>
                    <span class="num text-sm text-slate-200">{{ row.amount }} 元</span>
                    <StatChip :tone="roleTones[row.role ?? ''] ?? 'neutral'" size="xs">
                      {{ row.role_label }}
                    </StatChip>
                    <StatChip v-if="row.zodiac_label" tone="nebula" size="xs">
                      {{ row.zodiac_label }}
                    </StatChip>
                    <StatChip :tone="waveTones[row.wave_type ?? ''] ?? 'neutral'" size="xs">
                      {{ row.wave_label }}
                    </StatChip>
                    <StatChip
                      v-for="reason in ticketSoftReasons(row)"
                      :key="`ticket-${row.number}-${reason}`"
                      :tone="SOFT_REASON_TONES[reason] ?? 'neutral'"
                      size="xs"
                      dot
                    >
                      {{ SOFT_REASON_LABELS[reason] }}
                    </StatChip>
                    <StatChip v-if="row.in_lattice_band" tone="aqua" size="xs" outline>
                      预测带内
                    </StatChip>
                  </div>
                </div>
              </div>

              <!-- 覆盖报告 -->
              <div class="space-y-2 border-t border-white/5 pt-3">
                <div class="flex flex-wrap items-baseline justify-between gap-2">
                  <p class="text-sm font-medium text-slate-200">覆盖报告</p>
                  <p class="text-xs text-slate-500">
                    覆盖 {{ ticket.coverage.covered_count }}/{{ ticket.coverage.total_numbers }} 号 ·
                    候选池 {{ ticket.coverage.candidate_pool_size }} 个
                  </p>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <StatChip tone="neutral" size="sm">
                    大 {{ ticket.coverage.big_small.big }}（≥{{ ticket.coverage.big_small.big_min }}）
                  </StatChip>
                  <StatChip tone="neutral" size="sm">小 {{ ticket.coverage.big_small.small }}</StatChip>
                  <StatChip tone="neutral" size="sm">奇 {{ ticket.coverage.odd_even.odd }}</StatChip>
                  <StatChip tone="neutral" size="sm">偶 {{ ticket.coverage.odd_even.even }}</StatChip>
                  <StatChip tone="neutral" size="sm">
                    尾数
                    {{ ticket.coverage.tail_digit.filter(item => item.count > 0).map(item => `${item.digit}×${item.count}`).join('、') || '—' }}
                  </StatChip>
                </div>
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="text-xs text-slate-500">生肖分布</span>
                  <StatChip
                    v-for="item in ticket.coverage.zodiac.filter(row => row.count > 0)"
                    :key="`ticket-zodiac-${item.group}`"
                    tone="nebula"
                    size="xs"
                  >
                    {{ item.label || item.code || `第${item.group + 1}组` }} ×{{ item.count }}
                  </StatChip>
                </div>
                <p class="text-[11px] leading-relaxed text-slate-500">
                  {{ ticket.coverage.candidate_pool_note }}
                  未覆盖的 {{ ticket.coverage.uncovered_count }} 个号本期不投 —— 覆盖范围是这只票的取舍，不是命中概率。
                </p>
              </div>

              <!-- 诚实页脚：期望值 / 随机基线 / 结果分布 / 历史最久连续未中 -->
              <div class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p class="text-sm font-medium text-slate-200">诚实提示</p>
                  <StatChip tone="amber" size="sm">{{ ticket.claim }} · {{ ticket.claim_label }}</StatChip>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <StatChip tone="bloom" size="sm">
                    每 100 元期望 {{ yuan(ticket.honest.ev_per_100) }} 元
                  </StatChip>
                  <StatChip tone="bloom" size="sm">
                    本票期望亏损 {{ yuan(ticket.honest.expected_loss_for_this_ticket) }} 元
                  </StatChip>
                  <StatChip tone="neutral" size="sm">
                    命中概率 {{ pct(ticket.honest.baseline_hit_rate) }}（= {{ ticket.picks.length }}/49，不因选号改变）
                  </StatChip>
                  <StatChip tone="neutral" size="sm">
                    不中 {{ pct(ticket.honest.hit_distribution.p_zero_hits) }} · 至少中一次
                    {{ pct(ticket.honest.hit_distribution.p_at_least_one_hit) }}
                  </StatChip>
                </div>
                <p class="text-[11px] leading-relaxed text-slate-500">
                  {{ ticket.honest.ev_note }} {{ ticket.honest.baseline_note }}
                </p>
                <p class="text-[11px] leading-relaxed text-slate-500">
                  本池样本内走步回测：{{ ticket.honest.in_sample.evaluated }} 期命中
                  {{ ticket.honest.in_sample.hits }} 期（{{ pct(ticket.honest.in_sample.hit_rate) }}）vs 随机
                  {{ pct(ticket.honest.in_sample.random_baseline_hit_rate) }}；判定
                  {{ ticket.honest.in_sample.verdict }}（{{ ticket.honest.in_sample.verdict_label }}）。
                  历史最久连续未中：{{ ticket.honest.in_sample.max_dry_streak_periods }} 期。
                  {{ ticket.honest.in_sample.note }}
                </p>
              </div>

              <!-- 复制 / 导出 / 冻结 -->
              <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
                <GlassButton
                  variant="primary"
                  size="lg"
                  block
                  class="min-h-[48px]"
                  @click="copyTicket"
                >
                  {{ ticketCopied ? '已复制 ✓' : '复制出票单' }}
                </GlassButton>
                <PulseRing :trigger="ticketCopyHit.key" tone="aqua" :rings="2" />
                <GlassButton
                  variant="glass"
                  size="lg"
                  class="min-h-[48px] w-full shrink-0 sm:w-auto"
                  @click="exportTicket"
                >
                  导出 TXT / JSON
                </GlassButton>
                <GlassButton
                  variant="glass"
                  size="lg"
                  class="min-h-[48px] w-full shrink-0 sm:w-auto"
                  :loading="freezePending"
                  :disabled="freezePending || !freezeAvailable"
                  :title="freezeAvailable ? '开奖前冻结，开奖后按同期号诚实计分' : freezeUnavailableReason"
                  @click="freezeTicket"
                >
                  冻结到台账
                </GlassButton>
              </div>

              <p v-if="ticketCopyError" class="text-center text-xs text-bloom-300" role="alert">
                {{ ticketCopyError }}
              </p>
              <p v-else-if="ticketCopied" class="text-center text-xs text-emerald-300" role="status">
                已复制到剪贴板
              </p>
              <p v-if="freezeNotice" class="text-center text-xs text-emerald-300" role="status">
                {{ freezeNotice }}
              </p>
              <p v-if="freezeError" class="text-center text-xs text-bloom-300" role="alert">
                {{ freezeError }}
              </p>
              <p v-if="!freezeAvailable" class="text-center text-[11px] leading-relaxed text-slate-500">
                台账暂不可用（{{ freezeUnavailableReason }}）：出票、复制、导出都不受影响；
                冻结也可以直接用 CLI 跑
                <code class="font-mono text-slate-400">scripts/forward_validate.py</code>。
              </p>
              <p v-else class="text-center text-[11px] leading-relaxed text-slate-500">
                台账已冻结 {{ ledger?.records ?? 0 }} 期、待开奖 {{ ledger?.pending_periods?.length ?? 0 }} 期；
                冻结只做诚实对账，不改变中奖概率与期望值。
              </p>

              <details class="rounded-2xl bg-white/5 px-4 py-3">
                <summary class="cursor-pointer text-xs text-slate-400">查看纯文本出票单</summary>
                <pre class="mt-2 overflow-x-auto whitespace-pre font-mono text-xs leading-relaxed text-slate-300">{{ ticket.ticket_text }}</pre>
              </details>
            </template>
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
