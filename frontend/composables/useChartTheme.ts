/**
 * 图表配色（手写 SVG 组件共用）。
 *
 * 所有颜色都带透明度，保证叠在琉璃面上时能透出后面的环境光。
 * 这里只描述「色板」，不描述任何业务口径：图表里的数值口径由调用方在图题里写明。
 */

export type ChartTone = 'aqua' | 'nebula' | 'bloom' | 'emerald' | 'amber' | 'slate'

export interface ChartToneColors {
  /** RGB 三元组（'34, 211, 238'），方便按强度拼 rgba() */
  rgb: string
  /** 主描边 */
  stroke: string
  /** 面积渐变起点 */
  fillFrom: string
  /** 面积渐变终点（透明） */
  fillTo: string
  /** 光晕 / 阴影 */
  glow: string
  /** 文字（轴标签 / 数值） */
  text: string
}

/** 默认循环序列：多序列时按顺序取色 */
export const CHART_TONE_ORDER: ChartTone[] = ['aqua', 'nebula', 'bloom', 'emerald', 'amber', 'slate']

export const CHART_TONES: Record<ChartTone, ChartToneColors> = {
  aqua: {
    rgb: '34, 211, 238',
    stroke: 'rgba(34, 211, 238, 0.95)',
    fillFrom: 'rgba(34, 211, 238, 0.34)',
    fillTo: 'rgba(34, 211, 238, 0)',
    glow: 'rgba(34, 211, 238, 0.55)',
    text: '#a1f1ff',
  },
  nebula: {
    rgb: '154, 123, 255',
    stroke: 'rgba(154, 123, 255, 0.95)',
    fillFrom: 'rgba(124, 77, 255, 0.32)',
    fillTo: 'rgba(124, 77, 255, 0)',
    glow: 'rgba(124, 77, 255, 0.55)',
    text: '#d6ccff',
  },
  bloom: {
    rgb: '255, 106, 141',
    stroke: 'rgba(255, 106, 141, 0.95)',
    fillFrom: 'rgba(251, 63, 110, 0.3)',
    fillTo: 'rgba(251, 63, 110, 0)',
    glow: 'rgba(251, 63, 110, 0.5)',
    text: '#ffc7d3',
  },
  emerald: {
    rgb: '52, 211, 153',
    stroke: 'rgba(52, 211, 153, 0.95)',
    fillFrom: 'rgba(16, 185, 129, 0.3)',
    fillTo: 'rgba(16, 185, 129, 0)',
    glow: 'rgba(52, 211, 153, 0.5)',
    text: '#a7f3d0',
  },
  amber: {
    rgb: '251, 191, 36',
    stroke: 'rgba(251, 191, 36, 0.95)',
    fillFrom: 'rgba(245, 158, 11, 0.28)',
    fillTo: 'rgba(245, 158, 11, 0)',
    glow: 'rgba(251, 191, 36, 0.45)',
    text: '#fde68a',
  },
  slate: {
    rgb: '203, 213, 225',
    stroke: 'rgba(203, 213, 225, 0.9)',
    fillFrom: 'rgba(148, 163, 184, 0.26)',
    fillTo: 'rgba(148, 163, 184, 0)',
    glow: 'rgba(203, 213, 225, 0.4)',
    text: '#e2e8f0',
  },
}

/** 取色板，未知色调回落到 aqua */
export function chartTone(tone?: string | null): ChartToneColors {
  if (tone && tone in CHART_TONES) return CHART_TONES[tone as ChartTone]
  return CHART_TONES.aqua
}

/** 按序号取默认色 */
export function chartToneAt(index: number): ChartToneColors {
  return chartTone(CHART_TONE_ORDER[index % CHART_TONE_ORDER.length])
}

