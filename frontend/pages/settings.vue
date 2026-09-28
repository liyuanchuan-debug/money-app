<script setup lang="ts">
import {
  AVOID_COLD_DAYS_MAX,
  AVOID_COLD_DAYS_MIN,
  AVOID_COLD_DEFAULT_DAYS,
  AVOID_COLD_LABEL,
  CHIP_MODE_OPTIONS,
  TREND_BIAS_OPTIONS,
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

function isChipMode(value: unknown): value is ChipMode {
  return chipModes.some(option => option.value === value)
}

function isTrendBias(value: unknown): value is TrendBias {
  return trendBiasOptions.some(option => option.value === value)
}

const form = reactive({
  small_max: 10,
  normal_max: 30,
  pick_count: 6,
  total_amount: 50,
  amount_unit: 5,
  odds: 47,
  mode: 'even' as ChipMode,
  exclude_repeat_zodiac: false,
  trend_bias: 'neutral' as TrendBias,
  trend_window: 30,
  // 避冷加权：默认开启（与后端 DEFAULT_SETTINGS 一致），阈值默认 60 天
  avoid_cold_enabled: true,
  avoid_cold_days: AVOID_COLD_DEFAULT_DAYS,
})

const derivedBigMin = computed(() => form.normal_max + 1)

/** 有效注数：单挑恒 1 注，其余模式取默认注数 */
const effectivePicks = computed(() => (form.mode === 'single' ? 1 : Math.max(1, form.pick_count || 1)))

/** 所有模式：最大投注至少覆盖「每注 1 个最小单位」 */
const minTotal = computed(() => effectivePicks.value * (form.amount_unit || 1))

/** 均注预览金额列表（与后端 even 分配一致；随表单变化） */
const evenPreviewAmounts = computed(() =>
  previewEvenAmounts(form.total_amount, effectivePicks.value, form.amount_unit || 1),
)

/** 均注预览：每注大约多少单位 / 对应元（取列表里出现最多的那档作「大约」） */
const evenPreviewSummary = computed(() => {
  const amounts = evenPreviewAmounts.value
  const unit = Math.max(1, form.amount_unit || 1)
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
    // 缺字段 / 旧后端 → 默认不避开（与 DEFAULT_SETTINGS 一致）
    form.exclude_repeat_zodiac = settings.exclude_repeat_zodiac === true
    // 只在后端返回合法枚举时覆盖，避免把状态搞成取值之外的脏值
    if (isChipMode(settings.mode)) form.mode = settings.mode
    if (isTrendBias(settings.trend_bias)) form.trend_bias = settings.trend_bias
    if (typeof settings.trend_window === 'number') form.trend_window = settings.trend_window
    // 缺字段 / 旧后端 → 默认开启避冷（与 DEFAULT_SETTINGS 一致），阈值回退 60
    form.avoid_cold_enabled = settings.avoid_cold_enabled !== false
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
    { label: '最大投注金额', value: form.total_amount, min: 1, max: 100000 },
    { label: '金额最小单位', value: form.amount_unit, min: 1, max: 10000 },
    {
      label: '避冷阈值（天）',
      value: form.avoid_cold_days,
      min: AVOID_COLD_DAYS_MIN,
      max: AVOID_COLD_DAYS_MAX,
    },
  ]
  for (const field of fields) {
    if (!Number.isInteger(field.value) || field.value < field.min || field.value > field.max) {
      return `${field.label}需为 ${field.min}-${field.max} 之间的整数`
    }
  }
  if (!Number.isFinite(form.odds) || form.odds < 1 || form.odds > 999) {
    return '赔率需为 1-999 之间的数字'
  }
  if (form.normal_max <= form.small_max) {
    return '常规波动上限必须大于小波动上限'
  }
  // 所有模式：最大投注须覆盖每注 ≥1 个最小单位，且为最小单位的整数倍
  if (form.total_amount < minTotal.value) {
    const suggested = minTotal.value
    return `最大投注金额至少需要 ${effectivePicks.value} 注 × ${form.amount_unit} 元 = ${suggested} 元，当前只有 ${form.total_amount} 元；请调高最大投注金额或减少注数`
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
    if (isChipMode(settings.mode)) form.mode = settings.mode
    if (isTrendBias(settings.trend_bias)) form.trend_bias = settings.trend_bias
    if (typeof settings.trend_window === 'number') form.trend_window = settings.trend_window
    form.avoid_cold_enabled = settings.avoid_cold_enabled !== false
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
                  均注：各注按最小单位尽量均分；侧重：主推约占 4/6 单位，其余均分；单挑：只出 1 注（整份最大投注
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
                  min="1"
                  max="100000"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">
                  这是唯一的预算上限：所有筹码模式都按它分配（默认 50）。须为最小单位的整数倍，且至少
                  <span class="num text-slate-300">{{ minTotal }}</span> 元
                  （{{ effectivePicks }} 注 × {{ form.amount_unit }} 元）。最大投注只是本次要拆分的金额，
                  不代表任何收益预期。
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
                  min="1"
                  max="10000"
                  inputmode="numeric"
                  :class="inputClass"
                >
                <p class="text-xs text-slate-500">
                  注码粒度：各注金额按最小单位（{{ form.amount_unit }} 元）的倍数分配，总和 = 最大投注；
                  每注至少 1 个单位（默认 5）。
                </p>
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
                  预览：均注大约每注
                  <span class="num text-slate-200">{{ evenPreviewSummary.units }}</span>
                  单位（{{ evenPreviewSummary.yuan }} 元）；示例金额为
                  <span class="num text-slate-200">{{ evenPreviewSummary.listText }}</span>
                  元。侧重 / 单挑 / 随机会按各自规则重分，但仍是最小单位的倍数。
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
                      关闭（默认）时，同肖号可以入选；只始终排除最新一期特码本身。
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
