<script setup lang="ts">
/**
 * ZodiacGlyph —— 生肖字形 + 琉璃徽章。
 *
 * 字形：**手写 SVG 线条**（零外部图标 / 图表依赖，与本项目既有风格一致）。
 *   12 个字形共用同一套笔法：48 × 48 画布、单色 stroke、stroke-width 3、
 *   round 端点与拐角、只描边不填充（眼 / 鼻 / 蹄等小点用填充圆做重音），
 *   所以 12 个摆在一起是一套，而不是 12 张不一样的贴图。
 *   颜色不写死在字形里：线条一律 currentColor，由徽章按生肖强调色注入。
 *
 * 徽章：外圈用该生肖的强调色做 1px 环 + 一层同色光晕，
 *   内部是淡染的琉璃球面（顶部内高光 + 底部压暗），让 12 张卡有统一节奏。
 *
 * 兜底：未知 / 缺失生肖码时不画线条图形，改渲染 useZodiac 里的 emoji
 *   （宁可用可辨认的 emoji，也不交付一个认不出的抽象块）。
 */
import { isZodiacCode, zodiacIdentity } from '~/composables/useZodiac'

const props = withDefaults(
  defineProps<{
    /** 生肖英文码（RAT / OX / …）；未知码走 emoji 兜底 */
    code?: string | null
    /** 尺寸档位 */
    size?: 'sm' | 'md' | 'lg'
    /** 是否绘制琉璃徽章（关掉则只画线条字形） */
    medallion?: boolean
  }>(),
  {
    code: null,
    size: 'md',
    medallion: true,
  },
)

const identity = computed(() => zodiacIdentity(props.code))
/** 只有已知的 12 个生肖码才有手写线条字形；其余走 emoji 兜底 */
const hasGlyph = computed(() => isZodiacCode(props.code))

const BOX: Record<string, string> = {
  sm: 'h-10 w-10',
  md: 'h-12 w-12',
  lg: 'h-16 w-16',
}

/** 徽章：同色系 1px 环 + 顶部内高光 + 底部压暗 + 外发光 */
const medallionStyle = computed(() => ({
  border: `1px solid ${zodiacRing(0.5)}`,
  backgroundImage: [
    'radial-gradient(circle at 32% 26%, rgba(255, 255, 255, 0.34), transparent 62%)',
    `radial-gradient(circle at 50% 50%, rgba(${identity.value.rgb}, 0.30), transparent 78%)`,
    'linear-gradient(160deg, rgba(255, 255, 255, 0.10), rgba(6, 8, 20, 0.62))',
  ].join(', '),
  boxShadow: `inset 0 1px 0 0 rgba(255, 255, 255, 0.28), 0 0 24px -8px ${zodiacRing(0.9)}`,
}))

/** 徽章外的一圈同色光环，让 12 个字形排在一起更有节奏 */
const haloStyle = computed(() => ({ boxShadow: `0 0 0 1px ${zodiacRing(0.2)}` }))

function zodiacRing(alpha: number) {
  return `rgba(${identity.value.rgb}, ${alpha})`
}
</script>

