<script setup lang="ts">
/**
 * LineChart —— 手写 SVG 折线图（无任何图表库）。
 *
 * 用途：特码走势一类的连续序列。支持多序列、参考带、虚线预测延伸段、叠加标记、本期候选段、数据点标注、悬浮提示、数据不足态。
 * 响应式：靠 viewBox + w-full h-auto，宽高比恒定，永不变形；
 *         悬浮提示用百分比定位，因此与 SVG 的缩放完全同步。
 *
 * 横向滑动（scrollable，opt-in，默认关）：
 *   - 每个点占固定像素宽（由「一屏 windowSize 期」反推），SVG 的固有宽度随期数增长；
 *   - 左右手势 / 方向键移动的是「号码光标」（高亮点 + 提示），不是整图自由平移；
 *     光标移出视口时组件才程序化 scrollLeft 跟随，避免和页面纵向滚动抢手势；
 *   - 打开时自动贴到最新一端（右端）并把光标落在最新一期；
 *   - y 轴刻度放在滚动区之外的固定轴条里，不会跟着数据一起滑出视口；
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
import { chartTone, chartToneAt, formatAxisValue } from '~/composables/useChartTheme'

interface LineSeries {
  /** 序列名（提示框里显示） */
  name?: string
  /** 数值序列，null 表示缺失（该点断开） */
  values: Array<number | null>
  /** 色调，缺省按序号自动分配 */
  tone?: ChartTone
  /**
   * 从该下标开始的分段改画虚线（「预测延伸段」，opt-in）。
   * 例：历史 N 点 + 预测点落在下标 N 时传 N —— 第 N-1→N 这段画成虚线，
   * 视觉上像主线的自然延续，而不是一段悬空短杠；缺省整条实线。
   */
  dashedFromIndex?: number | null
  /** 虚线段的色调；缺省沿用本序列 tone（可用来给预测段换色，如琥珀） */
  dashTone?: ChartTone
}

interface ReferenceBand {
  from: number
  to: number
  label?: string
  tone?: ChartTone
}

/**
 * 叠加标记（非时序序列）：在预测列上画候选圆点 + 可选号码标签（不再画横向短杠）。
 * 典型用途：需要落在同一列上对照预测带的少量标记。
 * 若只想画一条整宽参考线，可不传 atIndex（此时不画点，只画水平虚线）。
 */
interface ChartMarker {
  /** 与序列同一单位的 y 值 */
  value: number
  /** 点旁短标签（如号码 07） */
  label?: string
  tone?: ChartTone
  /**
   * 落点 x 下标（期序号）。传入则画在该期；不传则画整宽水平虚线参考（无圆点）。
   */
  atIndex?: number | null
}

/**
 * 本期候选点（opt-in）：接在主序列之后、横向铺开的一小串候选点。
 * 与 ChartMarker 的区别：候选段占用自己的 x 位置（第 i 个候选 =
 * 序列长度 + i），按 x 顺序用虚线连成「主线继续走的一小段」；
 * ChartMarker 则是落在同一预测列上的非时序标记。两者都不传就不绘制。
 * 口径：候选是「本期候选」，不是真实开奖，调用方必须在图例里写明。
 */
interface CandidatePoint {
  /** 与主序列同一单位的 y 值 */
  value: number
  /** 点旁号码短标签（如 07） */
  label?: string
  /** 单点色调（如按是否落在预测带内区分）；缺省用 candidateTone */
  tone?: ChartTone
}

/**
 * 竖列候选点（opt-in）：全部候选共用同一 x 的**单列**排布（不是横向铺开段）。
 * - x：候选列就是**最后一个普通等距下标**（与真实期、预测点共用同一 x 步长），
 *   落在序列绘图区右边界上；其右侧留出号码标签宽度，不与右边缘贴死；
 * - y：默认绘图区内**等距槽位**（只为了让每个号码读得清，纵向不是数值/时间坐标）；
 *   传 candidateColumnByValue = true 时改为 y = yAt(value)：纵轴同单位时这一列即取值的点阵分布；
 * - 连线：从 candidateColumnFromIndex 指定的「最新真实数据点」向每个槽位画虚线扇形；
 * - 标签：槽位间距不足时整列隐藏标签，号码交给光标提示（绝不重叠）。
 * 口径：本期候选，非真实开奖；纵列等距时只为人眼读号码，不表示取值大小。
 */
interface ColumnCandidate {
  /** 与主序列同一单位的取值（仅作记录/提示用；**槽位 y 由等距排布决定，不用它定位**） */
  value: number
  /** 槽位号码短标签（如 07） */
  label?: string
  /** 单点色调（如按是否落在预测带内区分）；缺省用 candidateTone */
  tone?: ChartTone
}

/**
 * 数据点标注（opt-in，默认空 = 不绘制，其余调用方零影响）。
 * 在指定下标的**真实数据点**旁画「圆点 + 文字」：点始终落在该点自身的 y 上
 * （与序列同一单位，组件不做任何换算），文字由调用方原样给出。
 * 典型用途：差值图里把「特码 23」这种**另一个单位**的原值写在最右数据点旁，
 * 让人能在图上核对原值，而不会把差值误读成特码 —— 口径由调用方在文字里写清。
 */
interface PointAnnotation {
  /** 落点 x 下标（期序号），须落在当前绘制窗口内 */
  index: number
  /**
   * 落点 y 值（与序列同一单位）；缺省取第一条在该下标有有效值的序列的取值，
   * 保证点永远贴在自己的数据点上，而不是悬空。
   */
  value?: number
  /** 标注文字，原样展示（如「第 282 期 特码 23｜差值 13」） */
  label: string
  /** 色调，缺省 slate（中性，不占用任何序列色） */
  tone?: ChartTone
}

