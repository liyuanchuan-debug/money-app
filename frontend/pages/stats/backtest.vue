<script setup lang="ts">
definePageMeta({ role: 'VIP' })

/**
 * 策略回测 —— POST /api/stats/backtest
 * --------------------------------------------------------------------------
 * 【诚实要求】本页绝不允许把回测结果包装成「引擎有效」：
 *   - 后端给出 hit_rate / random_baseline_hit_rate / hit_rate_minus_baseline /
 *     hit_rate_standard_error / evaluated / hits，判定口径是
 *     |命中率 − 随机参考值| 是否落在 2 倍抽样标准误内；
 *   - 差值落在噪声内时，页面必须直白地说「目前无法证明该策略优于随机」，
 *     判定文案由 composables/useStats.ts 的 backtestVerdict() 统一生成，页面不改写、不弱化；
 *   - 命中率的**理论上限**（actual_in_pool_rate）必须展示并解释：
 *     候选池始终排除上一期特码本身；若设置开启「避开重肖」，还会排除该特码的整组同生肖。
 *     当实际特码落在被排除的号码里时引擎不可能命中 —— 所以上限不是 100%。
 *
 * 【可复现】本页是**可改的输入面板**：面板里改动 mode / pick_count 会立刻用新参数重算下方
 * 结果（watch + 手动「重新计算」兜底）。未选的字段不放进请求体，由后端取**当前用户已保存的
 * 设置**（GET /api/settings）作为基线；显式选中的字段才是本次覆盖项。后端在 settings 里回报
 * 「本次运行实际生效的参数」，并在 parameter_sources 里逐字段标明来源
 * （request_override / saved_settings / default），页面原样展示这两组信息，保证可复现。
 *
 * 【注数可能被模式忽略】后端约定 effective_pick_count = 1（当 mode == single），
 * 此时 pick_count 只落库不参与计算 —— 改注数在数学上不可能改变结果。页面必须把这件事
 * 明说出来（settings.pick_count !== settings.effective_pick_count 即触发），
 * 否则用户会以为「改了参数但结果不重算」。
 *
 * TODO(auth / RBAC) —— 本页必须做 VIP 鉴权，当前鉴权系统未上线，禁止在这里实现假鉴权：
 *   1. 鉴权落地后 /stats/backtest 须按角色限制为 **VIP**（前端只做体验，真正拦截在后端）；
 *   2. 后端 AUTH_ENFORCED 打开后本接口会返回 403/401 —— 页面必须把 403 渲染成可读的
 *      「需要 VIP 权限」状态，不得白屏、不得静默失败、不得 fail-open 放行；
 *      该降级分支已实现在 StatsPageFrame 的错误态里（required-role="VIP"），此处不再重复判断。
 */
import { CHIP_MODE_OPTIONS, useApi } from '~/composables/useApi'
import type {
  BacktestPayload,
  BacktestStats,
  BacktestSweepPayload,
  BacktestSweepRow,
  BacktestSweepStats,
} from '~/composables/useStats'
import {
  STATS_DEFAULT_SAMPLE_LIMIT,
  backtestVerdict,
  formatPp,
  formatRate,
  formatSignedPp,
  padStatNumber,
  pctOf,
  useStats,
} from '~/composables/useStats'

const api = useStats()
const lotteryApi = useApi()
const sampleLimit = ref(STATS_DEFAULT_SAMPLE_LIMIT)

/** '' = 不覆盖该字段，沿用当前用户**已保存的设置**（不是前端硬编码，也不是 DEFAULT_SETTINGS） */
const modeOverride = ref<string>('')
const pickOverride = ref<string>('')

const PICK_COUNT_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

/** 只传显式覆盖项；未选的字段不传，交给后端用「已保存的设置」作基线 */
const payload = computed<BacktestPayload>(() => {
  const body: BacktestPayload = { limit: sampleLimit.value }
  if (modeOverride.value) body.mode = modeOverride.value
  if (pickOverride.value !== '') body.pick_count = Number(pickOverride.value)
  return body
})

/** 只读 / 纯计算：回测不写库 */
const { data, pending, error, refresh } = await useAsyncData<BacktestStats>(
  'stats-backtest',
  () => api.backtest(payload.value),
  { watch: [sampleLimit, modeOverride, pickOverride] },
)

useHead({ title: '策略回测 · 四叶沙盘' })

/** 判定文案（唯一来源；本页原样渲染，不重写不弱化） */
const verdict = computed(() => backtestVerdict(data.value))

