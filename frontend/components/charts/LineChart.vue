<script setup lang="ts">
/**
 * LineChart —— 手写 SVG 折线图（无任何图表库）。
 *
 * 用途：特码走势一类的连续序列。支持多序列、参考带、悬浮提示、数据不足态。
 * 响应式：靠 viewBox + w-full h-auto，宽高比恒定，永不变形；
 *         悬浮提示用百分比定位，因此与 SVG 的缩放完全同步。
 *
 * 横向滑动（scrollable，opt-in，默认关）：
 *   - 每个点占固定像素宽（由「一屏 windowSize 期」反推），SVG 的固有宽度随期数增长，
 *     外层容器 overflow-x-auto —— 左右滑动完全交给浏览器原生滚动，不写任何手势 JS，
 *     手机上才有惯性 / 触控板横扫 / 键盘方向键，也不和页面纵向滚动抢手势；
 *   - 打开时自动贴到最新一端（右端），只做「量容器宽度 + 设 scrollLeft」，
 *     默认不做动画，所以不会出现首屏跳版；
 *   - y 轴刻度放在滚动区之外的固定轴条里，滑动时不会跟着数据一起滑出视口；
 *   - 悬浮提示跟着 SVG 坐标走（滑动模式下用 px 定位，非滑动模式仍是百分比）。
 *   - scrollable=false（默认）时渲染结果与改造前一致，其余调用方零影响。
 *   - windowOptions 非空时在图下方渲染「一屏多少期」的档位切换（0 = 全部）。
 *
 * 口径提醒：本组件只画调用方给的数据。图题 / 说明必须写清数据口径
 *          （例：「本池已导入的最近 10 期」），禁止把结果当成越界口径的结论。
 */
import { nextTick, onBeforeUnmount, reactive, useId, watch } from 'vue'
import { useMotion } from '~/composables/useMotion'
import type { ChartTone } from '~/composables/useChartTheme'
import { chartToneAt, formatAxisValue } from '~/composables/useChartTheme'

interface LineSeries {
  /** 序列名（提示框里显示） */
  name?: string
  /** 数值序列，null 表示缺失（该点断开） */
  values: Array<number | null>
  /** 色调，缺省按序号自动分配 */
  tone?: ChartTone
}

interface ReferenceBand {
  from: number
  to: number
  label?: string
  tone?: ChartTone
}

const props = withDefaults(
  defineProps<{
    series?: LineSeries[]
    /** x 轴刻度文案，长度应与序列一致 */
    labels?: Array<string | number>
    /** 渲染高度（逻辑像素，作为 viewBox 高度） */
    height?: number
    /** 参考带（如波动阈值区间） */
    bands?: ReferenceBand[]
    yMin?: number | null
    yMax?: number | null
    showDots?: boolean
    showGrid?: boolean
    showArea?: boolean
    showTooltip?: boolean
    valueSuffix?: string
    emptyText?: string
    emptyHint?: string
    /**
     * 是否开启横向滑动（opt-in，默认关）。
     * 关：所有点压进容器宽度（改造前行为）；开：每个点占固定像素宽，容器可左右滑。
     */
    scrollable?: boolean
    /**
     * 滑动模式下「一屏显示多少期」的默认视窗（>0 生效；<=0 表示全部）。
     * 只是视窗初值，用户点档位后由组件内部维护，并通过 update:windowSize 回传。
     */
    windowSize?: number
    /**
     * 滑动模式下的档位列表（0 表示全部）；非空才渲染档位切换控件。
     * 例：[30, 60, 100, 0]
     */
    windowOptions?: number[]
  }>(),
  {
    series: () => [],
    labels: () => [],
    height: 220,
    bands: () => [],
    yMin: null,
    yMax: null,
    showDots: true,
    showGrid: true,
    showArea: true,
    showTooltip: true,
    valueSuffix: '',
    emptyText: '数据不足',
    emptyHint: '',
    scrollable: false,
    windowSize: 30,
    windowOptions: () => [],
  },
)

const emit = defineEmits<{ 'update:windowSize': [value: number] }>()

