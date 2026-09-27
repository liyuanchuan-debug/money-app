<script setup lang="ts">
/**
 * PulseRing —— 命中光环：从自身向外扩一圈（可多圈）扩散的环。
 *
 * 用法：放在需要发光的元素内部（父元素需 position: relative），
 * 每次想打一下就改变 trigger（一般直接绑 useHitPulse().key）。
 * 减少动效 / 关闭特效时不渲染任何环。
 */

const props = withDefaults(
  defineProps<{
    /** 触发值：变化一次 = 打一圈 */
    trigger?: number | string | boolean
    /** 环的颜色 */
    tone?: 'aqua' | 'nebula' | 'bloom'
    /** 一次触发叠几圈 */
    rings?: number
    /** 圈的粗细档位 */
    weight?: 'thin' | 'normal'
  }>(),
  {
    trigger: 0,
    tone: 'aqua',
    rings: 2,
    weight: 'normal',
  },
)

const TONE: Record<string, string> = {
  aqua: 'border-aqua-300/70 shadow-[0_0_38px_-6px_rgba(34,211,238,0.8)]',
  nebula: 'border-nebula-300/70 shadow-[0_0_40px_-6px_rgba(124,77,255,0.85)]',
  bloom: 'border-bloom-400/70 shadow-[0_0_38px_-6px_rgba(251,63,110,0.8)]',
}

const WEIGHT: Record<string, string> = {
  thin: 'border',
  normal: 'border-2',
}

const { shouldAnimate } = useMotion()
const keys = ref<number[]>([])
let seed = 0
let timer: ReturnType<typeof setTimeout> | null = null

watch(
  () => props.trigger,
  () => {
    if (!shouldAnimate.value) return
    const next: number[] = []
    for (let i = 0; i < Math.max(1, props.rings); i += 1) {
      seed += 1
      next.push(seed)
    }
    keys.value = next
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      keys.value = []
      timer = null
    }, 1800)
  },
)

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
  timer = null
})
</script>

<template>
  <span class="pointer-events-none absolute inset-0 block overflow-visible" aria-hidden="true">
    <span
      v-for="(key, index) in keys"
      :key="key"
      class="absolute inset-0 block rounded-[inherit] animate-pulse-ring"
      :class="[TONE[props.tone], WEIGHT[props.weight]]"
      :style="{ animationDelay: `${index * 200}ms` }"
    />
  </span>
</template>
