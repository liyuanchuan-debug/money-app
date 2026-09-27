<script setup lang="ts">
/** 特码走势：访客可读，无登录门禁 */

/**
 * 特码走势 —— GET /api/stats/trend
 * --------------------------------------------------------------------------
 * 口径铁律：走势与波动只覆盖「本池已导入的 N 期特码样本」（后端 notes 原话）。
 * 波动阈值取自用户设置；访客 / 未登录时后端用全局默认阈值。
 * 本页只展示、不在前端另设阈值 —— 避免出现第二套口径。
 *
 * 权限：本页为公开只读数据。
 */
import type { TrendStats } from '~/composables/useStats'
import {
  STATS_DEFAULT_SAMPLE_LIMIT,
  formatRate,
  padStatNumber,
  useStats,
} from '~/composables/useStats'

const api = useStats()
const sampleLimit = ref(STATS_DEFAULT_SAMPLE_LIMIT)

/** 只读：走势不写库 */
const { data, pending, error, refresh } = await useAsyncData<TrendStats>(
  'stats-trend',
  () => api.trend(sampleLimit.value),
  { watch: [sampleLimit] },
)

useHead({ title: '特码走势 · 四叶沙盘' })

const series = computed(() => data.value?.series ?? [])
const settings = computed(() => data.value?.settings ?? null)
const distribution = computed(() => data.value?.wave_distribution ?? null)

/** 折线：按期号升序（旧 → 新），y 轴固定 1 - 49 */
const trendSeries = computed(() =>
  series.value.length >= 2
    ? [{ name: '特码', values: series.value.map(point => point.special_number) }]
    : [],
)
const trendLabels = computed(() => series.value.map(point => String(point.period)))

/** 波动分布柱状图（柱高 = 样本内出现次数） */
const waveBars = computed(() =>
  (distribution.value?.items ?? []).map(item => ({
    label: item.label,
    value: item.count,
    hint: `占相邻期对的 ${formatRate(item.rate)}`,
  })),
)

/** 波动色板：与 StatChip 的语义色保持一致（小=emerald / 常规=amber / 大跳=bloom） */
const WAVE_TONE: Record<string, 'emerald' | 'amber' | 'bloom' | 'neutral'> = {
  small: 'emerald',
  normal: 'amber',
  big: 'bloom',
}

/** 最近 60 期相邻波动明细（新 → 旧；序列第一期没有差值，直接跳过） */
const DETAIL_SIZE = 60
const recentPairs = computed(() =>
  [...series.value]
    .reverse()
    .filter(point => point.diff !== null)
    .slice(0, DETAIL_SIZE),
)

const latestPoint = computed(() => series.value[series.value.length - 1] ?? null)
/** 相邻期对不足 30 对时，后端口径是「分布不足以支持强结论」 */
const thinPairs = computed(() => {
  const total = distribution.value?.total_pairs ?? 0
  return total > 0 && total < 30
})

/** 样本内最常见的波动（纯样本内观察，措辞里必须带上「样本内」） */
const dominantText = computed(() => {
  const dist = distribution.value
  if (!dist?.dominant_label) return ''
  const hit = dist.items.find(item => item.type === dist.dominant)
  return `样本内最常见：${dist.dominant_label}（${hit?.count ?? '—'} / ${dist.total_pairs} 对，样本内观察）`
})
</script>