const VB_WIDTH = 720
const PAD = { top: 18, right: 18, bottom: 26, left: 42 }

/** 滑动模式下绘图区左侧留白：要避开图外固定 y 轴条的宽度（否则最左边那期永远被轴条挡住） */
const SCROLL_PAD_LEFT = 46
/** 固定 y 轴条宽度 + 右侧渐隐宽度（与模板里的 w-[30px] / w-2.5 对应） */
const SCROLL_AXIS_WIDTH = 30
/**
 * SSR / 无 window 时的兜底视口宽度。
 * 取「常见手机内容区」而非整屏 360：页边距 + 卡片内边距后走势卡大约 300–328。
 * 客户端会立刻用 window.innerWidth 粗估覆盖，ResizeObserver 再精修。
 */
const SCROLL_FALLBACK_VIEWPORT = 328
/** 每个点的最小像素步长（防止极端期数下算出 0 或负值） */
const SCROLL_MIN_STEP = 1
/** 滑动模式下 x 轴刻度之间的最小像素间距（避免标签叠成一团） */
const SCROLL_TICK_GAP = 64
/** 步长小于这个值就不画圆点：点会糊成一条粗线，反而看不清走势 */
const SCROLL_DOT_MIN_STEP = 7
/** 悬浮提示需要向上探出的高度（滚动区的 padding-top，避免提示被裁掉） */
const SCROLL_TOOLTIP_ROOM = 48

const uid = useId()
const { shouldAnimate } = useMotion()

/** 客户端首帧粗估：比死等 ResizeObserver 更早贴近真实宽度（桌面不再长期停在 328） */
function estimateViewportWidth(): number {
  if (!import.meta.client || typeof window === 'undefined') return SCROLL_FALLBACK_VIEWPORT
  // 页水平 padding（px-5≈40）粗减；夹在合理区间，避免极端窄/宽把步长算飞
  return Math.max(240, Math.min(960, Math.round(window.innerWidth - 40)))
}

/* -------------------------------------------------------------------------- */
/* 横向滑动：视窗状态 + 容器测量                                               */
/* -------------------------------------------------------------------------- */

const scrollerEl = ref<HTMLElement | null>(null)
/** 容器宽度（px）；客户端先粗估，量到 scroller 后再覆盖 */
const viewportWidth = ref(estimateViewportWidth())
/**
 * 是否已经在客户端量到真实容器宽度。
 * SSR（以及客户端首帧）拿不到宽度：这时**只渲染最近「一屏」**，
 * 否则会用兜底宽度把全部期数画成一段最旧的历史 —— 首帧闪一下旧数据再跳到最新端，
 * 正是「打开就该看到最新」要避免的。量到宽度后立刻切回全量 + 贴最新端。
 */
const viewportReady = ref(false)
/** 一屏显示多少期；0 = 全部 */
const zoom = ref(normalizeWindow(props.windowSize))

/** 滚动位置快照，用来决定两侧渐隐提示要不要出现 */
const scrollState = reactive({ left: 0, max: 0 })

let resizeObserver: ResizeObserver | null = null

/** 档位 / windowSize 的归一化：非有限值或 <=0 一律当「全部」 */
function normalizeWindow(value: number): number {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return 0
  return Math.max(1, Math.trunc(numeric))
}

const vbHeight = computed(() => Math.max(150, props.height))

const pointCount = computed(() =>
  props.series.reduce((max, item) => Math.max(max, item.values?.length ?? 0), 0),
)

const hasData = computed(() =>
  props.series.some((item) =>
    (item.values ?? []).some((v) => typeof v === 'number' && Number.isFinite(v)),
  ),
)

/** 实际生效的一屏期数：不会超过真实期数（期数不足一屏时直接铺满） */
const effectiveWindow = computed(() => {
  const count = pointCount.value
  if (count <= 0) return 0
  const wanted = zoom.value > 0 ? zoom.value : count
  return Math.max(1, Math.min(wanted, count))
})

/**
 * 这一帧真正要画的期数：
 * 滑动模式在量到容器宽度之前只画最近「一屏」（SSR 首帧），量到之后画全量。
 * 非滑动模式永远是全量，与改造前一致。
 */
