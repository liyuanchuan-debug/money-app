<script setup lang="ts">
import type { DrawItem, NextPeriod } from '~/composables/useApi'
import type { NormalizedDraw } from '~/composables/useDraws'
import { normalizeDraw, padNumber, periodText, todayLocal, zodiacText } from '~/composables/useDraws'

definePageMeta({ role: 'ADMIN' })

/**
 * 开奖号录入（单号）—— 独立页面，不再挂在首页。
 *
 * 两种模式：
 *   - 新增：POST /api/draws/quick（期号/日期可留空自动补）
 *   - 纠正：POST /api/draws/{id}/correct（必须指定已存在期号；覆盖特码并重算盈亏）
 *
 * 仅 ADMIN：路由门禁 + 后端 require_admin。
 */

const api = useApi()

/** 录入模式：create=新增下一期；correct=纠正已保存期 */
const entryMode = ref<'create' | 'correct'>('create')

/**
 * 最新一期：只用于「本池最新一期」提示与「已保存后刷新状态」。
 * 记录为空时后端返回 200 + null；只有接口本身不可用才退到列表接口。
 */
const { data: latestRaw, refresh: refreshLatest } = await useAsyncData<DrawItem | null>(
  'draws-latest',
  async () => {
    try {
      return await api.latestDraw()
    } catch {
      const fallback = await api.listDraws(1, 0)
      return fallback?.[0] ?? null
    }
  },
)

const latest = computed(() => normalizeDraw(latestRaw.value))

/**
 * 下一期预填值：GET /api/draws/next-period。
 *
 * 期号 = max(period) + 1、日期 = 今天（Asia/Shanghai），与 POST /api/draws/quick 的缺省
 * 口径**同源**，所以预填值就是后端 upsert 真正会用的值，不必在浏览器里按本地时区自己算
 * （避免跨时区 / 深夜录入时差一天）。接口不可用时只退到「最新期号 + 1 / 浏览器本地今天」，
 * 只影响预填，不影响提交（两个字段都可以手改，也都允许留空交给后端补）。
 */
const { data: nextPeriod, refresh: refreshNextPeriod } = await useAsyncData<NextPeriod | null>(
  'draws-next-period',
  async () => {
    try {
      return await api.nextPeriod()
    } catch {
      return null
    }
  },
)

/** 建议期号：优先用服务端口径；服务端不可用时才退到「最新期号 + 1」 */
const suggestedPeriod = computed(() => {
  const fromServer = nextPeriod.value?.suggested_period
  if (typeof fromServer === 'number' && Number.isFinite(fromServer) && fromServer >= 1) {
    return fromServer
  }
  const value = latest.value?.period
  return typeof value === 'number' && Number.isFinite(value) ? value + 1 : null
})

/** 建议日期：优先用服务端的 UTC+8 口径；退路才是浏览器本地日期 */
const suggestedDate = computed(() => {
  const fromServer = nextPeriod.value?.suggested_draw_date
  if (typeof fromServer === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(fromServer)) {
    return fromServer
  }
  return todayLocal()
})

/* ---------------- 表单 ---------------- */
const period = ref('')
/** 用户手动改过期号后，就不再被服务端的「下一期」建议覆盖 */
const periodTouched = ref(false)
/** 日期同理：改过之后不再被服务端建议覆盖（两个字段都必须可覆盖） */
const drawDate = ref('')
const dateTouched = ref(false)
const specialNumber = ref('')

watch(
  suggestedPeriod,
  (value) => {
    if (periodTouched.value) return
    period.value = value === null ? '' : String(value)
  },
  { immediate: true },
)

watch(
  suggestedDate,
  (value) => {
    if (dateTouched.value) return
    drawDate.value = value
  },
  { immediate: true },
)

const submitting = ref(false)
const errorMessage = ref('')
const infoMessage = ref('')
const savedDraw = ref<NormalizedDraw | null>(null)
const hit = useHitPulse()

const numbers = Array.from({ length: 49 }, (_, index) => index + 1)

function pickNumber(value: number) {
  specialNumber.value = String(value)
  errorMessage.value = ''
}

function onPeriodInput() {
  periodTouched.value = true
}

