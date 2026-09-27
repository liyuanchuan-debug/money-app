<script setup lang="ts">
/**
 * SpecialBall —— 特码琉璃球（玻璃球体，不是圆形色块）。
 *
 * 质感由四层构成：
 *   1. .glass-ball 底：径向镜面高光 + 底部折射 + 深阴影
 *   2. .glass-ball::after：conic-gradient 抠出的边缘折射环
 *   3. .glass-ball-sheen：可绕球心旋转的镜面高光（rolling 时旋转 → 像在滚动）
 *   4. .glass-ball-number：带柔光的等宽数字
 *
 * rolling=true 用于「财富密码生成中 / 数字滚动中」这类过程态。
 */

const props = withDefaults(
  defineProps<{
    /** 号码（1-49），null / undefined 显示占位符 */
    number?: number | string | null
    /** 尺寸档位 */
    size?: 'sm' | 'md' | 'lg' | 'xl'
    /** 滚动 / 生成中状态 */
    rolling?: boolean
    /** 色调（影响外发光颜色） */
    tone?: 'aqua' | 'nebula' | 'bloom'
    /** 补零位数，0 表示不补零 */
    pad?: number
    /** 球下方的说明文字 */
    label?: string
    /** 是否显示外发光 */
    glow?: boolean
  }>(),
  {
    number: null,
    size: 'md',
    rolling: false,
    tone: 'aqua',
    pad: 2,
    label: '',
    glow: true,
  },
)

const SIZE: Record<string, string> = {
  sm: 'h-9 w-9 text-sm',
  md: 'h-12 w-12 text-lg',
  lg: 'h-16 w-16 text-2xl',
  xl: 'h-24 w-24 text-3xl sm:text-4xl',
}

const GLOW: Record<string, string> = {
  aqua: 'rgba(34, 211, 238, 0.6)',
  nebula: 'rgba(124, 77, 255, 0.6)',
  bloom: 'rgba(251, 63, 110, 0.55)',
}

const text = computed(() => {
  const value = props.number
  if (value === null || value === undefined || value === '') return '—'
  const numeric = Number(value)
  if (Number.isNaN(numeric)) return String(value)
  return props.pad > 0 ? String(numeric).padStart(props.pad, '0') : String(numeric)
})
</script>

<template>
  <span class="inline-flex flex-col items-center gap-1.5">
    <span
      class="glass-ball shrink-0"
      :class="[SIZE[props.size], props.rolling ? 'animate-breathe' : '']"
      :style="props.glow ? { '--glass-ball-glow': GLOW[props.tone] } : {}"
    >
      <span
        class="pointer-events-none absolute inset-0"
        :class="props.rolling ? 'animate-ball-spin' : ''"
        aria-hidden="true"
      >
        <span class="glass-ball-sheen" />
      </span>
      <span
        class="glass-ball-number num"
        :class="props.rolling ? 'opacity-70 blur-[0.6px]' : ''"
      >
        {{ text }}
      </span>
    </span>
    <span
      v-if="props.label || $slots.label"
      class="text-[11px] leading-none text-slate-400"
    >
      <slot name="label">{{ props.label }}</slot>
    </span>
  </span>
</template>