const renderCount = computed(() => {
  const count = pointCount.value
  if (count <= 0) return 0
  if (!props.scrollable || viewportReady.value) return count
  return Math.max(1, Math.min(effectiveWindow.value || count, count))
})

/** 渲染窗口的起始下标：窗口永远贴住最新一端（最新一期在右） */
const renderStart = computed(() => Math.max(0, pointCount.value - renderCount.value))

/** 滑动内容的固有宽度：期数越多越宽，滚动条才有东西可滚 */
const contentWidth = computed(() => {
  if (!props.scrollable) return VB_WIDTH
  const count = renderCount.value
  const viewport = viewportWidth.value > 0 ? viewportWidth.value : SCROLL_FALLBACK_VIEWPORT
  if (count <= 1) return Math.round(viewport)
  const plot = Math.max(24, viewport - SCROLL_PAD_LEFT - PAD.right)
  // 一屏 per 期 = per-1 个间隔铺满绘图区
  const step = Math.max(SCROLL_MIN_STEP, plot / Math.max(1, effectiveWindow.value - 1))
  return Math.round(SCROLL_PAD_LEFT + step * (count - 1) + PAD.right)
})

const vbWidth = computed(() => (props.scrollable ? contentWidth.value : VB_WIDTH))
const plotLeft = computed(() => (props.scrollable ? SCROLL_PAD_LEFT : PAD.left))
const plotRight = computed(() => vbWidth.value - PAD.right)
const plotTop = PAD.top
const plotBottom = computed(() => vbHeight.value - PAD.bottom)
const plotWidth = computed(() => Math.max(1, plotRight.value - plotLeft.value))
const plotHeight = computed(() => Math.max(1, plotBottom.value - plotTop))

/** 每个点的水平像素步长（仅滑动模式有意义） */
const scrollStep = computed(() =>
  props.scrollable && renderCount.value > 1 ? plotWidth.value / (renderCount.value - 1) : 0,
)

/** 点太密就不画圆点（点会连成粗线，读不出单个取值） */
const showPoints = computed(() =>
  props.showDots && (!props.scrollable || scrollStep.value >= SCROLL_DOT_MIN_STEP),
)

const canScroll = computed(() => scrollState.max > 2)
const canScrollRight = computed(() => canScroll.value && scrollState.left < scrollState.max - 2)
/** 已经贴在最新一端 */
const atNewest = computed(() => !canScroll.value || scrollState.left >= scrollState.max - 2)

/** 档位列表：去重 + 升序（「全部」排在最后），非滑动模式不渲染 */
const windowChoices = computed(() => {
  if (!props.scrollable) return [] as number[]
  const seen = new Set<number>()
  const sized: number[] = []
  let all = false
  for (const raw of props.windowOptions) {
    const option = normalizeWindow(raw)
    if (seen.has(option)) continue
    seen.add(option)
    if (option === 0) all = true
    else sized.push(option)
  }
  sized.sort((a, b) => a - b)
  return all ? [...sized, 0] : sized
})

function windowLabel(option: number) {
  return option > 0 ? `${option} 期` : '全部'
}

const scrollerLabel = computed(
  () => `走势折线图，共 ${pointCount.value} 期；可左右滑动查看更早或更新的期数`,
)

function measureViewport(el: HTMLElement) {
  const width = Math.round(el.clientWidth)
  if (width <= 0) return
  // 量到真实宽度：从「只画最近一屏」切到全量（首帧不会先画一段最旧的历史）
  viewportReady.value = true
  if (width !== viewportWidth.value) viewportWidth.value = width
}

function readScrollState(el: HTMLElement) {
  scrollState.max = Math.max(0, Math.round(el.scrollWidth - el.clientWidth))
  scrollState.left = Math.max(0, Math.round(el.scrollLeft))
}

function onScroll() {
  // 非滑动模式下不会滚动，直接短路（也避免把 conditional 表达式直接写成 @scroll 的处理器：
  // 编译器会把它当成内联语句 `$event => (cond ? onScroll : undefined)`，结果永远不调用）
  if (!props.scrollable) return
  const el = scrollerEl.value
  if (el) readScrollState(el)
}

