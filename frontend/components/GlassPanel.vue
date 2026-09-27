<script setup lang="ts">
/**
 * GlassPanel —— L1 琉璃承载面（页面分区用）。
 *
 * 定位：低模糊、低染色、大圆角，负责「托住」内容；
 * 真正的可交互质感交给 L2 GlassCard / L3 GlassButton。
 *
 * 依赖：必须在 AppBackground 之上使用，否则 backdrop-blur 无内容可折射。
 */

const props = withDefaults(
  defineProps<{
    /** 渲染的根标签，默认 section */
    as?: string
    /** 内边距档位 */
    padding?: 'none' | 'sm' | 'md' | 'lg'
    /** 圆角档位 */
    rounded?: 'lg' | 'xl' | '2xl' | '3xl'
    /** 表面变体：默认 / 加强（hero）/ 减弱（嵌套）/ 无模糊（叠在玻璃上） */
    variant?: 'default' | 'strong' | 'soft' | 'plain'
    /** 是否追加一层强调色外发光 */
    glow?: boolean
    /** 外发光颜色 */
    tone?: 'aqua' | 'nebula' | 'bloom' | 'neutral'
  }>(),
  {
    as: 'section',
    padding: 'md',
    rounded: '2xl',
    variant: 'default',
    glow: false,
    tone: 'neutral',
  },
)

const PADDING: Record<string, string> = {
  none: '',
  sm: 'p-3.5',
  md: 'p-5',
  lg: 'p-6 sm:p-8',
}

const ROUNDED: Record<string, string> = {
  lg: 'rounded-lg',
  xl: 'rounded-xl',
  '2xl': 'rounded-2xl',
  '3xl': 'rounded-3xl',
}

const VARIANT: Record<string, string> = {
  default: 'glass-panel',
  strong: 'glass-panel-strong',
  soft: 'glass-panel-soft',
  plain: 'glass-panel-plain',
}

const TONE_GLOW: Record<string, string> = {
  aqua: 'shadow-glow-aqua',
  nebula: 'shadow-glow-nebula',
  bloom: 'shadow-glow-bloom',
  neutral: '',
}
</script>

<template>
  <component
    :is="props.as"
    :class="[
      VARIANT[props.variant],
      ROUNDED[props.rounded],
      PADDING[props.padding],
      props.glow ? TONE_GLOW[props.tone] : '',
    ]"
  >
    <slot />
  </component>
</template>
