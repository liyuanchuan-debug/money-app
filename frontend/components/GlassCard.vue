<script setup lang="ts">
/**
 * GlassCard —— L2 可交互琉璃卡片。
 *
 * 与普通「毛玻璃」的区别就在那条 1px 的渐变描边（.glass-edge，conic-gradient + mask 抠环）：
 * 它模拟琉璃的边缘折射，让卡片「有厚度」；悬停时卡片上浮 + 一道光带横扫表面。
 */

const props = withDefaults(
  defineProps<{
    /** 渲染的根标签，默认 div（列表里可传 li / article） */
    as?: string
    /** 内边距档位 */
    padding?: 'none' | 'sm' | 'md' | 'lg'
    /** 圆角档位 */
    rounded?: 'lg' | 'xl' | '2xl' | '3xl'
    /** 渐变描边色调（mixed 为青紫玫三色循环） */
    edge?: 'mixed' | 'aqua' | 'nebula' | 'bloom'
    /** 是否可交互（悬停上浮 + 光带扫过） */
    interactive?: boolean
    /** 是否处于选中 / 高亮态 */
    selected?: boolean
    /** 关闭悬停光带 */
    sweep?: boolean
  }>(),
  {
    as: 'div',
    padding: 'md',
    rounded: '2xl',
    edge: 'mixed',
    interactive: true,
    selected: false,
    sweep: true,
  },
)

const PADDING: Record<string, string> = {
  none: '',
  sm: 'p-3.5',
  md: 'p-5',
  lg: 'p-6',
}

const ROUNDED: Record<string, string> = {
  lg: 'rounded-lg',
  xl: 'rounded-xl',
  '2xl': 'rounded-2xl',
  '3xl': 'rounded-3xl',
}

/** 描边色调：用 inline style 覆盖 .glass-edge::before 的 conic-gradient 起点色 */
const EDGE_STYLE: Record<string, string> = {
  mixed: 'conic-gradient(from 140deg, rgba(34,211,238,0.55), rgba(124,77,255,0.50), rgba(251,63,110,0.42), rgba(34,211,238,0.55))',
  aqua: 'conic-gradient(from 140deg, rgba(34,211,238,0.75), rgba(34,211,238,0.15), rgba(161,241,255,0.65))',
  nebula: 'conic-gradient(from 140deg, rgba(154,123,255,0.75), rgba(124,77,255,0.18), rgba(214,204,255,0.6))',
  bloom: 'conic-gradient(from 140deg, rgba(255,106,141,0.75), rgba(251,63,110,0.18), rgba(255,199,211,0.6))',
}
</script>

<template>
  <component
    :is="props.as"
    class="glass-card glass-edge"
    :class="[
      ROUNDED[props.rounded],
      PADDING[props.padding],
      props.sweep ? 'glass-sweep glass-sweep-host' : '',
      props.interactive ? 'glass-card-lift' : '',
      props.selected ? 'glass-card-active' : '',
    ]"
    :style="{ '--glass-edge-image': EDGE_STYLE[props.edge] }"
  >
    <slot />
  </component>
</template>