/** 贴到最新一端（右端）；smooth 也要先问过动效总开关 */
function snapToNewest(smooth = false) {
  const el = scrollerEl.value
  if (!el || !import.meta.client) return
  const max = Math.max(0, el.scrollWidth - el.clientWidth)
  const useSmooth = smooth && shouldAnimate.value
  el.scrollTo({ left: max, behavior: useSmooth ? 'smooth' : 'auto' })
  if (useSmooth) {
    // 平滑滚动期间 scrollLeft 还没到目标值，先按目标记账，避免渐隐提示闪一下
    scrollState.max = max
    scrollState.left = max
    return
  }
  readScrollState(el)
}

/** 等 DOM 尺寸稳定后再贴边，避免读到中间态 */
async function syncScroll(pin: boolean, smooth = false) {
  await nextTick()
  const el = scrollerEl.value
  if (!el) return
  if (pin) snapToNewest(smooth)
  readScrollState(el)
}

function selectWindow(option: number) {
  const next = normalizeWindow(option)
  if (next === zoom.value) return
  zoom.value = next
  emit('update:windowSize', next)
  // 换档位后回到最新一端：最新优先是全站约定，用户不用自己再滑回去
  void syncScroll(true)
}

// 容器出现 / 消失（v-if）时挂载测量；出现后先贴最新端
watch(
  scrollerEl,
  (el) => {
    resizeObserver?.disconnect()
    resizeObserver = null
    if (!el || !import.meta.client) return
    measureViewport(el)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => measureViewport(el))
      resizeObserver.observe(el)
    }
    readScrollState(el)
    // 先同步贴一次边（此刻量到的宽度就是浏览器里的真实宽度），
    // 让首帧尽量少看到「停在最旧一端」的样子；量到真实视口后再贴一次。
    snapToNewest()
    void syncScroll(true)
  },
  { immediate: true, flush: 'post' },
)

// 期数 / 档位变化 → 固有宽度变化：原来贴在最新端就继续贴住（否则会跳到别的期次）
watch(
  contentWidth,
  () => {
    const el = scrollerEl.value
    if (!el) return
    const wasAtNewest = scrollState.max <= 2 || scrollState.left >= scrollState.max - 2
    void syncScroll(wasAtNewest)
  },
  { flush: 'post' },
)

// 外部改 windowSize（v-model）时同步内部视窗
watch(
  () => props.windowSize,
  (value) => {
    const next = normalizeWindow(value)
    if (next !== zoom.value) zoom.value = next
  },
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
})

const bounds = computed(() => {
  const pool: number[] = []
  for (const item of props.series) {
    for (const v of item.values ?? []) {
      if (typeof v === 'number' && Number.isFinite(v)) pool.push(v)
    }
  }
  for (const band of props.bands) {
    if (Number.isFinite(band.from)) pool.push(band.from)
    if (Number.isFinite(band.to)) pool.push(band.to)
  }
  if (!pool.length) return { min: 0, max: 1 }

  const rawMin = Math.min(...pool)
  const rawMax = Math.max(...pool)
  let min = props.yMin ?? rawMin
  let max = props.yMax ?? rawMax
  if (max <= min) max = min + 1
  const headroom = (max - min) * 0.12
  if (props.yMin === null || props.yMin === undefined) min -= headroom
  if (props.yMax === null || props.yMax === undefined) max += headroom
  return { min, max }
})

function xAt(index: number) {
  const count = renderCount.value
  if (count <= 1) return plotLeft.value + plotWidth.value / 2
  // 只画窗口内的点：下标先减掉窗口起点，最新一期永远贴在右端
  return plotLeft.value + ((index - renderStart.value) / (count - 1)) * plotWidth.value
}

function yAt(value: number) {
  const range = bounds.value.max - bounds.value.min || 1
  const ratio = (value - bounds.value.min) / range
  return plotBottom.value - ratio * plotHeight.value
}

function round(value: number) {
  return Math.round(value * 100) / 100
}

