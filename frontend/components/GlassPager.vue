<script setup lang="ts">
/**
 * GlassPager —— 移动优先的琉璃分页器（/history 与 /draws 共用同一实现，防止两页漂移）。
 *
 * 为什么是客户端切片：
 *   GET /api/draws?limit=500 一次就能把整个池子取回来（200 期 ≈ 21 KB），
 *   后端**没有** total 计数字段，所以「共 N 期 / 第 X / Y 页」只有在浏览器里切片才算得准；
 *   翻页不再发请求、不转圈，DOM 永远只有一页的行。
 *
 * 手机口径：
 *   - 按钮一律 ≥ 44px 高（触屏最小可点尺寸），4 个就够：首页 / 上一页 / 下一页 / 末页；
 *     绝不铺一整排页码按钮——那等于把「太长」的毛病换个地方犯；
 *   - 触屏反馈靠 `.glass-control:active`（按下去有位移），不依赖 hover；
 *   - 当前的「第 X / Y 页」是纯文本指示，不是按钮，位置最显眼。
 *
 * 页码安全：
 *   内部 watch 盯住 total / pageSize / page，一旦越界（删到最后一页空了、清空、刷新变短）
 *   就把页码夹回最后一页并 emit 新值，所以**不可能**停在空白页上。
 *   父页面只需要在「刷新后想回到第 1 页」时自己 set page = 1。
 *
 * 动效：翻页后的滚动定位走 useMotion().shouldAnimate —— 系统 prefers-reduced-motion: reduce
 *   或用户手动关特效时用瞬时跳转（scrollIntoView 的 behavior 会覆盖 CSS 的 scroll-behavior，
 *   所以必须在这里显式判断）。
 *
 * Props:
 *   - page         当前页（1 起）。用 v-model:page 双向绑定
 *   - pageSize     每页条数
 *   - total        总条数（客户端切片，精确值）
 *   - unit         计数单位，默认「期」→ 文案「第 41-60 期 / 共 200 期」
 *   - label        无障碍标签（role="group"）
 *   - scrollTarget 可选选择器：翻页后把该元素滚到视口顶部（例如表格容器）
 * Emits:
 *   - update:page  新页码（含自动夹紧后的值）
 */
const props = withDefaults(
  defineProps<{
    /** 当前页（1 起） */
    page: number
    /** 每页条数 */
    pageSize: number
    /** 总条数 */
    total: number
    /** 计数单位（出现在「第 41-60 期 / 共 200 期」里） */
    unit?: string
    /** 无障碍标签 */
    label?: string
    /** 翻页后要滚到视口顶部的元素选择器；留空则不动滚动位置 */
    scrollTarget?: string
  }>(),
  {
    unit: '期',
    label: '列表分页',
    scrollTarget: '',
  },
)

const emit = defineEmits<{
  (event: 'update:page', value: number): void
}>()

const { shouldAnimate } = useMotion()

/** 总页数：空列表也算 1 页，避免出现「第 1 / 0 页」 */
const pageCount = computed(() =>
  Math.max(1, Math.ceil(props.total / Math.max(1, props.pageSize))),
)

/** 夹紧到 [1, pageCount]：非法值（NaN / 0 / 负数）一律落回第 1 页 */
function clampPage(value: number): number {
  const safe = Number.isFinite(value) ? Math.trunc(value) : 1
  return Math.min(Math.max(1, safe), pageCount.value)
}

/** 展示用页码（props.page 理论上已被夹紧，这里再兜一次底防止闪出越界页码） */
const current = computed(() => clampPage(props.page))

const rangeStart = computed(() =>
  props.total ? (current.value - 1) * props.pageSize + 1 : 0,
)
const rangeEnd = computed(() => Math.min(props.total, current.value * props.pageSize))

/** 例：第 41-60 期 / 共 200 期 */
const rangeText = computed(() =>
  props.total
    ? `第 ${rangeStart.value}-${rangeEnd.value} ${props.unit} / 共 ${props.total} ${props.unit}`
    : '',
)
/** 例：第 3 / 10 页 */
const pageText = computed(() => `第 ${current.value} / ${pageCount.value} 页`)

const canPrev = computed(() => current.value > 1)
const canNext = computed(() => current.value < pageCount.value)

/** 翻页后把列表拉回视口（手机上不这么做会停在上一页的页尾，看不到新一页的表头） */
function scrollToList() {
  if (!props.scrollTarget || !import.meta.client) return
  const element = document.querySelector(props.scrollTarget)
  if (!element) return
  element.scrollIntoView({
    behavior: shouldAnimate.value ? 'smooth' : 'auto',
    block: 'start',
  })
}

function go(target: number) {
  const next = clampPage(target)
  if (next === current.value) return
  emit('update:page', next)
  // 等父页面按新页码切完行、DOM 高度稳定之后再滚动，避免滚到一半又跳一下
  nextTick(() => scrollToList())
}

/**
 * 列表缩短（删除 / 清空 / 刷新）后把越界页码夹回最后一页。
 * immediate：首次挂载时若初始 page 就超界（例如刷新后列表变小）也一并纠正。
 */
watch(
  () => [props.total, props.pageSize, props.page] as const,
  () => {
    const next = clampPage(props.page)
    if (next !== props.page) emit('update:page', next)
  },
  { immediate: true },
)
</script>

<template>
  <!-- 空列表整块不渲染；有数据但只有一页时只显示「第 x-y 期 / 共 n 期」，不留一排死按钮 -->
  <div v-if="total > 0" class="flex flex-col gap-2.5" role="group" :aria-label="label">
    <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <p class="num text-xs text-slate-400">
        {{ rangeText }}
      </p>
      <p class="num text-xs text-slate-500" aria-live="polite">
        {{ pageText }}
      </p>
    </div>

    <div v-if="pageCount > 1" class="flex items-stretch gap-1.5">
      <GlassButton
        size="sm"
        variant="glass"
        class="min-h-[44px] shrink-0"
        :disabled="!canPrev"
        aria-label="跳到第一页"
        @click="go(1)"
      >
        首页
      </GlassButton>
      <GlassButton
        size="sm"
        variant="glass"
        class="min-h-[44px] min-w-0 flex-1"
        :disabled="!canPrev"
        aria-label="上一页"
        @click="go(current - 1)"
      >
        上一页
      </GlassButton>
      <GlassButton
        size="sm"
        variant="glass"
        class="min-h-[44px] min-w-0 flex-1"
        :disabled="!canNext"
        aria-label="下一页"
        @click="go(current + 1)"
      >
        下一页
      </GlassButton>
      <GlassButton
        size="sm"
        variant="glass"
        class="min-h-[44px] shrink-0"
        :disabled="!canNext"
        aria-label="跳到最后一页"
        @click="go(pageCount)"
      >
        末页
      </GlassButton>
    </div>
  </div>
</template>
