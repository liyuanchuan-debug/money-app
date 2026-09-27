<script setup lang="ts">
/**
 * HeatGrid —— 手写 SVG 热力网格（01 - 49 出现热度 / 遗漏视图）。
 *
 * 强度驱动：取值先按调用方传入的这批数据「等频」切成若干色档，
 * 每档取**同一个色相家族**里的一个真实色阶 token（浅 → 深，见 useChartTheme 的
 * heatRamp），靠明度拉开档距。
 * 方向：**值越大越深**（出现越多越浓），最浅的一档给最小值。零值档例外，走中性灰。
 *
 * 特别注意别退回旧实现：早先这里是「同一个颜色配不同 alpha」，
 * 叠在深色琉璃底上明度被压平，中频段（本池 200 期里大多是 3-6 次）几乎同一块颜色 ——
 * 那才是「不够明显」的根因。整条色阶只有一个色系，但区分度必须来自明度，
 * 非零档一律共用同一个 alpha（alpha 不随档位变化）。
 *
 * 零值档（一次都没出现 / 遗漏为 0）固定走「贴底色的中性灰」：填充明度、
 * 描边色相、文字明度三处都和非零档分开，不会被误读成「中档热度」。
 * 方向反转为「越热越深」之后，最深的一档（最热）也落在暗色端 —— 所以最高档由
 * useChartTheme 换上**同家族的强调色描边**（aqua-400 实线，其余档是本档深色实线），
 * 与零值档的极淡中性灰平色格对开；两块暗色因此不会混成一块。
 * 高亮（最新一期）仍是最粗最亮的 aqua-200 光圈，最高档只加同宽的彩色描边，
 * 不会把「最新一期」这一个信号稀释掉。
 *
 * 可读性：legend=true 时在网格下方给出色阶图例（档位区间由同一份分档逻辑算出，
 * 不可能和格子配色脱节）+ 一行点按读数兜底；悬停提示照旧。数值单位与口径不在这里
 * 编造，由调用方用 value-unit / legend-scope 传入。
 *
 * 响应式：viewBox + w-full h-auto；格子为正方形，列数可配（默认 7 列 × 7 行）。
 * 无动效：静态色阶、不注册任何过渡，天然满足 prefers-reduced-motion。
 * 口径提醒：热度只统计调用方传入的数据（例：本池已导入的开奖记录）。
 */
import type { ChartTone, HeatBin } from '~/composables/useChartTheme'
import { buildHeatScale, heatBinLevel } from '~/composables/useChartTheme'

interface HeatCell {
  /** 格内主标签，一般是号码 */
  number: number | string
  /** 强度值（出现次数 / 遗漏期数等） */
  value: number
  /** 次级标签（可放生肖等） */
  label?: string
  /** 悬停 / 点按提示里的补充说明 */
  hint?: string
}

const props = withDefaults(
  defineProps<{
    cells?: HeatCell[]
    /** 列数 */
    columns?: number
    /** 色阶上界；缺省取本批数据的最大值（分档本身按等频切，这里只用来标注最高档的区间上界） */
    max?: number | null
    tone?: ChartTone
    /** 是否显示格内主标签 */
    showNumbers?: boolean
    /** 是否显示次级标签 */
    showLabels?: boolean
    /** 高亮某几格（如最新一期） */
    highlight?: Array<number | string>
    /** 格子间距（viewBox 单位） */
    gap?: number
    /** 是否显示色阶图例（含点按读数）；默认关，避免影响未打开它的调用方布局 */
    legend?: boolean
    /** 数值单位（如 '次'）：只用于图例说明与读数文案，不参与任何计算 */
    valueUnit?: string
    /** 图例里的口径说明（调用方传「本池已导入 N 期数据内」这类原话） */
    legendScope?: string
    emptyText?: string
    emptyHint?: string
  }>(),
  {
    cells: () => [],
    columns: 7,
    max: null,
    tone: 'aqua',
    showNumbers: true,
    showLabels: false,
    highlight: () => [],
    gap: 6,
    legend: false,
    valueUnit: '',
    legendScope: '',
    emptyText: '数据不足',
    emptyHint: '',
  },
)

const VB_WIDTH = 720
const CAPTION_SPACE = 0

