<script setup lang="ts">
/**
 * AnimatedNumber —— 会「滚动落位」的数字。
 *
 * - 用 useNumberRoll（requestAnimationFrame）实现，卸载自动清理；
 * - 减少动效 / 关闭特效时直接渲染最终值；
 * - 一律带 .num（tabular-nums），滚动时数字不会左右抖动。
 */

const props = withDefaults(
  defineProps<{
    /** 最终值；null / undefined 显示占位符 */
    value: number | null | undefined
    /** 补零位数（0 = 不补零，适合百分比） */
    pad?: number
    /** 滚动时长（毫秒） */
    duration?: number
    /** 错开延迟（毫秒） */
    stagger?: number
    /** 随机值范围（滚动过程中的假值只会在这个区间里跳） */
    min?: number
    max?: number
    /** 前缀 / 后缀（如 % 或 元） */
    prefix?: string
    suffix?: string
    /** 额外 class */
    textClass?: string
    /** 空值占位符 */
    placeholder?: string
  }>(),
  {
    pad: 2,
    duration: 900,
    stagger: 0,
    min: 1,
    max: 49,
    prefix: '',
    suffix: '',
    textClass: '',
    placeholder: '—',
  },
)

const { display, rolling } = useNumberRoll(() => props.value, {
  duration: props.duration,
  stagger: props.stagger,
  min: props.min,
  max: props.max,
})

const text = computed(() => {
  const value = display.value
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return props.placeholder
  }
  const digits = props.pad > 0 ? String(Math.round(value)).padStart(props.pad, '0') : String(Math.round(value))
  return `${props.prefix}${digits}${props.suffix}`
})
</script>

<template>
  <span
    class="num inline-block tabular-nums"
    :class="[props.textClass, rolling ? 'opacity-75 blur-[0.5px]' : '']"
  >
    {{ text }}
  </span>
</template>
