<script setup lang="ts">
/** 冷热统计：访客可读（只读样本统计，非个人投注能力） */

/**
 * 冷热统计 —— GET /api/stats/frequency
 * --------------------------------------------------------------------------
 * 口径铁律：只统计「本池已导入的 N 期数据」这个样本。
 * 后端明确警告过：出现次数偏差受样本量影响极大，**不能断言任何号码 / 生肖「更热」**；
 * 本池 200 期的样本功效（sample_power.can_support_strong_conclusion）为 false，
 * 所以本页把「样本功效」放在显眼位置，并把所有排行明确标成「原始计数 + 样本内观察」。
 *
 * 权限：本页为公开只读数据；个人投注能力（recommend / pnl）仍须 VIP。
 * 若接口异常，渲染可读错误态，不得白屏、不得静默 fail-open
 * （StatsPageFrame 错误态已覆盖）。
 */
import type { FrequencyStats } from '~/composables/useStats'
import {
  STATS_DEFAULT_SAMPLE_LIMIT,
  formatRate,
  formatSignedPp,
  padStatNumber,
  useStats,
} from '~/composables/useStats'

const api = useStats()
const sampleLimit = ref(STATS_DEFAULT_SAMPLE_LIMIT)

/** 只读：冷热统计不写库 */
const { data, pending, error, refresh } = await useAsyncData<FrequencyStats>(
  'stats-frequency',
  () => api.frequency(sampleLimit.value),
  { watch: [sampleLimit] },
)

useHead({ title: '冷热统计 · 四叶沙盘' })

const numbers = computed(() => data.value?.numbers ?? [])
const zodiacs = computed(() => data.value?.zodiacs ?? [])
const samplePower = computed(() => data.value?.sample_power ?? null)

/** 热力图：格内数值 = 样本内出现次数（只画原始计数，不画偏差 —— 负数会被强度映射吃掉） */
const heatCells = computed(() =>
  numbers.value.map(row => ({
    number: padStatNumber(row.number),
    value: row.appearances,
    label: row.zodiac_label,
    hint:
      `样本内出现 ${row.appearances} 次 · 出现率 ${formatRate(row.rate)} · `
      + `期望出现 ${row.expected_count.toFixed(1)} 次（1/49）`,
  })),
)

const maxAppearances = computed(() =>
  numbers.value.reduce((max, row) => Math.max(max, row.appearances), 0),
)

/** 生肖柱状图：样本内出现次数（柱顶是次数，提示里带上期望值作对照） */
const zodiacBars = computed(() =>
  zodiacs.value.map((row) => {
    const expected = row.expected_rate === null
      ? null
      : row.expected_rate * (data.value?.sample_size ?? 0)
    return {
      label: row.zodiac_label,
      value: row.appearances,
      hint:
        `期望 ${expected === null ? '—' : `${expected.toFixed(1)} 次`} · `
        + `偏差 ${formatSignedPp(row.deviation)}`,
    }
  }),
)

/**
 * 出现次数最多 / 最少的号码（原始计数）。
 * 这里刻意**不做热度结论**：样本量不足时次数差异基本是抽样波动。
 */
const byAppearancesDesc = computed(() =>
  [...numbers.value].sort((a, b) => b.appearances - a.appearances || a.number - b.number),
)
const topNumbers = computed(() => byAppearancesDesc.value.slice(0, 5))
const bottomNumbers = computed(() => byAppearancesDesc.value.slice(-5).reverse())
</script>

