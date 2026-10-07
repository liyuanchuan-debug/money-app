<script setup lang="ts">
import {
  AMOUNT_UNIT_MAX,
  AMOUNT_UNIT_MIN,
  AMOUNT_UNIT_STEP,
  AVOID_COLD_DAYS_MAX,
  AVOID_COLD_DAYS_MIN,
  AVOID_COLD_DEFAULT_DAYS,
  AVOID_COLD_LABEL,
  CHIP_MODE_OPTIONS,
  LATTICE_WINDOW_DEFAULT,
  LATTICE_WINDOW_MAX,
  LATTICE_WINDOW_MIN,
  MIN_BET_AMOUNT,
  REPEAT_NUMBER_WEIGHT_DEFAULT,
  REPEAT_ZODIAC_WEIGHT_DEFAULT,
  PICK_ROLE_LABELS,
  ROLE_ORDER,
  ROLE_WEIGHT_DEFENSE_DEFAULT,
  ROLE_WEIGHT_MAX,
  ROLE_WEIGHT_MIN,
  ROLE_WEIGHT_PRESETS,
  ROLE_WEIGHT_PRIMARY_DEFAULT,
  ROLE_WEIGHT_SECONDARY_DEFAULT,
  SOFT_WEIGHT_MAX,
  SOFT_WEIGHT_MIN,
  SOFT_WEIGHT_OPTIONS,
  STALE_PERIODS_DEFAULT,
  STALE_PERIODS_MAX,
  STALE_PERIODS_MIN,
  STALE_WEIGHT_DEFAULT,
  TOTAL_AMOUNT_MAX,
  TOTAL_AMOUNT_MIN,
  TREND_BIAS_OPTIONS,
  TREND_WINDOW_DEFAULT,
  TREND_WINDOW_OPTIONS,
  previewEvenAmounts,
  type ChipMode,
  type TrendBias,
} from '~/composables/useApi'

definePageMeta({ role: 'USER' })

/**
 * 设置 —— 波动阈值、筹码模式与投注金额的**唯一持久化入口**。
 *
 * 金额口径（与后端 services/lottery.py 同源）：
 *   - 「最大投注金额」是**唯一的预算真值**，所有模式都从它取预算；
 *   - 「金额最小单位」是注码粒度：所有模式下每一注金额必须是它的正整数倍；
 *   - 各注金额按单位个数分配，总和 = 最大投注（须为最小单位的整数倍）。
 *
 * 默认值口径：本页保存的 mode / trend_bias 是全局默认值；
 * 「波浪买入法」（/recommend）页里的临时切换只是当次预览，绝不写回这里。
 */
const api = useApi()

const loading = ref(true)
const saving = ref(false)
const errorMessage = ref('')
const infoMessage = ref('')

/** 筹码模式取值表来自 useApi（== 后端 services/lottery.py 的 MODES） */
const chipModes = CHIP_MODE_OPTIONS
const trendBiasOptions = TREND_BIAS_OPTIONS
const trendWindowOptions = TREND_WINDOW_OPTIONS
/** 软降权权重档位（1.0 = 不降权） */
const softWeightOptions = SOFT_WEIGHT_OPTIONS

function isChipMode(value: unknown): value is ChipMode {
  return chipModes.some(option => option.value === value)
}

function isTrendBias(value: unknown): value is TrendBias {
  return trendBiasOptions.some(option => option.value === value)
}

const form = reactive({
  // 与后端 services/lottery.py 的 DEFAULT_SETTINGS 对齐（10 / 30 / neutral / 不避重肖）
  small_max: 10,
  normal_max: 30,
  pick_count: 10,
  total_amount: 50,
  amount_unit: 5,
  odds: 47,
  mode: 'even' as ChipMode,
  exclude_repeat_zodiac: false,
  trend_bias: 'neutral' as TrendBias,
  trend_window: TREND_WINDOW_DEFAULT,
  // 避冷加权（自然日口径）：已被「按**期数**的软降权」取代，默认关闭
  avoid_cold_enabled: false,
  avoid_cold_days: AVOID_COLD_DEFAULT_DAYS,
  // 三类软降权（重号 / 同肖 / 冷号）：不排除、只降权
  include_repeat_number: true,
  repeat_number_weight: REPEAT_NUMBER_WEIGHT_DEFAULT,
  repeat_zodiac_weight: REPEAT_ZODIAC_WEIGHT_DEFAULT,
  stale_periods: STALE_PERIODS_DEFAULT,
  stale_weight: STALE_WEIGHT_DEFAULT,
  // 预测波动线 + 号码点阵（参与选号：带内优先）
  lattice_enabled: true,
  lattice_window: LATTICE_WINDOW_DEFAULT,
  // 角色金额配额（均注模式）：主推 : 次选 : 防守 = 3:2:1（防守最低）
  role_w_primary: ROLE_WEIGHT_PRIMARY_DEFAULT,
  role_w_secondary: ROLE_WEIGHT_SECONDARY_DEFAULT,
  role_w_defense: ROLE_WEIGHT_DEFENSE_DEFAULT,
})

const derivedBigMin = computed(() => form.normal_max + 1)

/** 有效注数：单挑恒 1 注，其余模式取默认注数 */
const effectivePicks = computed(() => (form.mode === 'single' ? 1 : Math.max(1, form.pick_count || 1)))

/** 每注下限：金额必须是单位倍数，故取「单位」与「每注最低 5 元」的较大者 */
const minPerPick = computed(() => Math.max(MIN_BET_AMOUNT, form.amount_unit || AMOUNT_UNIT_MIN))

/** 所有模式：最大投注至少覆盖「每注 × 每注下限」 */
const minTotal = computed(() => effectivePicks.value * minPerPick.value)

