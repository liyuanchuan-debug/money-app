<script setup lang="ts">
/**
 * StatChip —— 小号琉璃药丸标签（生肖 / 波动类型 / 口径说明等）。
 *
 * 只做标签，不承载交互；色调仅用于区分含义（emerald=小波动、amber=常规、bloom=大跳）。
 */

const props = withDefaults(
  defineProps<{
    /** 文案；也可以用默认插槽 */
    label?: string | number | null
    /** 色调 */
    tone?: 'aqua' | 'nebula' | 'bloom' | 'emerald' | 'amber' | 'neutral'
    /** 尺寸档位 */
    size?: 'xs' | 'sm' | 'md'
    /** 左侧是否显示色点 */
    dot?: boolean
    /** 是否只描边不填充 */
    outline?: boolean
  }>(),
  {
    label: '',
    tone: 'neutral',
    size: 'sm',
    dot: false,
    outline: false,
  },
)

const TONE: Record<string, string> = {
  aqua: 'border-aqua-400/40 bg-aqua-400/10 text-aqua-200',
  nebula: 'border-nebula-400/40 bg-nebula-400/10 text-nebula-200',
  bloom: 'border-bloom-400/40 bg-bloom-400/10 text-bloom-200',
  emerald: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200',
  amber: 'border-amber-400/40 bg-amber-400/10 text-amber-200',
  neutral: 'border-white/15 bg-white/5 text-slate-300',
}

const OUTLINE: Record<string, string> = {
  aqua: 'border-aqua-400/50 bg-transparent text-aqua-200',
  nebula: 'border-nebula-400/50 bg-transparent text-nebula-200',
  bloom: 'border-bloom-400/50 bg-transparent text-bloom-200',
  emerald: 'border-emerald-400/50 bg-transparent text-emerald-200',
  amber: 'border-amber-400/50 bg-transparent text-amber-200',
  neutral: 'border-white/20 bg-transparent text-slate-300',
}

const DOT: Record<string, string> = {
  aqua: 'bg-aqua-300',
  nebula: 'bg-nebula-300',
  bloom: 'bg-bloom-400',
  emerald: 'bg-emerald-400',
  amber: 'bg-amber-400',
  neutral: 'bg-slate-400',
}

const SIZE: Record<string, string> = {
  xs: 'px-2 py-0.5 text-[10px]',
  sm: 'px-2.5 py-1 text-[11px]',
  md: 'px-3 py-1.5 text-xs',
}
</script>

<template>
  <span
    class="inline-flex w-fit items-center gap-1.5 rounded-full border font-medium whitespace-nowrap backdrop-blur-sm"
    :class="[props.outline ? OUTLINE[props.tone] : TONE[props.tone], SIZE[props.size]]"
  >
    <span
      v-if="props.dot"
      class="h-1.5 w-1.5 shrink-0 rounded-full"
      :class="DOT[props.tone]"
      aria-hidden="true"
    />
    <slot>{{ props.label }}</slot>
  </span>
</template>