const settings = computed(() => data.value?.settings ?? null)
const evaluationWindow = computed(() => data.value?.evaluation_window ?? null)

/* ---------------- 参数来源 / 注数是否被模式忽略（让「重算」看得见） ---------------- */

/** 后端 parameter_sources 的取值 → 汉字标签（键与后端逐字段一一对应，禁止自造取值） */
const PARAMETER_SOURCE_LABELS: Record<string, string> = {
  request_override: '本次覆盖',
  saved_settings: '已保存设置',
  default: '后端默认',
}

/** 后端回报的逐字段来源；老响应没有该字段时为 null（页面按「—」处理，不编来源） */
const parameterSources = computed(() => data.value?.parameter_sources ?? null)

/** 取某个生效参数的来源标签；后端没给来源时返回「—」，绝不猜成「已保存设置」 */
function parameterSourceLabel(key: 'mode' | 'pick_count' | 'small_max' | 'normal_max'): string {
  const code = parameterSources.value?.[key]
  if (!code) return '—'
  return PARAMETER_SOURCE_LABELS[code] ?? code
}

/** 面板里可覆盖的两项，是否由「本次请求」显式指定（用于给下拉框加提示） */
const modeIsOverride = computed(() => parameterSources.value?.mode === 'request_override')
const pickIsOverride = computed(() => parameterSources.value?.pick_count === 'request_override')

/**
 * 注数是否被当前模式忽略：后端 effective_pick_count 与 pick_count 不一致时即为真。
 * 当前后端只在 mode == single（单挑）时这样（固定 1 注），但这里按「不等就提示」判断，
 * 不写死 single，避免后端以后调整规则时前端又说错话。
 */
const pickCountIgnored = computed(() => {
  const current = settings.value
  if (!current) return false
  return current.effective_pick_count !== current.pick_count
})

/** 能真正比较注数的模式清单：当前模式之外的全部模式（不写死，后端加新模式也不会漏） */
const pickCountCapableModes = computed(() => {
  const currentMode = settings.value?.mode
  return CHIP_MODE_OPTIONS.filter((option) => option.value !== currentMode)
    .map((option) => `「${option.label}」`)
    .join('、')
})

/** 注数被忽略时的白话说明（必须告诉用户「改注数不会改变结果」以及怎么改才有效） */
const pickCountIgnoredNote = computed(() => {
  const current = settings.value
  if (!current || !pickCountIgnored.value) return ''
  const pickPhrase = pickIsOverride.value
    ? `你本次选的 ${current.pick_count} 注（来源：本次覆盖）`
    : `已保存设置里的 ${current.pick_count} 注`
  return `当前筹码模式是「${effectiveModeLabel.value}」，该模式按规则固定为 `
    + `${current.effective_pick_count} 注：${pickPhrase}不参与计算，`
    + '所以在这套模式下改「注数」不会改变下面的回测结果。'
    + `要比较不同注数，请先把筹码模式改成${pickCountCapableModes.value}。`
})

/** 手动兜底：参数没变但结果想看一次新的，或自动重算没触发时用 */
const recomputing = computed(() => pending.value)

/** 生效的筹码模式中文名（复用 settings 页的取值表，避免取值漂移） */
const effectiveModeLabel = computed(() => {
  const mode = settings.value?.mode
  if (!mode) return '—'
  return CHIP_MODE_OPTIONS.find(option => option.value === mode)?.label ?? mode
})

const doubleStandardError = computed(() =>
  data.value?.hit_rate_standard_error === null || data.value?.hit_rate_standard_error === undefined
    ? null
    : 2 * data.value.hit_rate_standard_error,
)

/** 波动分解柱状图：柱高 = 命中次数（提示里给出评估期数与命中率） */
function waveBars(items: Array<{ label: string, evaluated: number, hits: number, hit_rate: number | null }> | undefined) {
  return (items ?? []).map(item => ({
    label: item.label,
    value: item.hits,
    hint: `命中 ${item.hits} / 评估 ${item.evaluated} 期 · 命中率 ${formatRate(item.hit_rate)}`,
  }))
}

const realizedWaveBars = computed(() => waveBars(data.value?.wave_breakdown?.items))
const prevWaveBars = computed(() => waveBars(data.value?.wave_breakdown_by_prev?.items))

/* ---------------- 明细列表（默认只展开最近若干期，避免 198 行长列表） ---------------- */