/** 角色配额精确命中哪一档（不在档位表内 = custom） */
const rolePreset = computed(() => {
  const match = ROLE_WEIGHT_PRESETS.find(
    preset =>
      preset.primary === form.role_w_primary
      && preset.secondary === form.role_w_secondary
      && preset.defense === form.role_w_defense,
  )
  return match?.value ?? 'custom'
})

/** 1:1:1 时，均注回到「按注数严格均分」（旧行为） */
const roleQuotaUniform = computed(() =>
  form.role_w_primary === form.role_w_secondary
  && form.role_w_secondary === form.role_w_defense,
)

function applyRolePreset(value: string) {
  const preset = ROLE_WEIGHT_PRESETS.find(item => item.value === value)
  if (!preset) return
  form.role_w_primary = preset.primary
  form.role_w_secondary = preset.secondary
  form.role_w_defense = preset.defense
}

/** 均注预览金额列表（与后端 distribute_units_by_role 同源；随表单变化） */
const evenPreviewAmounts = computed(() =>
  previewEvenAmounts(
    form.total_amount,
    effectivePicks.value,
    form.amount_unit || AMOUNT_UNIT_MIN,
    {
      primary: form.role_w_primary,
      secondary: form.role_w_secondary,
      defense: form.role_w_defense,
    },
  ),
)

/** 均注预览按角色分组的合计（用于展示「防守配额最低」） */
const evenPreviewByRole = computed(() => {
  const amounts = evenPreviewAmounts.value
  const totals: Record<string, number> = { primary: 0, secondary: 0, defense: 0 }
  const counts: Record<string, number> = { primary: 0, secondary: 0, defense: 0 }
  amounts.forEach((amount, index) => {
    const role = index === 0 ? 'primary' : index % 2 === 1 ? 'secondary' : 'defense'
    totals[role] += amount
    counts[role] += 1
  })
  return ROLE_ORDER
    .map(role => ({
      role,
      label: PICK_ROLE_LABELS[role],
      total: totals[role],
      count: counts[role],
      avg: counts[role] ? totals[role] / counts[role] : 0,
    }))
    .filter(row => row.count > 0)
})

/** 均注预览：每注大约多少单位 / 对应元（取列表里出现最多的那档作「大约」） */
const evenPreviewSummary = computed(() => {
  const amounts = evenPreviewAmounts.value
  const unit = Math.max(1, form.amount_unit || AMOUNT_UNIT_MIN)
  if (!amounts.length) return null
  const unitsList = amounts.map(a => a / unit)
  const baseUnits = Math.min(...unitsList)
  return {
    units: baseUnits,
    yuan: baseUnits * unit,
    listText: amounts.join(' / '),
  }
})

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    const settings = await api.getSettings()
    form.small_max = settings.small_max
    form.normal_max = settings.normal_max
    form.pick_count = settings.pick_count
    // 兼容旧后端：没有 total_amount 时回退为「旧单注金额 × 注数」，绝不落成 0
    form.total_amount = settings.total_amount ?? (settings.bet_unit * settings.pick_count)
    form.amount_unit = settings.amount_unit ?? 5
    form.odds = typeof settings.odds === 'number' ? settings.odds : 47
    // 缺字号 / 旧后端：默认不避开重肖、保留重号（与 DEFAULT_SETTINGS 一致）
    form.exclude_repeat_zodiac = settings.exclude_repeat_zodiac === true
    form.include_repeat_number = settings.include_repeat_number !== false
    form.repeat_number_weight = typeof settings.repeat_number_weight === 'number'
      ? settings.repeat_number_weight
      : REPEAT_NUMBER_WEIGHT_DEFAULT
    form.repeat_zodiac_weight = typeof settings.repeat_zodiac_weight === 'number'
      ? settings.repeat_zodiac_weight
      : REPEAT_ZODIAC_WEIGHT_DEFAULT
    form.stale_periods = typeof settings.stale_periods === 'number'
      ? settings.stale_periods
      : STALE_PERIODS_DEFAULT
    form.stale_weight = typeof settings.stale_weight === 'number'
      ? settings.stale_weight
      : STALE_WEIGHT_DEFAULT
    form.lattice_enabled = settings.lattice_enabled !== false
    form.lattice_window = typeof settings.lattice_window === 'number'
      ? settings.lattice_window
      : LATTICE_WINDOW_DEFAULT
    form.role_w_primary = typeof settings.role_w_primary === 'number'
      ? settings.role_w_primary
      : ROLE_WEIGHT_PRIMARY_DEFAULT
    form.role_w_secondary = typeof settings.role_w_secondary === 'number'
      ? settings.role_w_secondary
      : ROLE_WEIGHT_SECONDARY_DEFAULT
    form.role_w_defense = typeof settings.role_w_defense === 'number'
      ? settings.role_w_defense
      : ROLE_WEIGHT_DEFENSE_DEFAULT
    // 只在后端返回合法枚举时覆盖，避免把状态搞成取值之外的脏值
    if (isChipMode(settings.mode)) form.mode = settings.mode
    if (isTrendBias(settings.trend_bias)) form.trend_bias = settings.trend_bias
    if (typeof settings.trend_window === 'number') form.trend_window = settings.trend_window
    // 缺字段 / 旧后端 → 默认关闭避冷（与 DEFAULT_SETTINGS 一致），阈值回退 60
    form.avoid_cold_enabled = settings.avoid_cold_enabled === true
    form.avoid_cold_days = typeof settings.avoid_cold_days === 'number'
      ? settings.avoid_cold_days
      : AVOID_COLD_DEFAULT_DAYS
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '加载设置失败'
  } finally {
    loading.value = false
  }
}

await load()

