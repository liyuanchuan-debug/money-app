<script setup lang="ts">
/**
 * HeatScaleLegend —— 热力网格的色阶图例。
 *
 * 只负责「色块 → 档位区间」的呈现：档位区间与颜色由 HeatGrid 的分档逻辑
 * （useChartTheme 的 buildHeatScale）算好后整份传进来，所以图例不可能和格子配色脱节。
 *
 * 色阶是**单色系亮度梯度**：每块用同一份填充色（同 alpha 的 rgba），描边取该档的
 * solid（实色），这样小色块上也能看出「一个颜色由浅到深」的递进。
 * 从左到右必须读成「0（中性灰虚线）→ 最小值（最浅）→ … → 最大值（最深）」——
 * 顺序由 HeatGrid 的 bins 决定（第 0 项恒为 0 值档），方向不能和格子配色打架。
 * 零值档的描边额外用中性灰 + 虚线，跟非零档的实色描边分开；
 * 最高档（最深）用同一家族换过强调色的实线边框 + 更高的色块，跟零值档的暗色分开。
 *
 * 手机优先：等宽列 + 短的区间文案，360px 下不横向溢出、不需要横向滚动；
 * 文案统一 truncate，列再窄也只是省略，不会把父容器撑破。
 * 无动效（静态图例），不需要 prefers-reduced-motion 分支。
 */
export interface HeatLegendItem {
  /** 档位区间文案（如 '0' / '1-2' / '7-10'），单位由 caption 说明 */
  label: string
  /** 色块颜色：与格子同一份填充色（带透明度，叠在琉璃面上观感一致） */
  fill: string
  /**
   * 色块边框色：非最高档传本档 solid（实色，小色块上最干脆），
   * 最高档传本档 stroke（已被 useChartTheme 换成家族强调色）。
   */
  border?: string
  /** 是否最高档（= 最深最浓的一档，边框更亮、色块更高） */
  top?: boolean
  /** 是否零值档（中性灰虚线描边，明确「没有热度」） */
  zero?: boolean
}

const props = withDefaults(
  defineProps<{
    items?: HeatLegendItem[]
    /** 单位与口径说明（例：单位：次 · 本池已导入 200 期数据内） */
    caption?: string
  }>(),
  {
    items: () => [],
    caption: '',
  },
)
</script>

<template>
  <div v-if="props.items.length" class="w-full space-y-1.5">
    <div class="flex gap-1" role="img" :aria-label="`色阶图例：${props.items.map(item => item.label).join(' / ')}`">
      <div v-for="item in props.items" :key="item.label" class="min-w-0 flex-1">
        <div
          class="w-full rounded-[3px] border-2"
          :class="[
            item.top ? 'h-3' : 'h-2.5',
            item.zero ? 'border-dashed' : '',
          ]"
          :style="{
            backgroundColor: item.fill,
            borderColor: item.zero ? 'rgba(148, 163, 184, 0.45)' : item.border,
          }"
        />
        <p class="num mt-1 truncate text-center text-[9px] leading-tight text-slate-400">
          {{ item.label }}
        </p>
      </div>
    </div>
    <p v-if="props.caption" class="text-[10px] leading-relaxed text-slate-500">
      {{ props.caption }}
    </p>
  </div>
</template>
