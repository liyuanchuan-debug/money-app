<template>
  <!-- 纯色底：去掉动画色球 + 满页 backdrop-blur 叠层，减轻滚动合成卡顿 -->
  <div class="relative min-h-[100dvh] bg-[#05060f]">
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </div>
</template>