function validate(): string | null {
  if (!isChipMode(form.mode)) return '筹码模式取值异常，请重新选择'
  const fields: Array<{ label: string; value: number; min: number; max: number }> = [
    { label: '小波动上限', value: form.small_max, min: 0, max: 48 },
    { label: '常规波动上限', value: form.normal_max, min: 0, max: 48 },
    { label: '默认注数', value: form.pick_count, min: 1, max: 10 },
    {
      label: '最大投注金额',
      value: form.total_amount,
      min: TOTAL_AMOUNT_MIN,
      max: TOTAL_AMOUNT_MAX,
    },
    { label: '金额最小单位', value: form.amount_unit, min: AMOUNT_UNIT_MIN, max: AMOUNT_UNIT_MAX },
    {
      label: '避冷阈值（天）',
      value: form.avoid_cold_days,
      min: AVOID_COLD_DAYS_MIN,
      max: AVOID_COLD_DAYS_MAX,
    },
    {
      label: '冷号口径（期数）',
      value: form.stale_periods,
      min: STALE_PERIODS_MIN,
      max: STALE_PERIODS_MAX,
    },
    {
      label: '预测波动线取样期数',
      value: form.lattice_window,
      min: LATTICE_WINDOW_MIN,
      max: LATTICE_WINDOW_MAX,
    },
  ]
  for (const field of fields) {
    if (!Number.isInteger(field.value) || field.value < field.min || field.value > field.max) {
      return `${field.label}需为 ${field.min}-${field.max} 之间的整数`
    }
  }
  const weights: Array<{ label: string; value: number }> = [
    { label: '重号降权系数', value: form.repeat_number_weight },
    { label: '同肖降权系数', value: form.repeat_zodiac_weight },
    { label: '冷号降权系数', value: form.stale_weight },
  ]
  for (const item of weights) {
    if (
      !Number.isFinite(item.value)
      || item.value < SOFT_WEIGHT_MIN
      || item.value > SOFT_WEIGHT_MAX
    ) {
      return `${item.label}需为 ${SOFT_WEIGHT_MIN}-${SOFT_WEIGHT_MAX} 之间的数字`
    }
  }
  // 金额最小单位按 5 元一档（注码粒度）
  if (form.amount_unit % AMOUNT_UNIT_STEP !== 0) {
    return `金额最小单位须为 ${AMOUNT_UNIT_STEP} 的整数倍（即 5 元一档），当前 ${form.amount_unit} 元`
  }
  const roleWeights: Array<{ label: string; value: number }> = [
    { label: '主推金额配额', value: form.role_w_primary },
    { label: '次选金额配额', value: form.role_w_secondary },
    { label: '防守金额配额', value: form.role_w_defense },
  ]
  for (const item of roleWeights) {
    if (
      !Number.isFinite(item.value)
      || item.value < ROLE_WEIGHT_MIN
      || item.value > ROLE_WEIGHT_MAX
    ) {
      return `${item.label}需为 ${ROLE_WEIGHT_MIN}-${ROLE_WEIGHT_MAX} 之间的数字`
    }
  }
  if (!Number.isFinite(form.odds) || form.odds < 1 || form.odds > 999) {
    return '赔率需为 1-999 之间的数字'
  }
  if (form.normal_max <= form.small_max) {
    return '常规波动上限必须大于小波动上限'
  }
  // 所有模式：最大投注须覆盖每注 × 每注下限（金额单位与 5 元取大者），且为最小单位整数倍
  if (form.total_amount < minTotal.value) {
    const suggested = minTotal.value
    return `最大投注金额至少需要 ${effectivePicks.value} 注 × ${minPerPick.value} 元 = ${suggested} 元（每注最低 ${MIN_BET_AMOUNT} 元），当前只有 ${form.total_amount} 元；请调高最大投注金额或减少注数`
  }
  if (form.total_amount % form.amount_unit !== 0) {
    const floor = Math.floor(form.total_amount / form.amount_unit) * form.amount_unit
    const ceil = floor + form.amount_unit
    return `最大投注金额须是金额最小单位 ${form.amount_unit} 元的整数倍，当前 ${form.total_amount} 元除不尽（可改为 ${floor} 元或 ${ceil} 元）`
  }
  return null
}

