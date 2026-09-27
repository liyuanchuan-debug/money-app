<script setup lang="ts">
/**
 * BarChart —— 手写 SVG 柱状图（无图表库）。
 *
 * 用途：出现频次、波动分布一类的分类计数。
 * 响应式：viewBox + w-full h-auto，宽高比恒定；悬停提示用百分比定位。
 *
 * 配色：**一个色系** —— 所有柱子共用 `tone` 指定的同一个色相家族，
 * 沿 useChartTheme 的单色系亮度色阶按数值取色（**越高越深**，与热力网格同向），
 * 不再一根柱子一个色相。tone 缺省 aqua（品牌主色）；
 * 单根柱子仍可用 bars[i].tone 覆盖（不传就用统一色系）。
 * 注意：区分度来自明度而不是透明度 —— 具体原因见 useChartTheme 的 heatRamp 注释。
 *
 * 口径提醒：柱子的数值口径必须写进图题（例：「本系统已导入数据」），
 *          数据不足时请让 bars 为空，组件会显示「数据不足」而不是画 0 柱。
 */
import type { ChartTone } from '~/composables/useChartTheme'
import { chartTone, heatStepAt, formatAxisValue } from '~/composables/useChartTheme'

interface BarItem {
  label: string
  value: number
  /** 单根柱子的色系覆盖；不传就用整图统一的 tone，保证默认是一个色系 */
  tone?: ChartTone
  /** 悬停提示里的补充说明 */
  hint?: string
}

const props = withDefaults(
  defineProps<{
    bars?: BarItem[]
    height?: number
    /** 纵轴最大值；缺省取数据最大值 */
    max?: number | null
    /** 整图统一色系（单色系亮度梯度）；单根柱子可用 bars[i].tone 覆盖 */
    tone?: ChartTone
    showValues?: boolean
    showGrid?: boolean
    valueSuffix?: string
    emptyText?: string
    emptyHint?: string
  }>(),
  {
    bars: () => [],
    height: 220,
    max: null,
    tone: 'aqua',
    showValues: true,
    showGrid: true,
    valueSuffix: '',
    emptyText: '数据不足',
    emptyHint: '',
  },
)

const VB_WIDTH = 720
const PAD = { top: 26, right: 14, bottom: 30, left: 38 }

const vbHeight = computed(() => Math.max(150, props.height))
const plotLeft = PAD.left
const plotRight = computed(() => VB_WIDTH - PAD.right)
const plotTop = PAD.top
const plotBottom = computed(() => vbHeight.value - PAD.bottom)
const plotWidth = computed(() => Math.max(1, plotRight.value - plotLeft))
const plotHeight = computed(() => Math.max(1, plotBottom.value - plotTop))

/** 只有「存在柱子且至少一根为正」才算有数据，避免把空数据画成 0 柱 */
const hasData = computed(() => props.bars.length > 0 && props.bars.some((bar) => bar.value > 0))

const upperBound = computed(() => {
  const values = props.bars.map((bar) => (Number.isFinite(bar.value) ? bar.value : 0))
  const raw = props.max && props.max > 0 ? props.max : Math.max(1, ...values)
  return raw <= 0 ? 1 : raw
})

interface Column {
  index: number
  label: string
  value: number
  hint?: string
  colors: BarColors
  x: number
  y: number
  width: number
  barHeight: number
  center: number
  showLabel: boolean
}

/** 单色系柱色：填充/描边取同一色相家族亮度梯子上的一档，数值文字用该家族的可读亮字 */
interface BarColors {
  fill: string
  stroke: string
  label: string
}

const columns = computed<Column[]>(() => {
  const count = props.bars.length
  if (!count) return []
  const slot = plotWidth.value / count
  const width = Math.max(4, Math.min(slot * 0.62, 54))
  const labelStep = Math.max(1, Math.ceil(count / 14))

  return props.bars.map((bar, index) => {
    const value = Number.isFinite(bar.value) ? Math.max(0, bar.value) : 0
    const ratio = Math.min(1, value / upperBound.value)
    const barHeight = Math.max(ratio > 0 ? 3 : 0, ratio * plotHeight.value)
    const center = plotLeft + slot * index + slot / 2
    // 整图统一一个色系：按数值比例在亮度梯子上取一档（越高越深）
    const tone = bar.tone ?? props.tone
    const step = heatStepAt(tone, ratio)
    return {
      index,
      label: bar.label,
      value,
      hint: bar.hint,
      colors: { fill: step.fill, stroke: step.stroke, label: chartTone(tone).text },
      x: center - width / 2,
      y: plotBottom.value - barHeight,
      width,
      barHeight,
      center,
      showLabel: index % labelStep === 0 || index === count - 1,
    }
  })
})

const gridLines = computed(() => {
  const count = 4
  return Array.from({ length: count + 1 }, (_, i) => {
    const value = (upperBound.value * i) / count
    const ratio = value / upperBound.value
    return { value, y: plotBottom.value - ratio * plotHeight.value, label: formatAxisValue(value) }
  })
})

