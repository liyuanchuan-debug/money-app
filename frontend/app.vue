<template>
  <div class="relative min-h-[100dvh]">
    <!-- 环境光背景：固定、满视口、不拦截指针，琉璃面靠它才「有东西可折射」 -->
    <AppBackground />

    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </div>
</template>