async function save() {
  errorMessage.value = ''
  infoMessage.value = ''

  const invalid = validate()
  if (invalid) {
    errorMessage.value = invalid
    return
  }

  saving.value = true
  try {
    // 只提交可写字段；大跳下限等派生值不参与提交
    const settings = await api.updateSettings({
      small_max: form.small_max,
      normal_max: form.normal_max,
      pick_count: form.pick_count,
      total_amount: form.total_amount,
      amount_unit: form.amount_unit,
      odds: form.odds,
      mode: form.mode,
      exclude_repeat_zodiac: form.exclude_repeat_zodiac,
      include_repeat_number: form.include_repeat_number,
      repeat_number_weight: form.repeat_number_weight,
      repeat_zodiac_weight: form.repeat_zodiac_weight,
      stale_periods: form.stale_periods,
      stale_weight: form.stale_weight,
      lattice_enabled: form.lattice_enabled,
      lattice_window: form.lattice_window,
      role_w_primary: form.role_w_primary,
      role_w_secondary: form.role_w_secondary,
      role_w_defense: form.role_w_defense,
      trend_bias: form.trend_bias,
      trend_window: form.trend_window,
      avoid_cold_enabled: form.avoid_cold_enabled,
      avoid_cold_days: form.avoid_cold_days,
    })
    form.small_max = settings.small_max
    form.normal_max = settings.normal_max
    form.pick_count = settings.pick_count
    form.total_amount = settings.total_amount
    form.amount_unit = settings.amount_unit
    form.odds = typeof settings.odds === 'number' ? settings.odds : 47
    form.exclude_repeat_zodiac = settings.exclude_repeat_zodiac === true
    form.include_repeat_number = settings.include_repeat_number !== false
    form.repeat_number_weight = typeof settings.repeat_number_weight === 'number'
      ? settings.repeat_number_weight
      : REPEAT_NUMBER_WEIGHT_DEFAULT
    form.repeat_zodiac_weight = typeof settings.repeat_zodiac_weight === 'number'
      ? settings.repeat_zodiac_weight
      : REPEAT_ZODIAC_WEIGHT_DEFAULT
    form.stale_periods = typeof settings.stale_periods === 'number'
      ? settings.stale_periods
      : STALE_PERIODS_DEFAULT
    form.stale_weight = typeof settings.stale_weight === 'number'
      ? settings.stale_weight
      : STALE_WEIGHT_DEFAULT
    form.lattice_enabled = settings.lattice_enabled !== false
    form.lattice_window = typeof settings.lattice_window === 'number'
      ? settings.lattice_window
      : LATTICE_WINDOW_DEFAULT
    form.role_w_primary = typeof settings.role_w_primary === 'number'
      ? settings.role_w_primary
      : ROLE_WEIGHT_PRIMARY_DEFAULT
    form.role_w_secondary = typeof settings.role_w_secondary === 'number'
      ? settings.role_w_secondary
      : ROLE_WEIGHT_SECONDARY_DEFAULT
    form.role_w_defense = typeof settings.role_w_defense === 'number'
      ? settings.role_w_defense
      : ROLE_WEIGHT_DEFENSE_DEFAULT
    if (isChipMode(settings.mode)) form.mode = settings.mode
    if (isTrendBias(settings.trend_bias)) form.trend_bias = settings.trend_bias
    if (typeof settings.trend_window === 'number') form.trend_window = settings.trend_window
    form.avoid_cold_enabled = settings.avoid_cold_enabled === true
    form.avoid_cold_days = typeof settings.avoid_cold_days === 'number'
      ? settings.avoid_cold_days
      : AVOID_COLD_DEFAULT_DAYS
    infoMessage.value = '设置已保存'
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/** 玻璃输入：字号 ≥16px（移动端不触发自动缩放），高度 ≥48px 好点 */
const inputClass
  = 'glass-input num h-12 w-full px-4 text-base font-semibold text-white sm:w-36'
const readonlyInputClass
  = 'glass-input num h-12 w-full cursor-not-allowed px-4 text-base font-semibold text-slate-500 sm:w-36'
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
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">设置</h1>
          <p class="max-w-xl text-sm text-slate-400">
            调整波动阈值、筹码模式与投注金额，保存后立即生效。
          </p>
          <StatChip tone="neutral" size="sm" dot>
            默认值只在这里保存 · 波浪买入法页的临时切换不改这里
          </StatChip>
        </div>
        <AppNav />
      </MotionReveal>

      <p v-if="loading" class="text-sm text-slate-400">加载中…</p>

      <MotionReveal v-else :index="1">
        <form @submit.prevent="save">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="space-y-5">
              <div class="space-y-2">
                <label for="small-max" class="block text-sm font-medium text-slate-200">
                  小波动上限
                </label>
                <input
                  id="small-max"
                  v-model.number="form.small_max"
                  type="number"
                  min="0"
                  max="48"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">差值 ≤ 该值视为小波动（默认 10）。</p>
              </div>

              <div class="space-y-2">
                <label for="normal-max" class="block text-sm font-medium text-slate-200">
                  常规波动上限
                </label>
                <input
                  id="normal-max"
                  v-model.number="form.normal_max"
                  type="number"
                  min="0"
                  max="48"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">
                  差值大于小波动上限且 ≤ 该值视为常规波动（默认 30）。
                </p>
              </div>

              <div class="space-y-2">
                <label for="big-min" class="block text-sm font-medium text-slate-200">
                  大跳下限
                </label>
                <input
                  id="big-min"
                  :value="`> ${form.normal_max}`"
                  type="text"
                  disabled
                  :class="readonlyInputClass"
                >
                <p class="text-xs text-slate-500">
                  自动推导：大跳下限 = 常规波动上限 + 1（当前为 {{ derivedBigMin }}），只读不可编辑，
                  调整「常规波动上限」即可改变它。
                </p>
              </div>

              <!-- 筹码模式：默认值（写入 settings）；「波浪买入法」页面的临时切换不改这里 -->
              <div class="space-y-2">
                <p id="chip-mode-label" class="text-sm font-medium text-slate-200">筹码模式</p>
                <div
                  role="radiogroup"
                  aria-labelledby="chip-mode-label"
                  class="flex flex-wrap gap-2"
                >
                  <GlassButton
                    v-for="option in chipModes"
                    :key="option.value"
                    role="radio"
                    :aria-checked="form.mode === option.value"
                    :variant="form.mode === option.value ? 'primary' : 'glass'"
                    class="min-h-[44px] px-4 text-base"
                    @click="form.mode = option.value"
                  >
                    {{ option.label }}
                  </GlassButton>
                </div>
                <p class="text-xs leading-relaxed text-slate-500">
                  均注：各注先保底 1 个单位，余量按下方「角色配额」分给主推 / 次选 / 防守（1:1:1 即严格均分）。
                  侧重：主推约占 4/6 单位，其余均分；单挑：只出 1 注（整份最大投注
                  押在一个号上）；随机分配：按最小单位随机拆给各注。四种模式的每一注金额都是最小单位的正整数倍。
                  保存后作为默认值：生成财富密码时按此模式分配筹码；在「波浪买入法」页面里临时切换只影响当次，
                  不改这里的默认值。
                </p>
              </div>

              <div class="space-y-2">
                <label for="pick-count" class="block text-sm font-medium text-slate-200">
                  默认注数
                </label>
                <input
                  id="pick-count"
                  v-model.number="form.pick_count"
                  type="number"
                  min="1"
                  max="10"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">
                  均注 / 侧重 / 随机分配模式下的默认注数（1-10，默认 6）；单挑模式恒为 1 注。
                </p>
              </div>

              <!-- 预算真值：最大投注金额 -->
              <div class="space-y-2">
                <label for="total-amount" class="block text-sm font-medium text-slate-200">
                  最大投注金额（元）
                </label>
                <input
                  id="total-amount"
                  v-model.number="form.total_amount"
                  type="number"
                  :min="TOTAL_AMOUNT_MIN"
                  :max="TOTAL_AMOUNT_MAX"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">
                  这是唯一的预算上限：所有筹码模式都按它分配（默认 50）。
                  <span class="text-slate-300">{{ TOTAL_AMOUNT_MIN }}–{{ TOTAL_AMOUNT_MAX }}</span> 元，
                  须为最小单位的整数倍，且至少
                  <span class="num text-slate-300">{{ minTotal }}</span> 元
                  （{{ effectivePicks }} 注 × {{ minPerPick }} 元，每注最低 {{ MIN_BET_AMOUNT }} 元）。
                  最大投注只是本次要拆分的金额，不代表任何收益预期。
                </p>
              </div>

              <!-- 最小单位：注码粒度（所有模式） -->
              <div class="space-y-2">
                <label for="amount-unit" class="block text-sm font-medium text-slate-200">
                  金额最小单位（元）
                </label>
                <input
                  id="amount-unit"
                  v-model.number="form.amount_unit"
                  type="number"
                  :min="AMOUNT_UNIT_MIN"
                  :max="AMOUNT_UNIT_MAX"
                  :step="AMOUNT_UNIT_STEP"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs leading-relaxed text-slate-500">
                  注码粒度：各注金额按最小单位（{{ form.amount_unit }} 元）的倍数分配，总和 = 最大投注；
                  每注至少 1 个单位（默认 {{ AMOUNT_UNIT_MIN }} 元，即 {{ AMOUNT_UNIT_STEP }} 元一档）。
                </p>
              </div>

              <!-- 角色金额配额（均注模式）：主推 : 次选 : 防守 -->
              <div class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
                <div class="space-y-1">
                  <p class="text-sm font-medium text-slate-200">
                    均注角色配额（主推 : 次选 : 防守）
                  </p>
                  <p class="text-xs leading-relaxed text-slate-500">
                    均注模式下，先给每注保底 {{ MIN_BET_AMOUNT }} 元（1 个注码单位），剩余预算按角色权重分给
                    主推 / 次选 / 防守三组，组内再均分 —— 默认 <span class="num text-slate-300">3 : 2 : 1</span>。
                    所以防守组拿到的配额最低。三项填成同一个值（如 1:1:1）即回到「按注数严格均分」的旧行为。
                    侧重 / 单挑 / 随机分配模式不走这套配额。
                  </p>
                </div>

                <div class="flex flex-wrap gap-2">
                  <GlassButton
                    v-for="preset in ROLE_WEIGHT_PRESETS"
                    :key="preset.value"
                    role="radio"
                    :aria-checked="rolePreset === preset.value"
                    :variant="rolePreset === preset.value ? 'primary' : 'glass'"
                    class="min-h-[44px] px-4 text-base"
                    @click="applyRolePreset(preset.value)"
                  >
                    {{ preset.label }}
                  </GlassButton>
                </div>

                <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div class="space-y-2">
                    <label for="role-w-primary" class="block text-sm font-medium text-slate-200">
                      {{ PICK_ROLE_LABELS.primary }}配额
                    </label>
                    <input
                      id="role-w-primary"
                      v-model.number="form.role_w_primary"
                      type="number"
                      :min="ROLE_WEIGHT_MIN"
                      :max="ROLE_WEIGHT_MAX"
                      step="0.5"
                      inputmode="decimal"
                      :class="inputClass"
                    >
                  </div>
                  <div class="space-y-2">
                    <label for="role-w-secondary" class="block text-sm font-medium text-slate-200">
                      {{ PICK_ROLE_LABELS.secondary }}配额
                    </label>
                    <input
                      id="role-w-secondary"
                      v-model.number="form.role_w_secondary"
                      type="number"
                      :min="ROLE_WEIGHT_MIN"
                      :max="ROLE_WEIGHT_MAX"
                      step="0.5"
                      inputmode="decimal"
                      :class="inputClass"
                    >
                  </div>
                  <div class="space-y-2">
                    <label for="role-w-defense" class="block text-sm font-medium text-slate-200">
                      {{ PICK_ROLE_LABELS.defense }}配额
                    </label>
                    <input
                      id="role-w-defense"
                      v-model.number="form.role_w_defense"
                      type="number"
                      :min="ROLE_WEIGHT_MIN"
                      :max="ROLE_WEIGHT_MAX"
                      step="0.5"
                      inputmode="decimal"
                      :class="inputClass"
                    >
                  </div>
                </div>

                <div class="space-y-1">
                  <p v-if="roleQuotaUniform" class="text-xs leading-relaxed text-slate-400">
                    三项相同 → 均注按注数严格均分（旧行为）。
                  </p>
                  <p v-else class="text-xs leading-relaxed text-slate-400">
                    当前分配：{{ evenPreviewSummary ? evenPreviewSummary.listText : '—' }} 元
                    <template v-if="evenPreviewByRole.length">
                      ｜按角色：
                      <span
                        v-for="row in evenPreviewByRole"
                        :key="row.role"
                        class="num text-slate-300"
                      >
                        {{ row.label }} {{ row.count }} 注共 {{ row.total }} 元（均 {{ row.avg }}）
                        <span class="text-slate-500">· </span>
                      </span>
                    </template>
                  </p>
                  <p
                    v-if="!roleQuotaUniform && form.total_amount <= minTotal"
                    class="text-xs leading-relaxed text-amber-300/90"
                  >
                    当前最大投注刚好等于「每注最低 {{ MIN_BET_AMOUNT }} 元 × {{ effectivePicks }} 注」，
                    没有余量可分配，角色配额不会产生金额差异；调高最大投注或减少注数才会拉开差距。
                  </p>
                </div>
              </div>

              <!-- 特码兑付倍数（用户设定，不是收益承诺） -->
              <div class="space-y-2">
                <label for="odds" class="block text-sm font-medium text-slate-200">
                  赔率
                </label>
                <input
                  id="odds"
                  v-model.number="form.odds"
                  type="number"
                  min="1"
                  max="999"
                  step="0.1"
                  inputmode="decimal"
                  :class="inputClass"
                >
                <p class="text-xs leading-relaxed text-slate-500">
                  特码玩法兑付倍数：中一注拿该注金额 × 赔率（默认 47）。这是你设定的兑付规则，
                  不是收益承诺；模拟收益仪表按此倍数试算已采用期（非真实下单兑付）。
                </p>
              </div>

              <!-- 分配说明 + 均注预览（不再展示误导性的「派生单注」） -->
              <div class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3">
                <p class="text-sm font-medium text-slate-200">
                  压注怎么拆
                </p>
                <p class="text-xs leading-relaxed text-slate-500">
                  各注金额按最小单位（{{ form.amount_unit }} 元）的倍数分配，总和 = 最大投注。
                  不必再填「单注金额」——改最大投注、最小单位、注数或筹码模式即可。
                </p>
                <p v-if="evenPreviewSummary" class="text-xs leading-relaxed text-slate-400">
                  预览：均注最低每注
                  <span class="num text-slate-200">{{ evenPreviewSummary.units }}</span>
                  单位（{{ evenPreviewSummary.yuan }} 元）；示例金额为
                  <span class="num text-slate-200">{{ evenPreviewSummary.listText }}</span>
                  元（角色配额见上方）。侧重 / 单挑 / 随机会按各自规则重分，但仍是最小单位的倍数。
                </p>
                <p v-else class="text-xs text-amber-300/90">
                  当前最大投注不足以按最小单位覆盖 {{ effectivePicks }} 注，请先调高最大投注或减少注数。
                </p>
              </div>
              <!-- 避开重肖：默认关；关掉后同肖号可以入选 -->
              <div class="space-y-2">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="space-y-1">
                    <p id="exclude-repeat-label" class="text-sm font-medium text-slate-200">
                      避开重肖
                    </p>
                    <p class="text-xs leading-relaxed text-slate-500">
                      开启后，财富密码与回测的候选池会排除「最新一期特码的全部同肖号码」。
                      关闭（默认）时，同肖号可以入选，但会按下方「同肖降权」压低排序与金额。
                      被选中时会标「同肖」徽章。
                    </p>
                  </div>
                  <GlassButton
                    role="switch"
                    :aria-checked="form.exclude_repeat_zodiac"
                    aria-labelledby="exclude-repeat-label"
                    :variant="form.exclude_repeat_zodiac ? 'primary' : 'glass'"
                    class="min-h-[44px] shrink-0 px-4 text-base"
                    @click="form.exclude_repeat_zodiac = !form.exclude_repeat_zodiac"
                  >
                    {{ form.exclude_repeat_zodiac ? '已开启' : '已关闭' }}
                  </GlassButton>
                </div>
              </div>

              <!-- 三类软降权：重号 / 同肖 / 冷号（不排除、只降权） -->
              <div class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
                <div class="space-y-1">
                  <p class="text-sm font-medium text-slate-200">
                    降权（不排除，只排后 + 压金额）
                  </p>
                  <p class="text-xs leading-relaxed text-slate-500">
                    三类号码，<span class="text-slate-300">不会</span>被剔除出候选池，只是排序靠后并按系数打折金额；
                    若仍被选中，会在推荐卡片上标出对应徽章。省下的金额不补给其它注。
                    这是本池样本内的偏好，不是概率，也不承诺提高命中率。
                  </p>
                </div>

                <!-- 上期出过的号（重号）是否保留 -->
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="space-y-1">
                    <p id="include-repeat-number-label" class="text-sm font-medium text-slate-200">
                      上期出过的号不避开
                    </p>
                    <p class="text-xs leading-relaxed text-slate-500">
                      开启（默认）：上期特码本身留在候选池内，按「重号降权系数」排后并打折。
                      关闭 = 旧行为，直接把上期特码本身排除。
                    </p>
                  </div>
                  <GlassButton
                    role="switch"
                    :aria-checked="form.include_repeat_number"
                    aria-labelledby="include-repeat-number-label"
                    :variant="form.include_repeat_number ? 'primary' : 'glass'"
                    class="min-h-[44px] shrink-0 px-4 text-base"
                    @click="form.include_repeat_number = !form.include_repeat_number"
                  >
                    {{ form.include_repeat_number ? '已开启' : '已关闭' }}
                  </GlassButton>
                </div>

                <!-- 重号降权系数 -->
                <div class="space-y-2">
                  <p id="repeat-number-weight-label" class="text-sm font-medium text-slate-200">
                    重号降权系数
                  </p>
                  <div
                    role="radiogroup"
                    aria-labelledby="repeat-number-weight-label"
                    class="flex flex-wrap gap-2"
                  >
                    <GlassButton
                      v-for="option in softWeightOptions"
                      :key="`rn-${option.value}`"
                      role="radio"
                      :aria-checked="form.repeat_number_weight === option.value"
                      :variant="form.repeat_number_weight === option.value ? 'primary' : 'glass'"
                      class="min-h-[44px] px-4 text-base"
                      @click="form.repeat_number_weight = option.value"
                    >
                      {{ option.label }}
                    </GlassButton>
                  </div>
                  <p class="text-xs text-slate-500">
                    当前 <span class="num text-slate-300">{{ form.repeat_number_weight }}</span>；
                    上期特码本身（差距 0）的排序权重与金额都乘以它（默认 0.5）。
                  </p>
                </div>

                <!-- 同肖降权系数 -->
                <div class="space-y-2">
                  <p id="repeat-zodiac-weight-label" class="text-sm font-medium text-slate-200">
                    同肖降权系数
                  </p>
                  <div
                    role="radiogroup"
                    aria-labelledby="repeat-zodiac-weight-label"
                    class="flex flex-wrap gap-2"
                  >
                    <GlassButton
                      v-for="option in softWeightOptions"
                      :key="`rz-${option.value}`"
                      role="radio"
                      :aria-checked="form.repeat_zodiac_weight === option.value"
                      :variant="form.repeat_zodiac_weight === option.value ? 'primary' : 'glass'"
                      class="min-h-[44px] px-4 text-base"
                      @click="form.repeat_zodiac_weight = option.value"
                    >
                      {{ option.label }}
                    </GlassButton>
                  </div>
                  <p class="text-xs text-slate-500">
                    当前 <span class="num text-slate-300">{{ form.repeat_zodiac_weight }}</span>；
                    与上期特码同肖、但不等于上期特码的号（默认 0.8）。
                  </p>
                </div>

                <!-- 冷号：按期数，一律降权 -->
                <div class="space-y-2">
                  <label for="stale-periods" class="block text-sm font-medium text-slate-200">
                    冷号口径（最近 N 期没出现过）
                  </label>
                  <input
                    id="stale-periods"
                    v-model.number="form.stale_periods"
                    type="number"
                    :min="STALE_PERIODS_MIN"
                    :max="STALE_PERIODS_MAX"
                    inputmode="numeric"
                    :class="inputClass"
                  >
                  <p class="text-xs leading-relaxed text-slate-500">
                    按<span class="text-slate-300">期数</span>（不是自然日）：本池样本内最近
                    <span class="num text-slate-300">{{ form.stale_periods }}</span>
                    期都没出现过的号一律降权（默认 60 期）。样本不足该期数时无法证明「一直没出现」，
                    此时不降权，避免把短样本里的号误判成冷号。
                  </p>
                </div>

                <div class="space-y-2">
                  <p id="stale-weight-label" class="text-sm font-medium text-slate-200">
                    冷号降权系数
                  </p>
                  <div
                    role="radiogroup"
                    aria-labelledby="stale-weight-label"
                    class="flex flex-wrap gap-2"
                  >
                    <GlassButton
                      v-for="option in softWeightOptions"
                      :key="`sw-${option.value}`"
                      role="radio"
                      :aria-checked="form.stale_weight === option.value"
                      :variant="form.stale_weight === option.value ? 'primary' : 'glass'"
                      class="min-h-[44px] px-4 text-base"
                      @click="form.stale_weight = option.value"
                    >
                      {{ option.label }}
                    </GlassButton>
                  </div>
                  <p class="text-xs text-slate-500">
                    当前 <span class="num text-slate-300">{{ form.stale_weight }}</span>
                    （默认 0.3）。金额打折下限为每注
                    <span class="num text-slate-300">{{ MIN_BET_AMOUNT }}</span> 元，号码仍会列出。
                  </p>
                </div>
              </div>

              <!-- 预测波动线 + 号码点阵 -->
              <div class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="space-y-1">
                    <p id="lattice-label" class="text-sm font-medium text-slate-200">
                      预测波动线 · 号码点阵
                    </p>
                    <p class="text-xs leading-relaxed text-slate-500">
                      用最近若干期相邻差值估一条「预测波动线」（中心 = 中位数、带宽 = P25~P75），
                      把 1~49 号按「与上期特码的差值是否落在这条线上」铺成点阵；
                      开启时选号先在预测波动桶内取满，出号优先落在带内，不足才向带外扩散。
                      这是样本内经验分布，不是真实概率。
                    </p>
                  </div>
                  <GlassButton
                    role="switch"
                    :aria-checked="form.lattice_enabled"
                    aria-labelledby="lattice-label"
                    :variant="form.lattice_enabled ? 'primary' : 'glass'"
                    class="min-h-[44px] shrink-0 px-4 text-base"
                    @click="form.lattice_enabled = !form.lattice_enabled"
                  >
                    {{ form.lattice_enabled ? '已开启' : '已关闭' }}
                  </GlassButton>
                </div>
                <div class="space-y-2">
                  <label for="lattice-window" class="block text-sm font-medium text-slate-200">
                    取样期数
                  </label>
                  <input
                    id="lattice-window"
                    v-model.number="form.lattice_window"
                    type="number"
                    :min="LATTICE_WINDOW_MIN"
                    :max="LATTICE_WINDOW_MAX"
                    inputmode="numeric"
                    :disabled="!form.lattice_enabled"
                    :class="form.lattice_enabled ? inputClass : readonlyInputClass"
                  >
                  <p class="text-xs text-slate-500">
                    0 = 本池全部样本；默认
                    <span class="num text-slate-300">{{ LATTICE_WINDOW_DEFAULT }}</span> 期。
                    样本不足两对差值时不生成预测波动线，点阵不参与选号。
                  </p>
                </div>
              </div>

              <!-- 近期走势加权：默认不加权；财富密码页可临时预览 -->
              <div class="space-y-2">
                <p id="trend-bias-setting-label" class="text-sm font-medium text-slate-200">
                  近期走势加权
                </p>
                <div
                  role="radiogroup"
                  aria-labelledby="trend-bias-setting-label"
                  class="flex flex-wrap gap-2"
                >
                  <GlassButton
                    v-for="option in trendBiasOptions"
                    :key="option.value"
                    role="radio"
                    :aria-checked="form.trend_bias === option.value"
                    :variant="form.trend_bias === option.value ? 'primary' : 'glass'"
                    class="min-h-[44px] px-4 text-base"
                    @click="form.trend_bias = option.value"
                  >
                    {{ option.label }}
                  </GlassButton>
                </div>
                <div class="flex flex-wrap gap-2">
                  <GlassButton
                    v-for="option in trendWindowOptions"
                    :key="option.value"
                    :variant="form.trend_window === option.value ? 'primary' : 'glass'"
                    class="min-h-[40px] px-3 text-sm"
                    @click="form.trend_window = option.value"
                  >
                    {{ option.label }}
                  </GlassButton>
                </div>
                <!-- 不加权时窗口档位对选号无影响：醒目提示，避免「选了档位就以为开启了加权」 -->
                <p
                  v-if="form.trend_bias === 'neutral'"
                  class="rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs leading-relaxed text-amber-200"
                >
                  当前是「不加权」：上面的近窗档位（近 20 / 30 / 60 / 100 / 全部）
                  <strong class="font-semibold">不影响选号</strong>，只用于走势分布参考。
                  要先选「热号偏好 / 中频优先 / 冷号偏好」并保存，近窗档位才会参与选号。
                </p>
                <p class="text-xs leading-relaxed text-slate-400">
                  走势加权管「偏热 / 偏冷 / 中频」；避冷加权管「冷号排后 + 金额封顶」；两者独立，可同时生效。
                </p>
                <p class="text-xs leading-relaxed text-slate-500">
                  没手动设置过时一律是「不加权」：等同旧的全历史遗漏优先，旧版本残留的「热号偏好」不会自动生效。
                  选好热 / 中 / 冷号偏好后要点「保存设置」才生效；生效后各波动桶内按近窗出现频次切主推 / 次选 / 防守三段参与选号。
                  这是样本内经验频率偏好，不承诺提高命中率。波浪买入法页可临时切换，不改这里的默认值。
                </p>

                <!-- 避冷加权：本组内的**独立开关**（不是第 5 个走势加权选项，互不排斥） -->
                <div class="mt-1 space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] px-3 py-3">
                  <div class="flex flex-wrap items-center justify-between gap-3">
                    <div class="space-y-1">
                      <p id="avoid-cold-label" class="text-sm font-medium text-slate-200">
                        {{ AVOID_COLD_LABEL }}
                      </p>
                      <p class="text-xs leading-relaxed text-slate-500">
                        冷号排后 + 金额封顶：距上次出现超过「避冷阈值」的号码（含本池样本内从未出现的号码）
                        排到候选队列末尾；间隔越久权重越低、配置金额越低，最多只给「保本金额」
                        （1 个金额最小单位 = {{ form.amount_unit }} 元）；不足 1 个最小单位的注金额记 0
                        （号码仍列出），省下的预算不补给其它注。
                      </p>
                    </div>
                    <GlassButton
                      role="switch"
                      :aria-checked="form.avoid_cold_enabled"
                      aria-labelledby="avoid-cold-label"
                      :variant="form.avoid_cold_enabled ? 'primary' : 'glass'"
                      class="min-h-[44px] shrink-0 px-4 text-base"
                      @click="form.avoid_cold_enabled = !form.avoid_cold_enabled"
                    >
                      {{ form.avoid_cold_enabled ? '已开启' : '已关闭' }}
                    </GlassButton>
                  </div>
                  <div class="flex flex-wrap items-start gap-3">
                    <div class="space-y-1.5">
                      <label for="avoid-cold-days" class="block text-xs text-slate-400">
                        避冷阈值（天）
                      </label>
                      <input
                        id="avoid-cold-days"
                        v-model.number="form.avoid_cold_days"
                        type="number"
                        :min="AVOID_COLD_DAYS_MIN"
                        :max="AVOID_COLD_DAYS_MAX"
                        inputmode="numeric"
                        :disabled="!form.avoid_cold_enabled"
                        :class="form.avoid_cold_enabled ? inputClass : readonlyInputClass"
                      >
                    </div>
                    <p class="max-w-md text-xs leading-relaxed text-slate-500">
                      距上次出现 ≤ {{ form.avoid_cold_days }} 天不惩罚（正好等于阈值也不惩罚），
                      超过则权重 = 阈值 ÷ 天数（{{ form.avoid_cold_days }} 天=1.0、
                      {{ form.avoid_cold_days * 2 }} 天≈0.5、{{ form.avoid_cold_days * 4 }} 天≈0.25）；
                      本池样本内从未出现的号按最冷处理（金额记 0）。
                      这是样本内偏好，不是概率，也不承诺提高命中率或收益。
                    </p>
                  </div>
                </div>
              </div>

              <hr class="glass-hairline">

              <div class="space-y-3">
                <GlassButton
                  type="submit"
                  variant="primary"
                  size="lg"
                  class="min-h-[48px] w-full sm:w-auto"
                  :loading="saving"
                  :disabled="saving"
                >
                  {{ saving ? '保存中…' : '保存设置' }}
                </GlassButton>

                <p v-if="errorMessage" class="text-sm text-bloom-300" role="alert">
                  {{ errorMessage }}
                </p>
                <p v-else-if="infoMessage" class="text-sm text-emerald-300" role="status">
                  {{ infoMessage }}
                </p>
              </div>
            </div>
          </GlassPanel>
        </form>
      </MotionReveal>
    </div>
  </main>
</template>