/** 平滑折线：用二次贝塞尔把折角磨圆，更像液体走势 */
function linePath(points: Array<{ x: number; y: number }>) {
  if (!points.length) return ''
  if (points.length === 1) return `M ${round(points[0].x)} ${round(points[0].y)}`
  if (points.length === 2) {
    return `M ${round(points[0].x)} ${round(points[0].y)} L ${round(points[1].x)} ${round(points[1].y)}`
  }
  let d = `M ${round(points[0].x)} ${round(points[0].y)}`
  for (let i = 1; i < points.length - 1; i += 1) {
    const midX = (points[i].x + points[i + 1].x) / 2
    const midY = (points[i].y + points[i + 1].y) / 2
    d += ` Q ${round(points[i].x)} ${round(points[i].y)} ${round(midX)} ${round(midY)}`
  }
  const last = points[points.length - 1]
  d += ` L ${round(last.x)} ${round(last.y)}`
  return d
}

const geometry = computed(() =>
  props.series.map((item, seriesIndex) => {
    const values = item.values ?? []
    const runs: Array<Array<{ x: number; y: number; value: number; index: number }>> = []
    let current: Array<{ x: number; y: number; value: number; index: number }> = []
    values.forEach((raw, index) => {
      // 窗口外的点不参与绘制（量到容器宽度前只画最近一屏）
      if (index < renderStart.value) return
      if (typeof raw === 'number' && Number.isFinite(raw)) {
        current.push({ x: xAt(index), y: yAt(raw), value: raw, index })
      } else if (current.length) {
        runs.push(current)
        current = []
      }
    })
    if (current.length) runs.push(current)

    return {
      name: item.name ?? `序列 ${seriesIndex + 1}`,
      colors: chartToneAt(seriesIndex),
      runs: runs.map((points) => ({
        points,
        line: linePath(points),
        area: points.length > 1
          ? `${linePath(points)} L ${round(points[points.length - 1].x)} ${round(plotBottom.value)} L ${round(points[0].x)} ${round(plotBottom.value)} Z`
          : '',
      })),
    }
  }),
)

const gridLines = computed(() => {
  const count = 4
  return Array.from({ length: count + 1 }, (_, i) => {
    const value = bounds.value.min + ((bounds.value.max - bounds.value.min) * i) / count
    return { value, y: yAt(value), label: formatAxisValue(value) }
  })
})

const xTicks = computed(() => {
  const total = pointCount.value
  if (total <= 0) return []
  const ticks: Array<{ index: number; x: number; label: string; anchor: string }> = []

  // 滑动模式：按像素间距取刻度，并从最新端往回等距铺 ——
  // 小步长下若从 0 开始铺，右端会挤出两根几乎重叠的刻度。
  if (props.scrollable) {
    const step = Math.max(1, Math.ceil(SCROLL_TICK_GAP / Math.max(0.5, scrollStep.value)))
    for (let i = total - 1; i >= renderStart.value; i -= step) {
      ticks.push({
        index: i,
        x: xAt(i),
        label: String(props.labels[i] ?? i + 1),
        anchor: i === total - 1 ? 'end' : 'middle',
      })
    }
    return ticks
  }

  const maxLabels = 8
  const step = Math.max(1, Math.ceil(total / maxLabels))
  for (let i = 0; i < total; i += step) {
    ticks.push({
      index: i,
      x: xAt(i),
      label: String(props.labels[i] ?? i + 1),
      anchor: 'middle',
    })
  }
  const lastIndex = total - 1
  if (ticks[ticks.length - 1]?.index !== lastIndex) {
    ticks.push({
      index: lastIndex,
      x: xAt(lastIndex),
      label: String(props.labels[lastIndex] ?? lastIndex + 1),
      anchor: 'end',
    })
  }
  return ticks
})

const hoverIndex = ref<number | null>(null)

function onPointerMove(event: PointerEvent) {
  if (!props.showTooltip || !hasData.value || renderCount.value <= 0) return
  const rect = (event.currentTarget as Element).getBoundingClientRect()
  if (!rect.width) return
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  // 只在渲染窗口内取下标（量到容器宽度前窗口＝最近一屏）
  const index = renderStart.value + Math.round(ratio * Math.max(1, renderCount.value - 1))
  hoverIndex.value = Math.min(pointCount.value - 1, Math.max(renderStart.value, index))
}

