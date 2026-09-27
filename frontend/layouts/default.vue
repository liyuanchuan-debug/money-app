<script setup lang="ts">
/**
 * 默认布局：只负责「页面外壳」。
 * - 把内容抬到环境光背景之上（z-10），并保证最小可视高度；
 * - 不设 transform / filter / backdrop-filter，避免把页面内的 position: fixed 元素
 *   关进包含块；
 * - 不渲染 AppNav：导航由各页自行放置，避免出现两条导航；
 * - 为移动端底部 Tab 栏（AppNav 里 teleport 到 body 的固定底栏）预留底部空间，
 *   否则页面最后一块内容会被底栏压住；≥ sm 不预留。
 *
 * 认证引导：
 * - 全站只在这里做一次 `ensureLoaded()`（onMounted ⇒ 只在浏览器跑，会话是 HttpOnly
 *   Cookie，服务端拿不到也不需要拿）；
 * - 布局实例在同布局的页面间跳转时会被复用，所以这段**不会**每次导航都跑一遍；
 * - `ensureLoaded()` 自身也是幂等的（已加载直接返回，并发调用共享同一个 Promise），
 *   即便将来别处也调用它，也只会打一次 /api/auth/me。
 */

const { ensureLoaded } = useAuth()

onMounted(() => {
  void ensureLoaded()
})
</script>

<template>
  <div class="relative z-10 flex min-h-[100dvh] flex-col">
    <!-- 4.5rem ≈ 底栏本体高度；再加一次 safe-area，与 AppNav 内 padding 对齐，避免末项被挡 -->
    <div class="flex flex-1 flex-col pb-[calc(4.5rem+env(safe-area-inset-bottom,0px))] sm:pb-0">
      <slot />
    </div>
  </div>
</template>