const results = computed(() => data.value?.results ?? [])
const COLLAPSED_SIZE = 20
const expanded = ref(false)
/** 'all' = 全部；'outOfPool' = 只看实际特码落在候选池外（引擎不可能命中）的期数 */
const resultFilter = ref<'all' | 'outOfPool'>('all')

const filteredResults = computed(() => {
  const rows = [...results.value].reverse()
  return resultFilter.value === 'outOfPool'
    ? rows.filter(row => !row.actual_in_candidate_pool)
    : rows
})

const visibleResults = computed(() =>
  expanded.value ? filteredResults.value : filteredResults.value.slice(0, COLLAPSED_SIZE),
)

const outOfPoolCount = computed(() => results.value.filter(row => !row.actual_in_candidate_pool).length)

/* ---------------- 参数扫描（方案 A：样本内对照，默认不写回设置） ---------------- */

const sweepPayload = computed<BacktestSweepPayload>(() => {
  const body: BacktestSweepPayload = { limit: sampleLimit.value }
  if (modeOverride.value) body.mode = modeOverride.value
  if (pickOverride.value !== '') body.pick_count = Number(pickOverride.value)
  return body
})

/**
 * 扫描默认不自动跑（约 32 组 × 走步，偏重）：用户点「开始扫描」才拉。
 * 与上方单次回测共用 sampleLimit / mode / pick 覆盖项，保证对照口径一致。
 */
const sweepLoaded = ref(false)
const {
  data: sweepData,
  pending: sweepPending,
  error: sweepError,
  refresh: refreshSweep,
} = await useAsyncData<BacktestSweepStats>(
  'stats-backtest-sweep',
  () => api.backtestSweep(sweepPayload.value),
  { immediate: false },
)

async function runSweep() {
  sweepLoaded.value = true
  await refreshSweep()
}

const sweepRows = computed(() => sweepData.value?.rows ?? [])

const applyingKey = ref<string | null>(null)
const applyMessage = ref('')
const applyError = ref('')

function sweepRowKey(row: BacktestSweepRow): string {
  return `${row.trend_bias}|${row.trend_window}|${row.avoid_cold_enabled ? 1 : 0}`
}

function verdictTone(kind: string | undefined): 'bloom' | 'aqua' | 'neutral' {
  if (kind === 'noise' || kind === 'insufficient') return 'bloom'
  if (kind === 'beyond') return 'aqua'
  return 'neutral'
}

/**
 * 显式「应用到设置」：只写扫描轴上的三项（+ avoid_cold_days 保持该行值）。
 * 文案必须说清：样本内对照，不是已验证优势，不承诺提高命中率。
 */
async function applySweepRow(row: BacktestSweepRow) {
  applyMessage.value = ''
  applyError.value = ''
  const key = sweepRowKey(row)
  applyingKey.value = key
  try {
    await lotteryApi.updateSettings({
      trend_bias: row.trend_bias as 'neutral' | 'hot' | 'mid' | 'cold',
      trend_window: row.trend_window,
      avoid_cold_enabled: row.avoid_cold_enabled,
      avoid_cold_days: row.avoid_cold_days,
    })
    applyMessage.value = (
      `已写入设置：${row.trend_bias_label} · ${row.trend_window_label} · `
      + `避冷${row.avoid_cold_enabled ? '开' : '关'}。`
      + '这是本池样本内对照，不是已验证优势，也不承诺提高命中率。'
      + '财富密码页下次生成会用新设置。'
    )
    // 刷新单次回测与扫描「当前设置」标记
    await Promise.all([refresh(), refreshSweep()])
  } catch (err: any) {
    applyError.value = err?.data?.detail || err?.message || '写入设置失败'
  } finally {
    applyingKey.value = null
  }
}
</script>

