<script setup lang="ts">
import { useId } from 'vue'

/**
 * BrandMark —— 「四叶沙盘」品牌标识（全站唯一真源）
 * =============================================================================
 * 图形：**手写 SVG 四叶草**（零外部图片 / 零字体 / 零依赖，与全站手写 SVG 图表一致）。
 *
 *   一片「心形小叶」按 90° 旋转四次拼成一朵四叶草：
 *     小叶局部坐标：叶尖在原点、朝 -y 生长，长 17.2、半宽 10.5、基部锥角约 96°；
 *     叶尖整体离中心 2.6，所以相邻两片在靠近中心处**轻微交叠**，中心不会露缝；
 *     四片叶合成一条 d（4 条子路径），nonzero 填充规则下交叠处仍是实心，不会挖出空洞；
 *     (x, y) → (−y, x) 即 90° 顺时针旋转，四片分别是 0° / 90° / 180° / 270°。
 *   叶柄：中心下方一根短圆头直线（y 5 → 24，stroke-width 3.6），画在叶片**下层**，
 *     接缝被叶片压住，看上去是长在叶心上的。
 *
 * 颜色（CLOVER 常量集中定义，逐个对应 tailwind.config.ts 的色阶）：
 *   叶片用 **aqua → emerald 渐变**：aqua-300 #63e6fb → aqua-400 #22d3ee → emerald-400 #34d399。
 *   四叶草天然是绿色，但本项目的识别色是青（aqua）；青到翠的过渡既读得出「绿」，
 *   又不会跳出琉璃主题（emerald 是 Tailwind 默认色阶，项目里 StatChip 已在用）。
 *   叶柄用更深的 aqua-500 #0bb6d6 → emerald-500 #10b981，压住整体重心。
 *   叠层顺序：中心暗芯 → 叶柄 → 叶片渐变 → 左上柔光 → 1px 玻璃描边 → 叶心暗芯，
 *   共 6 层，做出「渐变 + 顶部内高光」的琉璃质感。
 *
 * Props（全部可选）：
 *   - `size`     'sm' | 'md' | 'lg'  标记尺寸档位（sm 28px / md 36px / lg 56px），默认 'md'
 *   - `iconOnly` boolean             只显示四叶草、隐藏「四叶沙盘」字标（紧凑头部 / 类 favicon 用法），默认 false
 *   - `halo`     boolean             标记背后是否铺一层柔光（落在玻璃面上更「亮」），默认 true
 *   - `label`    string              品牌文案，同时用于字标与无障碍名称，默认 '四叶沙盘'
 *
 * SSR / 可访问性 / 动效 / 布局：
 *   - 渐变 id 由 vue 的 `useId()` 生成：服务端与客户端按同一渲染顺序取到同一个 id，水合一致，
 *     同一页面出现多个 BrandMark 也不会撞 id；
 *   - 尺寸全部是固定 class（h-7/w-7 等），**不存在布局跳动**；
 *   - 纯静态图形、**没有任何动画**，所以无需额外处理 prefers-reduced-motion
 *     （全站动效总开关见 composables/useMotion.ts；标记本身刻意不参与动画）；
 *   - 完整形态下标记 aria-hidden="true"，品牌名由可见字标承担；
 *     iconOnly 时容器 role="img" + aria-label，图形本身对读屏可见。
 *
 * ⚠️ 品牌名是「四叶沙盘」，图形是四叶草 —— 名字与图形不必互相解释，不要给图形加沙盘隐喻。
 */

/* -------------------------------------------------------------------------- */
/* 色板：逐一对应 tailwind.config.ts（emerald 为 Tailwind 默认色阶）           */
/* -------------------------------------------------------------------------- */
const CLOVER = {
  /** aqua-300 —— 叶片渐变起点（最亮） */
  aqua300: '#63e6fb',
  /** aqua-400 —— 叶片渐变中段（项目主强调色） */
  aqua400: '#22d3ee',
  /** emerald-400 —— 叶片渐变终点（读成「绿」的关键） */
  emerald400: '#34d399',
  /** aqua-500 —— 叶柄渐变起点 */
  aqua500: '#0bb6d6',
  /** emerald-500 —— 叶柄渐变终点 */
  emerald500: '#10b981',
  /** aqua-950 —— 叶心暗芯（叶脉交汇处的厚度感） */
  coreShade: '#063344',
  /** 柔光 / 玻璃描边的白 */
  white: '#ffffff',
} as const

