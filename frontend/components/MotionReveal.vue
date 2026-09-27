<script setup lang="ts">
/**
 * MotionReveal —— 错开入场容器（fade + blur + 上移）。
 *
 * 关键设计：**静态 class 里绝不写 opacity-0**。
 * 入场只靠 animation（fill-mode: both）完成，所以：
 *   - prefers-reduced-motion / html.motion-off 把动画关掉时，内容直接以终态可见；
 *   - 不会出现「动画被禁用 → 内容永久不可见」的事故。
 */

const props = withDefaults(
  defineProps<{
    /** 第几项（用于错开）；同组内递增即可 */
    index?: number
    /** 每项间隔（毫秒） */
    stagger?: number
    /** 基础延迟（毫秒） */
    delay?: number
    /** 单次时长（毫秒） */
    duration?: number
    /** 渲染标签 */
    as?: string
    /** 临时关掉（例如打印 / 静态导出） */
    disabled?: boolean
  }>(),
  {
    index: 0,
    stagger: 70,
    delay: 0,
    duration: 720,
    as: 'div',
    disabled: false,
  },
)

const { shouldAnimate } = useMotion()
const { delayFor } = useStagger({ step: props.stagger, base: props.delay })

const active = computed(() => shouldAnimate.value && !props.disabled)

const style = computed(() => {
  if (!active.value) return {}
  return {
    animationDelay: `${delayFor(props.index)}ms`,
    animationDuration: `${props.duration}ms`,
  }
})
</script>

<template>
  <component :is="props.as" :class="active ? 'animate-reveal-up' : ''" :style="style">
    <slot />
  </component>
</template>