function clearHover() {
  hoverIndex.value = null
}

const hoverInfo = computed(() => {
  const index = hoverIndex.value
  if (index === null || !hasData.value) return null

  const entries = props.series
    .map((item, seriesIndex) => ({
      name: item.name ?? `序列 ${seriesIndex + 1}`,
      value: item.values?.[index],
      colors: chartToneAt(seriesIndex),
    }))
    .filter((entry) => typeof entry.value === 'number' && Number.isFinite(entry.value))

  if (!entries.length) return null

  const ys = entries.map((entry) => yAt(entry.value as number))
  const pointX = xAt(index)
  // 滑动模式下 SVG 按 1:1 像素渲染，用 px 定位最准（百分比是按可视宽度算的，会随滚动漂移）；
  // 非滑动模式保持改造前的百分比定位，一个字都没变。
  const left = props.scrollable
    ? `${Math.round(Math.min(Math.max(pointX, SCROLL_PAD_LEFT), Math.max(SCROLL_PAD_LEFT, contentWidth.value - PAD.right)))}px`
    : `${Math.min(0.88, Math.max(0.12, pointX / VB_WIDTH)) * 100}%`
  // 滑动模式下滚动区有 padding-top（给提示框留出上探空间），百分比按 padding box 高度算
  const topOffset = props.scrollable ? SCROLL_TOOLTIP_ROOM : 0
  const topBox = vbHeight.value + topOffset
  return {
    index,
    left,
    top: `${((topOffset + Math.min(...ys)) / topBox) * 100}%`,
    guideX: pointX,
    label: props.labels[index] !== undefined ? String(props.labels[index]) : `第 ${index + 1} 项`,
    entries: entries.map((entry) => ({
      name: entry.name,
      text: `${formatAxisValue(entry.value as number)}${props.valueSuffix}`,
      dotStyle: { backgroundColor: entry.colors.stroke },
    })),
  }
})
</script>