function onDateInput() {
  dateTouched.value = true
}

/** 校验：特码 1-49 是硬约束；纠正模式必须给期号 */
function validate(): string | null {
  const numberText = specialNumber.value.trim()
  if (!numberText) return '请输入或点选 1-49 的特码'
  const number = Number(numberText)
  if (!Number.isInteger(number) || number < 1 || number > 49) {
    return '特码必须是 1-49 之间的整数'
  }

  const periodTextValue = period.value.trim()
  if (entryMode.value === 'correct') {
    if (!periodTextValue) return '纠正模式必须填写要纠正的期号'
    const parsed = Number(periodTextValue)
    if (!Number.isInteger(parsed) || parsed < 1) return '期号必须是正整数'
  } else if (periodTextValue) {
    const parsed = Number(periodTextValue)
    if (!Number.isInteger(parsed) || parsed < 1) return '期号必须是正整数（留空则由系统自动顺延）'
  }

  if (drawDate.value && !/^\d{4}-\d{2}-\d{2}$/.test(drawDate.value)) {
    return '日期格式应为 YYYY-MM-DD（留空则记今天）'
  }
  return null
}

/** 把 $fetch 的错误翻译成能给用户看的一句话 */
function describeError(err: unknown): string {
  const error = err as {
    status?: number
    statusCode?: number
    data?: { detail?: unknown }
    message?: string
  }
  const status = Number(error?.statusCode ?? error?.status ?? 0)
  if (status === 404 || status === 405) {
    return '录入接口暂时不可用（POST /api/draws/quick），请稍后重试'
  }

  const detail = error?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  // FastAPI 422：detail 是 [{ loc, msg, type }] 数组，拼成可读文本
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        const entry = item as { msg?: unknown; message?: unknown } | null
        const text = entry?.msg ?? entry?.message
        return typeof text === 'string' ? text : ''
      })
      .filter((text) => text.length > 0)
    if (parts.length) return parts.join('；')
  }
  return error?.message || '保存失败，请稍后重试'
}

async function submit() {
  if (submitting.value) return // 防重复提交（连点两次也只发一次）

  errorMessage.value = ''
  infoMessage.value = ''
  savedDraw.value = null

  const invalid = validate()
  if (invalid) {
    errorMessage.value = invalid
    return
  }

  const number = Number(specialNumber.value.trim())
  const periodValue = period.value.trim()
  const dateValue = drawDate.value.trim()

  submitting.value = true
  try {
    if (entryMode.value === 'correct') {
      const periodNum = Number(periodValue)
      const pool = await api.listDraws(500, 0)
      const target = (pool ?? []).find(item => item.period === periodNum)
      if (!target) {
        errorMessage.value = `找不到第 ${periodNum} 期开奖记录，无法纠正`
        return
      }
      if (target.special_number === number) {
        errorMessage.value = `第 ${periodNum} 期特码已是 ${padNumber(number)}，无需纠正`
        return
      }
      const ok = confirm(
        `纠正开奖：第 ${periodNum} 期特码将从 ${padNumber(target.special_number)} 改为 ${padNumber(number)}。\n`
        + '会覆盖原号码，并重算该期已采用推荐的命中与盈亏。确定继续？',
      )
      if (!ok) return

      const corrected = await api.correctDraw(target.id, {
        special_number: number,
        ...(dateValue ? { draw_date: dateValue } : {}),
      })
      const normalized = normalizeDraw(corrected.draw) ?? {
        id: target.id,
        draw_date: corrected.new_draw_date,
        period: corrected.period,
        special_number: corrected.new_special_number,
        zodiac: '',
        zodiac_label: '',
      }
      savedDraw.value = normalized
      infoMessage.value
        = `已纠正第 ${corrected.period} 期：特码 ${padNumber(corrected.old_special_number)} → ${padNumber(corrected.new_special_number)}`
        + `；重算采用快照 ${corrected.resettled_rounds} 条`
      hit.fire()
    } else {
      const response = await api.quickAddDraw({
        special_number: number,
        ...(periodValue ? { period: Number(periodValue) } : {}),
        ...(dateValue ? { draw_date: dateValue } : {}),
      })

      // 返回体理论上就是「装饰后的开奖对象」；解析失败也不要误报成功 / 失败
      const normalized = normalizeDraw(response) ?? {
        id: null,
        draw_date: dateValue,
        period: periodValue ? Number(periodValue) : null,
        special_number: number,
        zodiac: '',
        zodiac_label: '',
      }
      savedDraw.value = normalized
      infoMessage.value = `已保存：${periodText(normalized)} · 特码 ${padNumber(normalized.special_number)}`
      hit.fire()
    }

    // 刷新服务端的「下一期」建议与本池最新一期（与首页共用同一批 key）
    try {
      await Promise.all([
        refreshLatest(),
        refreshNextPeriod(),
        refreshNuxtData('draws-sample'),
        refreshNuxtData('draws'),
        refreshNuxtData('stats-pnl'),
        refreshNuxtData('recommend'),
      ])
    } catch {
      /* 刷新失败不影响「已保存」的事实，静默降级 */
    }

    // 期号 / 日期交回服务端建议值，方便连续录入（纠正模式保留期号便于再改）
    if (entryMode.value === 'create') {
      periodTouched.value = false
      dateTouched.value = false
      period.value = suggestedPeriod.value === null ? '' : String(suggestedPeriod.value)
      drawDate.value = suggestedDate.value
    }
    specialNumber.value = ''
  } catch (err) {
    errorMessage.value = describeError(err)
  } finally {
    submitting.value = false
  }
}

