<script setup lang="ts">
/**
 * StatsPageFrame —— /stats/* 四个统计页共用的页面外壳（移动优先，360 - 430px 主目标）。
 *
 * 承担四件事，让每个统计页只关心自己的图形：
 *   1. 页头（标题 / 副标题 / AppNav / 口径 chip / 数据状态 chip）；
 *   2. 共享的「样本期数」控件（只统计本池最近 N 期；触控目标 ≥ 44px，字号 16px 防 iOS 缩放）；
 *   3. 诚实降级：data_status = INSUFFICIENT → 醒目的「数据不足」横幅 + 原因，
 *      并列出 notes 里后端显式标注「数据不足」的薄样本提示；
 *   4. 错误态：403 / 401 渲染成可读的「需要 VIP 权限 / 需要登录」，
 *      其余错误显示后端 detail 并给重试按钮。
 *
 * 【口径铁律】本组件不产生任何业务数字，只搬运后端给的 scope / data_status_label / notes。
 * 后端没有结论时，页面绝不能自己补一个。
 *
 * 【鉴权】这里**不做任何角色判断、不做假鉴权、不 fail-open**：
 * 403 分支只是把服务端的拒绝渲染成人话。真正的拦截必须在后端（AUTH_ENFORCED）与路由中间件。
 */
import type { StatsEnvelope } from '~/composables/useStats'
import {
  STATS_SAMPLE_LIMIT_OPTIONS,
  formatCount,
  insufficientNotes,
  isInsufficient,
} from '~/composables/useStats'

const props = withDefaults(
  defineProps<{
    /** 页面标题 */
    title: string
    /** 一句话说明本页统计的是什么（建议带口径提示） */
    subtitle?: string
    /**
     * 本页的权限级别，**仅用于把 401/403 渲染成可读文案**，不参与任何放行判断。
     * PUBLIC = 公开只读统计（访客可读）
     * USER = 需登录的个人页
     * VIP = recommend / pnl / backtest 等
     */
    requiredRole?: 'VIP' | 'USER' | 'PUBLIC'
    /** 接口返回体（含 scope / sample_size / data_status / data_status_label / notes） */
    envelope?: StatsEnvelope | null
    /** 请求的样本期数（v-model:sample-limit） */
    sampleLimit: number
    /** 首次加载中 */
    pending?: boolean
    /** useAsyncData 的错误对象 */
    error?: unknown
    /** INSUFFICIENT 时的原因（后端 insufficient_reason） */
    insufficientReason?: string | null
  }>(),
  {
    subtitle: '',
    requiredRole: 'PUBLIC',
    envelope: null,
    pending: false,
    error: null,
    insufficientReason: null,
  },
)

const emit = defineEmits<{
  /** 样本期数变化（父页面据此重新取数） */
  'update:sampleLimit': [value: number]
  /** 重试 */
  retry: []
}>()

const sampleOptions = STATS_SAMPLE_LIMIT_OPTIONS

/* ---------------- 口径 / 状态 ---------------- */

/** 口径文案：优先用后端返回的 scope，原样展示 */
const scopeText = computed(() =>
  props.envelope?.scope ? `口径：${props.envelope.scope}` : '口径：本池已导入的样本内',
)

const statusLabel = computed(() => props.envelope?.data_status_label ?? '加载中')

const statusTone = computed(() => (insufficient.value ? 'bloom' : 'emerald'))

const insufficient = computed(() => isInsufficient(props.envelope))

/** 后端 notes 里显式写「数据不足」的薄样本提示（样本可用时也要照实展示） */
const thinNotes = computed(() => insufficientNotes(props.envelope))

/** 实际参与统计的期数 */
const actualSample = computed(() =>
  props.envelope ? formatCount(props.envelope.sample_size) : '—',
)

/** 实际样本期数（原始数字，交给 AnimatedNumber 滚动落位；null → 占位符） */
const actualSampleCount = computed(() => props.envelope?.sample_size ?? null)

/** INSUFFICIENT 原因：后端给 insufficient_reason 就用它，否则退回 notes */
const reasonText = computed(() => {
  if (props.insufficientReason) return props.insufficientReason
  return thinNotes.value[0] ?? '数据不足：后端没有给出足够样本，因此不提供结论。'
})

/* ---------------- 错误态 ---------------- */

const errorStatus = computed(() => {
  const raw = props.error as { statusCode?: unknown, status?: unknown } | null | undefined
  const value = Number(raw?.statusCode ?? raw?.status ?? 0)
  return Number.isFinite(value) ? value : 0
})

const errorDetail = computed(() => {
  const raw = props.error as { data?: { detail?: unknown }, message?: unknown } | null | undefined
  const detail = raw?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (typeof raw?.message === 'string' && raw.message.trim()) return raw.message
  return '请求失败'
})

/** 401 / 403：鉴权上线后服务端会这样拒绝，页面必须能读 */
const permissionDenied = computed(() => errorStatus.value === 401 || errorStatus.value === 403)

/**
 * 403 文案。注意：这只是一个**提示**，不是角色判断 ——
 * 页面不会因为拿不到这个状态就放行任何操作。
 */
const permissionText = computed(() => {
  if (props.requiredRole === 'VIP') {
    return '需要 VIP 权限：波浪买入法、模拟收益仪表与策略回测仅对 VIP 开放。'
  }
  if (props.requiredRole === 'USER') {
    return '需要登录：本功能需要已登录账号。'
  }
  return errorDetail.value || '暂时无法加载，请稍后重试。'
})

/** 有旧数据时的刷新失败：不挡住旧数据，只在顶部提示 */
const staleError = computed(() => Boolean(props.error) && Boolean(props.envelope))
/** 完全没有数据时的失败：整页错误态 */
const blockingError = computed(() => Boolean(props.error) && !props.envelope)