/** 顶部圆角的柱体路径 */
function barPath(column: Column) {
  const radius = Math.min(6, column.width / 2, Math.max(0, column.barHeight))
  const { x, y, width, barHeight } = column
  if (barHeight <= 0) return ''
  const bottom = y + barHeight
  return [
    `M ${x} ${bottom}`,
    `L ${x} ${y + radius}`,
    `Q ${x} ${y} ${x + radius} ${y}`,
    `L ${x + width - radius} ${y}`,
    `Q ${x + width} ${y} ${x + width} ${y + radius}`,
    `L ${x + width} ${bottom}`,
    'Z',
  ].join(' ')
}

const hoverIndex = ref<number | null>(null)

const hoverInfo = computed(() => {
  const index = hoverIndex.value
  if (index === null) return null
  const column = columns.value[index]
  if (!column) return null
  return {
    left: `${(column.center / VB_WIDTH) * 100}%`,
    top: `${(column.y / vbHeight.value) * 100}%`,
    label: column.label,
    value: `${formatAxisValue(column.value)}${props.valueSuffix}`,
    hint: column.hint,
  }
})
</script>

<template>
  <div class="relative w-full">
    <div
      v-if="!hasData"
      class="glass-panel-soft flex flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed border-white/10 px-4 text-center"
      :style="{ height: `${vbHeight}px` }"
    >
      <span class="text-sm text-slate-400">{{ emptyText }}</span>
      <span v-if="emptyHint" class="text-xs text-slate-500">{{ emptyHint }}</span>
    </div>

    <template v-else>
      <svg
        class="block h-auto w-full overflow-visible"
        :viewBox="`0 0 ${VB_WIDTH} ${vbHeight}`"
        preserveAspectRatio="xMidYMid meet"
        role="img"
      >
        <!-- 网格 + 纵轴刻度 -->
        <g v-if="props.showGrid">
          <line
            v-for="(line, lineIndex) in gridLines"
            :key="`grid-${lineIndex}`"
            :x1="plotLeft"
            :x2="plotRight"
            :y1="line.y"
            :y2="line.y"
            stroke="rgba(148, 163, 184, 0.14)"
            stroke-width="1"
            :stroke-dasharray="lineIndex === 0 ? undefined : '3 6'"
          />
          <text
            v-for="(line, lineIndex) in gridLines"
            :key="`grid-label-${lineIndex}`"
            :x="plotLeft - 8"
            :y="line.y + 3.5"
            text-anchor="end"
            font-size="9.5"
            fill="rgba(148, 163, 184, 0.7)"
            class="num"
          >
            {{ line.label }}
          </text>
        </g>

        <!-- 柱体 -->
        <g
          v-for="column in columns"
          :key="`bar-${column.index}`"
          :opacity="hoverIndex === null || hoverIndex === column.index ? 1 : 0.5"
          class="transition-opacity duration-300"
        >
          <path
            v-if="column.barHeight > 0"
            :d="barPath(column)"
            :fill="column.colors.fill"
            :stroke="column.colors.stroke"
            stroke-width="1.4"
          />
          <!-- 顶部高光：让柱体像琉璃条 -->
          <rect
            v-if="column.barHeight > 0"
            :x="column.x + 1.5"
            :y="column.y + 1"
            :width="Math.max(0, column.width - 3)"
            height="1.5"
            rx="0.75"
            fill="rgba(255, 255, 255, 0.5)"
          />
          <text
            v-if="props.showValues"
            :x="column.center"
            :y="Math.max(plotTop - 6, column.y - 6)"
            text-anchor="middle"
            font-size="10"
            :fill="column.colors.label"
            class="num"
          >
            {{ formatAxisValue(column.value) }}
          </text>
          <text
            v-if="column.showLabel"
            :x="column.center"
            :y="vbHeight - 9"
            text-anchor="middle"
            font-size="9.5"
            fill="rgba(148, 163, 184, 0.72)"
            class="num"
          >
            {{ column.label }}
          </text>
          <!-- 悬停热区：整列可点，避免小柱体难悬停 -->
          <rect
            :x="column.center - plotWidth / Math.max(1, columns.length) / 2"
            :y="plotTop"
            :width="plotWidth / Math.max(1, columns.length)"
            :height="plotHeight"
            fill="transparent"
            @pointerenter="hoverIndex = column.index"
            @pointerleave="hoverIndex = null"
          />
        </g>
      </svg>

      <div
        v-if="hoverInfo"
        class="glass-panel-strong pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-[130%] rounded-lg px-2.5 py-1.5 text-[11px] whitespace-nowrap"
        :style="{ left: hoverInfo.left, top: hoverInfo.top }"
      >
        <p class="text-slate-400">{{ hoverInfo.label }}</p>
        <p class="num text-sm text-slate-100">{{ hoverInfo.value }}</p>
        <p v-if="hoverInfo.hint" class="mt-0.5 text-[10px] text-slate-500">{{ hoverInfo.hint }}</p>
      </div>
    </template>
  </div>
</template>