/** 轴标签数字格式化：整数不带小数点，小数最多 1 位 */
export function formatAxisValue(value: number): string {
  const rounded = Math.round(value * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/* -------------------------------------------------------------------------- */
/* 热力网格 / 频次柱：单色系亮度色阶 + 分档                                   */
/* -------------------------------------------------------------------------- */

/**
 * 为什么整段都在这里：图表配色的唯一落点就是本文件。
 * HeatGrid / BarChart 不自己拼颜色，而是来这里取「色阶」——这样「颜色 → 数值」
 * 的映射只有一份实现，格子、柱子、图例、描边、文字色都跟着它走。
 *
 * 三条产品硬约束（改之前先读）：
 *   1) 一个色系 —— 整条色阶必须落在同一个色相家族里（默认 aqua / 青，品牌主色）。
 *   2) 方向：**值越大越深、越浓**。ramp[0] 是最浅的一档（给最小值），
 *      ramp[last] 是最深的一档（给最大值）——出现 1 次最浅淡，出现最多最深。
 *      这里只描述色阶方向，不代表业务口径（遗漏视图里「值」就是遗漏期数）。
 *   3) 区分度必须来自「明度 + 彩度」，**不能来自透明度**。
 *      早期实现是同一个颜色配不同 alpha 叠在深色琉璃底上，明度被底色压平：
 *      本池 200 期里大多数号码出现 3-6 次，那段中频在旧实现里几乎是同一块颜色，
 *      这才是「不够明显」的根因。所以下面每档都是同一家族的**真实色阶 token**
 *      （浅 → 深），非零档统一用同一个 alpha，把全部区分度交给 token 自身的明度。
 *
 * 为什么用「分档」而不是连续映射：等频切分才能把真实数据里最挤的中频段
 * （本池的 3/4/5/6 次）摊到不同档位上；连续映射会把它们重新压回同一亮度带。
 *
 * 方向反转带来的一条新风险（改动前必读）：最深的一档现在落在**最高值**端，
 * 于是「最热档」和「零值档」都是暗色块。两者必须靠描边、色相、文字三处拉开，
 * 见 HEAT_ZERO_STOP 与 buildHeatScale 里给最高档换上的家族强调色描边。
 */

export interface HeatRampStop {
  /** 纯色（图例色块边框 / 需要不带透明度的填充时的兜底） */
  solid: string
  /** 填充色（统一 alpha 的 rgba(solid)，叠在琉璃面上仍透出一点环境光） */
  fill: string
  /** 描边色（同色相、比填充更实一点，让相邻档的分界更干脆） */
  stroke: string
  /** 文字色：按 token 明度自动在「亮字 / 暗字」之间二选一，保证格内数字读得清 */
  text: string
  /** 透明度（供图例还原同一观感） */
  alpha: number
}

/**
 * 非零档统一透明度。刻意**不是一个数组** —— 只要它随档位变化，就是被禁止的
 * alpha 渐变，区分度会被深色底重新压平。0.9 = 近实心但还留一点玻璃感。
 */
const HEAT_FILL_ALPHA = 0.9

/** 文字色候选（都是项目色板 token）：亮字 aqua-50 / 暗字 ink-950 */
const HEAT_TEXT_LIGHT = '#ecfdff'
const HEAT_TEXT_DARK = '#05060f'

/**
 * 「亮字还是暗字」的判定阈值，直接由 WCAG 对比度公式推出来，不是拍脑袋的数字：
 * 暗字对比度 (L + 0.05) / 0.05，亮字对比度 1.05 / (L + 0.05)，
 * 两者相等时 (L + 0.05)² = 0.05 × 1.05 → L = √0.0525 − 0.05 ≈ 0.179。
 * 即：色相对亮度高于该值就用暗字，否则用亮字，两种情况下都是对比度更高的那个。
 *
 * 判定用的是**色阶 token 自身**的相对亮度（不是叠完底色的合成值）—— 这样选字结果
 * 只跟档位有关、不随面板底色漂移；代价是合成后对比度会略低于 token 的标称值。
 * 实测最紧的一档是 aqua-600 配暗字（合成后约 4.7:1，≈AA 正文阈值，余量很小），
 * 换档位时必须重算这一项。
 */
const HEAT_DARK_TEXT_LUMINANCE = Math.sqrt(0.05 * 1.05) - 0.05

/**
 * 各 tone 的单色系亮度梯子（浅 → 深）：全部是**同一个色相家族**的真实色阶 token，
 * 没有跨色相、也没有新造色相。
 *
 * 数组顺序 = 数值由小到大的颜色顺序：ramp[0] 给最低档（最浅），
 * ramp[last] 给最高档（最深）。所以「倒序一个家族」就等于把色阶方向反过来。
 *
 * 档位是用「相邻档 CIELAB L* 差的最小值最大化」穷举出来的（叠在 #0d1226 底、
 * alpha 0.9 上比较），所以每个家族的梯子都把可用色阶拉到最开。
 * 换档位之前请重新算一遍差值 —— 别凭手感调。
 *
 * 冷端为什么从 200 起（aqua）：方向反向后，最浅的一档落在**最低值**端，
 * 而 100 档叠在深色面板上接近一块白（合成 L*≈87）且会自动配近黑字，
 * 会让「只出现 1 次」的格子成为全表最抢眼的一块 —— 与「越热越深」的表达正好相反。
 * 抬到 200 后合成 L*≈83，仍是全表最浅（离次档 11.7，一眼能认出），但不刺眼。
 * 次档同时由 300 抬到 400：否则 200 与 300 只差 ΔL*≈5，真实数据里会糊成一块。
 * 在「最浅 = 200、最深 = 900」的约束下，这个组合是 aqua 全色阶里的最优解（min ΔL* 8.8）。
 *
 * 其余家族本轮只做「数组倒序」，相邻 ΔL* 的取值集合完全不变（方向反过来而已），
 * 因此不损失任何区分度。冷端抬档这条规则目前只在 aqua 上验证过（aqua 是 HeatGrid /
 * BarChart 实际渲染的唯一色阶）；如果以后有调用方改用别的 tone，
 * 请按上面同样的「max-min ΔL*」标准重新挑一次冷端，不要直接照抄 aqua 的档位。
 */
const HEAT_RAMP_SHADES: Record<ChartTone, readonly number[]> = {
  // aqua-200 → 400 → 500 → 600 → 700 → 900，相邻 ΔL* 11.7 / 8.8 / 11.4 / 9.8 / 13.1
  aqua: [200, 400, 500, 600, 700, 900],
  // emerald-100 → 300 → 500 → 600 → 700 → 900，相邻 ΔL* 10.2 / 15.7 / 10.9 / 9.6 / 14.2
  emerald: [100, 300, 500, 600, 700, 900],
  // nebula-100 → 200 → 300 → 400 → 500 → 700，相邻 ΔL* 7.0 / 10.9 / 11.3 / 11.1 / 14.0
  // （nebula-800/900 太暗，贴底色后与「零值档」分不开，所以**最深**的一档只到 700）
  nebula: [100, 200, 300, 400, 500, 700],
  // bloom-200 → 300 → 400 → 600 → 700 → 900，相邻 ΔL* 9.4 / 9.8 / 13.3 / 7.4 / 12.4
  bloom: [200, 300, 400, 600, 700, 900],
  // amber-100 → 300 → 500 → 600 → 700 → 900，相邻 ΔL* 9.0 / 12.7 / 11.3 / 11.9 / 14.6
  amber: [100, 300, 500, 600, 700, 900],
  // slate-100 → 300 → 400 → 500 → 600 → 700，相邻 ΔL* 10.5 / 16.8 / 16.6 / 11.5 / 7.8
  // （slate-800/900 太暗；slate 组取值也不取自品牌青色，仅作为 tone='slate' 时的兜底）
  slate: [100, 300, 400, 500, 600, 700],
}

/**
 * 同一色相家族的 6 档色阶 token（hex + token 名），hex 与 tailwind.config.ts /
 * Tailwind 默认色阶逐字一致。shade = -1 表示该家族没有对应档，构造时会跳过。
 */
const HEAT_SCALE_HEX: Record<ChartTone, Record<number, string>> = {
  aqua: {
    100: '#cff9ff', 200: '#a1f1ff', 300: '#63e6fb', 400: '#22d3ee', 500: '#0bb6d6',
    600: '#0592b0', 700: '#0a7490', 800: '#115e75', 900: '#144e63', 950: '#063344',
  },
  emerald: {
    100: '#d1fae5', 200: '#a7f3d0', 300: '#6ee7b7', 400: '#34d399', 500: '#10b981',
    600: '#059669', 700: '#047857', 800: '#065f46', 900: '#064e3b', 950: '#022c22',
  },
  nebula: {
    100: '#eae5ff', 200: '#d6ccff', 300: '#b8a5ff', 400: '#9a7bff', 500: '#7c4dff',
    600: '#6a2ff0', 700: '#5820cc', 800: '#481ca6', 900: '#3b1a85', 950: '#1e0b4d',
  },
  bloom: {
    100: '#ffe1e7', 200: '#ffc7d3', 300: '#ff9db2', 400: '#ff6a8d', 500: '#fb3f6e',
    600: '#e81d57', 700: '#c41348', 800: '#a0133f', 900: '#84133a', 950: '#4a0520',
  },
  amber: {
    100: '#fef3c7', 200: '#fde68a', 300: '#fcd34d', 400: '#fbbf24', 500: '#f59e0b',
    600: '#d97706', 700: '#b45309', 800: '#92400e', 900: '#78350f', 950: '#451a03',
  },
  slate: {
    100: '#f1f5f9', 200: '#e2e8f0', 300: '#cbd5e1', 400: '#94a3b8', 500: '#64748b',
    600: '#475569', 700: '#334155', 800: '#1e293b', 900: '#0f172a', 950: '#020617',
  },
}

/**
 * 零值档：**中性、贴底色**，不占任何色相。
 * 「一次都没出现 / 遗漏 0」必须一眼看出是「没有热度」，而不是某个中档 ——
 * 方向反转为「越热越深」之后这一点比之前更要紧：最深的一档（最热）也落在暗色端，
 * 所以零值档不能再只靠「暗」来表态，必须靠**中性**表态。它与非零档在三处分开：
 *   · 填充 = ink-800 @ 0.55，几乎就是面板底色（合成后 L*≈9），且是一块平色，
 *     没有同色相描边、没有加粗 —— 读起来是「空格」而不是「深色块」；
 *   · 描边 = 中性灰（非零档的描边一律是同色相的实色；最高档更会换成家族强调色）；
 *   · 文字 = 暗灰（非零档的文字是亮字或近黑字，对比度更高）。
 *
 * 图例（HeatScaleLegend）里零值档更用「中性灰虚线边框」明确表态；
 * 网格内的描边保持极淡的实线（格子只有 1px 级的边，虚线在缩放下会糊成噪点）。
 */
export const HEAT_ZERO_STOP: HeatRampStop = {
  solid: '#191d3f',
  fill: 'rgba(25, 29, 63, 0.55)',
  stroke: 'rgba(148, 163, 184, 0.30)',
  text: 'rgba(148, 163, 184, 0.55)',
  alpha: 0.55,
}

function hexToRgb(hex: string): [number, number, number] {
  const raw = hex.replace('#', '')
  return [
    Number.parseInt(raw.slice(0, 2), 16),
    Number.parseInt(raw.slice(2, 4), 16),
    Number.parseInt(raw.slice(4, 6), 16),
  ]
}

function hexToRgba(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** 相对亮度（WCAG 定义），用于自动挑格内文字色 */
function relativeLuminance(hex: string): number {
  const channel = (value: number) => {
    const c = value / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  const [r, g, b] = hexToRgb(hex)
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function buildRamp(shades: readonly number[], scale: Record<number, string>): HeatRampStop[] {
  const solids = shades
    .map(shade => scale[shade])
    .filter((hex): hex is string => Boolean(hex))
  return solids.map((solid) => {
    const dark = relativeLuminance(solid) > HEAT_DARK_TEXT_LUMINANCE
    return {
      solid,
      alpha: HEAT_FILL_ALPHA,
      fill: hexToRgba(solid, HEAT_FILL_ALPHA),
      stroke: hexToRgba(solid, 0.95),
      // 亮档配暗字、暗档配亮字 —— 文字反相本身就是一档额外的区分信号
      text: dark ? HEAT_TEXT_DARK : HEAT_TEXT_LIGHT,
    }
  })
}

const HEAT_RAMPS: Record<ChartTone, HeatRampStop[]> = {
  aqua: buildRamp(HEAT_RAMP_SHADES.aqua, HEAT_SCALE_HEX.aqua),
  emerald: buildRamp(HEAT_RAMP_SHADES.emerald, HEAT_SCALE_HEX.emerald),
  nebula: buildRamp(HEAT_RAMP_SHADES.nebula, HEAT_SCALE_HEX.nebula),
  amber: buildRamp(HEAT_RAMP_SHADES.amber, HEAT_SCALE_HEX.amber),
  bloom: buildRamp(HEAT_RAMP_SHADES.bloom, HEAT_SCALE_HEX.bloom),
  slate: buildRamp(HEAT_RAMP_SHADES.slate, HEAT_SCALE_HEX.slate),
}

/** 取某个 tone 的完整色阶（未知 tone 回落 aqua，与 chartTone 的兜底一致） */
export function heatRamp(tone?: string | null): HeatRampStop[] {
  if (tone && tone in HEAT_RAMPS) return HEAT_RAMPS[tone as ChartTone]
  return HEAT_RAMPS.aqua
}

/** 需要几档就等距取几档（档数由数据决定，色阶数量不够时按需重采样） */
export function sampleHeatRamp(tone: string | null | undefined, count: number): HeatRampStop[] {
  const ramp = heatRamp(tone)
  const size = Math.max(1, Math.round(count))
  if (size === 1) return [ramp[Math.floor((ramp.length - 1) / 2)] ?? ramp[0]!]
  return Array.from({ length: size }, (_, index) => {
    const at = Math.round((index * (ramp.length - 1)) / (size - 1))
    return ramp[at] ?? ramp[ramp.length - 1]!
  })
}

/**
 * 单值取色：给柱状图用 —— 同一个色相家族里按数值比例沿亮度梯子取一档
 * （**越大越深**，方向与 HeatGrid 的档位一致），而不是一根柱子一个色相。
 * ratio 是「该值 / 本组最大值」，非有限值一律按最低档（最浅）处理。
 */
export function heatStepAt(tone: string | null | undefined, ratio: number): HeatRampStop {
  const ramp = heatRamp(tone)
  const safe = Number.isFinite(ratio) ? Math.min(1, Math.max(0, ratio)) : 0
  return ramp[Math.round(safe * (ramp.length - 1))] ?? ramp[ramp.length - 1]!
}

/** 非零档的上限：再多也只是把已经分不开的取值拆得更碎 */
export const HEAT_BUCKET_LIMIT = 6

export interface HeatBin extends HeatRampStop {
  /** 档位序号（0 = 零值档；1..k 由冷到热，也就是由浅到深） */
  level: number
  /** 档位下界（含） */
  from: number
  /** 档位上界（含）；null 表示不设上界 */
  to: number | null
  /** 图例文案，只写区间（单位由调用方在说明里给出） */
  label: string
  /**
   * 是否最高档（= 最深最浓的一档）。
   * 网格 / 图例都靠它把「最热」与「零值」这两块暗色分开：最高档的描边换成同家族的
   * 强调色（见 buildHeatScale），零值档保持中性灰细描边。
   */
  top: boolean
}

export interface HeatScale {
  /** 完整档位表，第 0 项恒为零值档 */
  bins: HeatBin[]
  /** 最高档序号（全为 0 值时是 0） */
  maxLevel: number
  /** 非零档实际覆盖到的最大取值 */
  domainMax: number
}

/**
 * 把一组取值按「等频（分位数）」切成离散色档。
 *
 * 为什么是等频，而不是等宽或 sqrt 曲线：本池 200 期的取值高度集中在 3-6 次，
 * 等宽分档会把 4 和 5 挤进同一档（这是「不够明显」的另一半原因），
 * sqrt 则把靠上的中频段压得更扁。等频让最常见的那一段每次都能落到不同档位上（不同明度），
 * 图例再把真实区间写出来，读起来不会误解成绝对次数。
 *
 * 代价（要如实告诉调用方）：颜色表达的是「在本池样本内的相对位置」，不是绝对次数 ——
 * 所以 legend 必须显示区间、口径必须由调用方在图题/图例里声明。
 * 样本很小时档位会自动变少（切点去重），不会硬凑出空档。
 */
export function buildHeatScale(
  values: number[],
  options: { tone?: string | null; max?: number | null; buckets?: number } = {},
): HeatScale {
  const buckets = Math.max(
    1,
    Math.min(HEAT_BUCKET_LIMIT, Math.round(options.buckets ?? HEAT_BUCKET_LIMIT)),
  )
  const positives = values
    .filter(value => Number.isFinite(value) && value > 0)
    .sort((a, b) => a - b)
  const dataMax = positives.length ? positives[positives.length - 1]! : 0
  const requestedMax = Number(options.max)
  const domainMax = requestedMax > 0 ? Math.max(requestedMax, dataMax) : dataMax

  const zeroBin: HeatBin = { ...HEAT_ZERO_STOP, level: 0, from: 0, to: 0, label: '0', top: false }
  if (!positives.length) return { bins: [zeroBin], maxLevel: 0, domainMax: 0 }

  // 等频切点：取分位点处的真实取值，向下取整成整数边界后去重
  const cuts: number[] = []
  for (let i = 1; i < buckets; i += 1) {
    const at = Math.round((i / buckets) * (positives.length - 1))
    cuts.push(Math.floor(positives[at] ?? 0))
  }
  const edges = [...new Set(cuts.filter(cut => cut > 0))].sort((a, b) => a - b)

  const ranges: Array<{ from: number; to: number | null }> = []
  let lower = 1
  for (const cut of edges) {
    if (cut < lower) continue
    ranges.push({ from: lower, to: cut })
    lower = cut + 1
  }
  ranges.push({ from: lower, to: null })
  // 切点正好落在最大值上时会多出一个空档，丢掉它，最高档回到真实取值区间
  while (ranges.length > 1 && ranges[ranges.length - 1]!.from > dataMax) ranges.pop()

  const stops = sampleHeatRamp(options.tone, ranges.length)
  /**
   * 最高档（最深）的描边换成同家族的**强调色**（aqua 即品牌 aqua-400）。
   * 方向反转为「越热越深」之后，最高档和零值档都落在暗色端，如果两者都用本档 token
   * 自己的深色描边，缩到手机屏幕上会很难分辨 —— 所以给最高档加一圈亮而饱和的实线边，
   * 零值档保持中性灰细边，两块暗色的语义（最热 / 没有热度）一眼就能分开。
   * 强调色同样取自该色相家族（chartTone 的 stroke），没有跨色相。
   */
  const topStroke = chartTone(options.tone).stroke
  const bins: HeatBin[] = ranges.map((range, index) => {
    const stop = stops[index] ?? stops[stops.length - 1]!
    const isTop = index === ranges.length - 1
    return {
      ...stop,
      stroke: isTop ? topStroke : stop.stroke,
      level: index + 1,
      from: range.from,
      to: range.to,
      label: isTop
        ? (domainMax > range.from ? `${range.from}-${domainMax}` : String(range.from))
        : (range.from === range.to ? String(range.from) : `${range.from}-${range.to}`),
      top: isTop,
    }
  })

  return { bins: [zeroBin, ...bins], maxLevel: ranges.length, domainMax }
}

/** 取值 → 档位（找不到时落到最高档，避免漏色） */
export function heatBinLevel(scale: HeatScale, value: number): number {
  if (!(value > 0)) return 0
  const bin = scale.bins.find(
    item => item.level > 0 && value >= item.from && (item.to === null || value <= item.to),
  )
  return bin?.level ?? scale.maxLevel
}