<template>
  <span
    class="relative inline-flex shrink-0 items-center justify-center"
    :class="[BOX[props.size], props.medallion ? 'rounded-full backdrop-blur-sm' : '']"
    :style="props.medallion ? medallionStyle : undefined"
  >
    <span
      v-if="props.medallion"
      class="pointer-events-none absolute -inset-1 rounded-full"
      aria-hidden="true"
      :style="haloStyle"
    />

    <!-- 12 生肖手写线条字形（同一套笔法：48 画布 / 单色 stroke / 圆端点 / 只描边） -->
    <svg
      v-if="hasGlyph"
      viewBox="0 0 48 48"
      fill="none"
      stroke="currentColor"
      stroke-width="3"
      stroke-linecap="round"
      stroke-linejoin="round"
      role="img"
      :aria-label="`生肖 ${identity.label}`"
      class="relative"
      :class="props.medallion ? 'h-[62%] w-[62%]' : 'h-full w-full'"
      :style="{ color: identity.text }"
    >
      <!-- 鼠：圆耳 + 圆脸 + 长须 -->
      <g v-if="props.code === 'RAT'">
        <circle cx="14" cy="12.5" r="5.4" />
        <circle cx="34" cy="12.5" r="5.4" />
        <circle cx="24" cy="26" r="11" />
        <circle cx="20" cy="23.5" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="28" cy="23.5" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="24" cy="30" r="1.7" fill="currentColor" stroke="none" />
        <path d="M24 31.8v2.2" />
        <path d="M14.5 29.5 L7.5 27.5" />
        <path d="M14.5 32.5 L7.5 34" />
        <path d="M33.5 29.5 L40.5 27.5" />
        <path d="M33.5 32.5 L40.5 34" />
      </g>

      <!-- 牛：上翘牛角 + 方脸 + 双鼻孔 -->
      <g v-else-if="props.code === 'OX'">
        <path d="M13 14.5 C9 12.5 7 7.5 11 5" />
        <path d="M35 14.5 C39 12.5 41 7.5 37 5" />
        <path d="M12 20 C12 15.5 17 12.5 24 12.5 C31 12.5 36 15.5 36 20 L36 27 C36 33 30.5 38 24 38 C17.5 38 12 33 12 27 Z" />
        <path d="M12 21.5 C7.5 20 5.5 23 7.5 26" />
        <path d="M36 21.5 C40.5 20 42.5 23 40.5 26" />
        <circle cx="18.5" cy="22" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="29.5" cy="22" r="1.5" fill="currentColor" stroke="none" />
        <path d="M18 29 C20 28 28 28 30 29" />
        <circle cx="21" cy="32.5" r="1.3" fill="currentColor" stroke="none" />
        <circle cx="27" cy="32.5" r="1.3" fill="currentColor" stroke="none" />
        <path d="M20.5 35.5 C22 36.8 26 36.8 27.5 35.5" />
      </g>

      <!-- 虎：尖耳 + 额前「王」纹 + 猫科吻部 + 长须 -->
      <g v-else-if="props.code === 'TIGER'">
        <path d="M14 17.5 C12 11 14.5 8 19 11.5" />
        <path d="M34 17.5 C36 11 33.5 8 29 11.5" />
        <circle cx="24" cy="25" r="12.5" />
        <path d="M24 14.8v4.6" />
        <path d="M20.4 17.1h7.2" />
        <circle cx="18.5" cy="23" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="29.5" cy="23" r="1.5" fill="currentColor" stroke="none" />
        <path d="M17.5 29 C19.5 27.2 22 27.2 24 29 C26 27.2 28.5 27.2 30.5 29" />
        <path d="M22 31.4h4l-2 2.4Z" fill="currentColor" stroke="none" />
        <path d="M13 29 L6.5 27.5" />
        <path d="M13 32 L6.5 33.5" />
        <path d="M35 29 L41.5 27.5" />
        <path d="M35 32 L41.5 33.5" />
      </g>

      <!-- 兔：两支长耳 + 圆脸 + 三瓣嘴 -->
      <g v-else-if="props.code === 'RABBIT'">
        <ellipse cx="18.5" cy="11.5" rx="3.4" ry="8.6" transform="rotate(-7 18.5 11.5)" />
        <ellipse cx="29.5" cy="11.5" rx="3.4" ry="8.6" transform="rotate(7 29.5 11.5)" />
        <circle cx="24" cy="31" r="10.5" />
        <circle cx="20.5" cy="29.5" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="27.5" cy="29.5" r="1.4" fill="currentColor" stroke="none" />
        <path d="M24 32v1.8" />
        <path d="M24 33.8 C22.4 35.6 20.6 35 20 33.8" />
        <path d="M24 33.8 C25.6 35.6 27.4 35 28 33.8" />
      </g>

      <!-- 龙：蜿蜒龙身 + 分叉龙角 + 龙须 + 前后足 -->
      <g v-else-if="props.code === 'DRAGON'">
        <path d="M11 39 C4 35 5 26 14 25.5 C23 25 34 23 34 15" />
        <circle cx="36" cy="11" r="5" />
        <path d="M36 6 C35 3 37.5 0.8 40.5 2.8" />
        <circle cx="37.6" cy="10.5" r="1.2" fill="currentColor" stroke="none" />
        <path d="M32 13.5 C26.5 15 23 17.5 23 21" />
        <path d="M18 25.5 L17 31" />
        <path d="M27 23.5 L26 29" />
        <path d="M11 39 L7 41.5" />
        <path d="M11 39 L13.5 42.5" />
      </g>

      <!-- 蛇：横向波浪蛇身 + 抬起的头 + 分叉蛇信（与「龙」的竖向龙身刻意区分） -->
      <g v-else-if="props.code === 'SNAKE'">
        <circle cx="13" cy="25" r="4.2" />
        <circle cx="11.6" cy="24" r="1.1" fill="currentColor" stroke="none" />
        <path d="M10 27.5 L7 31" />
        <path d="M7 31 L4.5 33.5" />
        <path d="M7 31 L6.5 36" />
        <path d="M16 27.5 C18.5 31.5 22 32.5 26 30.5 C30 28.5 33 30.5 36 34 C38 36.5 41 37.5 44 36.5" />
      </g>

      <!-- 马：侧脸 + 立耳 + 鬃毛 + 鼻孔 -->
      <g v-else-if="props.code === 'HORSE'">
        <path d="M24 8 C18.5 10 16 14.5 16.5 19.5 L12 29 C10 33.5 12.5 37.5 17 38.5 L22 39.5 C26 40 29 37.5 29 33.5 L29 19 C29 13 27 9.5 24 8 Z" />
        <path d="M23 8 L23.5 3 L27.5 7.5" />
        <path d="M29 11 L34 9" />
        <path d="M29.5 16 L35 14.5" />
        <path d="M29.5 21 L35 20" />
        <circle cx="21.5" cy="16.5" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="17.5" cy="32" r="1.2" fill="currentColor" stroke="none" />
        <path d="M14.5 35.5 C16.5 36.6 19.5 36.6 21.5 36" />
      </g>

      <!-- 羊：向后卷曲的双角 + 侧耳 + 长脸 + 下颌须 -->
      <g v-else-if="props.code === 'GOAT'">
        <path d="M17 13.5 C11.5 13 8.5 8 11.5 4.5 C14 1.8 18 3.2 17.5 7" />
        <path d="M31 13.5 C36.5 13 39.5 8 36.5 4.5 C34 1.8 30 3.2 30.5 7" />
        <path d="M15 18 C10 17 8.5 20 11 22.5" />
        <path d="M33 18 C38 17 39.5 20 37 22.5" />
        <path d="M15 17 C15 13.5 19 11.5 24 11.5 C29 11.5 33 13.5 33 17 L33 27 C33 33 29 37.5 24 37.5 C19 37.5 15 33 15 27 Z" />
        <circle cx="20" cy="21" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="28" cy="21" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="24" cy="28" r="1.3" fill="currentColor" stroke="none" />
        <path d="M24 37.5v4" />
        <path d="M21.5 39.5 L21 43.5" />
        <path d="M26.5 39.5 L27 43.5" />
      </g>

      <!-- 猴：两侧圆耳 + 吻部椭圆 + 双鼻孔 -->
      <g v-else-if="props.code === 'MONKEY'">
        <circle cx="10" cy="24" r="4.2" />
        <circle cx="38" cy="24" r="4.2" />
        <circle cx="24" cy="24" r="11.5" />
        <path d="M16.5 26 C16.5 22.5 19.5 21 24 21 C28.5 21 31.5 22.5 31.5 26 C31.5 31.5 28.5 35 24 35 C19.5 35 16.5 31.5 16.5 26 Z" />
        <circle cx="20.5" cy="25.5" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="27.5" cy="25.5" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="22.3" cy="29.6" r="0.9" fill="currentColor" stroke="none" />
        <circle cx="25.7" cy="29.6" r="0.9" fill="currentColor" stroke="none" />
        <path d="M21 32.5 C22.4 33.8 25.6 33.8 27 32.5" />
      </g>

      <!-- 鸡：三瓣鸡冠 + 尖喙 + 肉垂 + 颈部 -->
      <g v-else-if="props.code === 'ROOSTER'">
        <path d="M20 12.5 C18 8 21 5.5 23 8.5 C24 4 28 4.5 28.5 8 C31 5.8 33.5 8 32.5 12" />
        <circle cx="25.5" cy="18" r="8.5" />
        <path d="M17.5 17.5 L10.5 20 L17.5 22.5" />
        <path d="M20 25 C19 29.5 22 31.5 24.5 30" />
        <circle cx="24.5" cy="16" r="1.4" fill="currentColor" stroke="none" />
        <path d="M21 26 C17.5 31 16.5 36.5 17.5 42.5" />
        <path d="M30.5 25.5 C34 30.5 35 36 34.5 42" />
      </g>

      <!-- 狗：垂耳 + 圆脸 + 椭圆吻部 -->
      <g v-else-if="props.code === 'DOG'">
        <ellipse cx="12" cy="20" rx="4.2" ry="8.6" transform="rotate(-10 12 20)" />
        <ellipse cx="36" cy="20" rx="4.2" ry="8.6" transform="rotate(10 36 20)" />
        <circle cx="24" cy="24" r="11.5" />
        <circle cx="19.5" cy="22" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="28.5" cy="22" r="1.4" fill="currentColor" stroke="none" />
        <ellipse cx="24" cy="30.5" rx="5" ry="3.6" />
        <circle cx="24" cy="29" r="1.6" fill="currentColor" stroke="none" />
        <path d="M24 31.5v1.8" />
        <path d="M24 33.3 C22 34.8 20.6 34.2 20 33" />
        <path d="M24 33.3 C26 34.8 27.4 34.2 28 33" />
      </g>

      <!-- 猪：三角形折耳 + 圆脸 + 猪吻双鼻孔 -->
      <g v-else-if="props.code === 'PIG'">
        <path d="M16.5 17.5 L12.5 6 L22.5 13.6" />
        <path d="M31.5 17.5 L35.5 6 L25.5 13.6" />
        <circle cx="24" cy="25" r="11.5" />
        <circle cx="19.5" cy="21" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="28.5" cy="21" r="1.4" fill="currentColor" stroke="none" />
        <ellipse cx="24" cy="30.5" rx="6.2" ry="4.6" />
        <circle cx="21.7" cy="30.5" r="1.2" fill="currentColor" stroke="none" />
        <circle cx="26.3" cy="30.5" r="1.2" fill="currentColor" stroke="none" />
      </g>
    </svg>

    <!-- 兜底：未知生肖码 → emoji，绝不交付认不出的抽象块 -->
    <span v-else class="relative text-[1.5rem] leading-none" aria-hidden="true">
      {{ identity.emoji }}
    </span>
  </span>
</template>