<template>
  <!-- 根节点沿用改造前的 class：外部 flex / grid 上下文里的尺寸计算与以前完全一致 -->
  <div class="relative w-full">
    <!-- 尺寸 / 滚动 host：滑动模式下的渐隐提示挂在这里（它不能跟着内容滚） -->
    <div class="relative">
      <!-- 图表视口：滑动模式下它就是横向滚动容器（原生 overflow-x，不写手势 JS） -->
      <div
        ref="scrollerEl"
        class="relative w-full"
        :class="props.scrollable ? 'chart-scroller overflow-x-auto overflow-y-hidden' : ''"
        :style="props.scrollable ? { paddingTop: `${SCROLL_TOOLTIP_ROOM}px` } : undefined"
        :tabindex="props.scrollable ? 0 : undefined"
        :role="props.scrollable ? 'group' : undefined"
        :aria-label="props.scrollable ? scrollerLabel : undefined"
        @scroll.passive="onScroll"
      >
        <!-- 空态：明确写「数据不足」，不编造曲线 -->
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
            :class="props.scrollable ? 'block overflow-visible' : 'block h-auto w-full overflow-visible'"
            :viewBox="`0 0 ${vbWidth} ${vbHeight}`"
            :width="props.scrollable ? vbWidth : undefined"
            :height="props.scrollable ? vbHeight : undefined"
            preserveAspectRatio="xMidYMid meet"
            role="img"
          >
            <defs>
              <linearGradient
                v-for="(item, seriesIndex) in geometry"
                :id="`${uid}-fill-${seriesIndex}`"
                :key="`grad-${seriesIndex}`"
                x1="0"
                y1="0"
                x2="0"
                y2="1"
              >
                <stop offset="0%" :stop-color="item.colors.fillFrom" />
                <stop offset="100%" :stop-color="item.colors.fillTo" />
              </linearGradient>
            </defs>

            <!-- 参考带 -->
            <g v-for="(band, bandIndex) in props.bands" :key="`band-${bandIndex}`">
              <rect
                :x="plotLeft"
                :y="Math.min(yAt(band.from), yAt(band.to))"
                :width="plotWidth"
                :height="Math.abs(yAt(band.to) - yAt(band.from))"
                :fill="chartToneAt(bandIndex + 1).fillFrom"
                opacity="0.35"
              />
              <text
                v-if="band.label"
                :x="plotRight - 4"
                :y="Math.min(yAt(band.from), yAt(band.to)) + 11"
                text-anchor="end"
                font-size="9"
                fill="rgba(226, 232, 240, 0.55)"
              >
                {{ band.label }}
              </text>
            </g>

            <!-- 网格与 y 轴刻度 -->
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
              <!--
                滑动模式：y 轴刻度改由图外的固定轴条画（见下方 overlay），
                否则一贴到最新端，刻度就跟着数据滑出视口了。
              -->
              <template v-if="!props.scrollable">
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
              </template>
            </g>

            <!-- x 轴刻度 -->
            <text
              v-for="tick in xTicks"
              :key="`xtick-${tick.index}`"
              :x="tick.x"
              :y="vbHeight - 8"
              :text-anchor="tick.anchor"
              font-size="9.5"
              fill="rgba(148, 163, 184, 0.66)"
              class="num"
            >
              {{ tick.label }}
            </text>

            <!-- 面积 + 折线 + 光晕 -->
            <g v-for="(item, seriesIndex) in geometry" :key="`series-${seriesIndex}`">
              <template v-for="(run, runIndex) in item.runs" :key="`run-${seriesIndex}-${runIndex}`">
                <path
                  v-if="props.showArea && run.area"
                  :d="run.area"
                  :fill="`url(#${uid}-fill-${seriesIndex})`"
                />
                <!-- 光晕：同一条线叠两层，粗而淡 + 细而亮，做出发光感 -->
                <path
                  :d="run.line"
                  fill="none"
                  :stroke="item.colors.glow"
                  :stroke-width="7"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  opacity="0.28"
                />
                <path
                  :d="run.line"
                  fill="none"
                  :stroke="item.colors.stroke"
                  :stroke-width="2.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
                <!-- 点太密（步长 < 7px）就不画圆点，否则糊成一条粗线 -->
                <g v-if="showPoints">
                  <circle
                    v-for="point in run.points"
                    :key="`dot-${seriesIndex}-${point.index}`"
                    :cx="point.x"
                    :cy="point.y"
                    r="2.6"
                    :fill="item.colors.stroke"
                  />
                </g>
              </template>
            </g>

            <!-- 悬浮：竖向参考线 + 高亮点 -->
            <g v-if="hoverInfo">
              <line
                :x1="hoverInfo.guideX"
                :x2="hoverInfo.guideX"
                :y1="plotTop"
                :y2="plotBottom"
                stroke="rgba(161, 241, 255, 0.45)"
                stroke-width="1"
                stroke-dasharray="3 4"
              />
              <template v-for="(item, seriesIndex) in geometry" :key="`hover-${seriesIndex}`">
                <circle
                  v-for="run in item.runs"
                  :key="`hover-run-${seriesIndex}-${run.points[0]?.index ?? 0}`"
                  v-show="run.points.some((point) => point.index === hoverInfo?.index)"
                  :cx="run.points.find((point) => point.index === hoverInfo?.index)?.x ?? 0"
                  :cy="run.points.find((point) => point.index === hoverInfo?.index)?.y ?? 0"
                  r="4.2"
                  :fill="item.colors.stroke"
                  stroke="rgba(5, 6, 15, 0.9)"
                  stroke-width="1.5"
                />
              </template>
            </g>

            <!--
              交互热区。touch-action 必须区分模式：
              非滑动模式下保持 pan-y（改造前行为，不许横向手势抢走页面）；
              滑动模式下必须放开，否则手指在图上左右划不会滚动容器 —— 这也是不用 JS 手势库的代价。
            -->
            <rect
              v-if="props.showTooltip"
              :x="plotLeft"
              :y="plotTop"
              :width="plotWidth"
              :height="plotHeight"
              fill="transparent"
              :style="props.scrollable ? 'touch-action: auto' : 'touch-action: pan-y'"
              @pointermove="onPointerMove"
              @pointerdown="onPointerMove"
              @pointerleave="clearHover"
            />
          </svg>

          <!-- 提示框：跟着 SVG 的坐标走（滑动模式下 SVG 是 1:1 像素，用 px 定位） -->
          <div
            v-if="hoverInfo"
            class="glass-panel-strong pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-[115%] rounded-lg px-2.5 py-1.5 text-[11px] whitespace-nowrap"
            :style="{ left: hoverInfo.left, top: hoverInfo.top }"
          >
            <p class="mb-0.5 text-slate-400">{{ hoverInfo.label }}</p>
            <p
              v-for="entry in hoverInfo.entries"
              :key="entry.name"
              class="num flex items-center gap-1.5 text-slate-100"
            >
              <span class="h-1.5 w-1.5 rounded-full" :style="entry.dotStyle" />
              <span>{{ entry.text }}</span>
            </p>
          </div>
        </template>
      </div>

      <!-- 滑动模式：固定在左侧的 y 轴条 + 两侧渐隐提示（都不跟着内容滚） -->
      <template v-if="props.scrollable && hasData">
        <div
          v-if="props.showGrid"
          class="pointer-events-none absolute inset-y-0 left-0 z-[2] flex"
          :style="{ paddingTop: `${SCROLL_TOOLTIP_ROOM}px` }"
          aria-hidden="true"
        >
          <div class="bg-ink-950/90" :style="{ width: `${SCROLL_AXIS_WIDTH}px` }">
            <svg
              class="block"
              :width="SCROLL_AXIS_WIDTH"
              :height="vbHeight"
              :viewBox="`0 0 ${SCROLL_AXIS_WIDTH} ${vbHeight}`"
            >
              <text
                v-for="(line, lineIndex) in gridLines"
                :key="`pinned-grid-label-${lineIndex}`"
                :x="SCROLL_AXIS_WIDTH - 8"
                :y="line.y + 3.5"
                text-anchor="end"
                font-size="9.5"
                fill="rgba(148, 163, 184, 0.7)"
                class="num"
              >
                {{ line.label }}
              </text>
            </svg>
          </div>
          <div class="w-2.5 bg-gradient-to-r from-ink-950/90 to-transparent" />
        </div>
        <div
          v-if="canScrollRight"
          class="pointer-events-none absolute inset-y-0 right-0 z-[2] w-8 bg-gradient-to-l from-ink-950/80 to-transparent"
          aria-hidden="true"
        />
      </template>
    </div>

    <!-- 滑动模式控件：滑动提示 / 回到最新 / 一屏多少期（没有数据时不显示，避免无效控件） -->
    <div
      v-if="props.scrollable && hasData"
      class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1"
    >
      <p class="text-[11px] text-slate-500">
        <template v-if="canScroll">左右滑动可查看更早 / 更新的期数；</template>
        纵轴为特码 1–49。
      </p>
      <div class="ml-auto flex flex-wrap items-center gap-1.5">
        <button
          v-if="canScroll && !atNewest"
          type="button"
          class="inline-flex min-h-[44px] items-center rounded-full px-3 text-[11px] font-medium text-aqua-200 transition-colors active:bg-white/10"
          @click="snapToNewest(true)"
        >
          回到最新 →
        </button>
        <button
          v-for="option in windowChoices"
          :key="option"
          type="button"
          class="inline-flex min-h-[44px] items-center rounded-full border px-3 text-xs font-medium transition-colors"
          :class="option === zoom
            ? 'border-aqua-300/50 bg-aqua-400/15 text-aqua-200'
            : 'border-white/10 text-slate-400 active:bg-white/10'"
          :aria-pressed="option === zoom ? 'true' : 'false'"
          @click="selectWindow(option)"
        >
          {{ windowLabel(option) }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/*
 * 只作用于滑动模式的滚动容器。
 * overscroll-behavior-x: contain —— 划到两端时不要把横向手势继续传给页面（iOS 的返回手势 / 浏览器后退）。
 */
.chart-scroller {
  overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
}
</style>