const props = withDefaults(
  defineProps<{
    series?: LineSeries[]
    /** x 轴刻度文案，长度应与序列一致（候选段可补空串，轴上不画空刻） */
    labels?: Array<string | number>
    /** 渲染高度（逻辑像素，作为 viewBox 高度） */
    height?: number
    /** 参考带（如波动阈值区间） */
    bands?: ReferenceBand[]
    /**
     * 叠加标记（opt-in）。不参与折线连线；只画虚线参考 / 预测列上的候选点。
     * 空数组 = 与改造前一致。
     */
    markers?: ChartMarker[]
    /**
     * 本期候选段（opt-in，默认空 = 不绘制、其余调用方零影响）。
     * 从主序列最后一个数据点之后横向铺开：第 i 个候选落在 x 下标（序列长度 + i），
     * y 与主序列同单位，虚线 + 独立色调画成「主线继续的一小段」（非真实开奖）。
     */
    candidates?: CandidatePoint[]
    /**
     * 竖列候选（opt-in，默认空 = 不绘制、其余调用方零影响）。
     * 全部候选共用同一 x（= 最后一个等距下标），槽位纵向排布，
     * 并各带一条虚线连接线，从「最新真实数据点」扇形发散到该槽位。
     * 纵向默认**等距**（不是数值/时间坐标）；传 candidateColumnByValue = true 时改为按真实取值定位，
     * 形成取值分布的点阵。
     * 横向：与真实期、预测点共用同一等距 x 步长（不再单独让出固定候选带），
     * 只在绘图区右边界之外留出号码标签宽度，保证这一列不贴死右边缘、也不会被裁；
     * 绘图区外不画网格，避免把等距竖列误读成数值轴。
     * 与 candidates（横向铺开段）互斥使用。口径：本期候选，非真实开奖。
     */
    candidateColumn?: ColumnCandidate[]
    /**
     * 竖列候选扇形的起点下标（opt-in，默认 null）。
     * 传「最新真实数据点」的下标（如差值图里最后一段真实差值）→ 虚线扇形从该点发散；
     * 缺省则回落到候选列之前最后一个有效序列点。窗口外则只画列、不画扇形。
     */
    candidateColumnFromIndex?: number | null
    /**
     * 竖列候选的 y 定位口径（opt-in，默认 false = 与改造前逐字节一致）：
     * - false：槽位在绘图区内**等距**排布（纵向不是数值/时间坐标，只为人眼读号码）；
     * - true：槽位 y = `yAt(value)` —— 纵轴本身已是同一单位（如特码 1–49）时，
     *   这一列就是候选取值的**点阵分布**（哪几段密、哪几段空，一眼可见）；
     *   号码接近时标签按确定性规则上下错开，错不开 / 最小步长过小则整列隐藏标签
     *   （号码交给光标提示），绝不重叠。
     */
    candidateColumnByValue?: boolean
    /**
     * 数据点标注（opt-in，默认空 = 不绘制、其余调用方零影响）。
     * 在指定下标的真实数据点旁画「圆点 + 文字」，用来把**另一个单位**的原值
     * 挂在图上（如差值图里标出「特码 23」）。文字原样展示，口径由调用方负责。
     */
    annotations?: PointAnnotation[]
    /** 候选段连线 / 默认点色调（单点可用 point.tone 覆盖）；缺省 emerald */
    candidateTone?: ChartTone
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
     * 滑动模式底部说明里「纵轴…」那句；空字符串则省略。
     * 默认仍写特码 1–49（首页 / 特码走势）；差值图可改成「纵轴为相邻差值」。
     */
    axisHint?: string
    /**
     * 是否开启横向浏览（opt-in，默认关）。
     * 关：所有点压进容器宽度（改造前行为）；
     * 开：每个点占固定像素宽；左右手势移动号码光标，视口随光标程序化跟随。
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
    markers: () => [],
    candidates: () => [],
    candidateColumn: () => [],
    candidateColumnFromIndex: null,
    candidateColumnByValue: false,
    annotations: () => [],
    candidateTone: 'emerald',
    yMin: null,
    yMax: null,
    showDots: true,
    showGrid: true,
    showArea: true,
    showTooltip: true,
    valueSuffix: '',
    emptyText: '数据不足',
    emptyHint: '',
    axisHint: '纵轴为特码 1–49。',
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
/**
 * 候选号码标签所需的最小像素步长：
 * 点距小于它时标签会互相压住，就只画点，号码交给光标提示。
 */
const CANDIDATE_LABEL_MIN_STEP = 6
/**
 * 竖列候选右侧的号码标签留白（viewBox 逻辑像素，opt-in）。
 * 候选列现在和真实期、预测点共用**同一个等距 x 步长**（不再额外让出一条固定「候选带」），
 * 所以绘图区右边界之外只需留出号码标签本身的宽度，避免贴边被裁：
 * 标签写在圆点右侧 COLUMN_LABEL_OFFSET，两位号码再加一点余量 → 26。
 * 无候选列时该留白为 0 → 与改造前（无此 prop 的调用方）逐字节一致。
 */
const COLUMN_LABEL_ROOM = 26
/** 候选列号码标签相对圆点的水平偏移（text-anchor=start，写在圆点右侧） */
const COLUMN_LABEL_OFFSET = 7
/** 候选列槽位在绘图区内的上/下留白（首末槽位不贴绘图区边） */
const COLUMN_SLOT_PAD_TOP = 10
const COLUMN_SLOT_PAD_BOTTOM = 10
/**
 * 竖列扇形连线的弯曲量：把「最新真实数据点 → 每个候选槽位」的直线改成一条
 * 向外（图右）鼓起的二次贝塞尔弧，控制点取两端横向中点再右移 bow。
 * 候选列不再有固定带宽后，扇形横向跨度 = 一个等距 x 步长，会随屏幕/期数在约 7–30px 间变化：
 * 固定的 10px 弯曲在大步长下合适、在小步长下却会顶到候选列上（弧线被压平甚至反折）。
 * 故改为按**实际跨度**取固定比例（全确定性、无 Math.random）：全束连线仍朝同一侧、观感一致；
 * 比例 < 0.5 保证控制点横向恒落在两端之间，弧线不外溢绘图区/内容框，也不压到右侧号码标签。
 * 上/下限沿用原来的 10px 量级，并把步长很小（<6px）时的弯曲兜底到 2px，避免退化成完全笔直。
 */
const COLUMN_FAN_BOW_RATIO = 0.35
const COLUMN_FAN_BOW_MIN = 2
const COLUMN_FAN_BOW_MAX = 10
/** 槽位间距小于它时号码标签必互相压住：整列隐藏标签，号码改由光标提示给出（确定性降级） */
const COLUMN_LABEL_MIN_SPACING = 11
/**
 * 真实值定位（candidateColumnByValue）下标签的确定性错开步长：
 * 按 y 升序排名上下交替（偶数在上、奇数在下），同一侧再按此步长拉开；
 * 同侧仍压不住（超出绘图区 / 循环用尽）→ 整列隐藏标签。全程无随机抖动。
 */
const COLUMN_LABEL_STEP = 10
/**
 * 候选列模式下提示框的右边界预留（px）：
 * 光标默认就落在最右的候选列上，提示框是以触点为中心（-translate-x-1/2）定位的，
 * 若仍按 PAD.right 夹右边界，默认那一屏提示框会被滚动容器裁掉右半截。
 * 只在候选列模式下生效，其余调用方（无此 prop）提示框定位一个字都没变。
 */
const COLUMN_TOOLTIP_ROOM = 96
/** 悬浮提示需要向上探出的高度（滚动区的 padding-top，避免提示被裁掉） */
const SCROLL_TOOLTIP_ROOM = 48
/** 左右滑动累计位移达到该像素（或约 0.65 个点距）才步进一格光标 */
const CURSOR_STEP_MIN_PX = 24
/** 判定横/竖手势的死区（px），避免误触 */
const GESTURE_AXIS_LOCK_PX = 8

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

/** 鼠标短暂悬停（仅桌面；松手后回落到 sticky 光标） */
const hoverIndex = ref<number | null>(null)
/** 粘性号码光标（滑动模式默认停在最新一期）——须在 snapToNewest 之前声明 */
const cursorIndex = ref<number | null>(null)

let resizeObserver: ResizeObserver | null = null

/** 档位 / windowSize 的归一化：非有限值或 <=0 一律当「全部」 */
function normalizeWindow(value: number): number {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return 0
  return Math.max(1, Math.trunc(numeric))
}

const vbHeight = computed(() => Math.max(150, props.height))

/** 折线序列自身的最长长度（不含本期候选段） */
const seriesCount = computed(() =>
  props.series.reduce((max, item) => Math.max(max, item.values?.length ?? 0), 0),
)

/** 竖列候选是否启用（默认空数组 → 关闭，其余调用方零影响） */
const hasColumn = computed(() => (props.candidateColumn?.length ?? 0) > 0)

/** 竖列候选占用的 x 下标（排在横向候选段之后，是全图最后一个可被光标到达的点） */
const columnSlotIndex = computed(
  () => seriesCount.value + (props.candidates?.length ?? 0),
)

/**
 * 绘图区右侧让给候选列号码标签的留白（opt-in）。
 * 候选列本身占用一个**普通 x 槽位**（与真实期同一步长），不再额外挤出一条固定候选带；
 * 这里只保留标签所需的横向留白。无候选列时为 0 → 与改造前完全一致。
 */
const columnLabelRoom = computed(() => (hasColumn.value ? COLUMN_LABEL_ROOM : 0))

/**
 * 全图 x 轴总点数 = 折线序列 + 本期候选段 + 候选列槽位。
 * 候选段接在预测点之后、每个候选占一个 x 步；候选列整列只占**一个** x 槽位。
 * 把它并入总点数，光标 / 滚动宽度 / x 刻度才会覆盖到候选列 —— 否则滑不过去、也会被裁到绘图区外。
 * 两者都为空时恒等于 seriesCount，与改造前完全一致。
 */
const pointCount = computed(
  () => seriesCount.value + (props.candidates?.length ?? 0) + (hasColumn.value ? 1 : 0),
)

const hasData = computed(() =>
  props.series.some((item) =>
    (item.values ?? []).some((v) => typeof v === 'number' && Number.isFinite(v)),
  )
  || (props.candidates ?? []).some(
    (item) => typeof item.value === 'number' && Number.isFinite(item.value),
  )
  || (props.candidateColumn ?? []).some(
    (item) => typeof item.value === 'number' && Number.isFinite(item.value),
  ),
)

/** 最新一期有有效数值的下标（光标默认落点） */
function latestValidIndex(): number | null {
  const count = seriesCount.value
  if (count <= 0 || !hasData.value) return null
  for (let i = count - 1; i >= 0; i -= 1) {
    for (const item of props.series) {
      const raw = item.values?.[i]
      if (typeof raw === 'number' && Number.isFinite(raw)) return i
    }
  }
  return count - 1
}

/**
 * 光标 / 「回到最新」的落点：有候选列或候选段就落在最后一个候选槽位（最新），
 * 否则落在最新一期数据点。两者都为空时与 latestValidIndex 完全一致。
 */
function newestIndex(): number | null {
  if (pointCount.value <= 0 || !hasData.value) return null
  if (hasColumn.value || (props.candidates?.length ?? 0) > 0) return pointCount.value - 1
  return latestValidIndex()
}

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

/**
 * 滑动内容的固有宽度：期数越多越宽，滚动条才有东西可滚。
 *
 * 关键：候选列也是**一个普通等距下标**，所以步长对全部点（含候选列槽位）统一 ——
 *   一屏 = per-1 个等距间隔铺满绘图区（plot = 视口 − 左让位 − 右内边距 − 标签留白），
 *   step = plot / (effectiveWindow − 1)，内容宽 = 左让位 + step·(count−1) + 标签留白 + 右内边距。
 * 无候选列时 labelRoom = 0、count 即全部点数 → 与改造前逐字节一致。
 */
const contentWidth = computed(() => {
  if (!props.scrollable) return VB_WIDTH
  const count = renderCount.value
  const viewport = viewportWidth.value > 0 ? viewportWidth.value : SCROLL_FALLBACK_VIEWPORT
  if (count <= 1) return Math.round(viewport)
  const labelRoom = columnLabelRoom.value
  const plot = Math.max(24, viewport - SCROLL_PAD_LEFT - PAD.right - labelRoom)
  const step = Math.max(SCROLL_MIN_STEP, plot / Math.max(1, effectiveWindow.value - 1))
  return Math.round(SCROLL_PAD_LEFT + step * (count - 1) + labelRoom + PAD.right)
})

const vbWidth = computed(() => (props.scrollable ? contentWidth.value : VB_WIDTH))
const plotLeft = computed(() => (props.scrollable ? SCROLL_PAD_LEFT : PAD.left))
// 候选列模式下，绘图区右边界只让出号码标签留白；无候选列时与改造前一致
const plotRight = computed(() => vbWidth.value - PAD.right - columnLabelRoom.value)
/**
 * 竖列候选的圆点 x：与真实期、预测点共用同一等距步长 —— 就是最后一个下标的位置
 * （= plotRight）。候选列不再有固定带宽/固定间距，右侧的号码标签落在预留的
 * COLUMN_LABEL_ROOM 里，绝不贴边。
 */
const columnX = computed(() => xAt(columnSlotIndex.value))
const plotTop = PAD.top
const plotBottom = computed(() => vbHeight.value - PAD.bottom)
const plotWidth = computed(() => Math.max(1, plotRight.value - plotLeft.value))
const plotHeight = computed(() => Math.max(1, plotBottom.value - plotTop))

/** 每个点的水平像素步长（仅滑动模式有意义） */
const scrollStep = computed(() =>
  props.scrollable && renderCount.value > 1 ? plotWidth.value / (renderCount.value - 1) : 0,
)

/**
 * 每个点的水平像素步长（与是否滑动无关）：
 * 用来判断候选号码标签是否画得下（滑动模式以外的调用方也可能带候选段）。
 */
const pointStep = computed(() =>
  renderCount.value > 1 ? plotWidth.value / (renderCount.value - 1) : 0,
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

const scrollerLabel = computed(() => {
  // 无候选段 / 无候选列时保持改造前的措辞（其余调用方的 aria 文案不变）
  const suffix = hasColumn.value
    ? '（末端为竖列候选）'
    : (props.candidates?.length ?? 0) > 0
      ? '（末端为本期候选段）'
      : ''
  if (!suffix) {
    return `走势折线图，共 ${pointCount.value} 期；左右滑动可移动号码光标，查看更早或更新的期数`
  }
  return `走势折线图，共 ${pointCount.value} 个点${suffix}；左右滑动可移动号码光标，查看更早或更新的位置`
})

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

/** 贴到最新一端（右端）并把号码光标落到最新位置（有候选段则落在最后一个候选） */
function snapToNewest(smooth = false) {
  const latest = newestIndex()
  if (latest !== null) cursorIndex.value = latest
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
  for (const marker of props.markers) {
    if (typeof marker.value === 'number' && Number.isFinite(marker.value)) {
      pool.push(marker.value)
    }
  }
  // 候选段的 y 值也要进值域，否则候选会被裁到绘图区外
  for (const candidate of props.candidates ?? []) {
    if (typeof candidate.value === 'number' && Number.isFinite(candidate.value)) {
      pool.push(candidate.value)
    }
  }
  // 竖列候选按真实值定位时（candidateColumnByValue），取值也要进值域，否则会被裁到绘图区外；
  // 等距排布模式不使用 value 定位，故不并入 —— 与改造前的值域逐字节一致。
  if (props.candidateColumnByValue) {
    for (const candidate of props.candidateColumn ?? []) {
      if (typeof candidate.value === 'number' && Number.isFinite(candidate.value)) {
        pool.push(candidate.value)
      }
    }
  }
  // 标注若显式给了 y 值，也要进值域（通常标注就落在数据点上，已在值域内）
  for (const note of props.annotations ?? []) {
    if (typeof note.value === 'number' && Number.isFinite(note.value)) {
      pool.push(note.value)
    }
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

/**
 * 统一下标 → x 映射：**所有**下标（真实期、预测点、候选段、候选列槽位）
 * 共用同一个等距步长 plotWidth / (renderCount − 1)，不再对候选列做特殊定位 ——
 * 相邻 x 间距处处相等。候选列槽位就是最后一个下标，故自然落在
 * plotLeft + plotWidth = plotRight（= columnX）上。
 * 只有 1 个点时居中；其余与非候选列场景下的公式逐字节一致。
 */
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

interface PathPoint {
  x: number
  y: number
}

/**
 * SVG 路径指令：起点恒等于上一条指令的终点，因此从任意指令边界切开都严丝合缝。
 * index = 该指令的**起始数据点**下标（M = 首点、C = 该段起点、L = 起点），
 * 用于定位「交界点在哪个数据点」。
 *
 * 注意 index 取「起点」而非「终点」：交界数据点 p[j] 是「以 j 为起点的那一段」的起点，
 * 因此实线尾必然仍穿过 p[j]（p[j] 是曲线的端点），交界处的圆点才落在实线上。
 */
interface PathCommand {
  type: 'M' | 'C' | 'L'
  index: number
  start: PathPoint
  /** 三次贝塞尔的两枚控制点（仅 C 指令） */
  ctrl1?: PathPoint
  ctrl2?: PathPoint
  end: PathPoint
}

/**
 * Catmull-Rom → 三次贝塞尔控制点（均匀参数化，张力固定 1/6，无需额外调参）。
 * 段 p[i] → p[i+1] 的切线由相邻点决定：
 *   ctrl1 = p[i]   + (p[i+1] − p[i−1]) / 6
 *   ctrl2 = p[i+1] − (p[i+2] − p[i])   / 6
 * 端点没有外侧邻居时钳位为自身（p[-1] = p[0]、p[n] = p[n-1]）——「重复端点」处理，
 * 确定性、无随机、无额外状态。曲线以数据点为端点，故**每个数据点都在曲线上**。
 */
function catmullRomControls(points: PathPoint[], i: number) {
  const p0 = points[i]
  const p1 = points[i + 1]
  const prev = points[i - 1] ?? p0
  const next = points[i + 2] ?? p1
  return {
    ctrl1: { x: p0.x + (p1.x - prev.x) / 6, y: p0.y + (p1.y - prev.y) / 6 },
    ctrl2: { x: p1.x - (next.x - p0.x) / 6, y: p1.y - (next.y - p0.y) / 6 },
  }
}

/**
 * 平滑折线：Catmull-Rom 转三次贝塞尔（C 指令）。
 * 与旧的「中点二次贝塞尔」不同，这里每个数据点都是曲线端点，所以画点必然落在线上。
 */
function linePath(points: PathPoint[]) {
  if (!points.length) return ''
  if (points.length === 1) return `M ${round(points[0].x)} ${round(points[0].y)}`
  if (points.length === 2) {
    // 只有两个点、没有内侧邻居可参照：退化成直线，保持视觉笔直
    return `M ${round(points[0].x)} ${round(points[0].y)} L ${round(points[1].x)} ${round(points[1].y)}`
  }
  let d = `M ${round(points[0].x)} ${round(points[0].y)}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const { ctrl1, ctrl2 } = catmullRomControls(points, i)
    const end = points[i + 1]
    d += ` C ${round(ctrl1.x)} ${round(ctrl1.y)} ${round(ctrl2.x)} ${round(ctrl2.y)} ${round(end.x)} ${round(end.y)}`
  }
  return d
}

/** 与 linePath 同构的指令序列（同样的 M / C / L 顺序与同一批坐标）。 */
function lineCommands(points: PathPoint[]): PathCommand[] {
  if (!points.length) return []
  const cmds: PathCommand[] = [{ type: 'M', index: 0, start: points[0], end: points[0] }]
  if (points.length === 1) return cmds
  if (points.length === 2) {
    cmds.push({ type: 'L', index: 0, start: points[0], end: points[1] })
    return cmds
  }
  for (let i = 0; i < points.length - 1; i += 1) {
    const { ctrl1, ctrl2 } = catmullRomControls(points, i)
    cmds.push({
      type: 'C',
      index: i,
      start: cmds[cmds.length - 1].end,
      ctrl1,
      ctrl2,
      end: points[i + 1],
    })
  }
  return cmds
}

function commandToPath(cmd: PathCommand) {
  if (cmd.type === 'M') return `M ${round(cmd.end.x)} ${round(cmd.end.y)}`
  if (cmd.type === 'L') return `L ${round(cmd.end.x)} ${round(cmd.end.y)}`
  const ctrl1 = cmd.ctrl1 as PathPoint
  const ctrl2 = cmd.ctrl2 as PathPoint
  return `C ${round(ctrl1.x)} ${round(ctrl1.y)} ${round(ctrl2.x)} ${round(ctrl2.y)} ${round(cmd.end.x)} ${round(cmd.end.y)}`
}

function commandsToPath(cmds: PathCommand[]) {
  return cmds.map(commandToPath).join(' ')
}

function midPoint(a: PathPoint, b: PathPoint): PathPoint {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

/**
 * 先对整段做一次平滑，再在「第 junction 个数据点」处把曲线精确切成实线尾 + 虚线头。
 *
 * 交界点落在「以 junction 为起点的那条三次贝塞尔」上（见 PathCommand.index 说明），
 * 在该贝塞尔 t = 0.5 处用 de Casteljau 二分（1 条 C → 2 条 C）：
 * - 两个子路径拼接后与原曲线逐点重合，交点处坐标完全相同（无缝隙、无重叠跳变）；
 * - 二分处两侧切线都等于 3·(e − mid)，方向一致，故交界处 C1 连续
 *   （虚线是主线的真正延续，而不是另行画的一条直线段）。
 */
function splitCubicAtHalf(cmd: PathCommand) {
  const ctrl1 = cmd.ctrl1 as PathPoint
  const ctrl2 = cmd.ctrl2 as PathPoint
  const a = midPoint(cmd.start, ctrl1)
  const b = midPoint(ctrl1, ctrl2)
  const c = midPoint(ctrl2, cmd.end)
  const d = midPoint(a, b)
  const e = midPoint(b, c)
  const mid = midPoint(d, e)
  return {
    head: { type: 'C', index: cmd.index, start: cmd.start, ctrl1: a, ctrl2: d, end: mid } as PathCommand,
    tail: { type: 'C', index: cmd.index, start: mid, ctrl1: e, ctrl2: c, end: cmd.end } as PathCommand,
    mid,
  }
}

function splitCommands(points: PathPoint[], junction: number) {
  const cmds = lineCommands(points)
  const splitAt = cmds.findIndex((cmd) => cmd.index === junction)
  if (splitAt < 0) {
    return { solid: commandsToPath(cmds), dashed: '', split: null as PathPoint | null }
  }
  const solid = cmds.slice(0, splitAt)
  const head = cmds[splitAt]
  const tail = cmds.slice(splitAt + 1)
  if (head.type === 'M') {
    // 交界点 = 首个数据点：实线只剩这个点，虚线从它整条重画
    return {
      solid: commandsToPath([...solid, head]),
      dashed: commandsToPath([head, ...tail]),
      split: head.end,
    }
  }
  if (head.type === 'C') {
    const { head: firstHalf, tail: secondHalf, mid } = splitCubicAtHalf(head)
    return {
      solid: commandsToPath([...solid, firstHalf]),
      dashed: commandsToPath([
        { type: 'M', index: head.index, start: mid, end: mid },
        secondHalf,
        ...tail,
      ]),
      split: mid,
    }
  }
  // L 指令（防御性：junction 正常不会落在末点）
  const cut = midPoint(head.start, head.end)
  return {
    solid: commandsToPath([
      ...solid,
      { type: 'L', index: head.index, start: head.start, end: cut },
    ]),
    dashed: commandsToPath([
      { type: 'M', index: head.index, start: cut, end: cut },
      { type: 'L', index: head.index, start: cut, end: head.end },
      ...tail,
    ]),
    split: cut,
  }
}

const geometry = computed(() =>
  props.series.map((item, seriesIndex) => {
    const values = item.values ?? []
    const solidColors = item.tone ? chartTone(item.tone) : chartToneAt(seriesIndex)
    const dashColors = item.dashTone ? chartTone(item.dashTone) : solidColors
    const dashFrom =
      typeof item.dashedFromIndex === 'number' && Number.isFinite(item.dashedFromIndex)
        ? Math.trunc(item.dashedFromIndex)
        : null

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

    // 按 dashedFromIndex 把每段连续折线切成「实线段 + 虚线段」：
    // 先对**整段**做一次平滑，再在交界数据点处精确二分曲线（splitCommands），
    // 这样虚线是主线曲线的真正延续（交点重合、切线一致），而不是各画各的两条线。
    const segments: Array<{
      points: Array<{ x: number; y: number; value: number; index: number }>
      dashed: boolean
      colors: ReturnType<typeof chartTone>
      line: string
      area: string
    }> = []
    for (const run of runs) {
      const cut = dashFrom === null ? -1 : run.findIndex((point) => point.index >= dashFrom)
      if (cut <= 0) {
        // 无切点（整段实线）或整段都落在虚线区：与改造前逐字节一致
        const isDashed = cut === 0
        segments.push({
          points: run,
          dashed: isDashed,
          colors: isDashed ? dashColors : solidColors,
          line: linePath(run),
          // 虚线段（预测延伸）不填面积，避免在末端鼓出一块三角
          area:
            !isDashed && run.length > 1
              ? `${linePath(run)} L ${round(run[run.length - 1].x)} ${round(plotBottom.value)} L ${round(run[0].x)} ${round(plotBottom.value)} Z`
              : '',
        })
        continue
      }
      // 交界数据点 = run[cut - 1]（实线尾与虚线头共用）；split 是曲线上的精确切点
      const split = splitCommands(run, cut - 1)
      const solidPoints = run.slice(0, cut)
      segments.push({
        points: solidPoints,
        dashed: false,
        colors: solidColors,
        line: split.solid,
        area:
          split.split && solidPoints.length > 1
            ? `${split.solid} L ${round(split.split.x)} ${round(plotBottom.value)} L ${round(solidPoints[0].x)} ${round(plotBottom.value)} Z`
            : '',
      })
      segments.push({
        points: run.slice(cut - 1),
        dashed: true,
        colors: dashColors,
        line: split.dashed,
        area: '',
      })
    }

    return {
      name: item.name ?? `序列 ${seriesIndex + 1}`,
      colors: solidColors,
      runs: segments,
    }
  }),
)

/**
 * 标记几何：
 * - 有 atIndex → 预测列上的候选圆点（实心点 + 号码标签，不再画横向短杠）；
 * - 无 atIndex → 整宽水平虚线参考线（不画点，保持改造前语义）。
 */
const markerGeometry = computed(() => {
  const maxIndex = Math.max(0, pointCount.value - 1)
  // 先按 y 值从大到小排，标签再向上错开时更少互相踩
  const ordered = props.markers
    .map((marker, markerIndex) => ({ marker, markerIndex }))
    .filter(({ marker }) => typeof marker.value === 'number' && Number.isFinite(marker.value))
    .sort((a, b) => (b.marker.value as number) - (a.marker.value as number))

  const out: Array<{
    key: string
    y: number
    x: number
    colors: ReturnType<typeof chartTone>
    label: string
    showDot: boolean
    labelY: number
  }> = []
  const usedYs: number[] = []
  for (const { marker, markerIndex } of ordered) {
    const raw = marker.value as number
    const hasIndex = typeof marker.atIndex === 'number' && Number.isFinite(marker.atIndex)
    let index: number | null = null
    if (hasIndex) {
      index = Math.min(maxIndex, Math.max(0, Math.trunc(marker.atIndex as number)))
      // 渲染窗口外（SSR 首帧只画最近一屏）时跳过，避免画到负坐标
      if (index < renderStart.value) continue
    }
    const y = yAt(raw)
    const colors = marker.tone ? chartTone(marker.tone) : chartTone('bloom')
    const x = index !== null ? xAt(index) : plotRight.value - 4
    // 标签向上错开：同一列上多个候选点时让号码不互相压
    let labelY = y - 7
    for (const prev of usedYs) {
      if (Math.abs(prev - labelY) < 10) labelY = prev - 10
    }
    usedYs.push(labelY)
    out.push({
      key: `mk-${markerIndex}-${raw}`,
      y,
      x,
      colors,
      label: marker.label ? String(marker.label) : '',
      showDot: index !== null,
      labelY,
    })
  }
  return out
})

/**
 * 预测列（有 atIndex 的标记所在期号）的竖向导引 x。
 * 候选点都落在同一列时，一条极淡的竖线能让它们读作「同一期」的分布，
 * 而不是散点。取第一条标记的 x，确定性、不做随机抖动。
 */
const predictionGuideX = computed(() => {
  const first = markerGeometry.value.find((marker) => marker.showDot)
  return first ? first.x : null
})

/**
 * 数据点标注几何：把调用方给的文字挂在指定下标的真实数据点上。
 * - 落点 y：优先用调用方给的 value，否则回落到第一条在该下标有有效值的序列；
 * - 文字：原样展示（口径由调用方负责），按点在图中的左右半区决定朝内还是朝外，
 *   避免贴边被裁；上边距夹在绘图区内；
 * - 窗口外的标注跳过（滑动模式 SSR 首帧只画最近一屏）。
 * annotations 为空时返回空数组，模板整段不渲染（其余调用方零影响）。
 */
const annotationGeometry = computed(() => {
  const out: Array<{
    key: string
    x: number
    y: number
    textX: number
    textY: number
    anchor: 'start' | 'end'
    colors: ReturnType<typeof chartTone>
    label: string
  }> = []
  const list = props.annotations ?? []
  if (!list.length) return out
  const last = pointCount.value - 1
  list.forEach((note, noteIndex) => {
    const rawIndex = Number(note.index)
    if (!Number.isFinite(rawIndex)) return
    const index = Math.trunc(rawIndex)
    if (index < renderStart.value || index > last) return
    let value: number | null | undefined = note.value
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      for (const item of props.series) {
        const seriesValue = item.values?.[index]
        if (typeof seriesValue === 'number' && Number.isFinite(seriesValue)) {
          value = seriesValue
          break
        }
      }
    }
    if (typeof value !== 'number' || !Number.isFinite(value)) return
    const x = xAt(index)
    const y = yAt(value)
    // 右半区朝内（向左写），左半区朝外（向右写），两端都不会越出绘图区太多
    const anchor: 'start' | 'end' = x > plotLeft.value + plotWidth.value / 2 ? 'end' : 'start'
    out.push({
      key: `note-${noteIndex}-${index}`,
      x,
      y,
      textX: anchor === 'end' ? x - 9 : x + 9,
      textY: Math.min(plotBottom.value - 4, Math.max(plotTop + 10, y - 9)),
      anchor,
      colors: note.tone ? chartTone(note.tone) : chartTone('slate'),
      label: note.label ? String(note.label) : '',
    })
  })
  return out
})

/**
 * 本期候选段几何：接在主序列最后一个数据点之后横向铺开的一串候选点。
 * - x：第 i 个候选 = 序列长度 + i（与主折线同一标尺，逐个向右排开）；
 * - y：与主序列同一单位的取值（调用方传什么就画什么，不做二次换算）；
 * - 连线：从候选段之前最后一个有效数据点（预测点）接出去，画成虚线延续段 ——
 *   读起来是「主线继续走的一小段」，而不是落在同一列上的竖向堆叠；
 * - 号码标签：在点旁上下交替错开，并做一次确定性去重叠（不做 Math.random 抖动）。
 * 候选段为空时返回 null，模板整段不渲染（其余调用方零影响）。
 */
const candidateGeometry = computed(() => {
  const list = props.candidates ?? []
  if (!list.length) return null
  const start = seriesCount.value
  const points = list
    .map((item, i) => ({
      index: start + i,
      value: item.value,
      label: item.label ? String(item.label) : '',
      tone: item.tone ? chartTone(item.tone) : chartTone(props.candidateTone),
      /** 0 = 号码标签在点上方，1 = 在下方（相邻交替，先避开两两相撞） */
      side: i % 2,
    }))
    .filter(
      (item) =>
        typeof item.value === 'number'
        && Number.isFinite(item.value)
        && item.index >= renderStart.value,
    )
  if (!points.length) return null

  // 起点接预测点：候选段之前最后一个有效数据点（缺失时让候选段自己成段）
  let anchor: { x: number; y: number } | null = null
  const anchorIndex = start - 1
  if (anchorIndex >= renderStart.value) {
    for (const item of props.series) {
      const raw = item.values?.[anchorIndex]
      if (typeof raw === 'number' && Number.isFinite(raw)) {
        anchor = { x: xAt(anchorIndex), y: yAt(raw) }
        break
      }
    }
  }

  const top = plotTop + 9
  const bottom = plotBottom.value - 2
  const usedTop: number[] = []
  const usedBottom: number[] = []
  const placed = points.map((point) => {
    const pool = point.side === 0 ? usedTop : usedBottom
    const initial = point.side === 0 ? yAt(point.value) - 7 : yAt(point.value) + 13
    let labelY = Math.min(bottom, Math.max(top, initial))
    let guard = 0
    while (guard < 12 && pool.some((prev) => Math.abs(prev - labelY) < 10)) {
      const next = Math.min(bottom, Math.max(top, labelY + (point.side === 0 ? -10 : 10)))
      if (next === labelY) break
      labelY = next
      guard += 1
    }
    pool.push(labelY)
    return { ...point, x: xAt(point.index), y: yAt(point.value), labelY }
  })

  const polyline = placed.map((point) => ({ x: point.x, y: point.y }))
  return {
    points: placed,
    line: linePath(anchor ? [anchor, ...polyline] : polyline),
    colors: chartTone(props.candidateTone),
    // 点距太窄时标签会糊成一片：这时只画点，号码交给光标提示
    showLabels: pointStep.value >= CANDIDATE_LABEL_MIN_STEP,
  }
})

/**
 * 竖列候选几何（opt-in，默认空 = 不绘制）。
 * - x：全部候选共用 columnX = 最后一个等距下标（与真实期、预测点同一步长，是**单列**、不是横向铺开）；
 * - y：默认绘图区内**等距槽位**（slot 0 在上、slot n-1 在下）—— 纵向不是数值/时间坐标，
 *   只为让每个号码读得清；candidateColumnByValue = true 时改为按真实取值 yAt(value) 定位，
 *   纵轴同单位时这一列就是取值的点阵分布；调用方传的 value 两种模式都保留给提示框；
 * - 扇形连线：从 candidateColumnFromIndex 指定的「最新真实数据点」→ 每个槽位，虚线；
 * - 标签：真实值模式下按 y 升序上下交替 + 固定步长确定性错开；最小步长过小或错不开时
 *   整列隐藏标签，号码交给光标提示（绝不重叠）。
 * candidateColumn 为空时返回 null，模板整段不渲染（其余调用方零影响）。
 */
const columnGeometry = computed(() => {
  const list = (props.candidateColumn ?? []).filter(
    (item) => typeof item.value === 'number' && Number.isFinite(item.value),
  )
  if (!list.length) return null
  const count = list.length
  const byValue = props.candidateColumnByValue === true
  const top = plotTop + COLUMN_SLOT_PAD_TOP
  const bottom = plotBottom.value - COLUMN_SLOT_PAD_BOTTOM
  const span = Math.max(1, bottom - top)
  const spacing = count > 1 ? span / (count - 1) : span
  const x = columnX.value
  const points = list.map((item, i) => ({
    key: `col-${i}`,
    x,
    // 槽位 y：默认等距（纵向只为人眼读号码，不是数值轴）；
    // candidateColumnByValue = true 时按真实取值 yAt(value)（纵轴同单位 → 这一列就是取值的点阵分布），
    // 并夹在绘图区内（防御：取值越界时不画到图外）。value 两种模式都保留给光标提示。
    y: byValue
      ? Math.min(bottom, Math.max(top, yAt(item.value)))
      : (count <= 1 ? (top + bottom) / 2 : top + (span * i) / (count - 1)),
    value: item.value,
    label: item.label ? String(item.label) : '',
    tone: item.tone ? chartTone(item.tone) : chartTone(props.candidateTone),
    /** 标签基线 y：等距模式沿用改造前的「圆点右侧、纵向居中」（下方赋值）；
     *  真实值模式由下方确定性错开给出；错不开 / 步长过小则整列隐藏标签。 */
    labelY: 0,
  }))

  /**
   * 标签「最小步长」：默认等距模式 = 槽位间距；
   * 真实值模式 = 相邻取值的最小纵向间距（号码挤在一起时会很小）。
   * 小于 COLUMN_LABEL_MIN_SPACING 时标签必互相压住 → 整列隐藏（号码交给光标提示）。
   */
  let minStep = spacing
  if (byValue && count > 1) {
    const ys = points.map((point) => point.y).sort((a, b) => a - b)
    minStep = Infinity
    for (let i = 1; i < ys.length; i += 1) minStep = Math.min(minStep, ys[i] - ys[i - 1])
  }
  let separated = true
  if (byValue) {
    // 真实值定位：圆点必须留在真实取值上，只把**标签**错开 ——
    // 按 y 升序排名上下交替（偶数在上、奇数在下），同一侧按固定步长拉开；
    // 全部确定性（无 Math.random）。同侧仍压不住则整列隐藏标签，绝不重叠。
    const ranked = points
      .map((point, i) => ({ i, y: point.y }))
      .sort((a, b) => a.y - b.y || a.i - b.i)
    const usedTop: number[] = []
    const usedBottom: number[] = []
    for (let rank = 0; rank < ranked.length; rank += 1) {
      const point = points[ranked[rank].i]
      const above = rank % 2 === 0
      const pool = above ? usedTop : usedBottom
      const step = above ? -COLUMN_LABEL_STEP : COLUMN_LABEL_STEP
      const initial = point.y + (above ? -8 : 13)
      let labelY = Math.min(bottom, Math.max(top, initial))
      let guard = 0
      while (guard < 12 && pool.some((prev) => Math.abs(prev - labelY) < COLUMN_LABEL_STEP)) {
        const next = Math.min(bottom, Math.max(top, labelY + step))
        if (next === labelY) break
        labelY = next
        guard += 1
      }
      if (pool.some((prev) => Math.abs(prev - labelY) < COLUMN_LABEL_STEP)) separated = false
      pool.push(labelY)
      point.labelY = labelY
    }
  } else {
    // 默认等距模式：与改造前逐字节一致 —— 标签就写在圆点右侧、纵向居中
    for (const point of points) point.labelY = point.y + 3.2
  }

  // 扇形起点：优先用调用方显式给的「最新真实数据点」下标；缺省回落到列槽前最后一个有效序列点。
  // 下标必须落在当前渲染窗口内（SSR 首帧只画最近一屏），否则只画列、不画扇形。
  let fromIndex: number | null = null
  const explicit = props.candidateColumnFromIndex
  if (typeof explicit === 'number' && Number.isFinite(explicit)) {
    const idx = Math.trunc(explicit)
    if (idx >= renderStart.value && idx < columnSlotIndex.value) fromIndex = idx
  } else {
    for (let i = columnSlotIndex.value - 1; i >= renderStart.value; i -= 1) {
      const hit = props.series.some((item) => {
        const raw = item.values?.[i]
        return typeof raw === 'number' && Number.isFinite(raw)
      })
      if (hit) {
        fromIndex = i
        break
      }
    }
  }
  let from: { x: number; y: number } | null = null
  if (fromIndex !== null) {
    for (const item of props.series) {
      const raw = item.values?.[fromIndex]
      if (typeof raw === 'number' && Number.isFinite(raw)) {
        from = { x: xAt(fromIndex), y: yAt(raw) }
        break
      }
    }
  }

  return {
    x,
    points,
    from,
    count,
    // 间距足够、且真实值模式下标签能错开才画号码；否则整列只画点，号码交给光标提示（确定性降级，绝不重叠）
    showLabels: count <= 1 || (minStep >= COLUMN_LABEL_MIN_SPACING && separated),
  }
})

/**
 * 竖列扇形连线：从「最新真实数据点」到单个候选槽位的**向外鼓起**的二次贝塞尔弧（M … Q …）。
 * - 端点严格是 (point.x, point.y)：弧线仍精确收在每个槽位圆点上，只是中途鼓起；
 * - 弯曲量 bow 按实际横向跨度（= 一个等距 x 步长）取比例，并夹在 [MIN, MAX]；
 *   控制点 = 两端横向中点向右平移 bow，纵向取两端中点；
 *   因比例 0.35 < 0.5，控制点横向恒落在 [from.x, point.x] 内，纵向落在两端之间，
 *   由二次贝塞尔的凸包性质 ⇒ 弧线不外溢绘图区/内容框，也不压到右侧号码标签（标签从 point.x + 7 起）；
 * - 全确定性（无 Math.random），同一端点多次渲染逐点一致。
 */
function fanArcPath(from: PathPoint, point: PathPoint) {
  const span = Math.max(0, point.x - from.x)
  const bow = Math.max(COLUMN_FAN_BOW_MIN, Math.min(COLUMN_FAN_BOW_MAX, span * COLUMN_FAN_BOW_RATIO))
  const ctrlX = Math.min(point.x, (from.x + point.x) / 2 + bow)
  const ctrlY = Math.min(plotBottom.value, Math.max(plotTop, (from.y + point.y) / 2))
  return `M ${round(from.x)} ${round(from.y)} Q ${round(ctrlX)} ${round(ctrlY)} ${round(point.x)} ${round(point.y)}`
}

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
      const raw = props.labels[i]
      // 候选段补的空标签不在轴上画空刻（否则右端会多出一道无名刻度）
      if (raw === '') continue
      ticks.push({
        index: i,
        x: xAt(i),
        label: String(raw ?? i + 1),
        anchor: i === total - 1 ? 'end' : 'middle',
      })
    }
    return ticks
  }

  const maxLabels = 8
  const step = Math.max(1, Math.ceil(total / maxLabels))
  for (let i = 0; i < total; i += step) {
    const raw = props.labels[i]
    if (raw === '') continue
    ticks.push({
      index: i,
      x: xAt(i),
      label: String(raw ?? i + 1),
      anchor: 'middle',
    })
  }
  const lastIndex = total - 1
  if (ticks[ticks.length - 1]?.index !== lastIndex && props.labels[lastIndex] !== '') {
    ticks.push({
      index: lastIndex,
      x: xAt(lastIndex),
      label: String(props.labels[lastIndex] ?? lastIndex + 1),
      anchor: 'end',
    })
  }
  return ticks
})

/* -------------------------------------------------------------------------- */
/* 号码光标：滑动模式下左右手势移动的是它（非整图自由平移）                      */
/* -------------------------------------------------------------------------- */

/** 当前高亮：悬停优先，否则用粘性光标（滑动模式始终有光标） */
const focusIndex = computed(() => {
  if (hoverIndex.value !== null) return hoverIndex.value
  if (props.scrollable) return cursorIndex.value
  return null
})

/** 光标落在候选段时对应的候选点（用于高亮点 + 提示框里补一条候选号码） */
const hoverCandidate = computed(() => {
  const index = focusIndex.value
  if (index === null) return null
  return candidateGeometry.value?.points.find((point) => point.index === index) ?? null
})

/** 光标落在竖列候选槽位时，整列几何（用于整体高亮 + 提示框标注） */
const hoverColumn = computed(() => {
  const index = focusIndex.value
  if (index === null || !hasColumn.value) return null
  if (index !== columnSlotIndex.value) return null
  return columnGeometry.value
})

const gesture = reactive({
  active: false,
  pointerId: -1,
  startX: 0,
  startY: 0,
  /** 锁轴：null=未判定，x=横向移光标，y=交给页面纵向滚动 */
  axis: null as null | 'x' | 'y',
  /** 横向累计位移，满一格步进一次 */
  acc: 0,
  /** 本次按下是否发生过横向步进（有则不算点击落点） */
  stepped: false,
})

function clampIndex(index: number): number {
  const max = Math.max(0, pointCount.value - 1)
  return Math.min(max, Math.max(0, index))
}

function indexFromClientX(clientX: number, target: Element): number | null {
  if (!hasData.value || renderCount.value <= 0) return null
  const rect = target.getBoundingClientRect()
  if (!rect.width) return null
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
  // 候选列模式：热区右边界只比序列绘图区多出一段号码标签留白（plotWidth + COLUMN_LABEL_ROOM），
  // 先按比例还原成相对热区左边缘的像素偏移，再统一按**绘图区宽度**归一化到下标 ——
  // 与 xAt 的等距映射严格一致（候选列就是最后一个普通下标，不再有固定带宽特例）。
  if (hasColumn.value) {
    const domainWidth = plotWidth.value + columnLabelRoom.value
    const svgOffset = ratio * domainWidth
    // 偏移落在绘图区右边界之外（号码标签留白里）→ 直接命中候选列槽位
    if (svgOffset >= plotWidth.value) return clampIndex(columnSlotIndex.value)
    const index = renderStart.value
      + Math.round((svgOffset / Math.max(1, plotWidth.value)) * Math.max(1, renderCount.value - 1))
    return clampIndex(Math.min(columnSlotIndex.value - 1, Math.max(renderStart.value, index)))
  }
  const index = renderStart.value + Math.round(ratio * Math.max(1, renderCount.value - 1))
  return clampIndex(Math.min(pointCount.value - 1, Math.max(renderStart.value, index)))
}

function cursorStepThreshold(): number {
  return Math.max(CURSOR_STEP_MIN_PX, scrollStep.value * 0.65)
}

/** 让粘性光标落在视口内（偏中）；只改 scrollLeft，不跟手拖动画布 */
async function ensureCursorVisible(smooth = false) {
  if (!props.scrollable || !import.meta.client) return
  await nextTick()
  const el = scrollerEl.value
  const index = cursorIndex.value
  if (!el || index === null) return
  const x = xAt(index)
  const viewLeft = el.scrollLeft
  const viewRight = viewLeft + el.clientWidth
  const margin = Math.max(32, scrollStep.value)
  if (x >= viewLeft + margin && x <= viewRight - margin) {
    readScrollState(el)
    return
  }
  const target = Math.max(0, Math.min(
    Math.max(0, el.scrollWidth - el.clientWidth),
    Math.round(x - el.clientWidth * 0.55),
  ))
  const useSmooth = smooth && shouldAnimate.value
  el.scrollTo({ left: target, behavior: useSmooth ? 'smooth' : 'auto' })
  if (useSmooth) {
    scrollState.max = Math.max(0, Math.round(el.scrollWidth - el.clientWidth))
    scrollState.left = target
    return
  }
  readScrollState(el)
}

function setCursor(index: number, pinScroll = true, smooth = false) {
  if (!props.scrollable || pointCount.value <= 0) return
  cursorIndex.value = clampIndex(index)
  if (pinScroll) void ensureCursorVisible(smooth)
}

function stepCursor(delta: number, smooth = false) {
  if (!props.scrollable || pointCount.value <= 0) return
  const base = cursorIndex.value ?? latestValidIndex() ?? 0
  setCursor(base + delta, true, smooth)
}

function onPointerMove(event: PointerEvent) {
  if (!props.showTooltip || !hasData.value || renderCount.value <= 0) return
  // 横向移光标进行中：不要用指尖坐标抢光标，否则会和步进打架
  if (props.scrollable && gesture.active && gesture.axis === 'x') return
  const index = indexFromClientX(event.clientX, event.currentTarget as Element)
  if (index === null) return
  if (props.scrollable) {
    // 滑动模式：悬停只做短暂预览；未按下时同步粘性光标（桌面鼠标扫过）
    hoverIndex.value = index
    if (!gesture.active && event.pointerType === 'mouse') {
      cursorIndex.value = index
    }
  } else {
    hoverIndex.value = index
  }
}

function clearHover() {
  hoverIndex.value = null
}

function onPointerDown(event: PointerEvent) {
  onPointerMove(event)
  if (!props.scrollable || !hasData.value) return
  gesture.active = true
  gesture.pointerId = event.pointerId
  gesture.startX = event.clientX
  gesture.startY = event.clientY
  gesture.axis = null
  gesture.acc = 0
  gesture.stepped = false
  try {
    (event.currentTarget as Element).setPointerCapture?.(event.pointerId)
  } catch {
    /* 部分环境无 capture，忽略 */
  }
}

function onPointerDrag(event: PointerEvent) {
  if (!props.scrollable || !gesture.active || event.pointerId !== gesture.pointerId) {
    onPointerMove(event)
    return
  }
  const dx = event.clientX - gesture.startX
  const dy = event.clientY - gesture.startY
  if (!gesture.axis) {
    if (Math.abs(dx) < GESTURE_AXIS_LOCK_PX && Math.abs(dy) < GESTURE_AXIS_LOCK_PX) return
    gesture.axis = Math.abs(dx) >= Math.abs(dy) ? 'x' : 'y'
    if (gesture.axis === 'x') {
      gesture.startX = event.clientX
      gesture.acc = 0
      hoverIndex.value = null
    }
  }
  if (gesture.axis !== 'x') return
  // 旧→新 = 左→右：手指左滑 → 光标往更新一侧（index+1）；右滑 → 更早（index-1）
  event.preventDefault()
  gesture.acc += event.clientX - gesture.startX
  gesture.startX = event.clientX
  const threshold = cursorStepThreshold()
  while (gesture.acc <= -threshold) {
    gesture.acc += threshold
    stepCursor(1)
    gesture.stepped = true
  }
  while (gesture.acc >= threshold) {
    gesture.acc -= threshold
    stepCursor(-1)
    gesture.stepped = true
  }
}

function onPointerUp(event: PointerEvent) {
  if (!props.scrollable || event.pointerId !== gesture.pointerId) {
    return
  }
  // 轻点：光标落到按下位置对应的号码
  if (!gesture.stepped && gesture.axis !== 'y') {
    const index = indexFromClientX(event.clientX, event.currentTarget as Element)
    if (index !== null) setCursor(index, true)
  }
  gesture.active = false
  gesture.pointerId = -1
  gesture.axis = null
  gesture.acc = 0
  gesture.stepped = false
  hoverIndex.value = null
}

function onKeyNavigate(event: KeyboardEvent) {
  if (!props.scrollable || !hasData.value) return
  if (event.key === 'ArrowLeft') {
    event.preventDefault()
    stepCursor(-1, true)
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    stepCursor(1, true)
  } else if (event.key === 'Home') {
    event.preventDefault()
    setCursor(0, true, true)
  } else if (event.key === 'End') {
    event.preventDefault()
    const newest = newestIndex()
    if (newest !== null) setCursor(newest, true, true)
  }
}

/** 触控板横向滚轮：同样移光标，并拦住原生横向滚动 */
function onWheelNavigate(event: WheelEvent) {
  if (!props.scrollable || !hasData.value) return
  if (Math.abs(event.deltaX) <= Math.abs(event.deltaY) || Math.abs(event.deltaX) < 2) return
  event.preventDefault()
  stepCursor(event.deltaX > 0 ? 1 : -1)
}

// 数据就绪 / 期数变化：光标默认落在最新位置（有候选段则落在最后一个候选；越界则收回）
watch(
  [pointCount, () => props.scrollable, hasData],
  () => {
    if (!props.scrollable || !hasData.value) {
      cursorIndex.value = null
      return
    }
    const newest = newestIndex()
    if (newest === null) {
      cursorIndex.value = null
      return
    }
    if (cursorIndex.value === null || cursorIndex.value > newest) {
      cursorIndex.value = newest
    }
  },
  { immediate: true },
)

const hoverInfo = computed(() => {
  const index = focusIndex.value
  if (index === null || !hasData.value) return null

  const entries: Array<{
    name: string
    value: number
    colors: ReturnType<typeof chartTone>
    numberLabel?: string
    /** 整列候选这类「不是单个取值」的提示：直接给成稿文字，跳过数值格式化 */
    textOverride?: string
  }> = props.series
    .map((item, seriesIndex) => ({
      name: item.name ?? `序列 ${seriesIndex + 1}`,
      value: item.values?.[index],
      colors: item.tone ? chartTone(item.tone) : chartToneAt(seriesIndex),
    }))
    .filter((entry) => typeof entry.value === 'number' && Number.isFinite(entry.value))
    .map((entry) => ({ ...entry, value: entry.value as number }))

  // 光标落在候选段：补一条候选号码提示（号码 + 与主序列同单位的取值）
  const candidate = hoverCandidate.value
  if (candidate) {
    entries.push({
      name: '本期候选（非真实开奖）',
      value: candidate.value,
      colors: candidate.tone,
      numberLabel: candidate.label,
    })
  }

  // 光标落在竖列候选槽位：整列共用一个 x，补一条整列提示（号码多时以图上的竖列标签为准，
  // 标签因槽位太密被隐藏时，这里把号码逐列出，保证光标仍能读到每个候选）
  const column = hoverColumn.value
  if (column) {
    const labels = column.points.map((point) => point.label).filter(Boolean)
    entries.push({
      name: '本期候选（非真实开奖）',
      value: column.points[0]?.value ?? 0,
      colors: chartTone(props.candidateTone),
      textOverride: column.showLabels
        ? `共 ${column.count} 个 · 竖列已标出`
        : `共 ${column.count} 个：${labels.join(' ') || '—'}`,
    })
  }

  if (!entries.length) return null

  // 整列提示不代表单个取值，不参与提示框纵向定位（用列顶槽位单独定位）
  const ys = entries
    .filter((entry) => entry.textOverride === undefined)
    .map((entry) => yAt(entry.value as number))
  let topY = ys.length ? Math.min(...ys) : plotTop
  // 整列提示用列顶槽位定位：等距模式 points[0] 就是列顶（最小值，字节等价）；
  // 真实值模式按取值排序，points[0] 可能是列底，故取整列 y 的最小值。
  if (column?.points.length) {
    topY = Math.min(topY, ...column.points.map((point) => point.y))
  }
  const pointX = xAt(index)
  // 滑动模式下 SVG 按 1:1 像素渲染，用 px 定位最准（百分比是按可视宽度算的，会随滚动漂移）；
  // 非滑动模式保持改造前的百分比定位，一个字都没变。
  // 候选列模式：光标默认就在最右的候选列上，右边界多留一段，避免提示框被滚动容器裁掉。
  const left = props.scrollable
    ? (() => {
        const maxLeft = hasColumn.value
          ? Math.max(SCROLL_PAD_LEFT, contentWidth.value - PAD.right - COLUMN_TOOLTIP_ROOM)
          : Math.max(SCROLL_PAD_LEFT, contentWidth.value - PAD.right)
        return `${Math.round(Math.min(Math.max(pointX, SCROLL_PAD_LEFT), maxLeft))}px`
      })()
    : `${Math.min(0.88, Math.max(0.12, pointX / VB_WIDTH)) * 100}%`
  // 滑动模式下滚动区有 padding-top（给提示框留出上探空间），百分比按 padding box 高度算
  const topOffset = props.scrollable ? SCROLL_TOOLTIP_ROOM : 0
  const topBox = vbHeight.value + topOffset
  // 标题：候选段补的是空标签，这里换成「候选 07」；候选列换成整列标题；其余保持改造前措辞
  const rawLabel = props.labels[index]
  const headerLabel = rawLabel !== undefined && String(rawLabel).trim() !== ''
    ? String(rawLabel)
    : candidate
      ? `候选 ${candidate.label || index + 1}`
      : column
        ? '本期候选（非真实开奖）'
        : `第 ${index + 1} 项`
  return {
    index,
    left,
    top: `${((topOffset + topY) / topBox) * 100}%`,
    guideX: pointX,
    label: headerLabel,
    entries: entries.map((entry) => ({
      name: entry.name,
      text: entry.textOverride ?? (entry.numberLabel
        ? `${entry.numberLabel} 号 · ${formatAxisValue(entry.value)}${props.valueSuffix}`
        : `${formatAxisValue(entry.value)}${props.valueSuffix}`),
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
        :style="props.scrollable
          ? { paddingTop: `${SCROLL_TOOLTIP_ROOM}px`, touchAction: 'pan-y' }
          : undefined"
        :tabindex="props.scrollable ? 0 : undefined"
        :role="props.scrollable ? 'group' : undefined"
        :aria-label="props.scrollable ? scrollerLabel : undefined"
        @scroll.passive="onScroll"
        @keydown="onKeyNavigate"
        @wheel="onWheelNavigate"
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
                <!-- 光晕：只在实线段叠发光层（虚线是预测延伸，发光会显脏） -->
                <path
                  v-if="!run.dashed"
                  :d="run.line"
                  fill="none"
                  :stroke="run.colors.glow"
                  :stroke-width="7"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  opacity="0.28"
                />
                <path
                  :d="run.line"
                  fill="none"
                  :stroke="run.colors.stroke"
                  :stroke-width="run.dashed ? 2 : 2.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  :stroke-dasharray="run.dashed ? '5 4' : undefined"
                />
                <!-- 点太密（步长 < 7px）就不画圆点；虚线段（预测延伸）也不画点 -->
                <g v-if="showPoints && !run.dashed">
                  <circle
                    v-for="point in run.points"
                    :key="`dot-${seriesIndex}-${point.index}`"
                    :cx="point.x"
                    :cy="point.y"
                    r="2.6"
                    :fill="run.colors.stroke"
                  />
                </g>
              </template>
            </g>

            <!--
              叠加标记：非时序序列，不连线。
              有 atIndex → 预测列上的候选圆点 + 号码标签（干净圆点，无横向短杠）；
              无 atIndex → 整宽水平虚线参考。
            -->
            <line
              v-if="predictionGuideX !== null"
              :x1="predictionGuideX"
              :x2="predictionGuideX"
              :y1="plotTop"
              :y2="plotBottom"
              stroke="rgba(148, 163, 184, 0.22)"
              stroke-width="1"
              stroke-dasharray="2 5"
              aria-hidden="true"
            />
            <g v-for="marker in markerGeometry" :key="marker.key" aria-hidden="true">
              <line
                v-if="!marker.showDot"
                :x1="plotLeft"
                :x2="plotRight"
                :y1="marker.y"
                :y2="marker.y"
                :stroke="marker.colors.stroke"
                stroke-width="1.15"
                stroke-dasharray="4 3"
                opacity="0.75"
              />
              <circle
                v-if="marker.showDot"
                :cx="marker.x"
                :cy="marker.y"
                r="3.6"
                :fill="marker.colors.stroke"
                stroke="rgba(5, 6, 15, 0.85)"
                stroke-width="1.4"
              />
              <text
                v-if="marker.label"
                :x="marker.showDot ? marker.x - 7 : plotRight - 4"
                :y="marker.labelY"
                text-anchor="end"
                font-size="9"
                :fill="marker.colors.text"
                class="num"
              >
                {{ marker.label }}
              </text>
            </g>

            <!--
              本期候选段：接在预测点之后横向铺开的一串候选点（opt-in，默认不画）。
              与主折线同一 x 标尺、同一 y 单位：虚线从这里接出去，读作「主线继续的一小段」；
              号码写在点旁（上下交替错开）。口径：本期候选，非真实开奖。
            -->
            <g v-if="candidateGeometry" aria-hidden="true">
              <path
                :d="candidateGeometry.line"
                fill="none"
                :stroke="candidateGeometry.colors.stroke"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                stroke-dasharray="5 4"
              />
              <g v-for="point in candidateGeometry.points" :key="`cand-${point.index}`">
                <circle
                  :cx="point.x"
                  :cy="point.y"
                  r="3.6"
                  :fill="point.tone.stroke"
                  stroke="rgba(5, 6, 15, 0.85)"
                  stroke-width="1.4"
                />
                <text
                  v-if="point.label && candidateGeometry.showLabels"
                  :x="point.x"
                  :y="point.labelY"
                  text-anchor="middle"
                  font-size="9"
                  :fill="point.tone.text"
                  class="num"
                >
                  {{ point.label }}
                </text>
              </g>
            </g>

            <!--
              竖列候选（opt-in，默认不画）：全部候选共用同一 x 的单列，x 就是最后一个等距下标（与真实期同一步长）。
              - 槽位纵向默认等距（只为人眼读号码，不是数值/时间坐标）；candidateColumnByValue 时
                按真实取值定位（纵轴同单位 → 这一列就是取值的点阵分布）；绘图区外不画网格；
              - 虚线扇形从「最新真实数据点」发散到每个槽位（口径：与最新号码的关联，非未来路径）；
              - 号码写在圆点右侧；槽位太密 / 真实值模式下错不开时整列隐藏标签（号码交给光标提示），绝不重叠。
              口径：本期候选，非真实开奖。
            -->
            <g v-if="columnGeometry" aria-hidden="true">
              <template v-if="columnGeometry.from">
                <path
                  v-for="point in columnGeometry.points"
                  :key="`colfan-${point.key}`"
                  :d="fanArcPath(columnGeometry.from, point)"
                  fill="none"
                  :stroke="point.tone.stroke"
                  stroke-width="1.5"
                  stroke-dasharray="3 3"
                  opacity="0.6"
                />
              </template>
              <g v-for="point in columnGeometry.points" :key="point.key">
                <circle
                  :cx="point.x"
                  :cy="point.y"
                  r="3.6"
                  :fill="point.tone.stroke"
                  stroke="rgba(5, 6, 15, 0.85)"
                  stroke-width="1.4"
                />
                <text
                  v-if="point.label && columnGeometry.showLabels"
                  :x="point.x + COLUMN_LABEL_OFFSET"
                  :y="point.labelY"
                  text-anchor="start"
                  font-size="9.5"
                  :fill="point.tone.text"
                  class="num"
                >
                  {{ point.label }}
                </text>
              </g>
              <!-- 列下方一行小注：等距模式点明纵向只是排布；真实值模式点明纵轴即取值轴 -->
              <text
                :x="columnGeometry.x"
                :y="vbHeight - 13"
                text-anchor="middle"
                font-size="8.5"
                fill="rgba(148, 163, 184, 0.6)"
              >
                {{ props.candidateColumnByValue ? '竖列候选 · 按真实值排布' : '竖列候选 · 非数值轴' }}
              </text>
            </g>

            <!--
              数据点标注（opt-in，默认不画）：把「另一个单位」的原值挂在真实数据点旁
              （如差值图里标出「特码 23」）。点仍落在该点自身的 y 上，文字只是说明，
              组件不做单位换算；文字带深色描边当底，叠在曲线上也读得清。
            -->
            <g v-for="note in annotationGeometry" :key="note.key" aria-hidden="true">
              <circle
                :cx="note.x"
                :cy="note.y"
                r="4.4"
                :fill="note.colors.stroke"
                stroke="rgba(5, 6, 15, 0.9)"
                stroke-width="1.6"
              />
              <text
                v-if="note.label"
                :x="note.textX"
                :y="note.textY"
                :text-anchor="note.anchor"
                font-size="10"
                :fill="note.colors.text"
                class="num"
                paint-order="stroke"
                stroke="rgba(5, 6, 15, 0.92)"
                stroke-width="3"
                stroke-linejoin="round"
              >
                {{ note.label }}
              </text>
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
                <template v-for="(run, runIndex) in item.runs" :key="`hover-run-${seriesIndex}-${runIndex}`">
                  <!-- 虚线段跳过首点：交界点在实线段里已高亮，避免两个不同色的高亮点叠在一起 -->
                  <circle
                    v-for="point in (run.dashed ? run.points.slice(1) : run.points)"
                    :key="`hover-pt-${seriesIndex}-${point.index}`"
                    v-show="point.index === hoverInfo?.index"
                    :cx="point.x"
                    :cy="point.y"
                    r="4.2"
                    :fill="run.colors.stroke"
                    stroke="rgba(5, 6, 15, 0.9)"
                    stroke-width="1.5"
                  />
                </template>
              </template>
              <!-- 光标落在候选段：高亮该候选点（候选点不在折线几何里，单独画） -->
              <circle
                v-if="hoverCandidate"
                :cx="hoverCandidate.x"
                :cy="hoverCandidate.y"
                r="4.2"
                :fill="hoverCandidate.tone.stroke"
                stroke="rgba(5, 6, 15, 0.9)"
                stroke-width="1.5"
              />
              <!-- 光标落在竖列候选槽位：整列一起高亮（整列共用一个 x，不属于折线几何） -->
              <template v-if="hoverColumn">
                <circle
                  v-for="point in hoverColumn.points"
                  :key="`hover-col-${point.key}`"
                  :cx="point.x"
                  :cy="point.y"
                  r="4.4"
                  :fill="point.tone.stroke"
                  stroke="rgba(5, 6, 15, 0.9)"
                  stroke-width="1.5"
                />
              </template>
            </g>

            <!--
              交互热区。touch-action 一律 pan-y：纵向交给页面滚动；
              滑动模式下横向由 JS 步进号码光标（非整图自由平移）。
              候选列模式把热区右边界扩到号码标签留白（plotRight + COLUMN_LABEL_ROOM），点按/滑动才能落到这一列。
            -->
            <rect
              v-if="props.showTooltip"
              :x="plotLeft"
              :y="plotTop"
              :width="hasColumn ? (columnX + COLUMN_LABEL_ROOM - plotLeft) : plotWidth"
              :height="plotHeight"
              fill="transparent"
              style="touch-action: pan-y"
              @pointermove="onPointerDrag"
              @pointerdown="onPointerDown"
              @pointerup="onPointerUp"
              @pointercancel="onPointerUp"
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
        <template v-if="canScroll">左右滑动移动号码光标（视口随光标跟随）；</template>
        <template v-else>点按或左右方向键可移动号码光标；</template>
        {{ props.axisHint }}
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
 * 横向浏览靠号码光标步进 + 程序化 scrollLeft，不靠手指自由拖动画布。
 */
.chart-scroller {
  overscroll-behavior-x: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
}
</style>