const columnCount = computed(() => Math.max(1, Math.round(props.columns)))
const rowCount = computed(() => Math.max(1, Math.ceil(props.cells.length / columnCount.value)))
const gap = computed(() => Math.max(0, props.gap))
const cellSize = computed(
  () => (VB_WIDTH - gap.value * (columnCount.value - 1)) / columnCount.value,
)
const vbHeight = computed(
  () => rowCount.value * cellSize.value + Math.max(0, rowCount.value - 1) * gap.value + CAPTION_SPACE,
)

const hasData = computed(() => props.cells.length > 0)

/** 取值为 0/脏数据一律按 0 处理，避免负值或 NaN 落到彩色档上 */
const values = computed(() =>
  props.cells.map(cell => (Number.isFinite(cell.value) ? Math.max(0, cell.value) : 0)),
)

/** 分档：等频切分 + 单色系亮度色阶（含零值档），口径由调用方在图题 / 图例里声明 */
const scale = computed(() =>
  buildHeatScale(values.value, {
    tone: props.tone,
    max: props.max,
  }),
)

const bins = computed(() => scale.value.bins)
const maxLevel = computed(() => scale.value.maxLevel)

const binByLevel = computed(() => {
  const map = new Map<number, HeatBin>()
  for (const bin of bins.value) map.set(bin.level, bin)
  return map
})

/** 图例条目：直接复用分档结果，所以图例和格子永远是同一份数据；零值档单独标记 */
const legendItems = computed(() =>
  bins.value.map(bin => ({
    label: bin.label,
    fill: bin.fill,
    /** 非最高档用本档 solid（小色块上最干脆）；最高档用换过强调色的 stroke */
    border: bin.top ? bin.stroke : bin.solid,
    top: bin.top,
    zero: bin.level === 0,
  })),
)

/** 单位与口径都来自调用方，组件不替调用方编造口径 */
const legendCaption = computed(() => {
  const unit = props.valueUnit ? `单位：${props.valueUnit}` : ''
  return [unit, props.legendScope].filter(Boolean).join(' · ')
})

interface Cell {
  index: number
  x: number
  y: number
  number: number | string
  label?: string
  hint?: string
  value: number
  level: number
  isTop: boolean
  isHighlighted: boolean
  showLabel: boolean
}

const layout = computed<Cell[]>(() =>
  props.cells.map((cell, index) => {
    const column = index % columnCount.value
    const row = Math.floor(index / columnCount.value)
    const value = values.value[index] ?? 0
    const level = heatBinLevel(scale.value, value)
    return {
      index,
      x: column * (cellSize.value + gap.value),
      y: row * (cellSize.value + gap.value),
      number: cell.number,
      label: cell.label,
      hint: cell.hint,
      value,
      level,
      isTop: level > 0 && level === maxLevel.value,
      isHighlighted: props.highlight.some(item => String(item) === String(cell.number)),
      showLabel: props.showLabels && cellSize.value >= 34,
    }
  }),
)

/** 档位 → 填充色；0 值走中性贴底色档，和有热度的彩色格在明度/色相/文字三处都分开 */
function cellFill(cell: Cell) {
  return (binByLevel.value.get(cell.level) ?? bins.value[0])?.fill ?? 'transparent'
}

function cellText(cell: Cell) {
  return (binByLevel.value.get(cell.level) ?? bins.value[0])?.text ?? 'rgba(148, 163, 184, 0.6)'
}

/**
 * 描边直接用本档的 stroke：零值档是极淡中性灰、普通档是同色相实色，
 * 最高档是 useChartTheme 换上的**家族强调色**（让它和零值档的暗色块一眼分开）。
 * 高亮（最新一期）优先级最高，覆盖档位描边。
 */
function cellStroke(cell: Cell) {
  if (cell.isHighlighted) return 'rgba(161, 241, 255, 0.95)'
  return (binByLevel.value.get(cell.level) ?? bins.value[0])?.stroke ?? 'rgba(255, 255, 255, 0.10)'
}

/** 悬停（桌面）：只影响提示气泡 */
const hoverIndex = ref<number | null>(null)
/** 点按（手机）：点一下格子在刻度下方读数，再点一下取消 */
const activeIndex = ref<number | null>(null)

const focusIndex = computed(() => activeIndex.value ?? hoverIndex.value)

function toggleCell(index: number) {
  activeIndex.value = activeIndex.value === index ? null : index
}

const hoverInfo = computed(() => {
  const index = focusIndex.value
  if (index === null) return null
  const cell = layout.value[index]
  if (!cell) return null
  return {
    left: `${((cell.x + cellSize.value / 2) / VB_WIDTH) * 100}%`,
    top: `${(cell.y / vbHeight.value) * 100}%`,
    number: cell.number,
    value: cell.value,
    label: cell.label,
    hint: cell.hint,
  }
})

