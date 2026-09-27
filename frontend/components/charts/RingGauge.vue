<script setup lang="ts">
/**
 * RingGauge —— 手写 SVG 圆环仪表（单个百分比，如命中率 / 号码覆盖度）。
 *
 * - 环宽、色调、尺寸可配；中心数字走 useNumberRoll（减少动效时直接落终态）；
 * - value 为 null / 非法值时显示「数据不足」，绝不用 0 % 冒充。
 * - 口径提醒：百分比的含义必须由调用方在 label / caption 里写清数据来源。
 */
import type { ChartTone } from '~/composables/useChartTheme'
import { chartTone } from '~/composables/useChartTheme'

const props = withDefaults(
  defineProps<{
    /** 0 - 100 的百分比；null 表示数据不足 */
    value: number | null | undefined
    /** 外框像素尺寸 */
    size?: number
    /** 环宽（viewBox 单位，基准 120） */
    strokeWidth?: number
    tone?: ChartTone
    /** 环下方的标题 */
    label?: string
    /** 环下方的补充说明 */
    caption?: string
    suffix?: string
    decimals?: number
    emptyText?: string
  }>(),
  {
    value: null,
    size: 176,
    strokeWidth: 9,
    tone: 'aqua',
    label: '',
    caption: '',
    suffix: '%',
    decimals: 0,
    emptyText: '数据不足',
  },
)

const VIEW = 120
const CENTER = VIEW / 2

const colors = computed(() => chartTone(props.tone))

const clamped = computed(() => {
  const raw = Number(props.value)
  if (props.value === null || props.value === undefined || !Number.isFinite(raw)) return null
  return Math.min(100, Math.max(0, raw))
})

const radius = computed(() => CENTER - props.strokeWidth / 2 - 4)
const circumference = computed(() => 2 * Math.PI * radius.value)
const dashLength = computed(() => (circumference.value * (clamped.value ?? 0)) / 100)

const { display } = useNumberRoll(() => clamped.value, { duration: 900, min: 0, max: 100 })

const displayText = computed(() => {
  if (display.value === null || display.value === undefined) return props.emptyText
  return `${display.value.toFixed(props.decimals)}${props.suffix}`
})
</script>

<template>
  <div class="flex flex-col items-center gap-2">
    <div class="relative" :style="{ width: `${props.size}px`, height: `${props.size}px` }">
      <svg
        class="block h-full w-full"
        :viewBox="`0 0 ${VIEW} ${VIEW}`"
        preserveAspectRatio="xMidYMid meet"
        role="img"
      >
        <!-- 外圈装饰 -->
        <circle
          :cx="CENTER"
          :cy="CENTER"
          :r="radius + props.strokeWidth / 2 + 2.5"
          fill="none"
          stroke="rgba(255, 255, 255, 0.06)"
          stroke-width="1"
        />
        <!-- 底轨 -->
        <circle
          :cx="CENTER"
          :cy="CENTER"
          :r="radius"
          fill="none"
          stroke="rgba(148, 163, 184, 0.16)"
          :stroke-width="props.strokeWidth"
          stroke-linecap="round"
        />
        <!-- 值环 -->
        <circle
          v-if="clamped !== null"
          :cx="CENTER"
          :cy="CENTER"
          :r="radius"
          fill="none"
          :stroke="colors.stroke"
          :stroke-width="props.strokeWidth"
          stroke-linecap="round"
          :stroke-dasharray="`${dashLength} ${circumference}`"
          :transform="`rotate(-90 ${CENTER} ${CENTER})`"
          style="transition: stroke-dasharray 0.9s cubic-bezier(0.22, 1, 0.36, 1)"
        />
        <!-- 内圈：琉璃厚度感 -->
        <circle
          :cx="CENTER"
          :cy="CENTER"
          :r="radius - props.strokeWidth / 2 - 2"
          fill="none"
          stroke="rgba(255, 255, 255, 0.08)"
          stroke-width="1"
        />
      </svg>

      <div class="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span
          class="num text-2xl font-semibold"
          :class="clamped === null ? 'text-sm font-normal text-slate-500' : 'text-white'"
        >
          {{ displayText }}
        </span>
      </div>
    </div>

    <p v-if="props.label" class="text-sm text-slate-300">{{ props.label }}</p>
    <p v-if="props.caption" class="text-center text-xs text-slate-500">{{ props.caption }}</p>
  </div>
</template>