<template>
  <StatsPageFrame
    v-model:sample-limit="sampleLimit"
    title="特码走势"
    subtitle="本池已导入样本内，特码按期号升序的走势，以及相邻两期的差值分类与波动分布。波动阈值取自当前配置，阈值一变分类就变。"
    required-role="PUBLIC"
    :envelope="data"
    :pending="pending"
    :error="error"
    @retry="refresh()"
  >
    <div class="flex flex-col gap-6">
      <MotionReveal :index="2">
        <!-- 走势折线 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-4">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <div class="space-y-1">
              <h2 class="text-lg font-medium text-white">特码走势</h2>
              <p class="text-xs text-slate-500">
                口径：本池样本内 {{ series.length }} 期特码，旧 → 新；y 轴固定 1 - 49。
              </p>
            </div>
            <div v-if="latestPoint" class="text-right">
              <p class="text-[11px] text-slate-500">最新一期</p>
              <p class="num text-sm text-slate-200">
                第 {{ latestPoint.period }} 期 ·
                {{ padStatNumber(latestPoint.special_number) }} · {{ latestPoint.zodiac_label }}
              </p>
            </div>
          </div>

          <LineChart
            :series="trendSeries"
            :labels="trendLabels"
            :height="240"
            :y-min="1"
            :y-max="49"
            empty-text="数据不足"
            empty-hint="本池至少要 2 期开奖记录才能连成走势"
          />
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="3">
        <!-- 波动分布 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-4">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <div class="space-y-1">
              <h2 class="text-lg font-medium text-white">相邻波动分布</h2>
              <p class="text-xs text-slate-500">
                差值 = |本期特码 − 上一期特码|，只统计本池样本内的相邻期对。
              </p>
            </div>
            <span class="num text-xs text-slate-500">
              相邻期对 {{ distribution?.total_pairs ?? 0 }} 对
            </span>
          </div>

          <BarChart
            :bars="waveBars"
            :height="220"
            value-suffix=" 对"
            empty-text="数据不足"
            empty-hint="本池至少要 2 期开奖记录才能计算相邻波动"
          />

          <div class="flex flex-wrap items-center gap-2">
            <StatChip v-if="dominantText" tone="aqua" size="xs" dot>
              {{ dominantText }}
            </StatChip>
            <StatChip v-if="thinPairs" tone="bloom" size="xs">
              相邻期对不足 30 对，分布不足以支持强结论
            </StatChip>
          </div>

          <!-- 阈值口径：来自后端 settings，只展示不自设 -->
          <div v-if="settings" class="rounded-xl border border-white/5 bg-white/5 px-3 py-2.5">
            <p class="text-[11px] font-medium text-slate-400">波动阈值（与 /api/settings 同一口径）</p>
            <div class="mt-1.5 flex flex-wrap gap-2">
              <StatChip tone="emerald" size="xs">小波动 ≤ {{ settings.small_max }}</StatChip>
              <StatChip tone="amber" size="xs">常规波动 ≤ {{ settings.normal_max }}</StatChip>
              <StatChip tone="bloom" size="xs">大跳 ≥ {{ settings.big_min }}</StatChip>
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="4">
        <!-- 波动明细 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
          <div class="flex flex-wrap items-end justify-between gap-2">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">波动明细（最近 {{ DETAIL_SIZE }} 期）</h3>
              <p class="text-[11px] text-slate-500">新 → 旧；差值以上一期为基准。</p>
            </div>
            <span class="num text-[11px] text-slate-500">{{ recentPairs.length }} 行</span>
          </div>

          <p v-if="!recentPairs.length" class="text-xs text-slate-500">
            数据不足：样本不足两期，无法计算相邻波动。
          </p>

          <ul v-else class="space-y-1.5">
            <li
              v-for="point in recentPairs"
              :key="point.period"
              class="flex min-h-[44px] items-center gap-3 rounded-xl border border-white/5 bg-white/5 px-3 py-2"
            >
              <span class="num w-16 shrink-0 text-xs text-slate-400">第{{ point.period }}期</span>
              <span class="num shrink-0 text-base font-semibold text-white">
                {{ padStatNumber(point.special_number) }}
              </span>
              <span class="shrink-0 text-xs text-slate-400">{{ point.zodiac_label }}</span>
              <span class="num ml-auto shrink-0 text-xs text-slate-400">
                差值 {{ point.diff }}
              </span>
              <StatChip
                :tone="WAVE_TONE[point.wave_type ?? 'normal'] ?? 'neutral'"
                size="xs"
              >
                {{ point.wave_label ?? '—' }}
              </StatChip>
            </li>
          </ul>
        </GlassPanel>
      </MotionReveal>
    </div>
  </StatsPageFrame>
</template>