function switchMode(mode: 'create' | 'correct') {
  entryMode.value = mode
  errorMessage.value = ''
  infoMessage.value = ''
  if (mode === 'correct' && latest.value?.period != null) {
    periodTouched.value = true
    period.value = String(latest.value.period)
    specialNumber.value = String(latest.value.special_number)
  }
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-2xl flex-col gap-6">
      <!-- 头部 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            开奖号录入
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            单号录入或纠正：新增时期号 / 日期可留空自动顺延；纠正模式会覆盖已保存特码并重算该期盈亏。
          </p>
          <!-- 如实告知：权限系统尚未落地，此处没有做任何假校验 -->
          <StatChip tone="bloom" size="sm" dot>ADMIN 专属功能 · 鉴权待接入</StatChip>
          <div class="flex flex-wrap gap-2" role="radiogroup" aria-label="录入模式">
            <GlassButton
              role="radio"
              :aria-checked="entryMode === 'create'"
              :variant="entryMode === 'create' ? 'primary' : 'glass'"
              class="min-h-[44px]"
              @click="switchMode('create')"
            >
              新增开奖
            </GlassButton>
            <GlassButton
              role="radio"
              :aria-checked="entryMode === 'correct'"
              :variant="entryMode === 'correct' ? 'primary' : 'glass'"
              class="min-h-[44px]"
              @click="switchMode('correct')"
            >
              纠正开奖
            </GlassButton>
          </div>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 当前基线（口径：本池已导入的开奖记录，不代表更广范围） -->
      <MotionReveal :index="1" class="flex flex-wrap items-center gap-2.5">
        <StatChip v-if="latest" tone="neutral" size="sm" dot>
          本池最新一期：{{ periodText(latest) }}
          <template v-if="latest.draw_date"> · {{ latest.draw_date }}</template>
        </StatChip>
        <StatChip v-else tone="neutral" size="sm" dot>
          本池还没有开奖记录（数据不足，期号可留空由后端从 1 起算）
        </StatChip>

        <StatChip v-if="suggestedPeriod !== null" tone="aqua" size="sm">
          建议下一期：第 {{ suggestedPeriod }} 期（服务端按 UTC+8 给出，可自行修改）
        </StatChip>
      </MotionReveal>

      <!-- 录入表单 -->
      <MotionReveal :index="2">
        <form @submit.prevent="submit">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="flex flex-col gap-5">
              <div class="grid gap-4 sm:grid-cols-2">
                <div class="space-y-2">
                  <label for="entry-period" class="block text-sm font-medium text-slate-200">
                    期号
                  </label>
                  <input
                    id="entry-period"
                    v-model="period"
                    type="text"
                    inputmode="numeric"
                    autocomplete="off"
                    :placeholder="entryMode === 'correct' ? '必填：要纠正的期号' : '留空自动顺延'"
                    class="glass-input num h-12 w-full px-4 text-base text-white"
                    @input="onPeriodInput"
                  />
                  <p class="text-[11px] text-slate-500">
                    <template v-if="entryMode === 'correct'">
                      填写已保存的期号；提交前会确认新旧特码，并重算该期盈亏。
                    </template>
                    <template v-else>
                      默认 = 服务端建议的下一期（最新期号 + 1）；可自行修改，留空则由后端自动补齐。
                    </template>
                  </p>
                </div>

                <div class="space-y-2">
                  <label for="entry-date" class="block text-sm font-medium text-slate-200">
                    日期
                  </label>
                  <input
                    id="entry-date"
                    v-model="drawDate"
                    type="date"
                    class="glass-input num h-12 w-full px-4 text-base text-white"
                    @input="onDateInput"
                  />
                  <p class="text-[11px] text-slate-500">
                    默认为服务端给出的今天（UTC+8）；可自行修改，留空则由后端记今天。
                  </p>
                </div>
              </div>

              <div class="space-y-2">
                <div class="flex items-baseline justify-between gap-3">
                  <label for="entry-number" class="block text-sm font-medium text-slate-200">
                    特码
                  </label>
                  <span class="num text-xs text-slate-500">1 - 49</span>
                </div>
                <div class="relative inline-flex w-full rounded-xl sm:w-32">
                  <input
                    id="entry-number"
                    v-model="specialNumber"
                    type="text"
                    inputmode="numeric"
                    maxlength="2"
                    autocomplete="off"
                    placeholder="01"
                    class="glass-input num h-14 w-full px-4 text-center text-2xl font-semibold text-white"
                    @keyup.enter="submit"
                  />
                  <PulseRing :trigger="hit.key" tone="aqua" :rings="2" />
                </div>
                <p class="text-[11px] text-slate-500">也可以直接点下方号码格选择。</p>
              </div>

              <!-- 触屏友好的 01-49 号码格（7 × 7） -->
              <div class="space-y-2">
                <p class="text-xs font-medium text-slate-400">快速点选</p>
                <div
                  class="grid grid-cols-7 gap-1"
                  role="group"
                  aria-label="特码快速选择（1-49）"
                >
                  <button
                    v-for="value in numbers"
                    :key="value"
                    type="button"
                    class="num flex aspect-square items-center justify-center rounded-lg border text-xs font-semibold transition-colors duration-200 select-none active:scale-95 sm:text-sm"
                    :class="Number(specialNumber) === value
                      ? 'border-transparent bg-cta-aqua text-ink-950 shadow-glow-aqua'
                      : 'border-white/10 bg-white/5 text-slate-200 active:bg-white/20'"
                    :aria-pressed="Number(specialNumber) === value"
                    @click="pickNumber(value)"
                  >
                    {{ padNumber(value) }}
                  </button>
                </div>
              </div>

              <div class="space-y-3">
                <GlassButton
                  type="submit"
                  variant="primary"
                  size="lg"
                  block
                  :loading="submitting"
                  :disabled="submitting"
                >
                  {{ submitting
                    ? (entryMode === 'correct' ? '纠正中…' : '保存中…')
                    : (entryMode === 'correct' ? '确认纠正开奖' : '保存开奖号') }}
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

      <!-- 保存结果 -->
      <MotionReveal v-if="savedDraw" :index="3">
        <GlassPanel padding="lg" rounded="3xl" class="flex flex-wrap items-center gap-5">
          <SpecialBall
            :number="savedDraw.special_number"
            size="lg"
            tone="aqua"
            label="本次保存"
            :glow="true"
          />
          <div class="space-y-1.5">
            <StatChip tone="emerald" size="sm" dot>已保存</StatChip>
            <p class="num text-sm text-slate-300">
              {{ periodText(savedDraw) }}
              <template v-if="savedDraw.draw_date"> · {{ savedDraw.draw_date }}</template>
            </p>
            <p class="text-sm text-slate-400">生肖 {{ zodiacText(savedDraw) || '—' }}</p>
          </div>
        </GlassPanel>
      </MotionReveal>
    </div>
  </main>
</template>