/**
 * 四叶草 = 1 片心形小叶 × 4 次 90° 旋转。
 * 生成脚本（一次性）：本地小叶取 0° 路径，逐点做 (x, y) → (−y, x)，
 * 再把每个子路径的旋转结果拼成下面这条 d。改图形时请一并保持「叶尖离中心 2.6」，
 * 否则中心会出现缝隙。
 */
const LEAF_UNION = [
  'M0 -2.6C5.7 -7.7 10.5 -9.5 10.5 -14.8C10.5 -18.2 7.6 -19.8 3.9 -19.8C1.7 -19.8 0.5 -18.8 0 -17.7C-0.5 -18.8 -1.7 -19.8 -3.9 -19.8C-7.6 -19.8 -10.5 -18.2 -10.5 -14.8C-10.5 -9.5 -5.7 -7.7 0 -2.6Z',
  'M2.6 0C7.7 5.7 9.5 10.5 14.8 10.5C18.2 10.5 19.8 7.6 19.8 3.9C19.8 1.7 18.8 0.5 17.7 0C18.8 -0.5 19.8 -1.7 19.8 -3.9C19.8 -7.6 18.2 -10.5 14.8 -10.5C9.5 -10.5 7.7 -5.7 2.6 0Z',
  'M0 2.6C-5.7 7.7 -10.5 9.5 -10.5 14.8C-10.5 18.2 -7.6 19.8 -3.9 19.8C-1.7 19.8 -0.5 18.8 0 17.7C0.5 18.8 1.7 19.8 3.9 19.8C7.6 19.8 10.5 18.2 10.5 14.8C10.5 9.5 5.7 7.7 0 2.6Z',
  'M-2.6 0C-7.7 -5.7 -9.5 -10.5 -14.8 -10.5C-18.2 -10.5 -19.8 -7.6 -19.8 -3.9C-19.8 -1.7 -18.8 -0.5 -17.7 0C-18.8 0.5 -19.8 1.7 -19.8 3.9C-19.8 7.6 -18.2 10.5 -14.8 10.5C-9.5 10.5 -7.7 5.7 -2.6 0Z',
].join('')

/** 叶柄：中心下方短直线（圆头端点，所以下缘到 y = 24 + 3.6/2 = 25.8） */
const STEM = 'M0 5V24'

/** viewBox：横向给到 ±25，纵向给到 −24…26，包住叶片（±19.8）、叶柄（25.8）和最大柔光（r 24） */
const VIEW_BOX = '-25 -24 50 50'

const props = withDefaults(
  defineProps<{
    /** 尺寸档位：sm 28px / md 36px / lg 56px */
    size?: 'sm' | 'md' | 'lg'
    /** 只显示四叶草、隐藏字标（紧凑头部 / 类 favicon 用法） */
    iconOnly?: boolean
    /** 标记背后是否铺一层柔光 */
    halo?: boolean
    /** 品牌文案（字标 + 无障碍名称） */
    label?: string
  }>(),
  {
    size: 'md',
    iconOnly: false,
    halo: true,
    label: '四叶沙盘',
  },
)

/** 标记盒子（固定像素尺寸：不随字体变化，也不会引起布局跳动） */
const MARK: Record<string, string> = {
  sm: 'h-7 w-7',
  md: 'h-9 w-9',
  lg: 'h-14 w-14',
}

/** 字标字号（跟着标记档位走） */
const WORDMARK: Record<string, string> = {
  sm: 'text-sm',
  md: 'text-base',
  lg: 'text-2xl',
}

/** 图形与字标的间距 */
const GAP: Record<string, string> = {
  sm: 'gap-1.5',
  md: 'gap-2',
  lg: 'gap-3',
}

/** 每个实例一套渐变 id（SSR 与客户端一致，见文件头注释） */
const uid = useId()
const ids = {
  leaf: `${uid}-bm-leaf`,
  stem: `${uid}-bm-stem`,
  sheen: `${uid}-bm-sheen`,
  core: `${uid}-bm-core`,
  halo: `${uid}-bm-halo`,
}
</script>

