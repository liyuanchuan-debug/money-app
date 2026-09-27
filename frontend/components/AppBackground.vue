<script setup lang="ts">
/**
 * AppBackground —— 环境光背景（固定、满视口、纯 CSS）。
 *
 * 为什么必须有这一层：液态琉璃只在「背后有东西可折射」时才成立。
 * 这里铺了 3 团缓慢漂移的模糊色球（青 / 紫 / 玫）+ 极细网格 + 颗粒噪点 + 暗角，
 * 上层所有的 .glass-panel / .glass-card / .glass-control 才有折射内容。
 *
 * 约束：
 * - pointer-events: none —— 绝不拦截鼠标；
 * - 只动 transform / opacity，不动 layout 属性；
 * - 动画开关由 main.css 的 prefers-reduced-motion 与 html.motion-off 统一接管，
 *   静止时色球仍按静态 transform 落在原位，不会消失。
 */
</script>

<template>
  <div class="ambient-root" aria-hidden="true">
    <div class="ambient-blob ambient-blob-aqua animate-aurora-1" />
    <div class="ambient-blob ambient-blob-nebula animate-aurora-2" />
    <div class="ambient-blob ambient-blob-bloom animate-aurora-3" />
    <div class="ambient-grid" />
    <div class="ambient-vignette" />
    <div class="ambient-noise" />
  </div>
</template>