/**
 * 点按读数：只在 legend 打开时渲染，手机上没有悬停也读得到具体数值。
 * 刻意只放「号码 + 数值」，不加口径长句 —— 360px 下必须一行放得下，
 * 口径说明统一放在下面的图例说明里，读数行不会因为点按换行抖动。
 */
const readout = computed(() => {
  const index = activeIndex.value
  if (index === null) return null
  const cell = layout.value[index]
  if (!cell) return null
  return { number: cell.number, value: cell.value }
})

const gridLabel = computed(
  () => `热度网格，共 ${props.cells.length} 格，色阶由浅到深共 ${maxLevel.value} 档`,
)
</script>

<template>
  <div class="relative w-full">
    <div
      v-if="!hasData"
      class="glass-panel-soft flex flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed border-white/10 px-4 py-8 text-center"
    >
      <span class="text-sm text-slate-400">{{ emptyText }}</span>
      <span v-if="emptyHint" class="text-xs text-slate-500">{{ emptyHint }}</span>
    </div>

    <template v-else>
      <svg
        class="block h-auto w-full"
        :viewBox="`0 0 ${VB_WIDTH} ${vbHeight}`"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        :aria-label="gridLabel"
      >
        <g v-for="cell in layout" :key="`cell-${cell.index}`">
          <rect
            :x="cell.x"
            :y="cell.y"
            :width="cellSize"
            :height="cellSize"
            rx="8"
            :fill="cellFill(cell)"
            :stroke="cellStroke(cell)"
            :stroke-width="focusIndex === cell.index || cell.isHighlighted ? 2.2 : 1.1"
          />
          <!-- 顶部内高光：琉璃格 -->
          <rect
            :x="cell.x + 2"
            :y="cell.y + 2"
            :width="Math.max(0, cellSize - 4)"
            height="1.2"
            rx="0.6"
            fill="rgba(255, 255, 255, 0.22)"
          />
          <text
            v-if="props.showNumbers"
            :x="cell.x + cellSize / 2"
            :y="cell.y + cellSize / 2 + (cell.showLabel ? -1 : 4)"
            text-anchor="middle"
            font-size="12"
            :fill="cellText(cell)"
            class="num"
          >
            {{ cell.number }}
          </text>
          <text
            v-if="cell.showLabel && cell.label"
            :x="cell.x + cellSize / 2"
            :y="cell.y + cellSize / 2 + 12"
            text-anchor="middle"
            font-size="8.5"
            fill="rgba(203, 213, 225, 0.6)"
          >
            {{ cell.label }}
          </text>
          <rect
            :x="cell.x"
            :y="cell.y"
            :width="cellSize"
            :height="cellSize"
            rx="8"
            fill="transparent"
            @pointerenter="hoverIndex = cell.index"
            @pointerleave="hoverIndex = null"
            @click="toggleCell(cell.index)"
          />
        </g>
      </svg>

      <div
        v-if="hoverInfo"
        class="glass-panel-strong pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-[125%] rounded-lg px-2.5 py-1.5 text-[11px] whitespace-nowrap"
        :style="{ left: hoverInfo.left, top: hoverInfo.top }"
      >
        <p class="text-slate-400">
          {{ hoverInfo.number }}
          <span v-if="hoverInfo.label">· {{ hoverInfo.label }}</span>
        </p>
        <p class="num text-sm text-slate-100">{{ hoverInfo.value }}{{ props.valueUnit }}</p>
        <p v-if="hoverInfo.hint" class="mt-0.5 text-[10px] text-slate-500">{{ hoverInfo.hint }}</p>
      </div>

      <!-- 图例 + 点按读数：档位区间和图例色块都来自同一份分档结果 -->
      <div v-if="props.legend" class="mt-3 space-y-2">
        <p class="flex min-h-[18px] flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]" aria-live="polite">
          <template v-if="readout">
            <span class="num font-semibold text-slate-100">{{ readout.number }}</span>
            <span class="num text-slate-200">{{ readout.value }}{{ props.valueUnit }}</span>
          </template>
          <span v-else class="text-slate-500">点按格子查看具体数值</span>
        </p>
        <HeatScaleLegend :items="legendItems" :caption="legendCaption" />
      </div>
    </template>
  </div>
</template>
