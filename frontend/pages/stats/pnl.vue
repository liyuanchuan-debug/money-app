<script setup lang="ts">
definePageMeta({ role: 'VIP' })

/**
 * 模拟收益仪表 —— GET /api/stats/pnl
 * --------------------------------------------------------------------------
 * 展示已采用财富密码快照的累计模拟成本 / 模拟兑付 / 模拟盈亏与命中率。
 * 诚实口径：按当期生成号码与设定赔率试算；非真实投注记录。
 * 赔率是用户设定兑付倍数；命中率是已结算期经验频率，不是真实概率。
 * 不做「稳赚」表述。
 */
import type { PnlStats } from '~/composables/useStats'
import {
  formatRate,
  isInsufficient,
  useStats,
} from '~/composables/useStats'

const api = useStats()

const { data, pending, error, refresh } = await useAsyncData<PnlStats>(
  'stats-pnl',
  () => api.pnl(20),
)

useHead({ title: '模拟收益仪表 · 四叶沙盘' })

const summary = computed(() => data.value?.summary ?? null)
const series = computed(() => data.value?.series ?? [])
const recent = computed(() => data.value?.recent ?? [])
const odds = computed(() => data.value?.odds ?? 47)
const insufficient = computed(() => isInsufficient(data.value))

function moneyText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—'
  const n = Number(value)
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(n % 1 === 0 ? 0 : 2)}`
}

function profitTone(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'text-slate-400'
  if (value > 0) return 'text-emerald-300'
  if (value < 0) return 'text-bloom-300'
  return 'text-slate-300'
}

/** 近期模拟盈亏条：用绝对值相对缩放，避免堆报表 */
const barMax = computed(() => {
  const values = recent.value
    .filter(row => row.status === 'SETTLED' && row.profit !== null)
    .map(row => Math.abs(Number(row.profit)))
  return Math.max(1, ...values, 1)
})

function barWidth(profit: number | null): string {
  if (profit === null || profit === undefined) return '0%'
  return `${Math.min(100, (Math.abs(Number(profit)) / barMax.value) * 100)}%`
}

const cumulativeSeries = computed(() =>
  series.value.length >= 1
    ? [{ name: '累计模拟盈亏', values: series.value.map(p => p.cumulative_profit) }]
    : [],
)
const cumulativeLabels = computed(() => series.value.map(p => String(p.period)))
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-3xl flex-col gap-6">
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            模拟收益仪表
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            按当期生成号码与设定赔率试算的模拟成本、模拟兑付与模拟盈亏；非真实投注记录，也未真实下单兑付。
            赔率是你设定的兑付倍数，不是收益承诺。
          </p>
          <div class="flex flex-wrap gap-2">
            <StatChip tone="amber" size="sm" dot>
              模拟买入 · 模拟收益
            </StatChip>
            <StatChip tone="aqua" size="sm" dot>
              当前赔率 {{ odds }} 倍
            </StatChip>
            <StatChip v-if="data?.scope" tone="neutral" size="sm">
              {{ data.scope }}
            </StatChip>
          </div>
        </div>
        <AppNav />
      </MotionReveal>

      <p v-if="pending && !data" class="text-sm text-slate-400">加载中…</p>

      <MotionReveal v-else-if="error" :index="1">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" tone="bloom">
          <p class="text-sm text-bloom-200">
            {{ (error as any)?.data?.detail || (error as any)?.message || '加载失败' }}
          </p>
          <GlassButton class="mt-4 min-h-[44px]" variant="glass" @click="refresh()">
            重试
          </GlassButton>
        </GlassPanel>
      </MotionReveal>

      <template v-else>
        <!-- 一屏主 KPI -->
        <MotionReveal :index="1">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div class="space-y-1">
                <p class="text-xs text-slate-500">模拟成本</p>
                <p class="num text-xl font-semibold text-white sm:text-2xl">
                  {{ moneyText(summary?.total_cost).replace(/^\+/, '') }}
                </p>
              </div>
              <div class="space-y-1">
                <p class="text-xs text-slate-500">模拟兑付</p>
                <p class="num text-xl font-semibold text-aqua-200 sm:text-2xl">
                  {{ moneyText(summary?.total_payout).replace(/^\+/, '') }}
                </p>
              </div>
              <div class="space-y-1">
                <p class="text-xs text-slate-500">模拟盈亏</p>
                <p
                  class="num text-xl font-semibold sm:text-2xl"
                  :class="profitTone(summary?.total_profit)"
                >
                  {{ moneyText(summary?.total_profit) }}
                </p>
              </div>
              <div class="space-y-1">
                <p class="text-xs text-slate-500">命中期数</p>
                <p class="num text-xl font-semibold text-white sm:text-2xl">
                  {{ summary?.hit_rounds ?? 0 }}
                  <span class="text-sm font-normal text-slate-500">
                    / {{ summary?.settled_rounds ?? 0 }}
                  </span>
                </p>
                <p class="text-xs text-slate-500">
                  命中率 {{ formatRate(summary?.hit_rate) }}
                  <span class="text-slate-600">（经验频率）</span>
                </p>
              </div>
            </div>

            <p
              v-if="insufficient"
              class="mt-4 rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-100"
            >
              数据不足：还没有已结算的模拟采用期。先在「波浪买入法」生成推荐（会按模拟买入自动采用），
              开奖入库后即可看到模拟盈亏。
            </p>
            <p v-else class="mt-4 text-xs leading-relaxed text-slate-500">
              {{ data?.odds_note || '各期模拟兑付按采用当时的赔率试算；非真实下单兑付。' }}
              待开奖 {{ summary?.pending_rounds ?? 0 }} 期未计入累计。
            </p>
          </GlassPanel>
        </MotionReveal>

        <!-- 累计模拟盈亏走势 -->
        <MotionReveal v-if="series.length >= 1" :index="2">
          <GlassPanel padding="lg" rounded="3xl">
            <h2 class="text-sm font-medium text-slate-200">累计模拟盈亏走势</h2>
            <p class="mt-1 text-xs text-slate-500">按已结算期顺序累加；单位元；模拟试算。</p>
            <div class="mt-4 h-48">
              <LineChart
                :series="cumulativeSeries"
                :labels="cumulativeLabels"
                :window="0"
              />
            </div>
          </GlassPanel>
        </MotionReveal>

        <!-- 近期各期 -->
        <MotionReveal :index="3">
          <GlassPanel padding="lg" rounded="3xl">
            <div class="flex items-center justify-between gap-3">
              <h2 class="text-sm font-medium text-slate-200">近期各期（模拟）</h2>
              <NuxtLink to="/settings" class="text-xs text-aqua-300">改赔率</NuxtLink>
            </div>
            <ul v-if="recent.length" class="mt-4 space-y-3">
              <li
                v-for="row in recent"
                :key="`${row.period}-${row.id}`"
                class="rounded-2xl border border-white/10 bg-white/[0.03] px-3 py-3"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="num text-sm font-medium text-white">第{{ row.period }}期</span>
                    <StatChip
                      size="xs"
                      :tone="row.status === 'PENDING' ? 'neutral' : row.hit ? 'emerald' : 'bloom'"
                    >
                      {{ row.status === 'PENDING' ? '待开奖' : row.hit ? '命中' : '未命中' }}
                    </StatChip>
                    <StatChip size="xs" tone="amber">
                      {{ row.stake_mode_label || '模拟买入' }}
                    </StatChip>
                    <span v-if="row.special_number != null" class="num text-xs text-slate-400">
                      特码 {{ String(row.special_number).padStart(2, '0') }}
                    </span>
                  </div>
                  <span
                    class="num text-sm font-semibold"
                    :class="profitTone(row.profit)"
                  >
                    {{ row.profit === null ? '—' : moneyText(row.profit) }}
                  </span>
                </div>
                <div class="mt-2 flex flex-wrap gap-3 text-xs text-slate-500">
                  <span>模拟成本 <span class="num text-slate-300">{{ moneyText(row.cost).replace(/^\+/, '') }}</span></span>
                  <span>模拟兑付 <span class="num text-slate-300">{{ moneyText(row.payout).replace(/^\+/, '') }}</span></span>
                  <span>赔率 <span class="num text-slate-300">{{ row.odds }}</span></span>
                </div>
                <div
                  v-if="row.status === 'SETTLED' && row.profit !== null"
                  class="mt-2 h-1.5 overflow-hidden rounded-full bg-white/5"
                >
                  <div
                    class="h-full rounded-full"
                    :class="Number(row.profit) >= 0 ? 'bg-emerald-400/70' : 'bg-bloom-400/70'"
                    :style="{ width: barWidth(row.profit) }"
                  />
                </div>
                <p class="mt-2 text-[11px] leading-relaxed text-slate-500">
                  号码
                  <span
                    v-for="pick in row.picks"
                    :key="`${row.period}-${pick.number}`"
                    class="num mr-1.5"
                    :class="pick.hit ? 'text-emerald-300' : 'text-slate-400'"
                  >
                    {{ String(pick.number).padStart(2, '0') }}×{{ pick.amount }}
                  </span>
                </p>
              </li>
            </ul>
            <p v-else class="mt-4 text-sm text-slate-500">
              暂无模拟采用记录。去「波浪买入法」生成一期即可按模拟买入自动采用。
            </p>
          </GlassPanel>
        </MotionReveal>

        <MotionReveal :index="4">
          <p class="text-xs leading-relaxed text-slate-600">
            {{ (data?.notes || []).join(' ') }}
          </p>
        </MotionReveal>
      </template>
    </div>
  </main>
</template>