<template>
  <span
    class="inline-flex max-w-full items-center select-none"
    :class="GAP[props.size]"
    :role="props.iconOnly ? 'img' : undefined"
    :aria-label="props.iconOnly ? props.label : undefined"
  >
    <!-- 四叶草标记：纯手写 SVG，无外部资源、无字体、无动画 -->
    <svg
      class="relative shrink-0"
      :class="MARK[props.size]"
      :viewBox="VIEW_BOX"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      :aria-hidden="props.iconOnly ? undefined : 'true'"
    >
      <defs>
        <!-- 叶片主体：aqua-300 → aqua-400 → emerald-400（青读主色，尾段转翠读成绿） -->
        <linearGradient :id="ids.leaf" x1="16%" y1="4%" x2="84%" y2="96%">
          <stop offset="0%" :stop-color="CLOVER.aqua300" />
          <stop offset="46%" :stop-color="CLOVER.aqua400" />
          <stop offset="100%" :stop-color="CLOVER.emerald400" />
        </linearGradient>

        <!-- 叶柄：aqua-500 → emerald-500，比叶片深一档，压住重心。
             必须用 userSpaceOnUse + 显式坐标：叶柄是纯垂直线（M0 5V24），几何 bbox 宽度为 0，
             默认的 objectBoundingBox 会退化成「不渲染」，Chrome 里这条 stroke 完全不上色。 -->
        <linearGradient
          :id="ids.stem"
          gradientUnits="userSpaceOnUse"
          x1="0"
          y1="5"
          x2="0"
          y2="24"
        >
          <stop offset="0%" :stop-color="CLOVER.aqua500" />
          <stop offset="100%" :stop-color="CLOVER.emerald500" />
        </linearGradient>

        <!-- 左上柔光：叠在叶片上做「顶部内高光」，超出半径即透明，天然被叶片轮廓裁切 -->
        <radialGradient
          :id="ids.sheen"
          gradientUnits="userSpaceOnUse"
          cx="-5"
          cy="-7"
          r="23"
        >
          <stop offset="0%" :stop-color="CLOVER.white" stop-opacity="0.52" />
          <stop offset="58%" :stop-color="CLOVER.white" stop-opacity="0.12" />
          <stop offset="100%" :stop-color="CLOVER.white" stop-opacity="0" />
        </radialGradient>

        <!-- 叶心暗芯：四片叶交汇处稍暗，做出叶脉交汇的厚度感 -->
        <radialGradient :id="ids.core" gradientUnits="userSpaceOnUse" cx="0" cy="0" r="7">
          <stop offset="0%" :stop-color="CLOVER.coreShade" stop-opacity="0.55" />
          <stop offset="100%" :stop-color="CLOVER.coreShade" stop-opacity="0" />
        </radialGradient>

        <!-- 可选柔光光晕：让标记在玻璃面上「发光」，r 24 已被 viewBox 完整容纳，不会裁切 -->
        <radialGradient :id="ids.halo" gradientUnits="userSpaceOnUse" cx="0" cy="0" r="24">
          <stop offset="0%" :stop-color="CLOVER.aqua400" stop-opacity="0.34" />
          <stop offset="52%" :stop-color="CLOVER.aqua400" stop-opacity="0.12" />
          <stop offset="100%" :stop-color="CLOVER.aqua400" stop-opacity="0" />
        </radialGradient>
      </defs>

      <circle v-if="props.halo" cx="0" cy="0" r="24" :fill="`url(#${ids.halo})`" />

      <!-- 叶柄先画，被叶片压住接缝 -->
      <path
        :d="STEM"
        :stroke="`url(#${ids.stem})`"
        stroke-width="3.6"
        stroke-linecap="round"
      />

      <!-- 四片叶（同一条 d 的 4 条子路径） -->
      <path :d="LEAF_UNION" :fill="`url(#${ids.leaf})`" />
      <!-- 顶部柔光 -->
      <path :d="LEAF_UNION" :fill="`url(#${ids.sheen})`" />
      <!-- 1px 玻璃描边（叶尖用 round join，避免尖角被削平） -->
      <path
        :d="LEAF_UNION"
        :stroke="CLOVER.white"
        stroke-opacity="0.26"
        stroke-width="1"
        stroke-linejoin="round"
      />
      <!-- 叶心暗芯压住内部接缝 -->
      <circle cx="0" cy="0" r="7" :fill="`url(#${ids.core})`" />
    </svg>

    <!-- 字标：品牌名走既有 .text-gradient（与页面 h1 同一套色），不额外引字体 -->
    <span
      v-if="!props.iconOnly"
      class="text-gradient font-semibold leading-none tracking-[0.12em] whitespace-nowrap"
      :class="WORDMARK[props.size]"
    >
      {{ props.label }}
    </span>
  </span>
</template>
