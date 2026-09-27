<script setup lang="ts">
/**
 * GlassButton —— L3 琉璃控件。
 *
 * 与 L2 卡片同一套「渐变描边 + 光带扫过」，尺寸更小、模糊更强、可点。
 * disabled 有明确视觉：降透明度、去阴影、禁掉光带、光标 not-allowed。
 */

const props = withDefaults(
  defineProps<{
    /** 视觉变体 */
    variant?: 'primary' | 'glass' | 'ghost' | 'danger'
    /** 尺寸档位 */
    size?: 'sm' | 'md' | 'lg'
    /** 是否占满整行 */
    block?: boolean
    /** 原生 button type */
    type?: 'button' | 'submit' | 'reset'
    disabled?: boolean
    /** 载入中：显示旋转指示器并自动禁用 */
    loading?: boolean
    /** 关闭悬停光带 */
    sweep?: boolean
  }>(),
  {
    variant: 'glass',
    size: 'md',
    block: false,
    type: 'button',
    disabled: false,
    loading: false,
    sweep: true,
  },
)

const VARIANT: Record<string, string> = {
  primary: 'glass-control-primary font-semibold',
  glass: '',
  ghost: 'glass-control-ghost',
  danger: 'glass-control-danger',
}

const SIZE: Record<string, string> = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2.5 text-sm',
  lg: 'px-6 py-3.5 text-base',
}
</script>

<template>
  <button
    :type="props.type"
    class="glass-control glass-edge inline-flex items-center justify-center gap-2 font-medium tracking-wide select-none"
    :class="[
      VARIANT[props.variant],
      SIZE[props.size],
      props.block ? 'w-full' : '',
      props.sweep ? 'glass-sweep glass-sweep-host' : '',
    ]"
    :disabled="props.disabled || props.loading"
    :aria-busy="props.loading || undefined"
  >
    <span
      v-if="props.loading"
      class="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent opacity-80"
      aria-hidden="true"
    />
    <span class="relative z-[3] inline-flex items-center gap-2">
      <slot />
    </span>
  </button>
</template>