<template>
  <StatsPageFrame
    v-model:sample-limit="sampleLimit"
    title="冷热统计"
    subtitle="本池已导入样本内，01 - 49 与 12 生肖的出现次数、出现率、期望值与偏差。偏差受样本量影响极大，本页只做样本内观察，不据此判断「更热 / 更冷」。"
    required-role="PUBLIC"
    :envelope="data"
    :pending="pending"
    :error="error"
    @retry="refresh()"
  >
    <div class="flex flex-col gap-6">
      <MotionReveal :index="2">
        <!-- 样本功效：能不能下结论，先说清楚 -->
        <GlassPanel
          v-if="samplePower"
          variant="strong"
          padding="md"
          rounded="2xl"
          :glow="!samplePower.can_support_strong_conclusion"
          :tone="samplePower.can_support_strong_conclusion ? 'neutral' : 'bloom'"
          class="space-y-3"
        >
          <div class="flex flex-wrap items-center gap-2">
            <StatChip
              :tone="samplePower.can_support_strong_conclusion ? 'emerald' : 'bloom'"
              size="sm"
              dot
            >
              {{ samplePower.can_support_strong_conclusion ? '样本可支持强结论' : '样本不足以支持强结论' }}
            </StatChip>
            <span class="num text-[11px] text-slate-400">
              样本 {{ samplePower.draws }} 期 · 可映射农历年 {{ samplePower.mapped_draws }} 期
            </span>
          </div>

          <p class="text-sm font-medium text-slate-200">样本功效（这点样本量能支撑什么结论）</p>

          <div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <div class="space-y-0.5">
              <p class="text-[11px] text-slate-500">单号码期望出现</p>
              <p class="num text-base font-semibold text-white">
                {{ samplePower.expected_appearances_per_number.toFixed(1) }} 次
              </p>
            </div>
            <div class="space-y-0.5">
              <p class="text-[11px] text-slate-500">单生肖期望出现</p>
              <p class="num text-base font-semibold text-white">
                {{ samplePower.expected_appearances_per_zodiac.toFixed(1) }} 次
              </p>
            </div>
            <div class="space-y-0.5">
              <p class="text-[11px] text-slate-500">支持强结论所需期数</p>
              <p class="num text-base font-semibold text-white">
                {{ samplePower.strong_conclusion_min_draws }} 期
              </p>
            </div>
          </div>

          <p v-if="!samplePower.can_support_strong_conclusion" class="text-xs leading-relaxed text-slate-300">
            本池样本 {{ samplePower.draws }} 期，单号码理论期望只出现
            {{ samplePower.expected_appearances_per_number.toFixed(1) }} 次；后端口径要求单号码期望出现 ≥ 10 次
            （约 {{ samplePower.strong_conclusion_min_draws }} 期）才谈得上强结论。
            因此本页所有次数差异都只是样本内观察，请勿外推。
          </p>
          <p v-else class="text-xs leading-relaxed text-slate-300">
            样本量已达到后端的强结论门槛（单号码期望出现 ≥ 10 次）。
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="3">
        <!-- 号码出现次数热力 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-4">
          <div class="flex flex-wrap items-end justify-between gap-3">
            <div class="space-y-1">
              <h2 class="text-lg font-medium text-white">01 - 49 出现次数视图</h2>
              <p class="text-xs text-slate-500">
                格内颜色越深 = 样本内出现次数越多；样本内一次都没出现的号码保持贴底色的中性灰（不是最深档）。
              </p>
            </div>
            <span class="num text-xs text-slate-500">最高 {{ maxAppearances }} 次</span>
          </div>

          <HeatGrid
            :cells="heatCells"
            :columns="7"
            :max="maxAppearances || null"
            empty-text="数据不足"
            empty-hint="本池还没有可用于统计的开奖记录"
          />
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="4">
        <!-- 生肖出现次数 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
          <div class="space-y-1">
            <h2 class="text-lg font-medium text-white">12 生肖出现次数</h2>
            <p class="text-xs text-slate-500">
              柱高 = 样本内出现次数。生肖期望值按各期开奖日所属农历年的生肖表加权（49 个号码并非均分给
              12 生肖），因此不是简单的 1/12。
            </p>
          </div>
          <BarChart
            :bars="zodiacBars"
            :height="230"
            value-suffix=" 次"
            empty-text="数据不足"
            empty-hint="本池还没有可用于统计的开奖记录"
          />
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="5">
        <!-- 生肖明细 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
          <div class="space-y-1">
            <h3 class="text-sm font-medium text-slate-200">生肖明细</h3>
            <p class="text-[11px] text-slate-500">
              实际 = 样本内出现次数；期望 = 样本期数 × 该生肖加权期望率；偏差 = 实际出现率 − 期望率。
            </p>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full min-w-[420px] text-left text-sm">
              <thead class="glass-table-head text-xs text-slate-400">
                <tr class="border-b border-white/10">
                  <th class="px-2 py-2 font-medium">生肖</th>
                  <th class="px-2 py-2 text-right font-medium">出现</th>
                  <th class="px-2 py-2 text-right font-medium">出现率</th>
                  <th class="px-2 py-2 text-right font-medium">期望率</th>
                  <th class="px-2 py-2 text-right font-medium">偏差</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in zodiacs"
                  :key="row.zodiac"
                  class="border-b border-white/5 last:border-b-0"
                >
                  <td class="px-2 py-2 text-slate-200">{{ row.zodiac_label }}</td>
                  <td class="num px-2 py-2 text-right text-slate-200">{{ row.appearances }}</td>
                  <td class="num px-2 py-2 text-right text-slate-300">{{ formatRate(row.rate) }}</td>
                  <td class="num px-2 py-2 text-right text-slate-400">{{ formatRate(row.expected_rate) }}</td>
                  <td
                    class="num px-2 py-2 text-right"
                    :class="(row.deviation ?? 0) > 0 ? 'text-aqua-200' : (row.deviation ?? 0) < 0 ? 'text-bloom-200' : 'text-slate-400'"
                  >
                    {{ formatSignedPp(row.deviation) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="6">
        <!-- 号码次数极值（原始计数） -->
        <div class="grid gap-4 sm:grid-cols-2">
          <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-2.5">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">样本内出现最多的 5 个号码</h3>
              <p class="text-[11px] text-slate-500">原始计数，不代表该号码「更热」。</p>
            </div>
            <ul class="space-y-1.5">
              <li
                v-for="row in topNumbers"
                :key="row.number"
                class="flex items-center gap-3 rounded-xl border border-white/5 bg-white/5 px-3 py-2"
              >
                <SpecialBall :number="row.number" size="sm" :glow="false" />
                <span class="text-sm text-slate-300">{{ row.zodiac_label }}</span>
                <span class="num ml-auto text-sm font-semibold text-aqua-200">
                  {{ row.appearances }}<span class="ml-0.5 text-[11px] font-normal">次</span>
                </span>
              </li>
            </ul>
          </GlassPanel>

          <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-2.5">
            <div class="space-y-1">
              <h3 class="text-sm font-medium text-slate-200">样本内出现最少的 5 个号码</h3>
              <p class="text-[11px] text-slate-500">原始计数，不代表该号码「更冷」，更不构成回补预期。</p>
            </div>
            <ul class="space-y-1.5">
              <li
                v-for="row in bottomNumbers"
                :key="row.number"
                class="flex items-center gap-3 rounded-xl border border-white/5 bg-white/5 px-3 py-2"
              >
                <SpecialBall :number="row.number" size="sm" :glow="false" />
                <span class="text-sm text-slate-300">{{ row.zodiac_label }}</span>
                <span class="num ml-auto text-sm font-semibold text-slate-300">
                  {{ row.appearances }}<span class="ml-0.5 text-[11px] font-normal">次</span>
                </span>
              </li>
            </ul>
          </GlassPanel>
        </div>
      </MotionReveal>
    </div>
  </StatsPageFrame>
</template>