<template>
  <StatsPageFrame
    v-model:sample-limit="sampleLimit"
    title="策略回测"
    subtitle="对本池的候选生成引擎做走步回测（walk-forward，严格无未来函数）：每期只用该期之前的数据生成候选号码，再与该期实际特码比对。下面是本池样本内的实测表现，不是承诺。"
    required-role="VIP"
    :envelope="data"
    :pending="pending"
    :error="error"
    @retry="refresh()"
  >
    <div class="flex flex-col gap-6">
      <MotionReveal :index="2">
        <!-- 结论：必须最先看到，且必须诚实 -->
        <GlassPanel
          variant="strong"
          padding="md"
          rounded="2xl"
          glow
          :tone="verdict.kind === 'noise' || verdict.kind === 'insufficient' ? 'bloom' : 'aqua'"
          class="space-y-3"
        >
          <div class="flex flex-wrap items-center gap-2">
            <StatChip
              :tone="verdict.kind === 'noise' || verdict.kind === 'insufficient' ? 'bloom' : 'aqua'"
              size="sm"
              dot
            >
              {{ verdict.label }}
            </StatChip>
          <span class="num text-[11px] text-slate-400">
            命中 <AnimatedNumber :value="data?.hits ?? null" :pad="0" /> /
            评估 <AnimatedNumber :value="data?.evaluated ?? null" :pad="0" /> 期
          </span>
          </div>
          <p data-backtest-verdict class="text-sm leading-relaxed text-slate-100">
            {{ verdict.text }}
          </p>
          <p v-if="verdict.withinNoise" class="text-xs leading-relaxed text-slate-400">
            换句话说：本池样本内，引擎的命中率与「在 1-49 里随机抓同样注数」没有可分辨的差别。
            这不代表引擎「选得差」，只代表这份样本还证明不了它更好。
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="3">
        <!-- 命中率 vs 随机参考值 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-5">
          <div class="space-y-1">
            <h2 class="text-lg font-medium text-white">命中率与随机参考值</h2>
            <p class="text-xs text-slate-500">
              随机参考值 = 有效注数 / 49，相当于在 1-49 中随机取同样注数；它只是对照，不是目标。
            </p>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <RingGauge
              :value="pctOf(data?.hit_rate)"
              :size="140"
              tone="aqua"
              :decimals="2"
              label="引擎命中率"
              :caption="`命中 ${data?.hits ?? '—'} / 评估 ${data?.evaluated ?? '—'} 期`"
              empty-text="数据不足"
            />
            <RingGauge
              :value="pctOf(data?.random_baseline_hit_rate)"
              :size="140"
              tone="slate"
              :decimals="2"
              label="随机参考值"
              :caption="`有效注数 ${data?.settings?.effective_pick_count ?? '—'} / 49`"
              empty-text="数据不足"
            />
          </div>

          <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
              <p class="text-[11px] text-slate-500">命中率 − 随机值</p>
              <p class="num text-base font-semibold text-white">
                {{ formatSignedPp(data?.hit_rate_minus_baseline) }}
              </p>
            </div>
            <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
              <p class="text-[11px] text-slate-500">抽样标准误</p>
              <p class="num text-base font-semibold text-white">
                {{ formatPp(data?.hit_rate_standard_error) }}
              </p>
            </div>
            <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
              <p class="text-[11px] text-slate-500">判定线（2×标准误）</p>
              <p class="num text-base font-semibold text-white">
                {{ formatPp(doubleStandardError) }}
              </p>
            </div>
            <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
              <p class="text-[11px] text-slate-500">平均可用号码数</p>
              <p class="num text-base font-semibold text-white">
                {{ data?.average_available_numbers === null || data?.average_available_numbers === undefined
                  ? '—' : data.average_available_numbers.toFixed(1) }}
              </p>
            </div>
          </div>

          <p v-if="data?.random_baseline_note" class="text-[11px] leading-relaxed text-slate-500">
            {{ data.random_baseline_note }}
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="4">
        <!-- 命中率的理论上限：低命中率不等于「选得差」 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-5">
          <div class="space-y-1">
            <h2 class="text-lg font-medium text-white">命中率的理论上限</h2>
            <p class="text-xs text-slate-500">
              这一项是理解命中率的关键：候选池始终排除「上一期特码本身」。
              <template v-if="data?.settings?.exclude_repeat_zodiac">
                当前已开启「避开重肖」，还会再排除该特码的整组同生肖号码；实际特码落在被排除号码里时，引擎无论多准都不可能命中。
              </template>
              <template v-else>
                当前未避开重肖，同肖号可以入选；只有实际特码恰好等于上一期特码时才算池外（几乎不会发生）。
              </template>
            </p>
          </div>

          <div class="flex flex-col items-center gap-4 sm:flex-row sm:items-center sm:justify-around">
            <RingGauge
              :value="pctOf(data?.actual_in_pool_rate)"
              :size="168"
              tone="amber"
              :decimals="2"
              label="实际特码落在候选池内的比例"
              caption="= 命中率的理论上限"
              empty-text="数据不足"
            />

            <div class="w-full space-y-2 sm:w-auto sm:min-w-[240px]">
              <div class="flex items-baseline justify-between gap-2">
                <span class="text-[11px] text-slate-500">理论上限</span>
                <span class="num text-sm text-slate-200">{{ formatRate(data?.actual_in_pool_rate) }}</span>
              </div>
              <div class="flex items-baseline justify-between gap-2">
                <span class="text-[11px] text-slate-500">引擎够不到的期数占比</span>
                <span class="num text-sm text-slate-200">
                  {{ data?.actual_in_pool_rate === null || data?.actual_in_pool_rate === undefined
                    ? '—' : formatRate(1 - data.actual_in_pool_rate) }}
                </span>
              </div>
              <div class="flex items-baseline justify-between gap-2">
                <span class="text-[11px] text-slate-500">池外期数（逐期）</span>
                <span class="num text-sm text-slate-200">
                  {{ outOfPoolCount }} / {{ results.length || '—' }} 期
                </span>
              </div>
              <p class="text-[11px] leading-relaxed text-slate-500">
                所以命中率的上限不是 100%：即使引擎在候选池内每次都猜中，也只能命中约
                {{ formatRate(data?.actual_in_pool_rate) }} 的期数。
                低命中率里有很大一部分来自这个规则，而不是「选得准不准」。
              </p>
            </div>
          </div>

          <p v-if="data?.actual_in_pool_note" class="text-[11px] leading-relaxed text-slate-500">
            {{ data.actual_in_pool_note }}
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="5">
        <!-- 本次运行参数（可复现） -->
        <GlassPanel variant="soft" padding="md" rounded="2xl" as="div" class="space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">本次运行参数</h3>
              <p class="text-[11px] leading-relaxed text-slate-500">
                这两个下拉框是本次回测的输入：改动任意一项会立刻用新参数重算下方结果。
                「跟随已保存设置」= 用「设置」页里保存的那一份；显式选中某一项 = 只覆盖这一项，
                其余仍取已保存设置。下方是后端回报的实际生效参数与每一项的来源，
                同一组参数任何时候都能复现同一次回测。
              </p>
            </div>
            <GlassButton
              variant="glass"
              size="sm"
              class="min-h-[44px] shrink-0"
              :loading="recomputing"
              :disabled="recomputing"
              @click="refresh()"
            >
              {{ recomputing ? '重新计算中…' : '重新计算' }}
            </GlassButton>
          </div>

          <!-- 覆盖项 -->
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="space-y-1.5">
              <span class="block text-[11px] text-slate-400">筹码模式（本次覆盖）</span>
              <select
                v-model="modeOverride"
                class="glass-input min-h-[44px] w-full px-4 py-2.5 text-base text-slate-100 outline-none"
              >
                <option value="" class="bg-ink-900">
                  跟随已保存设置
                </option>
                <option
                  v-for="option in CHIP_MODE_OPTIONS"
                  :key="option.value"
                  :value="option.value"
                  class="bg-ink-900"
                >
                  {{ option.label }}
                </option>
              </select>
              <span class="block text-[10px] text-slate-500">
                {{ modeIsOverride ? '本次运行使用你选中的模式' : '本次运行沿用「已保存设置」里的模式' }}
              </span>
            </label>

            <label class="space-y-1.5">
              <span class="block text-[11px] text-slate-400">注数（本次覆盖）</span>
              <select
                v-model="pickOverride"
                class="glass-input min-h-[44px] w-full px-4 py-2.5 text-base text-slate-100 outline-none"
              >
                <option value="" class="bg-ink-900">
                  跟随已保存设置
                </option>
                <option
                  v-for="count in PICK_COUNT_OPTIONS"
                  :key="count"
                  :value="String(count)"
                  class="bg-ink-900"
                >
                  {{ count }} 注
                </option>
              </select>
              <span class="block text-[10px]" :class="pickCountIgnored ? 'text-bloom-200' : 'text-slate-500'">
                {{ pickCountIgnored
                  ? '注数不参与计算（见下方说明）'
                  : (pickIsOverride ? '本次运行使用你选的注数' : '本次运行沿用「已保存设置」里的注数') }}
              </span>
            </label>
          </div>

          <!-- 生效参数 + 逐字段来源（来源用后端 parameter_sources 原样码，不自己猜） -->
          <div v-if="settings" class="space-y-2">
            <p class="text-[11px] text-slate-500">后端回报的实际生效参数（每格的「来源」指这一项取值的出处）</p>
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
                <p class="text-[11px] text-slate-500">模式</p>
                <p class="text-sm font-semibold text-white">{{ effectiveModeLabel }}</p>
                <p class="num text-[10px] text-slate-500">{{ settings.mode }}</p>
                <p class="text-[10px] text-slate-500">来源：{{ parameterSourceLabel('mode') }}</p>
              </div>
              <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
                <p class="text-[11px] text-slate-500">配置注数</p>
                <p class="num text-sm font-semibold text-white">{{ settings.pick_count }}</p>
                <p class="text-[10px] text-slate-500">来源：{{ parameterSourceLabel('pick_count') }}</p>
              </div>
              <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
                <p class="text-[11px] text-slate-500">有效注数</p>
                <p class="num text-sm font-semibold" :class="pickCountIgnored ? 'text-bloom-200' : 'text-white'">
                  {{ settings.effective_pick_count }}
                </p>
                <p class="text-[10px]" :class="pickCountIgnored ? 'text-bloom-200' : 'text-slate-500'">
                  {{ pickCountIgnored ? '由模式固定，忽略配置注数' : '与配置注数一致' }}
                </p>
              </div>
              <div class="rounded-xl border border-white/5 bg-white/5 px-3 py-2">
                <p class="text-[11px] text-slate-500">波动阈值</p>
                <p class="num text-sm font-semibold text-white">
                  小 ≤ {{ settings.small_max }} · 常规 ≤ {{ settings.normal_max }} · 大 ≥ {{ settings.big_min }}
                </p>
                <p class="text-[10px] text-slate-500">
                  来源：小 {{ parameterSourceLabel('small_max') }} · 常规 {{ parameterSourceLabel('normal_max') }}
                </p>
              </div>
            </div>
          </div>

          <!-- 注数被模式忽略：必须明说，否则用户会以为「改了参数但没重算」 -->
          <div
            v-if="pickCountIgnoredNote"
            data-backtest-ignored-hint
            class="space-y-1 rounded-xl border border-bloom-400/40 bg-bloom-400/10 px-3 py-2"
          >
            <p class="text-[11px] font-medium text-bloom-200">
              注数当前被筹码模式忽略
            </p>
            <p class="text-[11px] leading-relaxed text-slate-200">
              {{ pickCountIgnoredNote }}
            </p>
          </div>

          <p v-if="evaluationWindow" class="text-[11px] leading-relaxed text-slate-500">
            {{ evaluationWindow.description }}
          </p>
          <p v-if="data?.scope" class="num text-[11px] text-slate-500">
            口径：{{ data.scope }}（sample_size = {{ data.sample_size }}）
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="6">
        <!-- 参数扫描：样本内对照，默认不写回设置 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1">
              <h2 class="text-lg font-medium text-white">参数扫描（样本内对照）</h2>
              <p class="text-xs leading-relaxed text-slate-500">
                网格扫描走势偏好 × 近窗 × 避冷开关（约 32 组），每组走与财富密码同源的 walk-forward 回测。
                排序按命中率降序<strong class="font-medium text-slate-300">仅便于浏览，不是最优策略</strong>；
                默认不写回设置。判定口径与上方单次回测相同。
              </p>
            </div>
            <GlassButton
              variant="primary"
              size="sm"
              class="min-h-[44px] shrink-0"
              :loading="sweepPending"
              :disabled="sweepPending"
              @click="runSweep"
            >
              {{ sweepPending ? '扫描中…' : (sweepLoaded ? '重新扫描' : '开始扫描') }}
            </GlassButton>
          </div>

          <p class="rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-100">
            样本内排名靠前 ≠ 已验证优势。差值落在抽样噪声内时，无法证明该组优于随机；
            超过噪声也不构成对未来命中能力的任何承诺。禁止把结果说成「已优化命中率」。
          </p>

          <p v-if="sweepError" class="text-xs text-bloom-200">
            扫描失败：{{ (sweepError as any)?.data?.detail || (sweepError as any)?.message || '未知错误' }}
          </p>

          <template v-if="sweepData">
            <div class="flex flex-wrap gap-2">
              <StatChip tone="neutral" size="sm">共 {{ sweepData.combo_count }} 组</StatChip>
              <StatChip tone="bloom" size="sm">噪声内 {{ sweepData.noise_count }}</StatChip>
              <StatChip tone="aqua" size="sm">超噪声 {{ sweepData.beyond_count }}</StatChip>
              <StatChip
                v-if="sweepData.insufficient_count"
                tone="neutral"
                size="sm"
              >
                数据不足 {{ sweepData.insufficient_count }}
              </StatChip>
            </div>

            <ul class="space-y-1 text-[11px] leading-relaxed text-slate-500">
              <li v-for="(note, index) in sweepData.notes" :key="index">
                {{ note }}
              </li>
            </ul>

            <p
              v-if="applyMessage"
              class="rounded-xl border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-xs leading-relaxed text-emerald-100"
            >
              {{ applyMessage }}
            </p>
            <p v-if="applyError" class="text-xs text-bloom-200">{{ applyError }}</p>

            <div class="overflow-x-auto rounded-2xl border border-white/10">
              <table class="min-w-full text-left text-xs text-slate-200">
                <thead class="bg-white/5 text-[11px] text-slate-400">
                  <tr>
                    <th class="px-3 py-2 font-medium">走势偏好</th>
                    <th class="px-3 py-2 font-medium">近窗</th>
                    <th class="px-3 py-2 font-medium">避冷</th>
                    <th class="px-3 py-2 font-medium">命中率</th>
                    <th class="px-3 py-2 font-medium">− 随机</th>
                    <th class="px-3 py-2 font-medium">判定</th>
                    <th class="px-3 py-2 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in sweepRows"
                    :key="sweepRowKey(row)"
                    class="border-t border-white/5"
                    :class="row.matches_baseline ? 'bg-aqua-400/5' : ''"
                  >
                    <td class="px-3 py-2">
                      <span>{{ row.trend_bias_label }}</span>
                      <StatChip
                        v-if="row.matches_baseline"
                        class="ml-1"
                        tone="aqua"
                        size="xs"
                      >
                        当前设置
                      </StatChip>
                    </td>
                    <td class="num px-3 py-2">{{ row.trend_window_label }}</td>
                    <td class="px-3 py-2">{{ row.avoid_cold_enabled ? '开' : '关' }}</td>
                    <td class="num px-3 py-2">
                      {{ formatRate(row.hit_rate) }}
                      <span class="text-slate-500">
                        （{{ row.hits }}/{{ row.evaluated }}）
                      </span>
                    </td>
                    <td class="num px-3 py-2">{{ formatSignedPp(row.hit_rate_minus_baseline) }}</td>
                    <td class="px-3 py-2">
                      <StatChip :tone="verdictTone(row.verdict?.kind)" size="xs">
                        {{ row.verdict?.kind === 'noise' ? '噪声内'
                          : row.verdict?.kind === 'beyond' ? '超噪声'
                            : row.verdict?.kind === 'insufficient' ? '数据不足'
                              : '—' }}
                      </StatChip>
                    </td>
                    <td class="px-3 py-2">
                      <GlassButton
                        variant="glass"
                        size="sm"
                        class="min-h-[36px] px-3 text-xs"
                        :loading="applyingKey === sweepRowKey(row)"
                        :disabled="!!applyingKey || row.matches_baseline"
                        @click="applySweepRow(row)"
                      >
                        {{ row.matches_baseline ? '已是当前' : '应用到设置' }}
                      </GlassButton>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="text-[11px] leading-relaxed text-slate-500">
              「应用到设置」只写入走势偏好 / 近窗 / 避冷三项，并打上「已手动设置」标记；
              不会改筹码模式或注数。写入后财富密码下次生成才用新配置。
            </p>
          </template>

          <p v-else-if="!sweepPending" class="text-xs text-slate-500">
            尚未扫描。点「开始扫描」后才会请求后端（只读，不改设置）。
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="7">
        <!-- 波动分解 -->
        <div class="grid gap-4 sm:grid-cols-2">
          <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">命中次数 · 按当期实际波动</h3>
              <p class="text-[11px] leading-relaxed text-slate-500">
                属于**事后归因**（当期实际与上一期的差值分类），不参与预测。
              </p>
            </div>
            <BarChart
              :bars="realizedWaveBars"
              :height="210"
              value-suffix=" 次命中"
              empty-text="数据不足"
              empty-hint="没有可评估的期数"
            />
          </GlassPanel>

          <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">命中次数 · 按预测时已知的上一期波动</h3>
              <p class="text-[11px] leading-relaxed text-slate-500">
                用的是预测当时**已知**的上一期波动（没有未来函数），这才是有意义的切分。
              </p>
            </div>
            <BarChart
              :bars="prevWaveBars"
              :height="210"
              value-suffix=" 次命中"
              empty-text="数据不足"
              empty-hint="没有可评估的期数"
            />
          </GlassPanel>
        </div>
      </MotionReveal>
      <MotionReveal :index="8">
        <!-- 逐期明细 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
          <div class="space-y-2">
            <div class="flex flex-wrap items-end justify-between gap-2">
              <div class="space-y-1">
                <h3 class="text-sm font-medium text-slate-200">逐期明细（{{ results.length }} 期）</h3>
                <p class="text-[11px] text-slate-500">新 → 旧；每期只用该期之前的数据生成候选号码。</p>
              </div>
              <span class="num text-[11px] text-slate-500">显示 {{ visibleResults.length }} 行</span>
            </div>

            <div class="flex flex-wrap gap-2" role="group" aria-label="明细筛选">
              <button
                type="button"
                class="min-h-[44px] rounded-xl border px-4 text-sm font-medium transition-colors duration-200 select-none active:scale-[0.98]"
                :class="resultFilter === 'all'
                  ? 'border-aqua-400/60 bg-aqua-400/15 text-aqua-100'
                  : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                :aria-pressed="resultFilter === 'all'"
                @click="resultFilter = 'all'"
              >
                全部
              </button>
              <button
                type="button"
                class="min-h-[44px] rounded-xl border px-4 text-sm font-medium transition-colors duration-200 select-none active:scale-[0.98]"
                :class="resultFilter === 'outOfPool'
                  ? 'border-bloom-400/60 bg-bloom-400/15 text-bloom-100'
                  : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
                :aria-pressed="resultFilter === 'outOfPool'"
                @click="resultFilter = 'outOfPool'"
              >
                只看池外（{{ outOfPoolCount }} 期）
              </button>
            </div>
          </div>

          <p v-if="!visibleResults.length" class="text-xs text-slate-500">
            数据不足：没有符合当前筛选的可评估期数。
          </p>

          <ul v-else class="space-y-2">
            <li
              v-for="row in visibleResults"
              :key="row.period"
              class="space-y-1.5 rounded-xl border border-white/5 bg-white/5 px-3 py-2"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="num text-xs text-slate-400">第{{ row.period }}期</span>
                <span class="num text-[10px] text-slate-500">{{ row.draw_date }}</span>
                <StatChip
                  class="ml-auto"
                  :tone="row.hit ? 'emerald' : 'neutral'"
                  size="xs"
                  :outline="!row.hit"
                >
                  {{ row.hit ? '命中' : '未中' }}
                </StatChip>
              </div>

              <div class="flex flex-wrap items-center gap-1.5">
                <span class="text-[11px] text-slate-500">预测</span>
                <span
                  v-for="number in row.predicted"
                  :key="`${row.period}-${number}`"
                  class="num rounded-md border px-1.5 py-0.5 text-xs"
                  :class="number === row.actual
                    ? 'border-emerald-400/50 bg-emerald-400/10 text-emerald-200'
                    : 'border-white/10 bg-white/5 text-slate-200'"
                >
                  {{ padStatNumber(number) }}
                </span>
                <span class="text-[11px] text-slate-500">实际</span>
                <span
                  class="num rounded-md border px-1.5 py-0.5 text-xs font-semibold"
                  :class="row.hit
                    ? 'border-emerald-400/50 bg-emerald-400/10 text-emerald-200'
                    : 'border-white/15 text-slate-200'"
                >
                  {{ padStatNumber(row.actual) }}
                </span>
              </div>

              <p class="num text-[10px] leading-relaxed text-slate-500">
                实际波动 {{ row.realized_wave_label }}（差值 {{ row.realized_diff }}）·
                预测时上一期波动 {{ row.prev_wave_label ?? '—' }} · 实际特码
                <span :class="row.actual_in_candidate_pool ? 'text-slate-400' : 'text-bloom-200'">
                  {{ row.actual_in_candidate_pool ? '在候选池内' : '落在池外（引擎不可能命中）' }}
                </span>
              </p>
            </li>
          </ul>

          <button
            v-if="filteredResults.length > COLLAPSED_SIZE"
            type="button"
            class="glass-control min-h-[44px] w-full rounded-xl px-4 text-base font-medium text-slate-200 active:bg-white/15"
            @click="expanded = !expanded"
          >
            {{ expanded ? `收起（只显示最近 ${COLLAPSED_SIZE} 期）` : `展开全部 ${filteredResults.length} 期` }}
          </button>
        </GlassPanel>
      </MotionReveal>
    </div>
  </StatsPageFrame>
</template>