/** 首次加载：没有任何数据 */
const firstLoading = computed(() => props.pending && !props.envelope)
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-3xl flex-col gap-6">
      <!-- 页头 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            {{ title }}
          </h1>
          <p v-if="subtitle" class="max-w-xl text-sm text-slate-400">{{ subtitle }}</p>
          <div class="flex flex-wrap items-center gap-2">
            <StatChip tone="neutral" size="sm" dot>{{ scopeText }}</StatChip>
            <StatChip :tone="statusTone" size="sm">{{ statusLabel }}</StatChip>
          </div>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 样本期数（四页共用的口径控制） -->
      <MotionReveal :index="1">
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-2.5">
          <div class="flex items-baseline justify-between gap-2">
            <p class="text-sm font-medium text-slate-200">样本期数</p>
            <span class="num text-[11px] text-slate-500">
              实际样本 <AnimatedNumber :value="actualSampleCount" :pad="0" /> 期
            </span>
          </div>
          <div class="flex flex-wrap gap-2" role="group" aria-label="样本期数">
            <button
              v-for="option in sampleOptions"
              :key="option"
              type="button"
              class="num min-h-[44px] min-w-[64px] rounded-xl border px-4 text-base font-medium transition-colors duration-200 select-none active:scale-[0.98]"
              :class="option === sampleLimit
                ? 'border-aqua-400/60 bg-aqua-400/15 text-aqua-100'
                : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
              :aria-pressed="option === sampleLimit"
              @click="emit('update:sampleLimit', option)"
            >
              {{ option }}
            </button>
          </div>
          <p class="text-[11px] leading-relaxed text-slate-500">
            只统计本池最近 {{ sampleLimit }} 期；若本池总期数更少，则以本池全部期数为准。
            后端单次上限 1000 期。所有数字都只描述这一样本，样本之外的情况未知。
          </p>
        </GlassPanel>
      </MotionReveal>

      <!-- 首次加载 -->
      <GlassPanel v-if="firstLoading" variant="soft" padding="lg">
        <p class="text-center text-sm text-slate-400">加载中…</p>
      </GlassPanel>

      <!-- 阻断式错误（没有任何数据） -->
      <GlassPanel
        v-else-if="blockingError"
        variant="soft"
        padding="lg"
        :glow="permissionDenied"
        :tone="permissionDenied ? 'bloom' : 'neutral'"
        class="space-y-3"
      >
        <p
          class="text-sm font-medium"
          :class="permissionDenied ? 'text-bloom-200' : 'text-rose-300'"
        >
          {{ permissionDenied ? permissionText : '加载失败' }}
        </p>
        <p class="num text-xs break-words text-slate-500">
          HTTP {{ errorStatus || '—' }} · {{ errorDetail }}
        </p>
        <GlassButton variant="glass" size="md" @click="emit('retry')">重试</GlassButton>
      </GlassPanel>

      <template v-else>
        <!-- 有旧数据时的刷新失败：不隐藏旧数据，只提示 -->
        <GlassPanel
          v-if="staleError"
          variant="soft"
          padding="sm"
          rounded="2xl"
          as="div"
          class="space-y-2"
        >
          <p class="text-sm text-amber-200">
            刷新失败，下方仍是上一次成功的结果（可能已过期）。
          </p>
          <p class="num text-[11px] break-words text-slate-500">
            HTTP {{ errorStatus || '—' }} · {{ errorDetail }}
          </p>
          <GlassButton variant="ghost" size="sm" @click="emit('retry')">重试</GlassButton>
        </GlassPanel>

        <!-- 数据不足：醒目横幅 + 后端给的原因（绝不编结论） -->
        <GlassPanel
          v-if="insufficient"
          variant="strong"
          padding="md"
          rounded="2xl"
          glow
          tone="bloom"
          class="space-y-2"
        >
          <div class="flex flex-wrap items-center gap-2">
            <StatChip tone="bloom" size="sm" dot>数据不足</StatChip>
            <span class="num text-[11px] text-slate-400">
              样本 {{ actualSample }} 期 · 后端状态 {{ envelope?.data_status }}
            </span>
          </div>
          <p class="text-sm leading-relaxed text-slate-200">{{ reasonText }}</p>
          <p class="text-xs text-slate-500">
            按后端口径，样本不足时只列原始计数、不给任何候选或结论。
          </p>
        </GlassPanel>

        <!-- 样本可用但后端标注了薄样本：照样诚实提示 -->
        <GlassPanel
          v-else-if="thinNotes.length"
          variant="soft"
          padding="sm"
          rounded="2xl"
          as="div"
          class="space-y-1.5"
        >
          <p class="text-sm font-medium text-amber-200">样本偏薄</p>
          <ul class="space-y-1">
            <li v-for="note in thinNotes" :key="note" class="text-xs leading-relaxed text-slate-400">
              · {{ note }}
            </li>
          </ul>
        </GlassPanel>

        <!-- 页面主体 -->
        <slot :envelope="envelope" />

        <!-- 后端说明（原样展示，供读者核对口径） -->
        <GlassPanel
          v-if="envelope?.notes?.length"
          variant="soft"
          padding="sm"
          rounded="2xl"
          as="div"
          class="space-y-1.5"
        >
          <p class="text-xs font-medium text-slate-400">口径与说明</p>
          <ul class="space-y-1">
            <li
              v-for="note in envelope.notes"
              :key="note"
              class="text-[11px] leading-relaxed text-slate-500"
            >
              · {{ note }}
            </li>
          </ul>
        </GlassPanel>
      </template>
    </div>
  </main>
</template>
